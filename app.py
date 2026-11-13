# -*- coding: utf-8 -*-
# 校园论坛，Flask + SQLite 的小站。
# 启动：python app.py，浏览器打开 http://127.0.0.1:5000

import hmac;
import os;
import re;
import secrets;
import sqlite3;
import time;
from datetime import datetime, timedelta;
from functools import wraps;
from urllib.parse import urlsplit;

from flask import (
    Flask, abort, current_app, flash, g, jsonify, redirect,
    render_template, request, session, url_for,
);
from werkzeug.security import check_password_hash, generate_password_hash;

BASE_DIR = os.path.dirname(os.path.abspath(__file__));
DEFAULT_DATABASE = os.path.join(BASE_DIR, "forum.db");
PAGE_SIZE = 15;
MAX_TITLE_LENGTH = 60;
MAX_POST_CONTENT = 5000;
MAX_COMMENT_LENGTH = 1000;
MAX_BIO_LENGTH = 100;
MAX_KEYWORD_LENGTH = 50;
MAX_BODY_BYTES = 256 * 1024;  # 防止有人一次塞进超大表单。
LOGIN_MAX_FAILURES = 5;
LOGIN_LOCK_SECONDS = 300;

SECTIONS = ["学习交流", "校园生活", "二手交易", "失物招领", "活动通知", "灌水闲聊"];

SECTION_ICONS = {
    "学习交流": "book",
    "校园生活": "building",
    "二手交易": "bag",
    "失物招领": "search",
    "活动通知": "bell",
    "灌水闲聊": "chat",
};

AVATARS = ["rose", "coral", "peach", "apricot", "amber", "blush", "mauve", "clay"];
DEFAULT_AVATAR = "rose";

# 登录失败按 IP + 用户名记在内存里，单机够用，重启就清空。
LOGIN_FAILURES = {};

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        nickname TEXT NOT NULL,
        avatar TEXT NOT NULL DEFAULT 'rose',
        bio TEXT NOT NULL DEFAULT '',
        is_admin INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        section TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        views INTEGER NOT NULL DEFAULT 0,
        is_pinned INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        edited_at TEXT
    );
    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id),
        parent_id INTEGER REFERENCES comments(id),
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS likes (
        user_id INTEGER NOT NULL REFERENCES users(id),
        post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, post_id)
    );
    CREATE TABLE IF NOT EXISTS favorites (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, post_id)
    );
    CREATE INDEX IF NOT EXISTS idx_posts_section ON posts(section);
    CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_posts_pinned_created ON posts(is_pinned DESC, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id);
    CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
    CREATE INDEX IF NOT EXISTS idx_comments_user ON comments(user_id);
    CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);
    CREATE INDEX IF NOT EXISTS idx_likes_post ON likes(post_id);
    CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id);
    CREATE INDEX IF NOT EXISTS idx_favorites_post ON favorites(post_id);
