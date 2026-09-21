"""SQLite storage for bots, activity, articles, rate limits, and proposals.

Note: on Railway/Render's ephemeral disk this file is wiped on redeploy.
That's a known limitation, not a bug here — move DB_PATH to a mounted
volume or hosted Postgres once the system is running steadily.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    niche TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'stopped',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    slug TEXT NOT NULL,
    path TEXT NOT NULL,
    url TEXT,
    published_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS affiliate_programs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER,
    name TEXT NOT NULL,
    signup_url TEXT NOT NULL,
    affiliate_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    checklist TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS code_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    diff TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    price REAL,
    usd_value REAL,
    order_id TEXT,
    status TEXT NOT NULL,
    reasoning TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trading_activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS trading_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    daily_date TEXT,
    daily_start_value_usd REAL,
    tripped INTEGER NOT NULL DEFAULT 0,
    reason TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cutoff(window_minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()


@contextmanager
def get_conn():
    db_dir = os.path.dirname(settings.DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # The dashboard and worker are two separate long-lived processes
    # hitting this same file concurrently. WAL lets the dashboard read
    # while the worker writes instead of blocking on the default
    # rollback journal, and busy_timeout keeps a write from failing
    # immediately if it loses a brief race instead of raising
    # "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Migration: affiliate_url was added after affiliate_programs first
        # shipped. executescript's CREATE TABLE IF NOT EXISTS won't add a
        # column to a table that already exists, so add it explicitly for
        # any database created by an earlier version.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(affiliate_programs)")}
        if "affiliate_url" not in existing_cols:
            conn.execute("ALTER TABLE affiliate_programs ADD COLUMN affiliate_url TEXT")


# --- bots ---

def add_bot(name: str, niche: str, description: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bots (name, niche, description, status, created_at) "
            "VALUES (?, ?, ?, 'stopped', ?)",
            (name, niche, description, _now()),
        )
        return cur.lastrowid


def get_bots(status: str = None) -> list:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM bots WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM bots ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_bot(bot_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    return dict(row) if row else None


def set_bot_status(bot_id: int, status: str):
    with get_conn() as conn:
        conn.execute("UPDATE bots SET status=? WHERE id=?", (status, bot_id))


# --- activity log ---

def log_activity(bot_id, kind: str, detail: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO activity_log (bot_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
            (bot_id, _now(), kind, detail),
        )


def get_recent_activity(bot_id: int = None, limit: int = 20) -> list:
    with get_conn() as conn:
        if bot_id is not None:
            rows = conn.execute(
                "SELECT * FROM activity_log WHERE bot_id=? ORDER BY id DESC LIMIT ?",
                (bot_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# --- rate limiting / backoff ---

def record_publish(bot_id: int):
    with get_conn() as conn:
        conn.execute("INSERT INTO publishes (bot_id, ts) VALUES (?, ?)", (bot_id, _now()))


def count_recent_publishes(bot_id: int, window_minutes: int = 60) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM publishes WHERE bot_id=? AND ts >= ?",
            (bot_id, _cutoff(window_minutes)),
        ).fetchone()
    return row["c"]


def record_failure(bot_id: int):
    with get_conn() as conn:
        conn.execute("INSERT INTO failures (bot_id, ts) VALUES (?, ?)", (bot_id, _now()))


def count_recent_failures(bot_id: int, window_minutes: int = 30) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM failures WHERE bot_id=? AND ts >= ?",
            (bot_id, _cutoff(window_minutes)),
        ).fetchone()
    return row["c"]


# --- heartbeat ---

def record_heartbeat():
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO heartbeat (id, ts) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts",
            (_now(),),
        )


def get_heartbeat_age_seconds():
    with get_conn() as conn:
        row = conn.execute("SELECT ts FROM heartbeat WHERE id=1").fetchone()
    if not row:
        return None
    ts = datetime.fromisoformat(row["ts"])
    return (datetime.now(timezone.utc) - ts).total_seconds()


def get_setting_age_seconds(key: str):
    """Generic version of get_heartbeat_age_seconds for any ISO-timestamp
    value stored via set_setting — used by the realtime trading stream's
    own heartbeat, which is a separate process from the main worker."""
    value = get_setting(key)
    if not value:
        return None
    ts = datetime.fromisoformat(value)
    return (datetime.now(timezone.utc) - ts).total_seconds()


# --- key/value settings (e.g. auto_mode) ---

def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings_kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings_kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# --- articles ---

def add_article(bot_id: int, title: str, slug: str, path: str, url: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO articles (bot_id, title, slug, path, url, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bot_id, title, slug, path, url, _now()),
        )


def list_articles(bot_id: int = None) -> list:
    with get_conn() as conn:
        if bot_id is not None:
            rows = conn.execute(
                "SELECT * FROM articles WHERE bot_id=? ORDER BY id DESC", (bot_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM articles ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


# --- affiliate programs ---

def add_affiliate_program(bot_id, name: str, signup_url: str, checklist: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO affiliate_programs (bot_id, name, signup_url, status, checklist, created_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (bot_id, name, signup_url, checklist, _now()),
        )
        return cur.lastrowid


def list_affiliate_programs(status: str = None) -> list:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM affiliate_programs WHERE status=? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM affiliate_programs ORDER BY id DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def update_affiliate_status(program_id: int, status: str):
    with get_conn() as conn:
        conn.execute("UPDATE affiliate_programs SET status=? WHERE id=?", (status, program_id))


def set_affiliate_link(program_id: int, affiliate_url: str):
    """Records the real tracking link a human got after a program approved
    their signup, and flips the program to active — this is what actually
    lets generated articles include a working affiliate link."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE affiliate_programs SET affiliate_url=?, status='active' WHERE id=?",
            (affiliate_url, program_id),
        )


