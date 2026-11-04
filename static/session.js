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

  // Takes the browser's current sign-in (it may have changed since this page was opened, for example after signing
  // in again in another tab). Signed out: off to the sign-in page; a different account: the page reloads.
  // Returns false when the server could not say.
  async function refresh() {
    const previousUser = me && me.user && me.user.id;
    const fresh = await fetch("/api/auth/me", { headers: { Accept: "application/json" }, credentials: "same-origin" });
    if (fresh.status === 401) return toSignIn();
    if (!fresh.ok) return false;
    me = await fresh.json();
    if (!me.user || me.user.id !== previousUser) {
      location.reload();
      return never();
    }
    if (me.user.must_change_password && location.pathname !== "/account") {
      location.assign("/account");
      return never();
    }
    return true;
  }

  // fetch() can't report how much of a file has been sent, so uploads go through XMLHttpRequest:
  // options.onUpload(sentBytes, totalBytes) while sending, options.onSent() once everything is sent.
  // Resolves with a Response like fetch(); a broken connection rejects with a TypeError, as fetch() does.
  function send(url, options, headers) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const failed = () => reject(new TypeError("Failed to fetch"));
      xhr.open(options.method || "GET", url);
      headers.forEach((value, name) => xhr.setRequestHeader(name, value));
      if (options.onUpload) {
        xhr.upload.addEventListener("progress", (e) => options.onUpload(e.loaded, e.lengthComputable ? e.total : 0));
      }
      if (options.onSent) xhr.upload.addEventListener("load", () => options.onSent());
      xhr.addEventListener("load", () => {
        try {
          const responseHeaders = new Headers();
          xhr.getAllResponseHeaders().trim().split(/[\r\n]+/).forEach((line) => {
            const colon = line.indexOf(":");
            if (colon > 0) responseHeaders.append(line.slice(0, colon).trim(), line.slice(colon + 1).trim());
          });
          const body = [204, 205, 304].includes(xhr.status) ? null : xhr.responseText;
          resolve(new Response(body, { status: xhr.status, statusText: xhr.statusText, headers: responseHeaders }));
        } catch (err) {
          failed();
        }
      });
      ["error", "abort", "timeout"].forEach((type) => xhr.addEventListener(type, failed));
      xhr.send(options.body === undefined ? null : options.body);
    });
  }

  async function api(url, options = {}, retried = false) {
    const upload = Boolean(options.onUpload || options.onSent);
    // Before sending a file, make sure the sign-in still works and the security token is current,
    // so a big file isn't uploaded only to be refused at the end.
    if (upload && me && me.login_required && !retried) await refresh();
    const headers = new Headers(options.headers || {});
    if (me && me.csrf_token) headers.set("X-CSRF-Token", me.csrf_token);
    if (!headers.has("Accept")) headers.set("Accept", "application/json");
    const res = upload
      ? await send(url, options, headers)
      : await fetch(url, { ...options, headers, credentials: "same-origin" });
    if (me && me.login_required) {
      if (res.status === 401) return toSignIn();
      if (res.status === 403) {
        const body = await res.clone().json().catch(() => ({}));
        if (body.code === "password_change") {
          location.assign("/account");
          return never();
        }
        // This page was opened under an earlier sign-in: take the current one and try once more.
        if (body.code === "csrf" && !retried && await refresh()) return api(url, options, true);
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
