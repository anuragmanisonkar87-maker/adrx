"""Sign-in and user accounts (REQUIRE_LOGIN=on): roles, permissions, admin tools and the security around them."""

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import accounts
from app.accounts import security, web
from app.accounts.store import (SCHEMA, LastAdmin, describe_database, get_store, normalize_database_url, sessions,
                                users)
from app.config import Settings
from app.main import app

ADMIN_PASSWORD = "Correct-Horse-Battery-9"
AD = "Mumbai's No.1 skin clinic. 100% guaranteed results!"
# A Postgres database for tests (it is emptied!), e.g. postgresql://user:password@127.0.0.1:5432/postgres.
# With it, every test that uses `site` also runs on Postgres, the way the app runs on Supabase.
POSTGRES = os.getenv("TEST_DATABASE_URL")


@dataclass
class Site:
    path: Path
    kind: str  # sqlite | postgres


def _empty_postgres(url: str) -> None:
    engine = sa.create_engine(normalize_database_url(url))
    with engine.begin() as conn:
        conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    engine.dispose()


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES else []))
def site(request, monkeypatch, tmp_path):
    """Sign-in on, an empty accounts database, and the admin account from ADMIN_PASSWORD."""
    monkeypatch.setenv("REQUIRE_LOGIN", "on")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    if request.param == "postgres":
        _empty_postgres(POSTGRES)
        monkeypatch.setenv("DATABASE_URL", POSTGRES)
    accounts.bootstrap(Settings.from_env())
    return Site(tmp_path, request.param)


def browser() -> TestClient:
    return TestClient(app)  # its own cookies, like a separate browser


def sign_in(client: TestClient, username: str = "admin", password: str = ADMIN_PASSWORD) -> dict:
    """Signs in and sends the CSRF token with every later request of this client."""
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    client.headers[security.CSRF_HEADER] = r.json()["csrf_token"]
    return r.json()


def add_user(admin: TestClient, username: str, permissions=("check_text",), must_change=False,
             password="Temporary-Pass-42", role="user") -> dict:
    r = admin.post("/api/admin/users", json={"username": username, "password": password, "role": role,
                                             "permissions": list(permissions), "must_change_password": must_change})
    assert r.status_code == 201, r.text
    return r.json()["user"]


# --- signing in --------------------------------------------------------------------------------------------

def test_everything_needs_an_account(site):
    c = browser()
    assert c.get("/", follow_redirects=False).headers["location"] == "/login?next=/"
    assert c.get("/admin", follow_redirects=False).headers["location"] == "/login?next=/admin"
    assert c.get("/docs", follow_redirects=False).status_code == 303
    r = c.post("/api/check", data={"text": AD})
    assert r.status_code == 401 and r.json()["code"] == "sign_in"
    assert c.get("/api/admin/users").status_code == 401
    assert c.get("/api/health").json()["login_required"] is True  # Render's health check still works
    assert c.get("/login").status_code == 200 and c.get("/static/styles.css").status_code == 200