def list_active_affiliate_links(bot_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, affiliate_url FROM affiliate_programs "
            "WHERE bot_id=? AND status='active' AND affiliate_url IS NOT NULL",
            (bot_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- code proposals (approval is symbolic — see Known Limitations) ---

def add_code_proposal(description: str, diff: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO code_proposals (description, diff, status, created_at) "
            "VALUES (?, ?, 'pending', ?)",
            (description, diff, _now()),
        )
        return cur.lastrowid


def list_code_proposals(status: str = None) -> list:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM code_proposals WHERE status=? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM code_proposals ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def update_code_proposal_status(proposal_id: int, status: str):
    with get_conn() as conn:
        conn.execute("UPDATE code_proposals SET status=? WHERE id=?", (status, proposal_id))


# --- trading ---

def record_trade(pair, side, amount, price, usd_value, order_id, status, reasoning):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO trades (pair, side, amount, price, usd_value, order_id, status, "
            "reasoning, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pair, side, amount, price, usd_value, order_id, status, reasoning, _now()),
        )


def list_trades(limit: int = 50) -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def log_trading_activity(kind: str, detail: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO trading_activity_log (ts, kind, detail) VALUES (?, ?, ?)",
            (_now(), kind, detail),
        )


def get_recent_trading_activity(limit: int = 30) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trading_activity_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_trading_state():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM trading_state WHERE id=1").fetchone()
    return dict(row) if row else None


def reset_trading_day(daily_date: str, start_value_usd: float):
    """Called once per UTC day: records the portfolio's starting value so
    the circuit breaker has a baseline to measure loss against, and clears
    any previous trip."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO trading_state (id, daily_date, daily_start_value_usd, tripped, reason) "
            "VALUES (1, ?, ?, 0, NULL) "
            "ON CONFLICT(id) DO UPDATE SET daily_date=excluded.daily_date, "
            "daily_start_value_usd=excluded.daily_start_value_usd, tripped=0, reason=NULL",
            (daily_date, start_value_usd),
        )


def trip_circuit_breaker(reason: str):
    with get_conn() as conn:
        conn.execute("UPDATE trading_state SET tripped=1, reason=? WHERE id=1", (reason,))


def clear_circuit_breaker():
    """Manual override from the dashboard — does not touch daily_start_value_usd,
    so the loss is still measured against the same baseline for the rest of
    the day."""
    with get_conn() as conn:
        conn.execute("UPDATE trading_state SET tripped=0, reason=NULL WHERE id=1")
