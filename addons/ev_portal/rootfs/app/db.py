"""
SQLite-backed persistent session store for EV Portal.

Records are keyed by a deterministic idempotency key:
    idempotency_key = "ev:<charger_id>:<booking_id>"

All writes go to /data/ev_portal.db (overridable via EV_DB_PATH env var).
/data is guaranteed to exist in the HA Supervisor add-on environment; for
local development set EV_DB_PATH to a writable path.

sqlite3 is synchronous; all public coroutines delegate blocking calls to a
thread via asyncio.to_thread() to avoid blocking the event loop.
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional


DB_PATH: str = os.environ.get("EV_DB_PATH", "/data/ev_portal.db")

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    # Ensure parent directory exists (dev convenience; /data always exists in HA).
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    idempotency_key           TEXT PRIMARY KEY,
    charger_id                TEXT NOT NULL,
    booking_id                TEXT NOT NULL,
    session_id                TEXT NOT NULL,
    state                     TEXT NOT NULL DEFAULT 'CREATED',
    authorized                INTEGER NOT NULL DEFAULT 0,
    authorized_amount_cents   INTEGER NOT NULL DEFAULT 0,
    captured_amount_cents     INTEGER,
    square_environment        TEXT NOT NULL DEFAULT 'sandbox',

    -- Card display metadata (non-sensitive references only; no PAN/CVV/token)
    square_customer_id        TEXT,
    square_card_id            TEXT,
    card_brand                TEXT,
    card_last4                TEXT,
    card_exp_month            INTEGER,
    card_exp_year             INTEGER,

    -- Payment references
    square_payment_id         TEXT,
    square_capture_payment_id TEXT,
    square_order_id           TEXT,
    square_payment_link_url   TEXT,

    -- Audit
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    last_error                TEXT
)
"""

_CREATE_INDEX_SESSION_ID = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_session_id
ON sessions (session_id)
"""

_CREATE_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    reason          TEXT,
    before_json     TEXT,
    after_json      TEXT,
    result_json     TEXT
)
"""

_AUDIT_LOG_IDX = """
CREATE INDEX IF NOT EXISTS idx_audit_log_key
ON audit_log (idempotency_key)
"""


