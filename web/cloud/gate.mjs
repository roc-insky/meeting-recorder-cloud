// 登录门（三页共用）：未登录时页面上唯一可见的内容。
//
// 规则：登录门自己负责把 body 标成 guest（页头导航与账号区由 style.css 隐藏），
// 页面进入已登录状态时调用 setGuestMode(doc, false) 解除。
// 所有动态文本都通过 textContent 写入；不使用 innerHTML。
//
// 登录链接保持 `/api/cloud/auth/login` 这个裸路径（既有测试按此精确断言），
// 真正带 email / next 的地址由表单与点击处理器在运行时补全。

export const LOGIN_PATH = "/api/cloud/auth/login";

// 只接受站内相对路径，挡掉 //evil、反斜杠、控制字符与超长值。
export function safeNextPath(value) {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > 512) return "";
  if (!trimmed.startsWith("/") || trimmed.startsWith("//")) return "";
  for (let index = 0; index < trimmed.length; index += 1) {
    const code = trimmed.charCodeAt(index);
    if (code < 0x20 || code === 0x7f || code === 0x5c) return "";
  }
  return trimmed;
}

export function currentPath(doc) {
  const win = (doc && doc.defaultView) || globalThis;
  const location = win.location || {};
  const path = typeof location.pathname === "string" && location.pathname ? location.pathname : "/";
  const search = typeof location.search === "string" ? location.search : "";
  return safeNextPath(path + search) || "/";
}

// 登录链接用绝对地址（同源），既有 e2e 按 ORIGIN + "/api/cloud/auth/login" 精确断言。
export function absoluteLoginPath(doc, value) {
  const win = (doc && doc.defaultView) || globalThis;
  const origin = win.location && typeof win.location.origin === "string" ? win.location.origin : "";
  const path = typeof value === "string" && value ? value : LOGIN_PATH;
  if (!origin || /^[a-z][a-z0-9+.-]*:\/\//i.test(path)) return path;
  return origin + path;
}

export function loginHrefFor(doc, options) {
  const settings = options || {};
  const base = absoluteLoginPath(doc, settings.loginPath);
  const params = new URLSearchParams();
  const email = typeof settings.email === "string" ? settings.email.trim() : "";
  if (email) params.set("email", email);
  const next = settings.next === undefined ? currentPath(doc) : safeNextPath(settings.next);
  if (next) params.set("next", next);
  const query = params.toString();
  return query ? base + "?" + query : base;
}

export function setGuestMode(doc, guest) {
  if (!doc) return;
  const on = Boolean(guest);
  if (doc.body && doc.body.classList) doc.body.classList.toggle("auth-guest", on);
  const html = doc.documentElement;
  if (html && html.dataset) html.dataset.auth = on ? "guest" : "member";
}

function el(doc, tag, props) {
  const settings = props || {};
  const node = doc.createElement(tag);
  if (settings.className) node.className = settings.className;
  if (settings.text !== undefined) node.textContent = settings.text;
  for (const [key, value] of Object.entries(settings.attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    node.setAttribute(key, value);
  }
  return node;
}

// 渲染登录门并进入 guest 模式。options: { loginPath, next, message }
export function renderLoginGate(doc, options) {
  const settings = options || {};
  const base = absoluteLoginPath(doc, settings.loginPath);
  const next = settings.next === undefined ? currentPath(doc) : safeNextPath(settings.next);

  const section = el(doc, "section", {
    className: "login-gate",
    attrs: { "data-testid": "login-gate", "aria-labelledby": "login-gate-heading" },
  });
  section.append(
    el(doc, "h1", { text: "请先登录", attrs: { id: "login-gate-heading" } }),
    el(doc, "p", {
      text:
        typeof settings.message === "string" && settings.message
          ? settings.message
          : "登录后才能查看和使用会议记录、声纹库与职位面试。未登录时不会请求麦克风权限，也不会显示任何会议内容。",
    }),
  );

  const form = el(doc, "form", {
    className: "login-form",
    attrs: { method: "get", action: base, novalidate: "novalidate", "data-testid": "login-form" },
  });
  const field = el(doc, "div", { className: "login-field" });
  const input = el(doc, "input", {
    className: "login-email",
    attrs: {
      id: "login-email",
      name: "email",
      type: "email",
      autocomplete: "username",
      inputmode: "email",
      placeholder: "you@example.com",
      "data-testid": "login-email",
    },
  });
  field.append(
    el(doc, "label", { text: "邮箱", attrs: { for: "login-email" } }),
    input,
  );
  const error = el(doc, "p", {
    className: "login-error",
    attrs: { role: "alert", "data-testid": "login-error" },
  });
  error.hidden = true;
  const hidden = el(doc, "input", { attrs: { type: "hidden", name: "next", value: next } });
  const submit = el(doc, "a", {
    className: "btn btn--primary login-submit",
    text: "登录",
    attrs: { href: base, "data-testid": "login-link", role: "button" },
  });
  form.append(field, hidden, error, submit);
  section.append(form);

  const win = doc.defaultView || globalThis;
  const showError = (text) => {
    error.textContent = text;
    error.hidden = false;
    input.setAttribute("aria-invalid", "true");
    input.focus();
  };
  const submitLogin = () => {
    const email = typeof input.value === "string" ? input.value.trim() : "";
    if (!email) {
      showError("请填写邮箱地址。");
      return;
    }
    if (!email.includes("@")) {
      showError("邮箱地址格式不正确。");
      return;
    }
    error.hidden = true;
    input.removeAttribute("aria-invalid");
    const target = loginHrefFor(doc, { loginPath: base, email, next });
    if (win.location && typeof win.location.assign === "function") win.location.assign(target);
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitLogin();
  });
  submit.addEventListener("click", (event) => {
    event.preventDefault();
    submitLogin();
  });

  setGuestMode(doc, true);
  return section;
}
