"""Sign-in and user accounts, switched on with REQUIRE_LOGIN=on (README: "Sign-in and user accounts").

Two roles: an admin manages accounts (create, edit, activate, deactivate, reset passwords, choose what each account
may use) and may use everything; a user may use the features an admin allowed. Passwords are hashed with Argon2id,
sessions are random tokens in HttpOnly cookies (only their SHA-256 is stored), changes need a CSRF token, and
failed sign-ins are limited.
"""

from app.accounts.permissions import DEFAULT_PERMISSIONS, KIND_PERMISSION, KIND_WORDS, PERMISSIONS, ROLES
from app.accounts.service import Housekeeping, bootstrap
from app.accounts.store import AccountsUnavailable, User, get_store, reset_stores

__all__ = [
    "DEFAULT_PERMISSIONS", "KIND_PERMISSION", "KIND_WORDS", "PERMISSIONS", "ROLES",
    "Housekeeping", "bootstrap", "AccountsUnavailable", "User", "get_store", "reset_stores",
]