def _migrate_sessions(conn: sqlite3.Connection) -> None:
    """Add new columns to existing sessions table without breaking old rows."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    new_cols = [
        ("note",       "TEXT"),
        ("is_deleted", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, defn in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {defn}")
            log.info("db migration: added column sessions.%s", col)


def _init_db_sync() -> None:
    with _connect() as conn:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX_SESSION_ID)
        conn.execute(_CREATE_AUDIT_LOG)
        conn.execute(_AUDIT_LOG_IDX)
        _migrate_sessions(conn)
        conn.commit()


async def init_db() -> None:
    """Create the DB and tables if they do not already exist. Call once at startup."""
    await asyncio.to_thread(_init_db_sync)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _get_session_sync(idempotency_key: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    return dict(row) if row else None


async def get_session(idempotency_key: str) -> Optional[dict]:
    """Fetch a session by idempotency key, or None if not found."""
    row = await asyncio.to_thread(_get_session_sync, idempotency_key)
    log.info("get_session key=%r  found=%s  state=%s",
             idempotency_key, row is not None, row["state"] if row else None)
    return row


def _get_session_by_uid_sync(session_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


async def get_session_by_uid(session_id: str) -> Optional[dict]:
    """Fetch a session by the one-time session UUID, or None if not found."""
    row = await asyncio.to_thread(_get_session_by_uid_sync, session_id)
    log.info("get_session_by_uid session_id=%r  found=%s  state=%s",
             session_id, row is not None, row["state"] if row else None)
    return row


def _get_session_by_booking_id_sync(booking_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE booking_id = ? ORDER BY updated_at DESC LIMIT 1",
            (booking_id,),
        ).fetchone()
    return dict(row) if row else None


async def get_session_by_booking_id(booking_id: str) -> Optional[dict]:
    """Fetch the most recent session for a booking_id, or None if not found."""
    row = await asyncio.to_thread(_get_session_by_booking_id_sync, booking_id)
    log.info("get_session_by_booking_id booking_id=%r  found=%s  state=%s",
             booking_id, row is not None, row["state"] if row else None)
    return row


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _upsert_session_sync(session: dict) -> None:
    """Insert or update a session record. ``idempotency_key`` must be present."""
    now = _now()
    row = dict(session)
    row.setdefault("created_at", now)
    row["updated_at"] = now

    cols = list(row.keys())
    placeholders = ", ".join("?" * len(cols))
    update_pairs = ", ".join(
        # Never downgrade a terminal state
        "state = CASE WHEN sessions.state IN ('AUTHORIZED','CAPTURED') THEN sessions.state ELSE excluded.state END"
        if c == "state" else
        f"{c} = excluded.{c}"
        for c in cols
        if c not in ("idempotency_key", "created_at")
    )
    sql = (
        f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders})"
        f" ON CONFLICT(idempotency_key) DO UPDATE SET {update_pairs}"
    )
    with _connect() as conn:
        conn.execute(sql, list(row.values()))
        conn.commit()


async def upsert_session(session: dict) -> None:
    """Insert or update a session record atomically."""
    await asyncio.to_thread(_upsert_session_sync, session)


def _mark_authorized_sync(
    idempotency_key: str,
    square_payment_id: str,
    authorized_amount_cents: int,
    **card_meta: object,
) -> None:
    fields: dict = {
        "state":                    "AUTHORIZED",
        "authorized":               1,
        "authorized_amount_cents":  authorized_amount_cents,
        "square_payment_id":        square_payment_id,
        "last_error":               None,
        "updated_at":               _now(),
        **card_meta,
    }
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = [*fields.values(), idempotency_key]
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE sessions SET {set_clause} WHERE idempotency_key = ?",
            values,
        )
        conn.commit()
    log.info("mark_authorized key=%r  rows_updated=%d", idempotency_key, cur.rowcount)


async def mark_authorized(
    idempotency_key: str,
    square_payment_id: str,
    authorized_amount_cents: int,
    **card_meta: object,
) -> None:
    """Set state=AUTHORIZED and store the payment ID plus card display metadata."""
    await asyncio.to_thread(
        _mark_authorized_sync,
        idempotency_key,
        square_payment_id,
        authorized_amount_cents,
        **card_meta,
    )


def _mark_failed_sync(idempotency_key: str, error: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET state = 'FAILED', last_error = ?, updated_at = ?"
            " WHERE idempotency_key = ?",
            (error, _now(), idempotency_key),
        )
        conn.commit()


async def mark_failed(idempotency_key: str, error: str) -> None:
    """Set state=FAILED and record the error message. Not terminal for retry."""
    await asyncio.to_thread(_mark_failed_sync, idempotency_key, error)


def _mark_captured_sync(
    idempotency_key: str,
    square_capture_payment_id: str,
    captured_amount_cents: int,
) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE sessions
               SET state = 'CAPTURED',
                   square_capture_payment_id = ?,
                   captured_amount_cents = ?,
                   updated_at = ?
               WHERE idempotency_key = ?""",
            (square_capture_payment_id, captured_amount_cents, _now(), idempotency_key),
        )
        conn.commit()


async def mark_captured(
    idempotency_key: str,
    square_capture_payment_id: str,
    captured_amount_cents: int,
) -> None:
    """Set state=CAPTURED after a successful Square payment capture."""
    await asyncio.to_thread(
        _mark_captured_sync,
        idempotency_key,
        square_capture_payment_id,
        captured_amount_cents,
    )


