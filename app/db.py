"""Database connection and schema.

SQLite keeps the prototype self-contained. Production would use PostgreSQL with
row-level security; here the same guarantee is enforced by the single scoped
repository in repository.py, which every endpoint must go through.
"""

import sqlite3
from dataclasses import dataclass

from app import config


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    role: str
    name: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('matchmaker', 'lead', 'admin')),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    matchmaker_id TEXT NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    gender TEXT NOT NULL,
    age INTEGER NOT NULL,
    city TEXT NOT NULL,
    notes_enc TEXT
);
CREATE INDEX IF NOT EXISTS ix_clients_scope ON clients (tenant_id, matchmaker_id);

CREATE TABLE IF NOT EXISTS preference_cards (
    client_id TEXT PRIMARY KEY REFERENCES clients(id),
    tenant_id TEXT NOT NULL,
    hard_json TEXT NOT NULL,
    soft_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    gender TEXT NOT NULL,
    age INTEGER NOT NULL,
    city TEXT NOT NULL,
    religion TEXT NOT NULL,
    diet TEXT NOT NULL,
    smoking TEXT NOT NULL,
    drinking TEXT NOT NULL,
    wants_children TEXT NOT NULL,
    open_to_relocate INTEGER NOT NULL,
    height_cm INTEGER NOT NULL,
    education TEXT NOT NULL,
    profession TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_candidates_filter ON candidates (tenant_id, gender, age);

CREATE TABLE IF NOT EXISTS shares (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    client_id TEXT NOT NULL REFERENCES clients(id),
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    matchmaker_id TEXT NOT NULL REFERENCES users(id),
    outcome TEXT NOT NULL DEFAULT 'pending' CHECK (outcome IN ('pending', 'accepted', 'rejected')),
    check_status TEXT NOT NULL,
    fit_score INTEGER NOT NULL,
    override_reason TEXT,
    is_historical INTEGER NOT NULL DEFAULT 0,
    shared_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_shares_client ON shares (tenant_id, client_id);
CREATE INDEX IF NOT EXISTS ix_shares_matchmaker ON shares (tenant_id, matchmaker_id);

CREATE TABLE IF NOT EXISTS rejections (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    share_id TEXT NOT NULL REFERENCES shares(id),
    raw_text_enc TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'done', 'failed')),
    reason_code TEXT,
    reason_detail TEXT,
    confidence REAL,
    classifier TEXT,
    suggestion_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rejections_share ON rejections (tenant_id, share_id);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT,
    detail TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit_log (tenant_id, at);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    # WAL lets readers run while a write is in progress. The setting is stored in the
    # database file, so it is applied once here rather than on every connection.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()


def get_conn():
    """FastAPI dependency: one connection per request, always closed."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
