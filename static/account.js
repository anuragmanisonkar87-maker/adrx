(async () => {
  "use strict";

  const session = window.AdRxSession;
  const me = await session.load();
  if (!me || !me.login_required || !me.user) {
    location.assign("/");
    return;
  }
  session.renderMenu(document.getElementById("account-menu"));

  const $ = (id) => document.getElementById(id);
  const form = $("password-form");
  const current = $("current-password");
  const fresh = $("new-password");
  const again = $("confirm-password");
  const button = $("save-btn");
  const error = $("form-error");
  const ok = $("form-ok");
  const firstTime = me.user.must_change_password;

  $("first-time").hidden = !firstTime;
  $("title").textContent = firstTime ? "Choose your password" : "Change your password";
  $("signed-in-as").textContent = `Signed in as ${me.user.display_name} (${me.user.username}).`;
  $("account-username").value = me.user.username;
  current.focus();

  function show(message) {
    error.textContent = message;
    error.hidden = false;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    ok.hidden = true;
    if (!current.value) return show("Enter your current password.");
    if (fresh.value.length < 10) return show("The new password needs at least 10 characters.");
    if (fresh.value !== again.value) return show("The two new passwords are different.");
    button.disabled = true;
    try {
      const res = await session.api("/api/auth/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: current.value, new_password: fresh.value }),
      });
      if (!res.ok) throw new Error(await session.errorText(res));
      form.reset();
      $("account-username").value = me.user.username;
      ok.textContent = firstTime
        ? "Your password is saved. Taking you to the checker..."
        : "Your password is changed. Other devices where you were signed in are now signed out.";
      ok.hidden = false;
      if (firstTime) setTimeout(() => location.assign("/"), 1200);
    } catch (err) {
      show(err.message === "Failed to fetch" ? "Could not reach the server. Check your connection and try again." : err.message);
    } finally {
      button.disabled = false;
    }
  });
})();
