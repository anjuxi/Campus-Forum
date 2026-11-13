function csrfToken() {
  var meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}

// POST 带上 CSRF 头，会话过期就当成登录失效处理。
function postJson(url) {
  return fetch(url, {
    method: "POST",
    headers: { "X-CSRF-Token": csrfToken() },
    credentials: "same-origin"
  }).then(function (res) {
    if (res.redirected || !res.ok) {
      throw new Error("request-failed");
    }
    return res.json();
  });
}

function goLogin() {
  window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
}

// 提示消息 3 秒后淡出删掉。
document.querySelectorAll(".flash").forEach(function (el) {
  setTimeout(function () {
    el.style.transition = "opacity .4s";
    el.style.opacity = "0";
    setTimeout(function () { el.remove(); }, 400);
  }, 3000);
});

// 带 data-confirm 的表单，提交前先问一句。
document.querySelectorAll("form[data-confirm]").forEach(function (form) {
  form.addEventListener("submit", function (e) {
    if (!confirm(form.dataset.confirm)) {
      e.preventDefault();
    }
  });
});

// 主题记在 localStorage 里，图标靠 CSS 跟着 data-theme 切换。
var themeToggle = document.getElementById("theme-toggle");
function syncThemeButton() {
  if (!themeToggle) { return; }
  var isDark = document.documentElement.getAttribute("data-theme") === "dark";
  themeToggle.setAttribute("aria-pressed", isDark ? "true" : "false");
  themeToggle.setAttribute("title", isDark ? "切换到浅色模式" : "切换到深色模式");
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) { meta.setAttribute("content", isDark ? "#2a2024" : "#b85f76"); }
}
if (themeToggle) {
  syncThemeButton();
  themeToggle.addEventListener("click", function () {
    var root = document.documentElement;
    var isDark = root.getAttribute("data-theme") === "dark";
    if (isDark) {
      root.removeAttribute("data-theme");
      localStorage.setItem("campus-theme", "light");
    } else {
      root.setAttribute("data-theme", "dark");
      localStorage.setItem("campus-theme", "dark");
    }
    syncThemeButton();
  });
}

// 点赞和收藏都走同一个接口套路，点一下切一次状态。
var likeBtn = document.getElementById("like-btn");
if (likeBtn) {
  likeBtn.addEventListener("click", function () {
    if (likeBtn.dataset.login !== "1") {
      goLogin();
      return;
    }
    if (likeBtn.disabled) { return; }
    likeBtn.disabled = true;
    postJson("/post/" + likeBtn.dataset.postId + "/like")
      .then(function (data) {
        document.getElementById("like-count").textContent = data.count;
        var label = likeBtn.querySelector(".like-label");
        if (label) { label.textContent = data.liked ? "已赞" : "点赞"; }
        likeBtn.classList.toggle("btn-liked", data.liked);
        likeBtn.classList.toggle("btn-ghost", !data.liked);
        likeBtn.setAttribute("aria-pressed", data.liked ? "true" : "false");
      })
      .catch(function () { goLogin(); })
      .then(function () { likeBtn.disabled = false; });
  });
}

var favoriteBtn = document.getElementById("favorite-btn");
if (favoriteBtn) {
  favoriteBtn.addEventListener("click", function () {
    if (favoriteBtn.dataset.login !== "1") {
      goLogin();
      return;
    }
    if (favoriteBtn.disabled) { return; }
    favoriteBtn.disabled = true;
    postJson("/post/" + favoriteBtn.dataset.postId + "/favorite")
      .then(function (data) {
        document.getElementById("favorite-count").textContent = data.count;
        var label = favoriteBtn.querySelector(".favorite-label");
        if (label) { label.textContent = data.favorited ? "已收藏" : "收藏"; }
        favoriteBtn.classList.toggle("btn-faved", data.favorited);
        favoriteBtn.classList.toggle("btn-ghost", !data.favorited);
        favoriteBtn.setAttribute("aria-pressed", data.favorited ? "true" : "false");
      })
      .catch(function () { goLogin(); })
      .then(function () { favoriteBtn.disabled = false; });
  });
}

// 点「回复」把父评论 id 填进隐藏域，再在输入框上方显示 @谁。
var parentInput = document.getElementById("parent-id");
var replyTip = document.getElementById("reply-tip");
var replyTo = document.getElementById("reply-to");
var commentBox = document.getElementById("comment-content");

document.querySelectorAll(".reply-btn").forEach(function (btn) {
  btn.addEventListener("click", function () {
    if (!parentInput || !replyTip || !replyTo) { return; }
    parentInput.value = btn.dataset.commentId;
    replyTo.textContent = "@" + btn.dataset.nickname;
    replyTip.style.display = "block";
    if (commentBox) { commentBox.focus(); }
  });
});

var cancelReply = document.getElementById("cancel-reply");
if (cancelReply) {
  cancelReply.addEventListener("click", function () {
    if (parentInput) { parentInput.value = ""; }
    if (replyTip) { replyTip.style.display = "none"; }
  });
}

// 输入框旁边那个 0/100 的字数。
document.querySelectorAll("[data-count-for]").forEach(function (counter) {
  var field = document.getElementById(counter.dataset.countFor);
  if (!field) { return; }
  var max = parseInt(counter.dataset.max || "0", 10);
  function update() {
    var length = field.value.length;
    counter.textContent = length + "/" + max;
    counter.classList.toggle("over", max > 0 && length >= max);
  }
  field.addEventListener("input", update);
  update();
});

// Ctrl / Cmd + Enter 直接发评论。
if (commentBox) {
  commentBox.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      var form = commentBox.closest("form");
      if (form) { form.submit(); }
    }
  });
}

// 有历史记录就走浏览器返回，直接打开的就按链接走。
document.querySelectorAll("[data-back]").forEach(function (link) {
  link.addEventListener("click", function (event) {
    if (window.history.length > 1) {
      event.preventDefault();
      window.history.back();
    }
  });
});
