"""Sign-in pages, the account APIs and the gate in front of everything (with REQUIRE_LOGIN=on)."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.accounts import passwords, security
from app.accounts.permissions import DEFAULT_PERMISSIONS, PERMISSIONS, ROLES, SHORT_LABELS, clean_permissions
from app.accounts.security import sign_in_limits
from app.accounts.store import (AccountStore, AccountsUnavailable, LastAdmin, Session, User, UsernameTaken,
                                database_url, describe_database, get_store, now)
from app.config import PROJECT_DIR, Settings

log = logging.getLogger("nmc.accounts")

STATIC_DIR = PROJECT_DIR / "static"
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
USERNAME_RULE = ("Usernames have 3 to 32 characters: small letters, digits, dots, dashes or underscores, "
                 "starting with a letter or digit.")
UNAVAILABLE = ("Sign-in isn't available right now because the accounts database can't be reached. "
               "Please try again in a minute.")

# Open without signing in: the sign-in page, the health check (Render, keep-awake) and the static files
PUBLIC_PATHS = {"/login", "/api/health", "/api/auth/login", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/",)
# All that an account that must choose a new password can use
PASSWORD_CHANGE_PATHS = {"/account", "/api/auth/me", "/api/auth/password", "/api/auth/logout"}

router = APIRouter()


@dataclass(frozen=True)
class SignedIn:
    user: User
    session: Session


def signed_in(request: Request) -> SignedIn | None:
    return getattr(request.state, "auth", None)


# --- the gate ------------------------------------------------------------------------------------------------

def _resolve(settings: Settings, token: str) -> SignedIn | None:
    store = get_store(settings)
    found = store.session_user(security.hash_token(token))
    if found is None:
        return None
    session, user = found
    t = now()
    idle = t - session.last_seen_at > settings.session_idle_hours * 3600
    if t >= session.expires_at or idle or not user.is_active:
        store.delete_session(session.id)
        return None
    if t - session.last_seen_at >= 300:  # write "last seen" at most every 5 minutes
        store.touch_session(session.id)
    return SignedIn(user, session)


async def gate(request: Request, call_next):
    """Deny by default: with REQUIRE_LOGIN=on, every page and API needs a signed-in account except the sign-in page,
    the health check and the static files. Changes (POST, PATCH...) also need the session's CSRF token."""
    request.state.auth = None
    settings = Settings.from_env()
    if not settings.require_login:
        return await call_next(request)
    path = request.url.path
    unsafe = request.method not in security.SAFE_METHODS
    if unsafe and not security.same_origin(request):
        return JSONResponse(status_code=403, content={"detail": "Requests from other websites are not accepted."})
    public = path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
    token = security.session_token(request)
    # scripts, styles, fonts and the health check never need the account: no database lookup for them
    if token and not (path.startswith(PUBLIC_PREFIXES) or path in ("/api/health", "/favicon.ico")):
        try:
            request.state.auth = await run_in_threadpool(_resolve, settings, token)
        except AccountsUnavailable:
            if not public:
                return JSONResponse(status_code=503, content={"detail": UNAVAILABLE})
    auth: SignedIn | None = request.state.auth
    if public:
        return await call_next(request)
    if auth is None:
        if path.startswith("/api/") or unsafe:
            response = JSONResponse(status_code=401, content={"detail": "Please sign in.", "code": "sign_in"})
        else:
            target = path + (f"?{request.url.query}" if request.url.query else "")
            response = RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)
        if token:
            security.clear_session_cookie(response)  # an expired or unknown session
        return response
    if unsafe and not security.csrf_ok(request, auth.session.csrf_token):
        return JSONResponse(status_code=403, content={"detail": "This page is out of date. Reload it and try again.",
                                                      "code": "csrf"})
    if auth.user.must_change_password and path not in PASSWORD_CHANGE_PATHS:
        if path.startswith("/api/"):
            return JSONResponse(status_code=403, content={"detail": "Choose a new password first.",
                                                          "code": "password_change"})
        return RedirectResponse("/account", status_code=303)
    return await call_next(request)


