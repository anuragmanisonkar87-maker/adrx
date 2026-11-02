"""The accounts database: users, sign-in sessions and the activity log.

DATABASE_URL points to Postgres, for example Supabase's connection string. Without it, a SQLite file in DATA_DIR is
used. On Postgres the tables live in their own schema, "adrx": Supabase's Data API only publishes the schemas it is
told to (by default "public"), and row-level security on every table plus revoked grants for Supabase's API roles
("anon", "authenticated") keep the tables closed even if that changes. The app itself connects as the database
owner, so it is not affected.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote, urlsplit

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.accounts.permissions import PERMISSIONS
from app.config import Settings

log = logging.getLogger("nmc.accounts")

SCHEMA = "adrx"
AUDIT_KEEP = 10_000  # newest activity-log entries kept

metadata = sa.MetaData()

users = sa.Table(
    "users", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("username", sa.String(32), nullable=False, unique=True),
    sa.Column("display_name", sa.String(80), nullable=False, server_default=""),
    sa.Column("password_hash", sa.String(255), nullable=False),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("permissions", sa.String(255), nullable=False, server_default=""),
    sa.Column("is_active", sa.Boolean, nullable=False),
    sa.Column("must_change_password", sa.Boolean, nullable=False),
    sa.Column("created_at", sa.BigInteger, nullable=False),
    sa.Column("updated_at", sa.BigInteger, nullable=False),
    sa.Column("last_login_at", sa.BigInteger),
    sa.Column("password_changed_at", sa.BigInteger),
)

sessions = sa.Table(
    "sessions", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),  # SHA-256 of the cookie, never the cookie
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("csrf_token", sa.String(64), nullable=False),
    sa.Column("created_at", sa.BigInteger, nullable=False),
    sa.Column("last_seen_at", sa.BigInteger, nullable=False),
    sa.Column("expires_at", sa.BigInteger, nullable=False),
    sa.Column("ip", sa.String(64)),
    sa.Column("user_agent", sa.String(200)),
)

audit_log = sa.Table(
    "audit_log", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("at", sa.BigInteger, nullable=False),
    sa.Column("actor", sa.String(32)),
    sa.Column("action", sa.String(40), nullable=False),
    sa.Column("target", sa.String(32)),
    sa.Column("detail", sa.String(300)),
    sa.Column("ip", sa.String(64)),
)


def now() -> int:
    return int(time.time())


class AccountsUnavailable(RuntimeError):
    """The accounts database can't be reached or set up."""


class UsernameTaken(ValueError):
    pass


class LastAdmin(ValueError):
    """The change would leave no active admin account."""


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role: str
    permissions: frozenset
    is_active: bool
    must_change_password: bool
    created_at: int
    updated_at: int
    last_login_at: int | None
    password_hash: str = field(repr=False, compare=False)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can(self, permission: str) -> bool:
        """What the account may use right now (nothing while it is deactivated)."""
        return self.is_active and (self.is_admin or permission in self.permissions)

    @property
    def granted(self) -> list[str]:
        """What the account has been given, kept while it is deactivated: everything for an admin."""
        return [name for name in PERMISSIONS if self.is_admin or name in self.permissions]

    def public(self) -> dict:
        """What the pages may see: never the password hash."""
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "role": self.role,
            "permissions": self.granted,
            "is_active": self.is_active,
            "must_change_password": self.must_change_password,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }


@dataclass(frozen=True)
class Session:
    id: int
    user_id: int
    csrf_token: str = field(repr=False)
    created_at: int = 0
    last_seen_at: int = 0
    expires_at: int = 0


def _user(row) -> User:
    return User(
        id=row.id,
        username=row.username,
        display_name=row.display_name or "",
        role=row.role,
        permissions=frozenset(p for p in (row.permissions or "").split(",") if p in PERMISSIONS),
        is_active=bool(row.is_active),
        must_change_password=bool(row.must_change_password),
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_login_at=row.last_login_at,
        password_hash=row.password_hash,
    )


def _describe(exc: Exception) -> str:
    text = str(getattr(exc, "orig", None) or exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0][:200] if text else ''}"


# --- connection ----------------------------------------------------------------------------------------------

