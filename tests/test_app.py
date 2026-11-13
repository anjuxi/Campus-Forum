# -*- coding: utf-8 -*-
# 接口测试，认证、发帖、评论、点赞、收藏和权限边界都测一遍。

import re;
import sqlite3;

import pytest;

from app import LOGIN_FAILURES, create_app;

TOKEN_PATTERN = re.compile(r'name="csrf-token" content="([^"]+)"');


@pytest.fixture()
def app(tmp_path):
    # 每个测试用独立的临时库，互不干扰。
    LOGIN_FAILURES.clear();
    application = create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.db"),
        "SECRET_KEY": "test-secret-key",
    });
    yield application;
    LOGIN_FAILURES.clear();


@pytest.fixture()
def client(app):
    return app.test_client();


def get_csrf(client, path="/login"):
    # 令牌在页面 meta 里，抓出来给后面的 POST 用。
    html = client.get(path).get_data(as_text=True);
    match = TOKEN_PATTERN.search(html);
    assert match is not None;
    return match.group(1);


def register(client, username, password="secret123", nickname=""):
    token = get_csrf(client, "/register");
    return client.post("/register", data={
        "csrf_token": token,
        "username": username,
        "nickname": nickname,
        "password": password,
        "password2": password,
    }, follow_redirects=True);


def login(client, username, password="secret123"):
    token = get_csrf(client, "/login");
    return client.post("/login", data={
        "csrf_token": token,
        "username": username,
        "password": password,
    }, follow_redirects=True);


def logout(client):
    token = get_csrf(client, "/");
    return client.post("/logout", data={"csrf_token": token}, follow_redirects=True);


def create_post(client, title="测试帖子标题", content="这是一段测试正文内容", section="学习交流"):
    # 发完直接回新帖 id，方便接着测评论和点赞。
    token = get_csrf(client, "/post/new");
    response = client.post("/post/new", data={
        "csrf_token": token,
        "section": section,
        "title": title,
        "content": content,
    }, follow_redirects=True);
    assert response.status_code == 200;
    return last_post_id(client);


def last_post_id(client):
    # 绕过接口直接查库，拿自增主键最省事。
    db = sqlite3.connect(client.application.config["DATABASE"]);
    try:
        row = db.execute("SELECT id FROM posts ORDER BY id DESC LIMIT 1").fetchone();
    finally:
        db.close();
    return row[0] if row else None;


def last_comment_id(client):
    db = sqlite3.connect(client.application.config["DATABASE"]);
    try:
        row = db.execute("SELECT id FROM comments ORDER BY id DESC LIMIT 1").fetchone();
    finally:
        db.close();
    return row[0] if row else None;


def test_index_renders(client):
    response = client.get("/");
    assert response.status_code == 200;
    assert "校园论坛" in response.get_data(as_text=True);


def test_first_user_becomes_admin(client):
    response = register(client, "alice", nickname="爱丽丝");
    assert "注册成功" in response.get_data(as_text=True);
    db = sqlite3.connect(client.application.config["DATABASE"]);
    try:
        row = db.execute("SELECT is_admin FROM users WHERE username = ?", ("alice",)).fetchone();
    finally:
        db.close();
    assert row[0] == 1;


def test_second_user_is_not_admin(client):
    register(client, "alice");
    register(client, "bob");
    db = sqlite3.connect(client.application.config["DATABASE"]);
    try:
        row = db.execute("SELECT is_admin FROM users WHERE username = ?", ("bob",)).fetchone();
    finally:
        db.close();
    assert row[0] == 0;


def test_duplicate_username_rejected(client):
    register(client, "alice");
    response = register(client, "alice");
    assert "该用户名已被注册" in response.get_data(as_text=True);


def test_login_and_logout(client):
    register(client, "alice");
    response = login(client, "alice");
    assert "欢迎回来" in response.get_data(as_text=True);
    response = logout(client);
    assert "你已退出登录" in response.get_data(as_text=True);
    assert client.get("/logout").status_code == 405;


def test_post_without_csrf_is_rejected(client):
    response = client.post("/login", data={"username": "alice", "password": "secret123"});
    assert response.status_code == 400;
    assert "请求" in response.get_data(as_text=True);


def test_login_lockout_after_failures(client):
    register(client, "alice");
    token = get_csrf(client, "/login");
    for _ in range(5):
        client.post("/login", data={
            "csrf_token": token, "username": "alice", "password": "wrong",
        }, follow_redirects=True);
    response = client.post("/login", data={
        "csrf_token": token, "username": "alice", "password": "secret123",
    }, follow_redirects=True);
    assert "尝试次数过多" in response.get_data(as_text=True);


def test_new_post_requires_login(client):
    response = client.get("/post/new");
    assert response.status_code == 302;
    assert "/login" in response.headers["Location"];


def test_create_and_edit_post(client):
    register(client, "alice");
    login(client, "alice");
    post_id = create_post(client, title="第一篇帖子");
    detail = client.get(f"/post/{post_id}").get_data(as_text=True);
    assert "第一篇帖子" in detail;

    token = get_csrf(client, f"/post/{post_id}/edit");
    response = client.post(f"/post/{post_id}/edit", data={
        "csrf_token": token,
        "section": "校园生活",
        "title": "改过的新标题",
        "content": "改过之后的正文内容",
    }, follow_redirects=True);
    assert "帖子已更新" in response.get_data(as_text=True);
    assert "改过的新标题" in response.get_data(as_text=True);


