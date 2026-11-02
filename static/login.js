(() => {
  "use strict";

  const form = document.getElementById("sign-in-form");
  const username = document.getElementById("username");
  const password = document.getElementById("password");
  const button = document.getElementById("sign-in-btn");
  const error = document.getElementById("form-error");

  // only a page on this site, never another website
  const safeNext = (path) =>
    path && path.startsWith("/") && !path.startsWith("//") && !path.includes("\\") && !path.includes("://") ? path : "/";

  function show(message) {
    error.textContent = message;
    error.hidden = false;
  }

  username.focus();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    const name = username.value.trim();
    if (!name || !password.value) return show("Enter your username and password.");
    button.disabled = true;
    button.textContent = "Signing in...";
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ username: name, password: password.value }),
        credentials: "same-origin",
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        password.value = "";
        password.focus();
        const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
        throw new Error(detail || `Sign-in failed (HTTP ${res.status}).`);
      }
      const next = new URLSearchParams(location.search).get("next");
      location.assign(body.user.must_change_password ? "/account" : safeNext(next));
    } catch (err) {
      show(err.message === "Failed to fetch" ? "Could not reach the server. Check your connection and try again." : err.message);
      button.disabled = false;
      button.textContent = "Sign in";
    }
  });
})();
