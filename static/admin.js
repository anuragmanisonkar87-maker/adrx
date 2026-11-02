(async () => {
  "use strict";

  const session = window.AdRxSession;
  const me = await session.load();
  if (!me || !me.login_required || !me.user || me.user.role !== "admin") {
    location.assign("/");
    return;
  }
  session.renderMenu(document.getElementById("account-menu"));

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const SHORT = { check_text: "Text", check_documents: "Documents", check_audio: "Audio", check_video: "Video", ai_review: "AI review" };
  const state = { users: [], labels: {}, defaults: [], meId: null, editing: null, minLength: 10 };

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === "class") node.className = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function when(stamp) {
    if (!stamp) return "Never";
    const seconds = Date.now() / 1000 - stamp;
    if (seconds < 60) return "Just now";
    if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
    return new Date(stamp * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }
  const fullTime = (stamp) => new Date(stamp * 1000).toLocaleString();

  function pageError(message) {
    const box = $("#page-error");
    box.textContent = message;
    box.hidden = !message;
  }

  /* ---------- dialogs ---------- */

  function openDialog(dialog) {
    const error = $("[data-error]", dialog);
    if (error) error.hidden = true;
    dialog.showModal();
  }
  function dialogError(dialog, message) {
    const error = $("[data-error]", dialog);
    error.textContent = message;
    error.hidden = false;
  }
  $$("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

  // A suggested password is exactly the server's minimum length (10): easy to pass on, with small and capital
  // letters and digits, and no look-alike characters (0/O, 1/l/I).
  function suggestPassword() {
    const alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    for (;;) {
      const values = crypto.getRandomValues(new Uint32Array(state.minLength));
      const password = Array.from(values, (v) => alphabet[v % alphabet.length]).join("");
      if (/[a-z]/.test(password) && /[A-Z]/.test(password) && /[2-9]/.test(password) && new Set(password).size >= 6) {
        return password;
      }
    }
  }

  function applyMinLength() {
    for (const id of ["add-password", "reset-password"]) $(`#${id}`).setAttribute("minlength", String(state.minLength));
    $$("[data-min-length]").forEach((n) => { n.textContent = String(state.minLength); });
  }

  function tooShort(dialog, password) {
    if (password.length >= state.minLength) return false;
    dialogError(dialog, `Use at least ${state.minLength} characters.`);
    return true;
  }
  $$("[data-suggest]").forEach((b) =>
    b.addEventListener("click", () => { $(`#${b.dataset.suggest}`).value = suggestPassword(); }));

  function renderChecks(dialog, selected) {
    const box = $("[data-perms]", dialog);
    box.textContent = "";
    for (const [name, label] of Object.entries(state.labels)) {
      const input = el("input", { type: "checkbox", name: "perm", value: name });
      input.checked = selected.includes(name);
      box.append(el("label", { class: "check" }, input, el("span", {}, label)));
    }
  }
  const checked = (dialog) => $$("input[name=perm]:checked", dialog).map((i) => i.value);
  const roleOf = (dialog) => ($("input[type=radio]:checked", dialog) || {}).value;

  function syncRole(dialog) {
    const admin = roleOf(dialog) === "admin";
    $$("input[name=perm]", dialog).forEach((i) => { i.disabled = admin; });
    $("[data-admin-hint]", dialog).hidden = !admin;
    $("[data-perms]", dialog).classList.toggle("is-off", admin);
  }
  $$("dialog input[type=radio]").forEach((r) => r.addEventListener("change", () => syncRole(r.closest("dialog"))));

  async function send(url, method, payload) {
    const options = { method };
    if (payload !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(payload);
    }
    const res = await session.api(url, options);
    if (!res.ok) throw new Error(await session.errorText(res));
    return res.json();
  }

  function showDetails(title, text, username, password) {
    const dialog = $("#details-dialog");
    $("#details-title").textContent = title;
    $("#details-text").textContent = text;
    $("#details-site").textContent = location.origin;
    $("#details-username").textContent = username;
    $("#details-password").textContent = password;
    const copy = $("#details-copy");
    copy.textContent = "Copy sign-in details";
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(`Website: ${location.origin}\nUsername: ${username}\nPassword: ${password}`);
        copy.textContent = "Copied";
      } catch (err) {
        copy.textContent = "Select and copy the details above";
      }
    };
    openDialog(dialog);
  }

  /* ---------- the list ---------- */

  function actionButton(label, onClick, kind = "quiet") {
    const b = el("button", { type: "button", class: `btn btn--small btn--${kind}` }, label);
    b.addEventListener("click", onClick);
    return b;
  }

  function userRow(u) {
    const self = u.id === state.meId;
    const name = el("td", { "data-label": "Name" },
      el("span", { class: "user__name" }, u.display_name),
      el("span", { class: "user__login" }, self ? `${u.username} (you)` : u.username));
    const role = el("td", { "data-label": "Role" },
      el("span", { class: `badge${u.role === "admin" ? " badge--admin" : ""}` }, u.role === "admin" ? "Admin" : "User"));
    const access = el("td", { "data-label": "Can check" });
    if (u.role === "admin") access.append(el("span", { class: "muted" }, "Everything"));
    else if (!u.permissions.length) access.append(el("span", { class: "muted" }, "Nothing yet"));
    else access.append(el("span", { class: "tags" }, ...u.permissions.map((p) => el("span", { class: "tag", title: state.labels[p] }, SHORT[p] || p))));
    const status = el("td", { "data-label": "Status" },
      el("span", { class: `badge ${u.is_active ? "badge--on" : "badge--off"}` }, u.is_active ? "Active" : "Deactivated"));
    if (u.is_active && u.must_change_password) status.append(el("span", { class: "user__note" }, "Must choose a password"));
    const last = el("td", { "data-label": "Last sign-in", title: u.last_login_at ? fullTime(u.last_login_at) : null }, when(u.last_login_at));
    if (u.sessions) last.append(el("span", { class: "user__note" }, `Signed in on ${u.sessions} device${u.sessions === 1 ? "" : "s"}`));
    const actions = el("td", { class: "row-actions" },
      actionButton("Edit", () => openEdit(u)),
      actionButton("Password", () => openPassword(u)));
    if (!self) {
      actions.append(u.is_active
        ? actionButton("Deactivate", () => confirmDeactivate(u), "danger-quiet")
        : actionButton("Activate", () => activate(u)));
      if (u.sessions) actions.append(actionButton("Sign out", () => confirmSignOut(u)));
    }
    return el("tr", { class: u.is_active ? null : "is-off" }, name, role, access, status, last, actions);
  }

  async function load() {
    try {
      const body = await send("/api/admin/users", "GET");
      Object.assign(state, { users: body.users, labels: body.permission_labels, defaults: body.default_permissions, meId: body.me,
        minLength: body.password_min_length || state.minLength });
      applyMinLength();
      const notice = $("#storage-notice");
      notice.hidden = body.storage.lasting;
      notice.textContent = "Accounts are kept on this server's own disk, which Render clears on every deploy and restart, " +
        "so accounts added here won't last. Set DATABASE_URL to your Supabase database to keep them.";
      $("#users-body").replaceChildren(...state.users.map(userRow));
      pageError("");
    } catch (err) {
      pageError(err.message);
    }
    loadActivity();
  }

  /* ---------- actions ---------- */

  $("#add-user-btn").addEventListener("click", () => {
    const dialog = $("#add-dialog");
    $("#add-form").reset();
    renderChecks(dialog, state.defaults);
    syncRole(dialog);
    $("#add-password").value = suggestPassword();
    openDialog(dialog);
    $("#add-username").focus();
  });

  $("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const dialog = $("#add-dialog");
    const payload = {
      username: $("#add-username").value.trim().toLowerCase(),
      display_name: $("#add-name").value.trim(),
      role: roleOf(dialog),
      permissions: checked(dialog),
      password: $("#add-password").value,
      must_change_password: $("#add-must-change").checked,
    };
    if (!payload.username) return dialogError(dialog, "Enter a username.");
    if (tooShort(dialog, payload.password)) return;
    try {
      const { user } = await send("/api/admin/users", "POST", payload);
      dialog.close();
      showDetails("Account created",
        payload.must_change_password ? `${user.display_name} chooses their own password at first sign-in.` : `${user.display_name} can sign in now.`,
        user.username, payload.password);
      load();
    } catch (err) {
      dialogError(dialog, err.message);
    }
  });

  function openEdit(u) {
    const dialog = $("#edit-dialog");
    state.editing = u;
    const self = u.id === state.meId;
    $("#edit-title").textContent = `Edit ${u.display_name}`;
    $("#edit-name").value = u.display_name === u.username ? "" : u.display_name;
    $$("input[name=edit-role]", dialog).forEach((r) => { r.checked = r.value === u.role; r.disabled = self; });
    $("[data-self-hint]", dialog).hidden = !self;
    renderChecks(dialog, u.permissions);
    syncRole(dialog);
    openDialog(dialog);
  }

  $("#edit-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const dialog = $("#edit-dialog");
    const u = state.editing;
    const payload = { display_name: $("#edit-name").value.trim(), permissions: checked(dialog) };
    if (u.id !== state.meId) payload.role = roleOf(dialog);
    try {
      await send(`/api/admin/users/${u.id}`, "PATCH", payload);
      dialog.close();
      load();
    } catch (err) {
      dialogError(dialog, err.message);
    }
  });

  function openPassword(u) {
    const dialog = $("#password-dialog");
    state.editing = u;
    const self = u.id === state.meId;
    $("#password-title").textContent = `New password for ${u.display_name}`;
    $("#password-lede").textContent = self
      ? "Your other devices are signed out. This one stays signed in."
      : "The account is signed out everywhere. Give the new password to its owner.";
    $("#reset-password").value = suggestPassword();
    $("#reset-must-change").checked = !self;
    $("#reset-must-change-row").hidden = self;
    openDialog(dialog);
  }

  $("#password-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const dialog = $("#password-dialog");
    const u = state.editing;
    const password = $("#reset-password").value;
    const mustChange = $("#reset-must-change").checked && u.id !== state.meId;
    if (tooShort(dialog, password)) return;
    try {
      await send(`/api/admin/users/${u.id}/password`, "POST", { password, must_change_password: mustChange });
      dialog.close();
      showDetails("Password set", mustChange ? `${u.display_name} chooses a new password at next sign-in.` : "The new password works now.",
        u.username, password);
      load();
    } catch (err) {
      dialogError(dialog, err.message);
    }
  });

  function confirmAction(title, text, okLabel, action) {
    const dialog = $("#confirm-dialog");
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    $("#confirm-ok").textContent = okLabel;
    $("#confirm-form").onsubmit = async (e) => {
      e.preventDefault();
      try {
        await action();
        dialog.close();
        load();
      } catch (err) {
        dialogError(dialog, err.message);
      }
    };
    openDialog(dialog);
  }

  const confirmDeactivate = (u) => confirmAction(
    `Deactivate ${u.display_name}?`,
    "They're signed out at once and can't sign in until you activate the account again. Nothing is deleted.",
    "Deactivate", () => send(`/api/admin/users/${u.id}`, "PATCH", { is_active: false }));

  const confirmSignOut = (u) => confirmAction(
    `Sign ${u.display_name} out everywhere?`,
    "They can sign in again with their password.",
    "Sign out", () => send(`/api/admin/users/${u.id}/sign-out`, "POST"));

  async function activate(u) {
    try {
      await send(`/api/admin/users/${u.id}`, "PATCH", { is_active: true });
      load();
    } catch (err) {
      pageError(err.message);
    }
  }

  /* ---------- activity ---------- */

  function who(name) {
    return { settings: "Server settings", "command line": "Command line" }[name] || name || "Someone";
  }

  function describe(ev) {
    const a = who(ev.actor);
    const t = ev.target || "";
    const extra = ev.detail ? ` (${ev.detail})` : "";
    switch (ev.action) {
      case "sign_in": return `${a} signed in`;
      case "sign_out": return `${a} signed out`;
      case "sign_in_failed": return t ? `Failed sign-in for ${t}${extra}` : "Failed sign-in with an unknown username";
      case "password_changed": return `${a} changed their password`;
      case "password_reset": return `${a} set a new password for ${t}${extra}`;
      case "user_created": return `${a} added ${t}${extra}`;
      case "user_updated": return `${a} changed ${t}${extra}`;
      case "user_activated": return `${a} activated ${t}`;
      case "user_deactivated": return `${a} deactivated ${t}`;
      case "signed_out_everywhere": return `${a} signed ${t} out everywhere${extra}`;
      default: return `${ev.action} ${t}`;
    }
  }

  async function loadActivity() {
    const list = $("#activity-list");
    try {
      const { events } = await send("/api/admin/activity?limit=60", "GET");
      list.replaceChildren(...(events.length ? events.map((ev) => el("li", { class: ev.action === "sign_in_failed" ? "is-warn" : null },
        el("time", { datetime: new Date(ev.at * 1000).toISOString(), title: fullTime(ev.at) }, when(ev.at)),
        el("span", { class: "activity__text" }, describe(ev)),
        ev.ip ? el("span", { class: "activity__ip" }, ev.ip) : null)) : [el("li", { class: "muted" }, "Nothing yet.")]));
    } catch (err) {
      list.replaceChildren(el("li", { class: "muted" }, err.message));
    }
  }

  load();
})();