def test_edit_permission(client):
    register(client, "alice");
    login(client, "alice");
    post_id = create_post(client);
    logout(client);

    register(client, "bob");
    login(client, "bob");
    assert client.get(f"/post/{post_id}/edit").status_code == 403;


def test_comment_reply_and_delete(client):
    register(client, "alice");
    login(client, "alice");
    post_id = create_post(client);
    logout(client);

    register(client, "bob");
    login(client, "bob");
    token = get_csrf(client, f"/post/{post_id}");
    client.post(f"/post/{post_id}/comment", data={
        "csrf_token": token, "content": "这是第一条评论",
    }, follow_redirects=True);
    comment_id = last_comment_id(client);
    assert comment_id is not None;

    logout(client);
    login(client, "alice");
    token = get_csrf(client, f"/post/{post_id}");
    response = client.post(f"/post/{post_id}/comment", data={
        "csrf_token": token, "content": "回复一下这条评论", "parent_id": str(comment_id),
    }, follow_redirects=True);
    assert "回复 @bob" in response.get_data(as_text=True);

    reply_id = last_comment_id(client);
    token = get_csrf(client, f"/post/{post_id}");
    response = client.post(f"/comment/{reply_id}/delete", data={
        "csrf_token": token,
    }, follow_redirects=True);
    assert "评论已删除" in response.get_data(as_text=True);


def test_like_toggle(client):
    register(client, "alice");
    login(client, "alice");
    post_id = create_post(client);
    token = get_csrf(client, f"/post/{post_id}");

    response = client.post(f"/post/{post_id}/like", headers={"X-CSRF-Token": token});
    assert response.get_json() == {"liked": True, "count": 1};
    response = client.post(f"/post/{post_id}/like", headers={"X-CSRF-Token": token});
    assert response.get_json() == {"liked": False, "count": 0};


def test_favorite_toggle_and_page(client):
    register(client, "alice");
    login(client, "alice");
    post_id = create_post(client, title="值得收藏的帖子");
    token = get_csrf(client, f"/post/{post_id}");

    response = client.post(f"/post/{post_id}/favorite", headers={"X-CSRF-Token": token});
    assert response.get_json() == {"favorited": True, "count": 1};
    assert "值得收藏的帖子" in client.get("/favorites").get_data(as_text=True);

    client.post(f"/post/{post_id}/favorite", headers={"X-CSRF-Token": token});
    assert "你还没有收藏任何帖子" in client.get("/favorites").get_data(as_text=True);


def test_search_escapes_wildcards(client):
    register(client, "alice");
    login(client, "alice");
    create_post(client, title="100%通过", content="折扣信息正文");
    create_post(client, title="普通帖子", content="普通正文内容");

    html = client.get("/", query_string={"q": "%"}).get_data(as_text=True);
    # 只看左侧列表区域，右侧热门榜单不受搜索条件影响。
    main_part = html.split("<aside", 1)[0];
    assert "100%通过" in main_part;
    assert "普通帖子" not in main_part;


def test_invalid_page_parameters(client):
    for value in ["abc", "²", "999", "-1"]:
        response = client.get("/", query_string={"page": value});
        assert response.status_code == 200;


def test_security_headers(client):
    response = client.get("/");
    assert response.headers["X-Content-Type-Options"] == "nosniff";
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN";
    assert "default-src 'self'" in response.headers["Content-Security-Policy"];


def test_post_content_is_escaped(client):
    register(client, "alice");
    login(client, "alice");
    post_id = create_post(client, title="<script>alert(1)</script>");
    html = client.get(f"/post/{post_id}").get_data(as_text=True);
    assert "<script>alert(1)</script>" not in html;
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html;


def test_admin_pin_and_delete(client):
    register(client, "alice");
    login(client, "alice");
    post_id = create_post(client, title="待置顶帖子");

    token = get_csrf(client, f"/post/{post_id}");
    response = client.post(f"/post/{post_id}/pin", data={
        "csrf_token": token,
    }, follow_redirects=True);
    assert "已置顶" in response.get_data(as_text=True);

    token = get_csrf(client, f"/post/{post_id}");
    response = client.post(f"/post/{post_id}/delete", data={
        "csrf_token": token,
    }, follow_redirects=True);
    assert "帖子已删除" in response.get_data(as_text=True);
    assert client.get(f"/post/{post_id}").status_code == 404;


def test_user_home_tabs(client):
    register(client, "alice");
    login(client, "alice");
    post_id = create_post(client, title="主页帖子标题");
    token = get_csrf(client, f"/post/{post_id}");
    client.post(f"/post/{post_id}/comment", data={
        "csrf_token": token, "content": "主页评论测试",
    }, follow_redirects=True);

    posts_html = client.get("/user/1", query_string={"tab": "posts"}).get_data(as_text=True);
    assert "主页帖子标题" in posts_html;
    comments_html = client.get("/user/1", query_string={"tab": "comments"}).get_data(as_text=True);
    assert "主页评论测试" in comments_html;