async def unavailable_handler(_request: Request, _exc: AccountsUnavailable):
    return JSONResponse(status_code=503, content={"detail": UNAVAILABLE})


# --- pages ---------------------------------------------------------------------------------------------------

def _page(name: str) -> FileResponse:
    return FileResponse(STATIC_DIR / name, headers={"Cache-Control": "no-store"})


@router.get("/login", include_in_schema=False)
def sign_in_page(request: Request, next: str | None = None):
    if not Settings.from_env().require_login:
        return RedirectResponse("/", status_code=303)
    if signed_in(request):
        return RedirectResponse(security.safe_next(next), status_code=303)
    return _page("login.html")


@router.get("/account", include_in_schema=False)
def account_page():
    if not Settings.from_env().require_login:
        return RedirectResponse("/", status_code=303)
    return _page("account.html")


@router.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    auth = signed_in(request)
    if not Settings.from_env().require_login or auth is None or not auth.user.is_admin:
        return RedirectResponse("/", status_code=303)
    return _page("admin.html")


# --- signing in and out --------------------------------------------------------------------------------------

class SignInBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordChangeBody(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


def _settings() -> Settings:
    settings = Settings.from_env()
    if not settings.require_login:
        raise HTTPException(404, "Sign-in is switched off on this server (REQUIRE_LOGIN).")
    return settings


def _wait_text(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _me(auth: SignedIn) -> dict:
    return {"login_required": True, "user": auth.user.public(), "csrf_token": auth.session.csrf_token,
            "permission_labels": PERMISSIONS}


@router.post("/api/auth/login")
def sign_in(body: SignInBody, request: Request):
    settings = _settings()
    ip = security.client_ip(request)
    username = body.username.strip().lower()
    wait = sign_in_limits.wait_seconds(ip, username)
    if wait:
        raise HTTPException(429, f"Too many failed sign-ins. Please try again in {_wait_text(wait)}.",
                            headers={"Retry-After": str(wait)})
    store = get_store(settings)
    user = store.find_user(username) if USERNAME_RE.match(username) else None
    if user is None:
        passwords.verify_nothing(body.password)  # as slow as a real check, so usernames can't be probed
        ok = False
    else:
        ok = passwords.verify_password(user.password_hash, body.password)
    if not ok:
        sign_in_limits.failed(ip, username)
        if user is not None:
            store.log_event("sign_in_failed", target=user.username, detail="wrong password", ip=ip)
            log.info("Failed sign-in for %s from %s", user.username, ip)
        else:
            store.log_event("sign_in_failed", detail="unknown username", ip=ip)
        raise HTTPException(401, "Wrong username or password.")
    if not user.is_active:
        store.log_event("sign_in_failed", target=user.username, detail="account deactivated", ip=ip)
        raise HTTPException(403, "This account is deactivated. Ask your admin to activate it again.")
    sign_in_limits.succeeded(ip, username)
    previous = signed_in(request)
    if previous:
        store.delete_session(previous.session.id)
    token, token_hash = security.new_token()
    max_age = settings.session_max_days * 86400
    session = store.create_session(user_id=user.id, token_hash=token_hash, csrf_token=security.new_csrf_token(),
                                   max_age=max_age, ip=ip, user_agent=request.headers.get("user-agent"))
    rehash = passwords.hash_password(body.password) if passwords.needs_rehash(user.password_hash) else None
    store.record_sign_in(user.id, rehash)
    store.log_event("sign_in", actor=user.username, ip=ip)
    response = JSONResponse(_me(SignedIn(user, session)), headers={"Cache-Control": "no-store"})
    security.set_session_cookie(response, request, token, max_age)
    return response


@router.post("/api/auth/logout")
def sign_out(request: Request):
    settings = _settings()
    auth = signed_in(request)
    response = JSONResponse({"ok": True})
    if auth:
        store = get_store(settings)
        store.delete_session(auth.session.id)
        store.log_event("sign_out", actor=auth.user.username, ip=security.client_ip(request))
    security.clear_session_cookie(response)
    return response


@router.get("/api/auth/me")
def me(request: Request):
    if not Settings.from_env().require_login:
        return {"login_required": False, "user": None}
    auth = signed_in(request)
    if auth is None:  # the gate answers first; kept for clarity
        raise HTTPException(401, "Please sign in.")
    return JSONResponse(_me(auth), headers={"Cache-Control": "no-store"})


@router.post("/api/auth/password")
def change_password(body: PasswordChangeBody, request: Request):
    settings = _settings()
    auth = signed_in(request)
    ip = security.client_ip(request)
    wait = sign_in_limits.wait_seconds(ip, auth.user.username)
    if wait:
        raise HTTPException(429, f"Too many wrong passwords. Please try again in {_wait_text(wait)}.")
    store = get_store(settings)
    if not passwords.verify_password(auth.user.password_hash, body.current_password):
        sign_in_limits.failed(ip, auth.user.username)
        raise HTTPException(400, "Your current password is not right.")
    if body.new_password == body.current_password:
        raise HTTPException(400, "Choose a password that is different from the current one.")
    problem = passwords.password_problem(body.new_password, auth.user.username)
    if problem:
        raise HTTPException(400, problem)
    store.set_password(auth.user.id, passwords.hash_password(body.new_password), must_change=False,
                       keep_session_id=auth.session.id)
    store.log_event("password_changed", actor=auth.user.username, target=auth.user.username, ip=ip)
    return {"ok": True}


# --- managing accounts (admins only) -------------------------------------------------------------------------

class NewUserBody(BaseModel):
    username: str = Field(max_length=64)
    display_name: str = Field("", max_length=80)
    password: str = Field(max_length=256)
    role: Literal["admin", "user"] = "user"
    permissions: list[str] = Field(default_factory=lambda: list(DEFAULT_PERMISSIONS), max_length=20)
    must_change_password: bool = True


class UserChangesBody(BaseModel):
    display_name: str | None = Field(None, max_length=80)
    role: Literal["admin", "user"] | None = None
    permissions: list[str] | None = Field(None, max_length=20)
    is_active: bool | None = None


class PasswordResetBody(BaseModel):
    password: str = Field(max_length=256)
    must_change_password: bool = True


def _admin(request: Request) -> tuple[Settings, SignedIn, AccountStore]:
    settings = _settings()
    auth = signed_in(request)
    if auth is None:
        raise HTTPException(401, "Please sign in.")
    if not auth.user.is_admin:
        raise HTTPException(403, "Only admins can manage accounts.")
    return settings, auth, get_store(settings)


def _clean_name(name: str | None) -> str | None:
    if name is None:
        return None
    return " ".join("".join(ch for ch in name if ch.isprintable()).split())[:80]


def _permissions(names: list[str] | None):
    if names is None:
        return None
    try:
        return clean_permissions(names)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _access_text(permissions) -> str:
    return ", ".join(SHORT_LABELS[p] for p in PERMISSIONS if p in permissions) or "nothing"


def _target(store: AccountStore, user_id: int) -> User:
    user = store.get_user(user_id)
    if user is None:
        raise HTTPException(404, "There is no such account.")
    return user


def _storage(settings: Settings) -> dict:
    info = describe_database(database_url(settings))
    # Render's free plan starts every deploy and restart with an empty disk
    info["lasting"] = not (info["kind"] == "sqlite" and os.getenv("RENDER"))
    return info


@router.get("/api/admin/users")
def list_accounts(request: Request):
    settings, auth, store = _admin(request)
    counts = store.session_counts()
    return {
        "users": [dict(user.public(), sessions=counts.get(user.id, 0)) for user in store.list_users()],
        "me": auth.user.id,
        "roles": list(ROLES),
        "permission_labels": PERMISSIONS,
        "default_permissions": list(DEFAULT_PERMISSIONS),
        "storage": _storage(settings),
    }


@router.post("/api/admin/users", status_code=201)
def create_account(body: NewUserBody, request: Request):
    _settings_, auth, store = _admin(request)
    username = body.username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, USERNAME_RULE)
    problem = passwords.password_problem(body.password, username)
    if problem:
        raise HTTPException(400, problem)
    permissions = _permissions(body.permissions)
    try:
        user = store.create_user(username=username, display_name=_clean_name(body.display_name) or "",
                                 password_hash=passwords.hash_password(body.password), role=body.role,
                                 permissions=permissions, must_change_password=body.must_change_password)
    except UsernameTaken:
        raise HTTPException(409, f"The username {username} is already taken.") from None
    store.log_event("user_created", actor=auth.user.username, target=username,
                    detail="admin" if body.role == "admin" else f"user; can check: {_access_text(permissions)}",
                    ip=security.client_ip(request))
    return {"user": user.public()}


@router.patch("/api/admin/users/{user_id}")
def update_account(user_id: int, body: UserChangesBody, request: Request):
    _settings_, auth, store = _admin(request)
    before = _target(store, user_id)
    if user_id == auth.user.id:
        if body.is_active is False:
            raise HTTPException(400, "You can't deactivate your own account.")
        if body.role is not None and body.role != "admin":
            raise HTTPException(400, "You can't remove your own admin role; another admin can.")
    permissions = _permissions(body.permissions)
    try:
        after = store.update_user(user_id, display_name=_clean_name(body.display_name), role=body.role,
                                  permissions=permissions, is_active=body.is_active)
    except LastAdmin:
        raise HTTPException(400, "At least one active admin account must remain.") from None
    ip = security.client_ip(request)
    if before.is_active != after.is_active:
        store.log_event("user_activated" if after.is_active else "user_deactivated", actor=auth.user.username,
                        target=after.username, ip=ip)
    changes = []
    if before.role != after.role:
        changes.append(f"role {before.role} -> {after.role}")
    if before.permissions != after.permissions:
        changes.append(f"can check: {_access_text(after.permissions)}")
    if before.display_name != after.display_name:
        changes.append("name")
    if changes:
        store.log_event("user_updated", actor=auth.user.username, target=after.username, detail="; ".join(changes),
                        ip=ip)
    return {"user": after.public()}


@router.post("/api/admin/users/{user_id}/password")
def reset_password(user_id: int, body: PasswordResetBody, request: Request):
    _settings_, auth, store = _admin(request)
    target = _target(store, user_id)
    problem = passwords.password_problem(body.password, target.username)
    if problem:
        raise HTTPException(400, problem)
    own = user_id == auth.user.id
    store.set_password(user_id, passwords.hash_password(body.password), must_change=body.must_change_password and not own,
                       keep_session_id=auth.session.id if own else None)
    store.log_event("password_reset", actor=auth.user.username, target=target.username,
                    detail="must choose a new one" if body.must_change_password and not own else None,
                    ip=security.client_ip(request))
    return {"ok": True}


@router.post("/api/admin/users/{user_id}/sign-out")
def sign_out_account(user_id: int, request: Request):
    _settings_, auth, store = _admin(request)
    target = _target(store, user_id)
    own = user_id == auth.user.id
    count = store.sign_out_everywhere(user_id, keep_session_id=auth.session.id if own else None)
    store.log_event("signed_out_everywhere", actor=auth.user.username, target=target.username,
                    detail=f"{count} session{'s' if count != 1 else ''}", ip=security.client_ip(request))
    return {"ok": True, "sessions": count}


@router.get("/api/admin/activity")
def activity(request: Request, limit: int = 100):
    _settings_, _auth, store = _admin(request)
    return {"events": store.recent_events(min(max(limit, 1), 500))}
