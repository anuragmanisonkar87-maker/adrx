// The signed-in account on AdRx pages: who it is, what it may use, the CSRF token for changes, and the account menu.
// With sign-in switched off on the server, everything is allowed and no menu is shown.
(() => {
  "use strict";

  let me = null;
  const never = () => new Promise(() => {}); // stops the page's own code while the browser goes elsewhere

  function toSignIn() {
    location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
    return never();
  }

  async function load() {
    let res;
    try {
      res = await fetch("/api/auth/me", { headers: { Accept: "application/json" }, credentials: "same-origin" });
    } catch (err) {
      return null;
    }
    if (res.status === 401) return toSignIn();
    if (!res.ok) return null;
    me = await res.json();
    if (me.login_required && me.user && me.user.must_change_password && location.pathname !== "/account") {
      location.assign("/account");
      return never();
    }
    return me;
  }

  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (me && me.csrf_token) headers.set("X-CSRF-Token", me.csrf_token);
    if (!headers.has("Accept")) headers.set("Accept", "application/json");
    const res = await fetch(url, { ...options, headers, credentials: "same-origin" });
    if (me && me.login_required) {
      if (res.status === 401) return toSignIn();
      if (res.status === 403) {
        const body = await res.clone().json().catch(() => ({}));
        if (body.code === "password_change") {
          location.assign("/account");
          return never();
        }
      }
    }
    return res;
  }

  async function errorText(res) {
    const body = await res.json().catch(() => ({}));
    if (Array.isArray(body.detail)) return body.detail.map((d) => d.msg).join("; ");
    return body.detail || `Something went wrong (HTTP ${res.status}).`;
  }

  function can(permission) {
    if (!me || !me.login_required) return true;
    return Boolean(me.user && me.user.permissions.includes(permission));
  }

  async function signOut() {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } finally {
      location.assign("/login");
    }
  }

  function link(href, text) {
    const a = document.createElement("a");
    a.href = href;
    a.textContent = text;
    if (location.pathname === href) a.setAttribute("aria-current", "page");
    return a;
  }

  function renderMenu(nav) {
    if (!nav || !me || !me.login_required || !me.user) return;
    const user = me.user;
    nav.textContent = "";
    const who = document.createElement("span");
    who.className = "account__who";
    const name = document.createElement("span");
    name.className = "account__name";
    name.textContent = user.display_name;
    const role = document.createElement("span");
    role.className = `badge ${user.role === "admin" ? "badge--admin" : ""}`;
    role.textContent = user.role === "admin" ? "Admin" : "User";
    who.append(name, role);
    nav.append(who);
    if (!user.must_change_password) {
      nav.append(link("/", "Checker"));
      if (user.role === "admin") nav.append(link("/admin", "Users"));
      nav.append(link("/account", "Password"));
    }
    const out = document.createElement("button");
    out.type = "button";
    out.className = "linkish";
    out.textContent = "Sign out";
    out.addEventListener("click", signOut);
    nav.append(out);
    nav.hidden = false;
    document.body.classList.add("has-account");
  }

  window.AdRxSession = { load, api, can, errorText, signOut, renderMenu, get me() { return me; } };
})();