def _mark_voided_sync(idempotency_key: str, square_payment_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE sessions
               SET state = 'VOIDED',
                   captured_amount_cents = 0,
                   square_capture_payment_id = ?,
                   updated_at = ?
               WHERE idempotency_key = ?""",
            (square_payment_id, _now(), idempotency_key),
        )
        conn.commit()


async def mark_voided(idempotency_key: str, square_payment_id: str) -> None:
    """Set state=VOIDED when the pre-auth hold is cancelled with no charge."""
    await asyncio.to_thread(_mark_voided_sync, idempotency_key, square_payment_id)


# ---------------------------------------------------------------------------
# Admin – DB queries
# ---------------------------------------------------------------------------

def _list_sessions_sync(
    limit: int,
    offset: int,
    state_filter: Optional[str],
    include_deleted: bool,
) -> list:
    clauses = []
    params = []
    if state_filter:
        clauses.append("state = ?")
        params.append(state_filter.upper())
    if not include_deleted:
        clauses.append("(is_deleted IS NULL OR is_deleted = 0)")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM sessions {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


async def list_sessions(
    limit: int = 50,
    offset: int = 0,
    state_filter: Optional[str] = None,
    include_deleted: bool = False,
) -> list:
    """Return sessions ordered by most-recently-updated, with optional filtering."""
    return await asyncio.to_thread(
        _list_sessions_sync, limit, offset, state_filter, include_deleted
    )


# ---------------------------------------------------------------------------
# Admin – DB mutations
# ---------------------------------------------------------------------------

def _add_note_sync(idempotency_key: str, note: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET note = ?, updated_at = ? WHERE idempotency_key = ?",
            (note, _now(), idempotency_key),
        )
        conn.commit()


async def add_note(idempotency_key: str, note: str) -> None:
    """Persist an operator note on a session record."""
    await asyncio.to_thread(_add_note_sync, idempotency_key, note)


def _soft_delete_sync(idempotency_key: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET is_deleted = 1, updated_at = ? WHERE idempotency_key = ?",
            (_now(), idempotency_key),
        )
        conn.commit()


async def soft_delete(idempotency_key: str) -> None:
    """Mark a session as deleted (is_deleted=1). Row is never physically removed."""
    await asyncio.to_thread(_soft_delete_sync, idempotency_key)


def _mark_canceled_sync(idempotency_key: str, square_payment_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE sessions
               SET state = 'CANCELED',
                   square_capture_payment_id = ?,
                   captured_amount_cents = 0,
                   updated_at = ?
               WHERE idempotency_key = ?""",
            (square_payment_id, _now(), idempotency_key),
        )
        conn.commit()


async def mark_canceled(idempotency_key: str, square_payment_id: str) -> None:
    """Set state=CANCELED after an admin-initiated void of a pre-auth."""
    await asyncio.to_thread(_mark_canceled_sync, idempotency_key, square_payment_id)


def _mark_refunded_sync(
    idempotency_key: str, refund_id: str, amount_cents: int
) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE sessions
               SET state = 'REFUNDED',
                   square_capture_payment_id = ?,
                   captured_amount_cents = ?,
                   updated_at = ?
               WHERE idempotency_key = ?""",
            (refund_id, amount_cents, _now(), idempotency_key),
        )
        conn.commit()


async def mark_refunded(
    idempotency_key: str, refund_id: str, amount_cents: int
) -> None:
    """Set state=REFUNDED after a successful Square refund."""
    await asyncio.to_thread(_mark_refunded_sync, idempotency_key, refund_id, amount_cents)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _write_audit_log_sync(
    actor: str,
    action: str,
    idempotency_key: str,
    reason: Optional[str],
    before_json: Optional[str],
    after_json: Optional[str],
    result_json: Optional[str],
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO audit_log
               (ts, actor, action, idempotency_key, reason, before_json, after_json, result_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now(), actor, action, idempotency_key,
             reason, before_json, after_json, result_json),
        )
        conn.commit()


async def write_audit_log(
    actor: str,
    action: str,
    idempotency_key: str,
    reason: Optional[str] = None,
    before_json: Optional[str] = None,
    after_json: Optional[str] = None,
    result_json: Optional[str] = None,
) -> None:
    """Append an entry to the audit_log table. Never logs secrets."""
    await asyncio.to_thread(
        _write_audit_log_sync,
        actor, action, idempotency_key,
        reason, before_json, after_json, result_json,
    )