def test_sign_in_sets_a_safe_cookie(site):
    c = browser()
    r = c.post("/api/auth/login", json={"username": "Admin ", "password": ADMIN_PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    for part in ("adrx_session=", "HttpOnly", "SameSite=lax", "Path=/", "Max-Age=604800"):
        assert part in cookie
    assert ADMIN_PASSWORD not in r.text and "$argon2" not in r.text and "password_hash" not in r.text
    body = r.json()
    assert body["user"]["role"] == "admin" and set(body["user"]["permissions"]) == set(accounts.PERMISSIONS)
    assert c.get("/", follow_redirects=False).status_code == 200
    assert c.get("/login", follow_redirects=False).headers["location"] == "/"  # already signed in
    assert c.get("/api/auth/me").json()["user"]["username"] == "admin"


def test_https_uses_a_host_only_secure_cookie(site):
    c = TestClient(app, base_url="https://adrx.example")
    r = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.headers["set-cookie"].startswith("__Host-adrx_session=") and "Secure" in r.headers["set-cookie"]
    assert "Strict-Transport-Security" in c.get("/").headers


def test_wrong_password_and_unknown_username_look_the_same(site):
    c = browser()
    wrong = c.post("/api/auth/login", json={"username": "admin", "password": "not-the-password"})
    unknown = c.post("/api/auth/login", json={"username": "nobody", "password": "not-the-password"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Wrong username or password."}


def test_failed_sign_ins_are_limited(site):
    c = browser()
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "admin", "password": "guess-guess-1"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 429 and 800 < int(r.headers["retry-after"]) <= 901
    assert "Too many failed sign-ins" in r.json()["detail"]


def test_sign_in_limits_count_per_address_and_per_account():
    limits = security.SignInLimits()
    t = 1_000_000.0
    for i in range(5):
        limits.failed("1.1.1.1", "asha", now=t + i)
    assert limits.wait_seconds("1.1.1.1", "asha", now=t + 10) == 891  # the first failure is 15 minutes old then
    assert limits.wait_seconds("2.2.2.2", "asha", now=t + 10) is None  # the owner, elsewhere, can still try
    assert limits.wait_seconds("1.1.1.1", "ravi", now=t + 10) is None
    assert limits.wait_seconds("1.1.1.1", "asha", now=t + 901) is None  # 15 minutes later
    for i in range(20):
        limits.failed("3.3.3.3", f"user{i}", now=t)
    assert limits.wait_seconds("3.3.3.3", "someone", now=t + 1) is not None  # one address trying many names


def test_changes_need_the_csrf_token_and_this_site(site):
    c = browser()
    token = sign_in(c)["csrf_token"]
    del c.headers[security.CSRF_HEADER]
    r = c.post("/api/check", data={"text": AD})
    assert r.status_code == 403 and r.json()["code"] == "csrf"
    assert c.post("/api/check", data={"text": AD}, headers={security.CSRF_HEADER: "wrong"}).status_code == 403
    assert c.post("/api/check", data={"text": AD}, headers={security.CSRF_HEADER: token}).status_code == 200
    r = c.post("/api/check", data={"text": AD}, headers={security.CSRF_HEADER: token, "Origin": "https://evil.example"})
    assert r.status_code == 403 and "other websites" in r.json()["detail"]
    r = browser().post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD},
                       headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_sign_out_ends_the_session(site):
    c = browser()
    sign_in(c)
    cookie = c.cookies.get(security.COOKIE)
    r = c.post("/api/auth/logout")
    assert r.status_code == 200 and 'adrx_session=""' in r.headers["set-cookie"]
    stale = browser()
    stale.cookies.set(security.COOKIE, cookie)
    assert stale.get("/api/auth/me").status_code == 401  # the old cookie is useless


def test_idle_and_old_sessions_expire(site, monkeypatch):
    c = browser()
    sign_in(c)
    assert c.get("/api/auth/me").status_code == 200
    monkeypatch.setattr(web, "now", lambda: int(time.time()) + 13 * 3600)  # 13 hours without using the site
    assert c.get("/api/auth/me").status_code == 401


def test_secrets_are_never_stored_in_plain_text(site):
    c = browser()
    sign_in(c)
    cookie = c.cookies.get(security.COOKIE)
    with get_store(Settings.from_env()).engine.connect() as conn:
        password_hash = conn.execute(sa.select(users.c.password_hash).where(users.c.username == "admin")).scalar()
        token_hash = conn.execute(sa.select(sessions.c.token_hash)).scalar()
    assert password_hash.startswith("$argon2id$") and ADMIN_PASSWORD not in password_hash
    assert token_hash == hashlib.sha256(cookie.encode()).hexdigest() and cookie not in token_hash


# --- accounts and permissions ------------------------------------------------------------------------------

def test_new_users_choose_their_own_password_first(site):
    admin = browser()
    sign_in(admin)
    add_user(admin, "priya", permissions=["check_text"], must_change=True)
    user = browser()
    assert sign_in(user, "priya", "Temporary-Pass-42")["user"]["must_change_password"] is True
    r = user.post("/api/check", data={"text": AD})
    assert r.status_code == 403 and r.json()["code"] == "password_change"
    assert user.get("/", follow_redirects=False).headers["location"] == "/account"
    r = user.post("/api/auth/password", json={"current_password": "wrong-wrong-1", "new_password": "My-Own-Secret-77"})
    assert r.status_code == 400
    r = user.post("/api/auth/password", json={"current_password": "Temporary-Pass-42", "new_password": "short"})
    assert r.status_code == 400 and "at least 10" in r.json()["detail"]
    r = user.post("/api/auth/password", json={"current_password": "Temporary-Pass-42",
                                               "new_password": "My-Own-Secret-77"})
    assert r.status_code == 200
    assert user.post("/api/check", data={"text": AD}).status_code == 200
    sign_in(browser(), "priya", "My-Own-Secret-77")


def test_each_feature_needs_its_permission(site, samples):
    admin = browser()
    sign_in(admin)
    priya = add_user(admin, "priya", permissions=["check_text"])
    user = browser()
    sign_in(user, "priya", "Temporary-Pass-42")
    assert user.post("/api/check", data={"text": AD}).status_code == 200

    def upload(name, mime):
        with open(samples / name, "rb") as fh:
            return user.post("/api/check", files={"file": (name, fh, mime)})

    r = upload("clinic_website.pdf", "application/pdf")
    assert r.status_code == 403 and r.json()["detail"] == "Your account can't check documents or images. Ask your admin for access."
    assert "audio or voice notes" in upload("voice_note_3_english_compliant.ogg", "audio/ogg").json()["detail"]
    assert "videos" in upload("reel_en.mp4", "video/mp4").json()["detail"]

    # the admin gives access: it works at once, without signing in again
    r = admin.patch(f"/api/admin/users/{priya['id']}", json={"permissions": ["check_documents"]})
    assert r.status_code == 200 and r.json()["user"]["permissions"] == ["check_documents"]
    assert upload("clinic_website.pdf", "application/pdf").status_code == 200
    r = user.post("/api/check", data={"text": AD})
    assert r.status_code == 403 and "pasted text" in r.json()["detail"]


def test_ai_review_needs_its_permission(site, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # AI review is on for the server
    admin = browser()
    sign_in(admin)
    add_user(admin, "ravi", permissions=["check_text"])
    user = browser()
    sign_in(user, "ravi", "Temporary-Pass-42")
    body = user.post("/api/check", data={"text": AD}).json()
    assert body["analysis"]["mode"] == "rules"  # no AI call for an account without "ai_review"


def test_deactivated_accounts_are_signed_out_at_once(site):
    admin = browser()
    sign_in(admin)
    ravi = add_user(admin, "ravi")
    user = browser()
    sign_in(user, "ravi", "Temporary-Pass-42")
    assert user.post("/api/check", data={"text": AD}).status_code == 200
    off = admin.patch(f"/api/admin/users/{ravi['id']}", json={"is_active": False}).json()["user"]
    assert off["is_active"] is False and off["permissions"] == ["check_text"]  # kept for when it's activated again
    assert user.post("/api/check", data={"text": AD}).status_code == 401
    r = browser().post("/api/auth/login", json={"username": "ravi", "password": "Temporary-Pass-42"})
    assert r.status_code == 403 and "deactivated" in r.json()["detail"]
    admin.patch(f"/api/admin/users/{ravi['id']}", json={"is_active": True})
    sign_in(browser(), "ravi", "Temporary-Pass-42")
    actions = [e["action"] for e in admin.get("/api/admin/activity").json()["events"]]
    assert {"user_created", "user_deactivated", "user_activated", "sign_in", "sign_in_failed"} <= set(actions)


def test_users_cannot_manage_accounts(site):
    admin = browser()
    sign_in(admin)
    add_user(admin, "asha")
    user = browser()
    sign_in(user, "asha", "Temporary-Pass-42")
    assert user.get("/api/admin/users").status_code == 403
    assert user.post("/api/admin/users", json={"username": "x-user", "password": "Whatever-Pass-11"}).status_code == 403
    assert user.patch("/api/admin/users/1", json={"is_active": False}).status_code == 403
    assert user.get("/admin", follow_redirects=False).headers["location"] == "/"


def test_admins_cannot_lock_everyone_out(site):
    admin = browser()
    me = sign_in(admin)["user"]
    assert admin.patch(f"/api/admin/users/{me['id']}", json={"is_active": False}).status_code == 400
    assert admin.patch(f"/api/admin/users/{me['id']}", json={"role": "user"}).status_code == 400
    ops = add_user(admin, "ops", role="admin")
    second = browser()
    sign_in(second, "ops", "Temporary-Pass-42")
    assert second.patch(f"/api/admin/users/{me['id']}", json={"role": "user"}).status_code == 200  # another admin can
    assert admin.get("/api/admin/users").status_code == 403  # no longer an admin, effective at once
    store = get_store(Settings.from_env())
    with pytest.raises(LastAdmin):
        store.update_user(ops["id"], is_active=False)  # "ops" is now the only active admin


def test_password_reset_and_sign_out_everywhere(site):
    admin = browser()
    sign_in(admin)
    asha = add_user(admin, "asha")
    phone, laptop = browser(), browser()
    sign_in(phone, "asha", "Temporary-Pass-42")
    sign_in(laptop, "asha", "Temporary-Pass-42")
    users = {u["username"]: u for u in admin.get("/api/admin/users").json()["users"]}
    assert users["asha"]["sessions"] == 2
    assert admin.post(f"/api/admin/users/{asha['id']}/sign-out").json()["sessions"] == 2
    assert phone.get("/api/auth/me").status_code == laptop.get("/api/auth/me").status_code == 401

    sign_in(phone, "asha", "Temporary-Pass-42")
    r = admin.post(f"/api/admin/users/{asha['id']}/password", json={"password": "Fresh-Start-2026"})
    assert r.status_code == 200
    assert phone.get("/api/auth/me").status_code == 401  # signed out by the reset
    assert browser().post("/api/auth/login", json={"username": "asha", "password": "Temporary-Pass-42"}).status_code == 401
    assert sign_in(browser(), "asha", "Fresh-Start-2026")["user"]["must_change_password"] is True


def test_new_account_rules(site):
    admin = browser()
    sign_in(admin)

    def create(**fields):
        body = {"username": "meera", "password": "Strong-Enough-55", **fields}
        return admin.post("/api/admin/users", json=body)

    assert "at least 10" in create(password="short1").json()["detail"]
    assert "easy to guess" in create(password="password123").json()["detail"]
    assert "username" in create(password="meera12345").json()["detail"]
    assert create(username="Meera Shah").status_code == 400  # spaces aren't allowed
    assert "Unknown permission" in create(permissions=["check_everything"]).json()["detail"]
    assert create().status_code == 201
    assert create().status_code == 409  # taken
    assert create(username="MEERA").status_code == 409  # usernames ignore case


def test_admin_list_never_shows_password_hashes(site):
    admin = browser()
    sign_in(admin)
    add_user(admin, "asha")
    body = admin.get("/api/admin/users").json()
    assert "$argon2" not in str(body) and "password_hash" not in str(body)
    assert body["storage"]["kind"] == site.kind and body["storage"]["lasting"] is True
    assert set(body["permission_labels"]) == set(accounts.PERMISSIONS)


def test_render_without_a_database_warns_that_accounts_wont_last(site, monkeypatch):
    if site.kind != "sqlite":
        pytest.skip("only accounts on the server's own disk are lost on Render")
    monkeypatch.setenv("RENDER", "true")
    admin = browser()
    sign_in(admin)
    assert admin.get("/api/admin/users").json()["storage"]["lasting"] is False


# --- the first admin account, settings and tools -----------------------------------------------------------

def test_admin_account_from_settings_and_the_way_back_in(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("REQUIRE_LOGIN", "on")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    accounts.bootstrap(Settings.from_env())
    assert "ADMIN_PASSWORD was not used" in caplog.text and "no active admin" in caplog.text
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    accounts.bootstrap(Settings.from_env())
    sign_in(browser())
    monkeypatch.setenv("ADMIN_PASSWORD", "Another-Password-31")
    accounts.bootstrap(Settings.from_env())  # an existing admin keeps its own password...
    sign_in(browser())
    monkeypatch.setenv("ADMIN_PASSWORD_RESET", "on")
    accounts.bootstrap(Settings.from_env())  # ...unless the reset is asked for
    sign_in(browser(), "admin", "Another-Password-31")


def test_the_app_start_up_creates_the_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("REQUIRE_LOGIN", "on")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    with TestClient(app) as c:  # runs the app's start-up
        sign_in(c)


def test_sign_in_off_keeps_the_open_site():
    c = browser()
    assert c.post("/api/check", data={"text": AD}).status_code == 200
    assert c.get("/api/auth/me").json() == {"login_required": False, "user": None}
    assert c.get("/admin", follow_redirects=False).headers["location"] == "/"
    assert c.get("/login", follow_redirects=False).headers["location"] == "/"
    assert c.get("/api/admin/users").status_code == 404
    assert c.get("/api/health").json()["login_required"] is False


def test_security_headers(site):
    c = browser()
    sign_in(c)
    headers = c.get("/").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"] and "script-src" not in headers[
        "content-security-policy"]  # default-src 'self' covers scripts
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    assert "no-store" not in c.get("/static/app.js").headers.get("cache-control", "")


def test_command_line_tool(site, monkeypatch, capsys):
    from app.accounts import __main__ as cli

    answers = iter(["Kitchen-Table-Lamp-8", "Kitchen-Table-Lamp-8"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["create", "neha", "--name", "Dr Neha", "--permissions", "check_text,check_audio"]) == 0
    assert cli.main(["deactivate", "neha"]) == 0
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "neha" in out and "DEACTIVATED" in out and "admin" in out
    assert cli.main(["deactivate", "admin"]) == 1  # the only admin


@pytest.mark.parametrize("given, expected", [
    ("/admin", "/admin"), ("/?x=1", "/?x=1"), ("//evil.example", "/"), ("https://evil.example", "/"),
    ("/\\evil.example", "/"), ("", "/"), (None, "/"),
])
def test_after_sign_in_only_pages_on_this_site(given, expected):
    assert security.safe_next(given) == expected


def test_database_addresses_from_supabase_paste_cleanly():
    pooler = "postgresql://postgres.abcd:p@ss#w/rd@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres"
    url = normalize_database_url(pooler)
    assert url == ("postgresql+psycopg://postgres.abcd:p%40ss%23w%2Frd@aws-0-ap-southeast-1.pooler.supabase.com"
                   ":5432/postgres")
    assert normalize_database_url("postgres://u:already%40encoded@h/db") == "postgresql+psycopg://u:already%40encoded@h/db"
    assert describe_database(url) == {"kind": "supabase", "where": "aws-0-ap-southeast-1.pooler.supabase.com"}
    assert "p%40ss" not in str(describe_database(url))
    prisma = "postgresql://u:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?pgbouncer=true&sslmode=require"
    assert normalize_database_url(prisma).endswith(":6543/postgres?sslmode=require")  # ORM-only option dropped
    with pytest.raises(ValueError, match="YOUR-PASSWORD"):
        normalize_database_url("postgresql://postgres.abcd:[YOUR-PASSWORD]@aws-0-x.pooler.supabase.com:5432/postgres")


def test_unreachable_database_says_what_to_fix(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("REQUIRE_LOGIN", "on")
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:pw@db.abcdefgh.supabase.co:5432/postgres")
    monkeypatch.setattr("app.accounts.store._engine", lambda url: sa.create_engine("sqlite:///" + str(tmp_path / "x" / "no.db")))
    with pytest.raises(accounts.AccountsUnavailable):
        get_store(Settings.from_env())
    assert "Session pooler" in caplog.text  # Render can't reach Supabase's direct (IPv6-only) address
    r = browser().post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 503 and "can't be reached" in r.json()["detail"]


def test_on_postgres_the_tables_are_closed_to_supabase_api_roles(site):
    if site.kind != "postgres":
        pytest.skip("Postgres only")
    store = get_store(Settings.from_env())
    with store.engine.connect() as conn:
        q = lambda sql: conn.execute(sa.text(sql)).scalar()  # noqa: E731
        assert q("SELECT count(*) FROM pg_tables WHERE schemaname = 'adrx'") == 3
        assert q("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename IN ('users', 'sessions')") == 0
        assert q("SELECT bool_and(relrowsecurity) FROM pg_class WHERE relnamespace = 'adrx'::regnamespace "
                 "AND relkind = 'r'") is True
        for role in ("anon", "authenticated"):
            if q(f"SELECT count(*) FROM pg_roles WHERE rolname = '{role}'"):
                assert q(f"SELECT has_schema_privilege('{role}', 'adrx', 'USAGE')") is False
                assert q(f"SELECT has_table_privilege('{role}', 'adrx.users', 'SELECT')") is False