def normalize_database_url(url: str) -> str:
    """Accepts Supabase's or any Postgres connection string (postgres://, postgresql://) or a SQLAlchemy URL.

    A password with characters such as @ : / # ? must be URL-encoded inside the address; this does that when it
    wasn't done, so the string can be pasted as it is.
    """
    url = url.strip().strip('"').strip("'")
    scheme, sep, rest = url.partition("://")
    if not sep:
        raise ValueError("DATABASE_URL must look like postgresql://user:password@host:5432/postgres")
    if "YOUR-PASSWORD" in url.upper() or "YOUR_PASSWORD" in url.upper():
        raise ValueError("DATABASE_URL still has the [YOUR-PASSWORD] placeholder: put the database password there")
    scheme = scheme.lower()
    if scheme in ("postgres", "postgresql", "postgresql+psycopg", "postgresql+psycopg2"):
        scheme = "postgresql+psycopg"
    if scheme.startswith("sqlite"):
        return f"{scheme}://{rest}"
    creds, at, host_part = rest.rpartition("@")
    if at and ":" in creds:
        user, _, password = creds.partition(":")
        rest = f"{user}:{quote(unquote(password), safe='')}@{host_part}"
    # options that only ORMs such as Prisma understand would make the Postgres driver refuse the address
    base, _, query = rest.partition("?")
    if query:
        kept = [p for p in query.split("&") if p.split("=", 1)[0].lower() not in ORM_ONLY_OPTIONS]
        rest = base + (f"?{'&'.join(kept)}" if kept else "")
    return f"{scheme}://{rest}"


ORM_ONLY_OPTIONS = {"pgbouncer", "connection_limit", "pool_timeout", "schema", "statement_cache_size",
                    "socket_timeout"}


def connection_hint(url: str) -> str:
    """A pointer for the log when the database can't be reached."""
    host = urlsplit(url).hostname or ""
    if host.startswith("db.") and host.endswith(".supabase.co"):
        return (" This is Supabase's direct connection, which Render and many other hosts can't reach (it only has "
                "an IPv6 address). Use the Session pooler connection string from the Connect button instead.")
    if "pooler.supabase.com" in host:
        return (" Check the database password in DATABASE_URL, and that the Supabase project isn't paused "
                "(Supabase dashboard: Resume project).")
    return ""


def describe_database(url: str) -> dict:
    """Where the accounts are kept, for the admin page (never the password)."""
    if url.startswith("sqlite"):
        return {"kind": "sqlite", "where": url.split(":///", 1)[-1]}
    host = urlsplit(url).hostname or "?"
    return {"kind": "supabase" if "supabase" in host else "postgres", "where": host}


def _engine(url: str) -> sa.Engine:
    if url.startswith("sqlite"):
        engine = sa.create_engine(url, connect_args={"check_same_thread": False, "timeout": 15})

        @sa.event.listens_for(engine, "connect")
        def _pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine
    parts = urlsplit(url)
    connect_args = {
        "connect_timeout": 10,
        # Supabase's transaction pooler (port 6543) can't keep prepared statements; this works with every pooler
        "prepare_threshold": None,
    }
    if parts.hostname not in ("localhost", "127.0.0.1", "::1") and "sslmode" not in parse_qs(parts.query):
        connect_args["sslmode"] = "require"
    engine = sa.create_engine(url, connect_args=connect_args, pool_size=3, max_overflow=2, pool_pre_ping=True,
                              pool_recycle=300, pool_timeout=20)
    return engine.execution_options(schema_translate_map={None: SCHEMA})


def _lock_down_postgres(conn) -> None:
    for table in metadata.sorted_tables:
        conn.execute(sa.text(f'ALTER TABLE "{SCHEMA}"."{table.name}" ENABLE ROW LEVEL SECURITY'))
    roles = conn.execute(sa.text("SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')"))
    for (role,) in roles.all():
        conn.execute(sa.text(f'REVOKE ALL ON SCHEMA "{SCHEMA}" FROM "{role}"'))
        conn.execute(sa.text(f'REVOKE ALL ON ALL TABLES IN SCHEMA "{SCHEMA}" FROM "{role}"'))


# --- the store -----------------------------------------------------------------------------------------------