""";


# 密钥优先取环境变量；没有就生成一份存到本地，避免把密钥写进代码。
def load_secret_key():
    env_key = os.environ.get("CAMPUS_SECRET_KEY", "").strip();
    if env_key:
        return env_key;
    key_path = os.path.join(BASE_DIR, ".secret_key");
    try:
        if os.path.exists(key_path):
            with open(key_path, "r", encoding="utf-8") as file:
                saved = file.read().strip();
            if saved:
                return saved;
        generated = secrets.token_hex(32);
        with open(key_path, "w", encoding="utf-8") as file:
            file.write(generated);
        # chmod 在 Windows 上不一定生效，失败也就算了。
        try:
            os.chmod(key_path, 0o600);
        except OSError:
            pass;
        return generated;
    except OSError:
        # 目录不可写时退回进程内随机密钥，重启后旧会话会失效。
        return secrets.token_hex(32);


def get_db():
    # 每个请求复用同一个连接，顺便把 SQLite 的并发参数调一下。
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=5);
        g.db.row_factory = sqlite3.Row;
        g.db.execute("PRAGMA foreign_keys = ON");
        g.db.execute("PRAGMA journal_mode = WAL");
        g.db.execute("PRAGMA synchronous = NORMAL");
        g.db.execute("PRAGMA busy_timeout = 5000");
    return g.db;


def close_db(exception=None):
    db = g.pop("db", None);
    if db is not None:
        db.close();


def ensure_column(db, table, column, ddl):
    # 老库缺字段时补一列，表名和类型都来自代码里的固定值。
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")};
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}");


def init_db(database):
    # 建表语句都带 IF NOT EXISTS，重复启动不会出问题。
    db = sqlite3.connect(database);
    db.row_factory = sqlite3.Row;
    db.execute("PRAGMA foreign_keys = ON");
    db.executescript(SCHEMA_SQL);
    ensure_column(db, "posts", "edited_at", "TEXT");
    # 早期版本拿 emoji 当头像，这里统一换成本项目的配色名。
    placeholders = ",".join("?" * len(AVATARS));
    db.execute(
        f"UPDATE users SET avatar = ? WHERE avatar NOT IN ({placeholders})",
        [DEFAULT_AVATAR] + AVATARS,
    );
    db.commit();
    db.close();


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S");


def register_hooks(app):
    app.before_request(before_request_handler);
    app.teardown_appcontext(close_db);
    app.after_request(add_security_headers);


def before_request_handler():
    # 每个请求先把当前用户挂到 g 上，写操作再校验一下 CSRF。
    g.user = None;
    user_id = session.get("user_id");
    if user_id:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone();
        if g.user is None:
            session.clear();
    # GET 不校验令牌，否则正常浏览也会被拦。
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        verify_csrf();


def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff");
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN");
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin");
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    );
    # 页面只加载自己的静态资源，CSP 就按同源收紧。
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'",
    );
    return response;


def get_csrf_token():
    # token 放在 session 里，模板和表单都从这里取。
    token = session.get("_csrf_token");
    if not token:
        token = secrets.token_urlsafe(32);
        session["_csrf_token"] = token;
    return token;


def verify_csrf():
    # 普通表单从字段取，fetch 请求从请求头取，两种都认。
    expected = session.get("_csrf_token");
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "");
    if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
        abort(400, description="请求校验失败，请刷新页面后重试");


def login_attempt_key(username):
    return f"{request.remote_addr or 'unknown'}|{(username or '').lower()}";


def login_lock_remaining(key):
    # 返回剩余秒数，0 表示没锁。
    record = LOGIN_FAILURES.get(key);
    if not record:
        return 0;
    count, unlock_at = record;
    if unlock_at and time.monotonic() >= unlock_at:
        LOGIN_FAILURES.pop(key, None);
        return 0;
    return int(unlock_at - time.monotonic()) if unlock_at else 0;


def record_login_failure(key):
    # 连续错 5 次锁 5 分钟，记录太多时顺手清掉过期项。
    count = LOGIN_FAILURES.get(key, (0, 0))[0] + 1;
    unlock_at = time.monotonic() + LOGIN_LOCK_SECONDS if count >= LOGIN_MAX_FAILURES else 0;
    LOGIN_FAILURES[key] = (count, unlock_at);
    if len(LOGIN_FAILURES) > 1000:
        expired = [item for item, value in LOGIN_FAILURES.items()
                   if value[1] and time.monotonic() >= value[1]];
        for item in expired:
            LOGIN_FAILURES.pop(item, None);


def clear_login_failure(key):
    LOGIN_FAILURES.pop(key, None);


def safe_next_url(raw):
    # 只放行站内路径，// 和反斜杠都按外链处理。
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return url_for("index");
    parts = urlsplit(raw);
    if parts.scheme or parts.netloc:
        return url_for("index");
    return raw;


def login_required(view):
    # 未登录先提醒，再把原路径塞进 next。
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("请先登录后再操作！", "warning");
            return redirect(url_for("login", next=request.path));
        return view(*args, **kwargs);
    return wrapped;


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None or not g.user["is_admin"]:
            abort(403);
        return view(*args, **kwargs);
    return wrapped;


def timeago(value):
    # 时间戳换成「几分钟前」这种说法。
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S");
    except (ValueError, TypeError):
        return value or "";
    seconds = (datetime.now() - dt).total_seconds();
    if seconds < 60:
        return "刚刚";
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前";
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前";
    if seconds < 86400 * 30:
        return f"{int(seconds // 86400)} 天前";
    return dt.strftime("%Y-%m-%d");


def excerpt(value, length=90):
    # 列表页预览用，正文太长就截断加省略号。
    text = re.sub(r"\s+", " ", (value or "")).strip();
    if len(text) <= length:
        return text;
    return text[:length] + "…";


def parse_page():
    # 只认纯数字页码，其它一律当第一页，省得后面 int() 直接炸。
    raw = request.args.get("page", "1").strip();
    if not raw or len(raw) > 6 or not raw.isascii() or not raw.isdigit():
        return 1;
    return max(int(raw), 1);


def page_count(total, size=PAGE_SIZE):
    # 总页数至少是 1，模板那边就不用再判空。
    return max((total + size - 1) // size, 1);


def escape_like(text):
    # 用户搜 % 或 _ 的时候不能当通配符使，先转义掉。
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_");


def validate_post_fields(section, title, content):
    # 返回第一条错误提示，全部合法就给 None。
    if section not in SECTIONS:
        return "请选择正确的板块！";
    if not (2 <= len(title) <= MAX_TITLE_LENGTH):
        return f"标题长度需在 2-{MAX_TITLE_LENGTH} 字之间！";
    if len(content) < 5:
        return "内容至少 5 个字！";
    if len(content) > MAX_POST_CONTENT:
        return f"内容不能超过 {MAX_POST_CONTENT} 字！";
    return None;


POST_LIST_FIELDS = """
    p.*, u.nickname, u.avatar, u.username,
    (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) AS like_count,
    (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count
""";

HOT_ORDER_SQL = """p.is_pinned DESC,
    ((SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) * 5 +
     (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) * 3 +
     p.views) DESC, p.created_at DESC""";


def get_hot_posts(limit=5):
    # 侧栏热门榜，点赞、评论、浏览量各给一个权重。
    return get_db().execute(f"""
        SELECT p.id, p.title, p.views,
               (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) AS like_count,
               (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count
        FROM posts p
        ORDER BY {HOT_ORDER_SQL}
        LIMIT ?
    """, (limit,)).fetchall();


def get_site_stats():
    # 首页侧栏的全站数据，一条 SQL 拿齐。
    row = get_db().execute("""
        SELECT (SELECT COUNT(*) FROM users) AS users,
               (SELECT COUNT(*) FROM posts) AS posts,
               (SELECT COUNT(*) FROM comments) AS comments
    """).fetchone();
    return {"users": row["users"], "posts": row["posts"], "comments": row["comments"]};


def index():
    # 首页帖子列表，板块、搜索、排序、分页都在这里处理。
    db = get_db();
    section = request.args.get("section", "").strip();
    keyword = request.args.get("q", "").strip()[:MAX_KEYWORD_LENGTH];
    sort = request.args.get("sort", "new");
    if section not in SECTIONS:
        section = "";  # 未知板块按「全部」处理，避免工具栏无选中项。
    if sort not in ("new", "hot"):
        sort = "new";  # 非法排序参数回落到最新。
    page = parse_page();

    # 动态拼接查询条件，所有值都走参数化占位符。
    where = [];
    params = [];
    if section:
        where.append("p.section = ?");
        params.append(section);
    if keyword:
        escaped = escape_like(keyword);
        where.append("(p.title LIKE ? ESCAPE '\\' OR p.content LIKE ? ESCAPE '\\')");
        params.extend([f"%{escaped}%", f"%{escaped}%"]);
    where_sql = ("WHERE " + " AND ".join(where)) if where else "";

    total = db.execute(
        f"SELECT COUNT(*) FROM posts p {where_sql}", params
    ).fetchone()[0];
    total_pages = page_count(total);
    page = min(page, total_pages);

    # 置顶永远排最前，热门按加权分数，最新按发布时间。
    order_sql = HOT_ORDER_SQL if sort == "hot" else "p.is_pinned DESC, p.created_at DESC";

    posts = db.execute(f"""
        SELECT {POST_LIST_FIELDS}
        FROM posts p JOIN users u ON u.id = p.user_id
        {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """, params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall();

    return render_template(
        "index.html", posts=posts, section=section, keyword=keyword, sort=sort,
        page=page, total_pages=total_pages, total=total,
        hot_posts=get_hot_posts(), stats=get_site_stats(),
    );


def register():
    # 第一个注册的用户自动给管理员。
    if g.user:
        return redirect(url_for("index"));
    if request.method == "POST":
        username = request.form.get("username", "").strip();
        nickname = request.form.get("nickname", "").strip() or username;
        password = request.form.get("password", "");
        password2 = request.form.get("password2", "");

        if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fa5]{2,20}", username):
            flash("用户名需为 2-20 位字母、数字、下划线或中文！", "error");
        elif not (1 <= len(nickname) <= 20):
            flash("昵称长度需在 1-20 字之间！", "error");
        elif len(password) < 6:
            flash("密码长度至少 6 位！", "error");
        elif len(password) > 128:
            flash("密码长度不能超过 128 位！", "error");
        elif password != password2:
            flash("两次输入的密码不一致！", "error");
        else:
            db = get_db();
            exists = db.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone();
            if exists:
                flash("该用户名已被注册！", "error");
            else:
                user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0];
                is_admin = 1 if user_count == 0 else 0;
                db.execute(
                    """INSERT INTO users (username, password_hash, nickname, avatar, is_admin, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (username, generate_password_hash(password), nickname,
                     DEFAULT_AVATAR, is_admin, now_str()),
                );
                db.commit();
                if is_admin:
                    flash("注册成功！你是本站第一位用户，已自动成为管理员！", "success");
                else:
                    flash("注册成功，请登录！", "success");
                return redirect(url_for("login"));
    return render_template("register.html");


def login():
    if g.user:
        return redirect(url_for("index"));
    if request.method == "POST":
        username = request.form.get("username", "").strip();
        password = request.form.get("password", "");
        key = login_attempt_key(username);
        wait = login_lock_remaining(key);
        if wait:
            flash(f"尝试次数过多，请 {wait} 秒后再试！", "error");
        else:
            user = get_db().execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone();
            if user and check_password_hash(user["password_hash"], password):
                clear_login_failure(key);
                session.clear();
                session["user_id"] = user["id"];
                session.permanent = True;
                flash(f"欢迎回来，{user['nickname']}！", "success");
                return redirect(safe_next_url(request.args.get("next", "")));
            record_login_failure(key);
            flash("用户名或密码错误！", "error");
    return render_template("login.html");


def logout():
    # 只接受 POST，防止别的页面用 GET 链接把人踢下线。
    session.clear();
    flash("你已退出登录！", "success");
    return redirect(url_for("index"));


@login_required
def new_post():
    if request.method == "POST":
        section = request.form.get("section", "");
        title = request.form.get("title", "").strip();
        content = request.form.get("content", "").strip();
        error = validate_post_fields(section, title, content);
        if error:
            flash(error, "error");
        else:
            db = get_db();
            cur = db.execute(
                """INSERT INTO posts (user_id, section, title, content, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (g.user["id"], section, title, content, now_str()),
            );
            db.commit();
            flash("发布成功！", "success");
            return redirect(url_for("post_detail", post_id=cur.lastrowid));
    return render_template("new_post.html");


@login_required
def edit_post(post_id):
    # 作者本人或管理员才能改。
    db = get_db();
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone();
    if post is None:
        abort(404);
    if post["user_id"] != g.user["id"] and not g.user["is_admin"]:
        abort(403);

    if request.method == "POST":
        section = request.form.get("section", "");
        title = request.form.get("title", "").strip();
        content = request.form.get("content", "").strip();
        error = validate_post_fields(section, title, content);
        if error:
            flash(error, "error");
        else:
            db.execute(
                """UPDATE posts SET section = ?, title = ?, content = ?, edited_at = ?
                   WHERE id = ?""",
                (section, title, content, now_str(), post_id),
            );
            db.commit();
            flash("帖子已更新！", "success");
            return redirect(url_for("post_detail", post_id=post_id));
        # 校验失败时回显用户刚提交的内容，而不是数据库里的旧内容。
        post = dict(post);
        post.update({"section": section, "title": title, "content": content});
    return render_template("edit_post.html", post=post);


def post_detail(post_id):
    # 进来先把浏览量加一，再取正文和评论。
    db = get_db();
    exists = db.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone();
    if exists is None:
        abort(404);
    db.execute("UPDATE posts SET views = views + 1 WHERE id = ?", (post_id,));
    db.commit();

    post = db.execute(f"""
        SELECT {POST_LIST_FIELDS},
               (SELECT COUNT(*) FROM favorites f WHERE f.post_id = p.id) AS favorite_count
        FROM posts p JOIN users u ON u.id = p.user_id
        WHERE p.id = ?
    """, (post_id,)).fetchone();
    if post is None:
        abort(404);

    comments = db.execute("""
        SELECT c.*, u.nickname, u.avatar, u.username,
               pu.nickname AS parent_nickname
        FROM comments c
        JOIN users u ON u.id = c.user_id
        LEFT JOIN comments pc ON pc.id = c.parent_id
        LEFT JOIN users pu ON pu.id = pc.user_id
        WHERE c.post_id = ?
        ORDER BY c.created_at ASC
    """, (post_id,)).fetchall();

    liked = False;
    favorited = False;
    if g.user:
        liked = db.execute(
            "SELECT 1 FROM likes WHERE user_id = ? AND post_id = ?",
            (g.user["id"], post_id),
        ).fetchone() is not None;
        favorited = db.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND post_id = ?",
            (g.user["id"], post_id),
        ).fetchone() is not None;

    return render_template(
        "post_detail.html", post=post, comments=comments,
        liked=liked, favorited=favorited,
    );


@login_required
def add_comment(post_id):
    db = get_db();
    post = db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone();
    if post is None:
        abort(404);

    content = request.form.get("content", "").strip();
    parent_id = request.form.get("parent_id") or None;
    # 校验被回复的评论：必须存在且属于当前帖子，否则按普通评论处理。
    if parent_id:
        parent_id = int(parent_id) if parent_id.isdigit() else None;
        if parent_id:
            parent = db.execute(
                "SELECT id FROM comments WHERE id = ? AND post_id = ?",
                (parent_id, post_id),
            ).fetchone();
            if parent is None:
                parent_id = None;
    if len(content) < 2:
        flash("评论至少 2 个字！", "error");
    elif len(content) > MAX_COMMENT_LENGTH:
        flash(f"评论不能超过 {MAX_COMMENT_LENGTH} 字！", "error");
    else:
        db.execute(
            """INSERT INTO comments (post_id, user_id, parent_id, content, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (post_id, g.user["id"], parent_id, content, now_str()),
        );
        db.commit();
        flash("评论发表成功！", "success");
    return redirect(url_for("post_detail", post_id=post_id) + "#comments");


@login_required
def toggle_like(post_id):
    # 前端 fetch 调这个接口，返回最新状态和总数。
    db = get_db();
    post = db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone();
    if post is None:
        abort(404);

    existing = db.execute(
        "SELECT 1 FROM likes WHERE user_id = ? AND post_id = ?",
        (g.user["id"], post_id),
    ).fetchone();
    if existing:
        db.execute(
            "DELETE FROM likes WHERE user_id = ? AND post_id = ?",
            (g.user["id"], post_id),
        );
        liked = False;
    else:
        db.execute(
            "INSERT OR IGNORE INTO likes (user_id, post_id, created_at) VALUES (?, ?, ?)",
            (g.user["id"], post_id, now_str()),
        );
        liked = True;
    db.commit();
    count = db.execute(
        "SELECT COUNT(*) FROM likes WHERE post_id = ?", (post_id,)
    ).fetchone()[0];
    return jsonify({"liked": liked, "count": count});


@login_required
def toggle_favorite(post_id):
    # 和点赞一个套路，只是换成收藏表。
    db = get_db();
    post = db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone();
    if post is None:
        abort(404);

    existing = db.execute(
        "SELECT 1 FROM favorites WHERE user_id = ? AND post_id = ?",
        (g.user["id"], post_id),
    ).fetchone();
    if existing:
        db.execute(
            "DELETE FROM favorites WHERE user_id = ? AND post_id = ?",
            (g.user["id"], post_id),
        );
        favorited = False;
    else:
        db.execute(
            "INSERT OR IGNORE INTO favorites (user_id, post_id, created_at) VALUES (?, ?, ?)",
            (g.user["id"], post_id, now_str()),
        );
        favorited = True;
    db.commit();
    count = db.execute(
        "SELECT COUNT(*) FROM favorites WHERE post_id = ?", (post_id,)
    ).fetchone()[0];
    return jsonify({"favorited": favorited, "count": count});


@login_required
def delete_post(post_id):
    db = get_db();
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone();
    if post is None:
        abort(404);
    if post["user_id"] != g.user["id"] and not g.user["is_admin"]:
        abort(403);
    # 先解除评论的自引用，再删除帖子，避免级联删除触发外键约束。
    db.execute("UPDATE comments SET parent_id = NULL WHERE post_id = ?", (post_id,));
    db.execute("DELETE FROM posts WHERE id = ?", (post_id,));
    db.commit();
    flash("帖子已删除！", "success");
    return redirect(url_for("index"));


@admin_required
def toggle_pin(post_id):
    db = get_db();
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone();
    if post is None:
        abort(404);
    new_value = 0 if post["is_pinned"] else 1;
    db.execute("UPDATE posts SET is_pinned = ? WHERE id = ?", (new_value, post_id));
    db.commit();
    flash("已置顶！" if new_value else "已取消置顶！", "success");
    return redirect(url_for("post_detail", post_id=post_id));


@login_required
def delete_comment(comment_id):
    db = get_db();
    comment = db.execute(
        "SELECT * FROM comments WHERE id = ?", (comment_id,)
    ).fetchone();
    if comment is None:
        abort(404);
    if comment["user_id"] != g.user["id"] and not g.user["is_admin"]:
        abort(403);
    # 先解除子评论对它的引用，避免外键约束阻止删除。
    db.execute("UPDATE comments SET parent_id = NULL WHERE parent_id = ?", (comment_id,));
    db.execute("DELETE FROM comments WHERE id = ?", (comment_id,));
    db.commit();
    flash("评论已删除！", "success");
    return redirect(url_for("post_detail", post_id=comment["post_id"]) + "#comments");


def user_home(user_id):
    # 资料、发帖、评论三块，帖子区和评论区分页各算各的。
    db = get_db();
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone();
    if user is None:
        abort(404);

    tab = request.args.get("tab", "posts");
    if tab not in ("posts", "comments"):
        tab = "posts";
    page = parse_page();

    post_total = db.execute(
        "SELECT COUNT(*) FROM posts WHERE user_id = ?", (user_id,)
    ).fetchone()[0];
    comment_total = db.execute(
        "SELECT COUNT(*) FROM comments WHERE user_id = ?", (user_id,)
    ).fetchone()[0];
    like_total = db.execute(
        """SELECT COUNT(*) FROM likes l JOIN posts p ON p.id = l.post_id
           WHERE p.user_id = ?""", (user_id,)
    ).fetchone()[0];

    posts = [];
    comments = [];
    if tab == "posts":
        total_pages = page_count(post_total);
        page = min(page, total_pages);
        posts = db.execute(f"""
            SELECT {POST_LIST_FIELDS}
            FROM posts p JOIN users u ON u.id = p.user_id
            WHERE p.user_id = ?
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """, (user_id, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall();
    else:
        total_pages = page_count(comment_total);
        page = min(page, total_pages);
        comments = db.execute("""
            SELECT c.*, p.title AS post_title, p.section AS post_section
            FROM comments c JOIN posts p ON p.id = c.post_id
            WHERE c.user_id = ?
            ORDER BY c.created_at DESC
            LIMIT ? OFFSET ?
        """, (user_id, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall();

    return render_template(
        "user_home.html", user=user, tab=tab, page=page, total_pages=total_pages,
        post_total=post_total, comment_total=comment_total, like_total=like_total,
        posts=posts, comments=comments,
    );


@login_required
def favorites():
    db = get_db();
    page = parse_page();
    total = db.execute(
        "SELECT COUNT(*) FROM favorites WHERE user_id = ?", (g.user["id"],)
    ).fetchone()[0];
    total_pages = page_count(total);
    page = min(page, total_pages);
    posts = db.execute(f"""
        SELECT {POST_LIST_FIELDS}
        FROM favorites f
        JOIN posts p ON p.id = f.post_id
        JOIN users u ON u.id = p.user_id
        WHERE f.user_id = ?
        ORDER BY f.created_at DESC
        LIMIT ? OFFSET ?
    """, (g.user["id"], PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall();
    return render_template(
        "favorites.html", posts=posts, total=total,
        page=page, total_pages=total_pages,
    );


@login_required
def settings():
    db = get_db();
    if request.method == "POST":
        action = request.form.get("action");

        if action == "profile":
            nickname = request.form.get("nickname", "").strip();
            avatar = request.form.get("avatar", DEFAULT_AVATAR);
            bio = request.form.get("bio", "").strip();
            if not (1 <= len(nickname) <= 20):
                flash("昵称长度需在 1-20 字之间！", "error");
            elif len(bio) > MAX_BIO_LENGTH:
                flash(f"个性签名不能超过 {MAX_BIO_LENGTH} 字！", "error");
            else:
                if avatar not in AVATARS:
                    avatar = DEFAULT_AVATAR;
                db.execute(
                    "UPDATE users SET nickname = ?, avatar = ?, bio = ? WHERE id = ?",
                    (nickname, avatar, bio, g.user["id"]),
                );
                db.commit();
                flash("资料已更新！", "success");

        elif action == "password":
            # 改密码得先验证原密码。
            old = request.form.get("old_password", "");
            new = request.form.get("new_password", "");
            new2 = request.form.get("new_password2", "");
            if not check_password_hash(g.user["password_hash"], old):
                flash("原密码不正确！", "error");
            elif len(new) < 6:
                flash("新密码长度至少 6 位！", "error");
            elif len(new) > 128:
                flash("新密码长度不能超过 128 位！", "error");
            elif new != new2:
                flash("两次输入的新密码不一致！", "error");
            else:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new), g.user["id"]),
                );
                db.commit();
                flash("密码修改成功！", "success");
        return redirect(url_for("settings"));

    return render_template("settings.html");


def page_not_found(error):
    return render_template("error.html", code=404, message="页面不存在或已被删除"), 404;


def forbidden(error):
    return render_template("error.html", code=403, message="你没有权限执行此操作"), 403;


def bad_request(error):
    message = getattr(error, "description", "") or "请求无效，请刷新页面后重试";
    return render_template("error.html", code=400, message=message), 400;


def payload_too_large(error):
    return render_template("error.html", code=413, message="提交的内容过长，请精简后重试"), 413;


def server_error(error):
    # 不让堆栈直接暴露给用户。
    return render_template("error.html", code=500, message="服务器开小差了，请稍后再试"), 500;


def register_error_handlers(app):
    app.register_error_handler(400, bad_request);
    app.register_error_handler(403, forbidden);
    app.register_error_handler(404, page_not_found);
    app.register_error_handler(413, payload_too_large);
    app.register_error_handler(500, server_error);


def register_routes(app):
    # endpoint 写死，模板里的 url_for 就不用跟着改。
    app.add_url_rule("/", "index", index, methods=["GET"]);
    app.add_url_rule("/register", "register", register, methods=["GET", "POST"]);
    app.add_url_rule("/login", "login", login, methods=["GET", "POST"]);
    app.add_url_rule("/logout", "logout", logout, methods=["POST"]);
    app.add_url_rule("/post/new", "new_post", new_post, methods=["GET", "POST"]);
    app.add_url_rule("/post/<int:post_id>", "post_detail", post_detail, methods=["GET"]);
    app.add_url_rule("/post/<int:post_id>/edit", "edit_post", edit_post, methods=["GET", "POST"]);
    app.add_url_rule("/post/<int:post_id>/comment", "add_comment", add_comment, methods=["POST"]);
    app.add_url_rule("/post/<int:post_id>/like", "toggle_like", toggle_like, methods=["POST"]);
    app.add_url_rule("/post/<int:post_id>/favorite", "toggle_favorite", toggle_favorite, methods=["POST"]);
    app.add_url_rule("/post/<int:post_id>/delete", "delete_post", delete_post, methods=["POST"]);
    app.add_url_rule("/post/<int:post_id>/pin", "toggle_pin", toggle_pin, methods=["POST"]);
    app.add_url_rule("/comment/<int:comment_id>/delete", "delete_comment", delete_comment, methods=["POST"]);
    app.add_url_rule("/user/<int:user_id>", "user_home", user_home, methods=["GET"]);
    app.add_url_rule("/favorites", "favorites", favorites, methods=["GET"]);
    app.add_url_rule("/settings", "settings", settings, methods=["GET", "POST"]);


def create_app(test_config=None):
    app = Flask(__name__);
    app.config.update(
        SECRET_KEY=load_secret_key(),
        DATABASE=os.environ.get("CAMPUS_DATABASE", "").strip() or DEFAULT_DATABASE,
        MAX_CONTENT_LENGTH=MAX_BODY_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("CAMPUS_COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    );
    if test_config:
        app.config.update(test_config);

    # 让 Jinja 模板可以直接使用板块、头像与 CSRF 令牌。
    app.jinja_env.globals.update(
        SECTIONS=SECTIONS,
        SECTION_ICONS=SECTION_ICONS,
        AVATARS=AVATARS,
        csrf_token=get_csrf_token,
    );
    app.template_filter("timeago")(timeago);
    app.template_filter("excerpt")(excerpt);

    register_hooks(app);
    register_routes(app);
    register_error_handlers(app);

    # 建应用时顺手把表建好，两种启动方式都能直接用。
    with app.app_context():
        init_db(app.config["DATABASE"]);
    return app;


app = create_app();


if __name__ == "__main__":
    print("——"*50);
    print("校园论坛已启动！");
    print("请在浏览器访问：http://127.0.0.1:5000 ！");
    print("第一位注册的用户将自动成为管理员！");
    print("——"*50);
    app.run(host="127.0.0.1", port=5000, debug=False);
    print("✅ 校园论坛服务已结束！");
