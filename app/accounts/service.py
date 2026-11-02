"""Start-up work for accounts: the tables, the first admin account, and regular clean-up."""

from __future__ import annotations

import logging
import threading

from app.accounts import passwords
from app.accounts.permissions import PERMISSIONS
from app.accounts.store import AccountsUnavailable, get_store
from app.config import Settings

log = logging.getLogger("nmc.accounts")


def bootstrap(settings: Settings) -> None:
    """Creates the tables, and the admin account from ADMIN_USERNAME / ADMIN_PASSWORD when it doesn't exist yet.

    With ADMIN_PASSWORD_RESET=on, an existing admin account gets ADMIN_PASSWORD as its password (and is made an
    active admin again): the way back in after a forgotten password. Turn it off again afterwards.
    """
    store = get_store(settings)
    name, password = settings.admin_username, settings.admin_password
    admin = store.find_user(name)
    if password and (admin is None or settings.admin_password_reset):
        problem = passwords.password_problem(password, name)
        if problem:
            log.error("ADMIN_PASSWORD was not used: %s", problem)
        elif admin is None:
            store.create_user(username=name, display_name="Site admin", password_hash=passwords.hash_password(password),
                              role="admin", permissions=PERMISSIONS.keys(), must_change_password=False)
            store.log_event("user_created", actor="settings", target=name, detail="admin account from ADMIN_PASSWORD")
            log.info("Created the admin account '%s' from ADMIN_PASSWORD", name)
        else:
            store.update_user(admin.id, role="admin", is_active=True)
            store.set_password(admin.id, passwords.hash_password(password), must_change=False)
            store.log_event("password_reset", actor="settings", target=name, detail="ADMIN_PASSWORD_RESET=on")
            log.warning("The password of '%s' was set from ADMIN_PASSWORD because ADMIN_PASSWORD_RESET is on. "
                        "Turn ADMIN_PASSWORD_RESET off again, or every restart sets it back.", name)
    if not store.has_active_admin():
        log.warning("Sign-in is on, but there is no active admin account yet. Set ADMIN_PASSWORD (at least %d "
                    "characters), or run: python -m app.accounts create %s --admin", passwords.MIN_LENGTH, name)


class Housekeeping:
    """Every few hours: removes expired sessions and old activity entries. On Supabase's free plan these few
    queries a day also count as activity, so the project isn't paused for being idle."""

    def __init__(self, settings: Settings, every_seconds: float = 6 * 3600) -> None:
        self.settings = settings
        self.every_seconds = every_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="accounts-housekeeping", daemon=True)

    def start(self) -> "Housekeeping":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> None:
        try:
            removed = get_store(self.settings).purge(self.settings.session_idle_hours * 3600)
            if removed:
                log.info("Removed %d expired sign-in sessions", removed)
        except AccountsUnavailable:
            pass  # already logged; tried again next time

    def _run(self) -> None:
        wait = 60.0  # the first clean-up shortly after start-up
        while not self._stop.wait(wait):
            self.run_once()
            wait = self.every_seconds