class AccountStore:
    def __init__(self, url: str) -> None:
        self.url = url
        self.kind = "sqlite" if url.startswith("sqlite") else "postgres"
        self.engine = _engine(url)
        self._ready = False
        self._failed_at = 0.0
        self._lock = threading.Lock()

    def ensure_ready(self) -> None:
        """Creates the tables the first time. Raises AccountsUnavailable when the database can't be reached."""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            if self._failed_at and time.monotonic() - self._failed_at < 15:
                raise AccountsUnavailable("The accounts database can't be reached (retrying shortly).")
            try:
                with self.engine.begin() as conn:
                    if self.kind == "postgres":
                        conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
                    metadata.create_all(conn)
                    if self.kind == "postgres":
                        try:
                            with conn.begin_nested():
                                _lock_down_postgres(conn)
                        except SQLAlchemyError as exc:
                            log.warning("Could not switch on row-level security for the accounts tables: %s",
                                        _describe(exc))
            except SQLAlchemyError as exc:
                self._failed_at = time.monotonic()
                log.error("The accounts database can't be reached: %s.%s", _describe(exc), connection_hint(self.url))
                raise AccountsUnavailable("The accounts database can't be reached.") from exc
            self._failed_at = 0.0
            self._ready = True

    @contextmanager
    def _tx(self):
        self.ensure_ready()
        try:
            with self.engine.begin() as conn:
                yield conn
        except IntegrityError:
            raise
        except SQLAlchemyError as exc:
            log.error("Accounts database error: %s", _describe(exc))
            raise AccountsUnavailable("The accounts database can't be reached.") from exc

    def dispose(self) -> None:
        self.engine.dispose()

    # users -------------------------------------------------------------------------------------------------

    def get_user(self, user_id: int) -> User | None:
        with self._tx() as conn:
            row = conn.execute(sa.select(users).where(users.c.id == user_id)).first()
        return _user(row) if row else None

    def find_user(self, username: str) -> User | None:
        with self._tx() as conn:
            row = conn.execute(sa.select(users).where(users.c.username == username.strip().lower())).first()
        return _user(row) if row else None

    def list_users(self) -> list[User]:
        with self._tx() as conn:
            rows = conn.execute(sa.select(users).order_by(users.c.username)).all()
        return [_user(row) for row in rows]

    def has_active_admin(self) -> bool:
        with self._tx() as conn:
            row = conn.execute(sa.select(users.c.id).where(users.c.role == "admin",
                                                           users.c.is_active == sa.true()).limit(1)).first()
        return row is not None

    def create_user(self, *, username: str, password_hash: str, role: str, permissions, display_name: str = "",
                    must_change_password: bool = True) -> User:
        t = now()
        values = {
            "username": username, "display_name": display_name, "password_hash": password_hash, "role": role,
            "permissions": ",".join(sorted(permissions)), "is_active": True,
            "must_change_password": must_change_password, "created_at": t, "updated_at": t,
            "password_changed_at": t,
        }
        try:
            with self._tx() as conn:
                if conn.execute(sa.select(users.c.id).where(users.c.username == username)).first():
                    raise UsernameTaken(username)
                new_id = conn.execute(users.insert().values(**values)).inserted_primary_key[0]
        except IntegrityError as exc:
            raise UsernameTaken(username) from exc
        return self.get_user(new_id)

    def update_user(self, user_id: int, **changes) -> User | None:
        """Changes display_name, role, permissions or is_active. Raises LastAdmin when no active admin would be
        left. Deactivating an account also signs it out everywhere."""
        values = {key: value for key, value in changes.items() if value is not None}
        if "permissions" in values:
            values["permissions"] = ",".join(sorted(values["permissions"]))
        values["updated_at"] = now()
        with self._tx() as conn:
            current = conn.execute(sa.select(users).where(users.c.id == user_id).with_for_update()).first()
            if current is None:
                return None
            stays_admin = values.get("role", current.role) == "admin" and values.get("is_active", current.is_active)
            if current.role == "admin" and current.is_active and not stays_admin:
                others = conn.execute(
                    sa.select(users.c.id).where(users.c.role == "admin", users.c.is_active == sa.true(),
                                                users.c.id != user_id).with_for_update()).all()
                if not others:
                    raise LastAdmin()
            conn.execute(users.update().where(users.c.id == user_id).values(**values))
            if values.get("is_active") is False:
                conn.execute(sessions.delete().where(sessions.c.user_id == user_id))
        return self.get_user(user_id)

    def set_password(self, user_id: int, password_hash: str, must_change: bool,
                     keep_session_id: int | None = None) -> None:
        """New password; every other session of the account is signed out."""
        t = now()
        with self._tx() as conn:
            conn.execute(users.update().where(users.c.id == user_id).values(
                password_hash=password_hash, must_change_password=must_change, password_changed_at=t, updated_at=t))
            stmt = sessions.delete().where(sessions.c.user_id == user_id)
            if keep_session_id is not None:
                stmt = stmt.where(sessions.c.id != keep_session_id)
            conn.execute(stmt)

    def record_sign_in(self, user_id: int, new_hash: str | None = None) -> None:
        values = {"last_login_at": now()}
        if new_hash:
            values["password_hash"] = new_hash
        with self._tx() as conn:
            conn.execute(users.update().where(users.c.id == user_id).values(**values))

    # sessions ----------------------------------------------------------------------------------------------

    def create_session(self, *, user_id: int, token_hash: str, csrf_token: str, max_age: int,
                       ip: str | None, user_agent: str | None) -> Session:
        t = now()
        with self._tx() as conn:
            new_id = conn.execute(sessions.insert().values(
                token_hash=token_hash, user_id=user_id, csrf_token=csrf_token, created_at=t, last_seen_at=t,
                expires_at=t + max_age, ip=(ip or "")[:64], user_agent=(user_agent or "")[:200],
            )).inserted_primary_key[0]
        return Session(id=new_id, user_id=user_id, csrf_token=csrf_token, created_at=t, last_seen_at=t,
                       expires_at=t + max_age)

    def session_user(self, token_hash: str) -> tuple[Session, User] | None:
        stmt = sa.select(
            sessions.c.id.label("session_id"), sessions.c.csrf_token, sessions.c.created_at.label("session_created"),
            sessions.c.last_seen_at, sessions.c.expires_at, users,
        ).join(users, users.c.id == sessions.c.user_id).where(sessions.c.token_hash == token_hash)
        with self._tx() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return None
        session = Session(id=row.session_id, user_id=row.id, csrf_token=row.csrf_token,
                          created_at=row.session_created, last_seen_at=row.last_seen_at, expires_at=row.expires_at)
        return session, _user(row)

    def touch_session(self, session_id: int) -> None:
        with self._tx() as conn:
            conn.execute(sessions.update().where(sessions.c.id == session_id).values(last_seen_at=now()))

    def delete_session(self, session_id: int) -> None:
        with self._tx() as conn:
            conn.execute(sessions.delete().where(sessions.c.id == session_id))

    def sign_out_everywhere(self, user_id: int, keep_session_id: int | None = None) -> int:
        stmt = sessions.delete().where(sessions.c.user_id == user_id)
        if keep_session_id is not None:
            stmt = stmt.where(sessions.c.id != keep_session_id)
        with self._tx() as conn:
            return conn.execute(stmt).rowcount

    def session_counts(self) -> dict[int, int]:
        """Signed-in sessions per account (not yet expired)."""
        with self._tx() as conn:
            rows = conn.execute(sa.select(sessions.c.user_id, sa.func.count()).where(sessions.c.expires_at > now())
                                .group_by(sessions.c.user_id)).all()
        return {user_id: count for user_id, count in rows}

    def purge(self, idle_seconds: int) -> int:
        """Removes expired sessions and old activity entries. Returns the number of sessions removed."""
        t = now()
        with self._tx() as conn:
            removed = conn.execute(sessions.delete().where(
                sa.or_(sessions.c.expires_at <= t, sessions.c.last_seen_at < t - idle_seconds))).rowcount
            cutoff = conn.execute(sa.select(audit_log.c.id).order_by(audit_log.c.id.desc())
                                  .offset(AUDIT_KEEP).limit(1)).scalar()
            if cutoff:
                conn.execute(audit_log.delete().where(audit_log.c.id <= cutoff))
        return removed

    # activity log ------------------------------------------------------------------------------------------

    def log_event(self, action: str, *, actor: str | None = None, target: str | None = None,
                  detail: str | None = None, ip: str | None = None) -> None:
        try:
            with self._tx() as conn:
                conn.execute(audit_log.insert().values(at=now(), action=action, actor=(actor or None),
                                                       target=(target or None), detail=(detail or "")[:300] or None,
                                                       ip=(ip or "")[:64] or None))
        except AccountsUnavailable:
            log.warning("Activity not recorded: %s %s", action, target or "")

    def recent_events(self, limit: int = 100) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(sa.select(audit_log).order_by(audit_log.c.id.desc()).limit(limit)).all()
        return [{"at": r.at, "actor": r.actor, "action": r.action, "target": r.target, "detail": r.detail,
                 "ip": r.ip} for r in rows]


# --- one store per database ----------------------------------------------------------------------------------

_stores: dict[str, AccountStore] = {}
_stores_lock = threading.Lock()


def database_url(settings: Settings) -> str:
    if settings.database_url:
        return normalize_database_url(settings.database_url)
    path = settings.data_dir / "accounts.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def get_store(settings: Settings) -> AccountStore:
    """The accounts store for these settings, with its tables in place. Raises AccountsUnavailable."""
    try:
        url = database_url(settings)
    except (ValueError, OSError) as exc:
        raise AccountsUnavailable(f"The accounts database setting is not usable: {exc}") from exc
    with _stores_lock:
        store = _stores.get(url)
        if store is None:
            try:
                store = _stores[url] = AccountStore(url)
            except Exception as exc:  # e.g. a driver that isn't installed or a malformed address
                raise AccountsUnavailable(f"The accounts database setting is not usable: {_describe(exc)}") from exc
    store.ensure_ready()
    return store


def reset_stores() -> None:
    """Closes every database connection (tests, shutdown)."""
    with _stores_lock:
        for store in _stores.values():
            store.dispose()
        _stores.clear()
