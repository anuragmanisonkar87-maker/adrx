"""Manage accounts from the command line (uses the same settings and database as the app):

    python -m app.accounts list
    python -m app.accounts create USERNAME [--admin] [--name "Full Name"] [--permissions check_text,check_audio]
    python -m app.accounts reset-password USERNAME
    python -m app.accounts activate USERNAME
    python -m app.accounts deactivate USERNAME
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime

from app.accounts import passwords
from app.accounts.permissions import DEFAULT_PERMISSIONS, PERMISSIONS, clean_permissions
from app.accounts.store import AccountsUnavailable, LastAdmin, UsernameTaken, database_url, get_store
from app.accounts.web import USERNAME_RE, USERNAME_RULE
from app.config import Settings


def _ask_password(username: str) -> str:
    while True:
        first = getpass.getpass(f"New password for {username}: ")
        problem = passwords.password_problem(first, username)
        if problem:
            print(problem)
            continue
        if getpass.getpass("Type it again: ") != first:
            print("The two passwords are different. Try again.")
            continue
        return first


def _when(stamp: int | None) -> str:
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M") if stamp else "never"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.accounts", description="Manage AdRx accounts.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show all accounts")
    create = sub.add_parser("create", help="add an account")
    create.add_argument("username")
    create.add_argument("--admin", action="store_true", help="make it an admin account")
    create.add_argument("--name", default="", help="full name shown in the app")
    create.add_argument("--permissions", default=",".join(DEFAULT_PERMISSIONS),
                        help=f"comma-separated, from: {', '.join(PERMISSIONS)}")
    for name, text in (("reset-password", "set a new password"), ("activate", "allow signing in again"),
                       ("deactivate", "stop the account from signing in")):
        sub.add_parser(name, help=text).add_argument("username")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    try:
        store = get_store(settings)
    except AccountsUnavailable as exc:
        print(f"{exc} ({database_url(settings).split('@')[-1]})", file=sys.stderr)
        return 1

    if args.command == "list":
        for user in store.list_users():
            access = "everything" if user.is_admin else (", ".join(user.granted) or "nothing")
            status = "active" if user.is_active else "DEACTIVATED"
            print(f"{user.username:<20} {user.role:<6} {status:<12} last sign-in {_when(user.last_login_at):<17} {access}")
        return 0

    username = args.username.strip().lower()
    if args.command == "create":
        if not USERNAME_RE.match(username):
            print(USERNAME_RULE, file=sys.stderr)
            return 1
        try:
            permissions = clean_permissions([p.strip() for p in args.permissions.split(",") if p.strip()])
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        password = _ask_password(username)
        try:
            store.create_user(username=username, display_name=args.name.strip(), role="admin" if args.admin else "user",
                              password_hash=passwords.hash_password(password), permissions=permissions,
                              must_change_password=False)
        except UsernameTaken:
            print(f"The username {username} is already taken.", file=sys.stderr)
            return 1
        store.log_event("user_created", actor="command line", target=username)
        print(f"Created {'admin' if args.admin else 'user'} account {username}.")
        return 0

    user = store.find_user(username)
    if user is None:
        print(f"There is no account called {username}.", file=sys.stderr)
        return 1
    if args.command == "reset-password":
        store.set_password(user.id, passwords.hash_password(_ask_password(username)), must_change=False)
        store.log_event("password_reset", actor="command line", target=username)
        print(f"New password set for {username}; the account was signed out everywhere.")
        return 0
    active = args.command == "activate"
    try:
        store.update_user(user.id, is_active=active)
    except LastAdmin:
        print("At least one active admin account must remain.", file=sys.stderr)
        return 1
    store.log_event("user_activated" if active else "user_deactivated", actor="command line", target=username)
    print(f"{username} is now {'active' if active else 'deactivated'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
