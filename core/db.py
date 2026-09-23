"""
One database module. Connection handling, schema, and every query.

The old build split this across database.py, db_safe.py and fastdb.py with
overlapping responsibilities and three tables nothing ever read. This is the
whole data layer.
"""
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import DATABASE_PATH

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def db_path() -> Path:
    p = Path(DATABASE_PATH)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def connect(timeout: float = 60.0):
    conn = sqlite3.connect(str(db_path()), timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        # WAL lets the dashboard read while the worker writes.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def retry(fn, attempts: int = 6, delay: float = 0.15):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            last = e
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(delay * (i + 1))
                continue
            raise
    raise last


def _now():
    return datetime.utcnow().isoformat()


# ----------------------------------------------------------------- schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'stopped',
    objective TEXT NOT NULL,
    niche TEXT DEFAULT '',
    genome TEXT DEFAULT '',
    generation INTEGER DEFAULT 1,
    parent_id INTEGER,
    revenue REAL DEFAULT 0.0,
    live_url TEXT DEFAULT '',
    created_at TEXT,
    last_active TEXT
);

CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER,
    title TEXT,
    path TEXT,
    page TEXT DEFAULT '',
    status TEXT DEFAULT 'needs_review',
    live_url TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    score INTEGER DEFAULT -1,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS earnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER,
    queue_id INTEGER,
    amount REAL,
    source TEXT,
    note TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER,
    action TEXT,
    detail TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER,
    text TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
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
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS trading_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS operator_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    source TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS operator_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal TEXT NOT NULL,
    status TEXT,
    answer TEXT,
    transcript TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS trading_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    daily_date TEXT,
    daily_start_value_usd REAL,
    tripped INTEGER NOT NULL DEFAULT 0,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS ix_bots_status   ON bots(status);
CREATE INDEX IF NOT EXISTS ix_queue_status  ON queue(status);
CREATE INDEX IF NOT EXISTS ix_queue_bot     ON queue(bot_id);
CREATE INDEX IF NOT EXISTS ix_actions_bot   ON actions(bot_id, action);
CREATE INDEX IF NOT EXISTS ix_actions_time  ON actions(created_at);
CREATE INDEX IF NOT EXISTS ix_earnings_bot  ON earnings(bot_id);
CREATE INDEX IF NOT EXISTS ix_orders_bot    ON orders(bot_id);
"""


def init():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------- bots

def create_bot(name, objective, niche="", genome="", generation=1, parent_id=None):
    def _go():
        conn = connect()
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO bots (name, objective, niche, genome, generation, "
                "parent_id, created_at, last_active, status) "
                "VALUES (?,?,?,?,?,?,?,?,'stopped')",
                (name, objective, niche, genome, generation, parent_id, _now(), _now()),
            )
            conn.commit()
            return c.lastrowid
        finally:
            conn.close()
    return retry(_go)


def get_bots(include_dead=False):
    def _go():
        conn = connect()
        try:
            c = conn.cursor()
            if include_dead:
                c.execute("SELECT * FROM bots ORDER BY id")
            else:
                c.execute("SELECT * FROM bots WHERE status != 'dead' ORDER BY id")
            return [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    return retry(_go)


def get_bot(bot_id):
    conn = connect()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM bots WHERE id = ?", (bot_id,))
        r = c.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def update_bot(bot_id, **fields):
    allowed = {"status", "objective", "niche", "genome", "live_url"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = connect()
    try:
        c = conn.cursor()
        c.execute(
            f"UPDATE bots SET {sets}, last_active = ? WHERE id = ?",
            list(fields.values()) + [_now(), bot_id],
        )
        conn.commit()
    finally:
        conn.close()


def kill_bot(bot_id):
    update_bot(bot_id, status="dead")


# ----------------------------------------------------------------- queue

def add_queue_item(bot_id, title, path, page=""):
    """`page` is the slug this item is about. Several items share one site
    folder, so without it a review would always read the newest page."""
    def _go():
        conn = connect()
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO queue (bot_id, title, path, page, status, created_at, updated_at) "
                "VALUES (?,?,?,?,'needs_review',?,?)",
                (bot_id, title, path, page, _now(), _now()),
            )
            conn.commit()
            return c.lastrowid
        finally:
            conn.close()
    return retry(_go)


def set_queue_status(item_id, status, live_url="", notes="", score=None):
    conn = connect()
    try:
        c = conn.cursor()
        if score is None:
            c.execute(
                "UPDATE queue SET status=?, live_url=COALESCE(NULLIF(?,''), live_url), "
                "notes=?, updated_at=? WHERE id=?",
                (status, live_url, notes[:500], _now(), item_id),
            )
        else:
            c.execute(
                "UPDATE queue SET status=?, live_url=COALESCE(NULLIF(?,''), live_url), "
                "notes=?, score=?, updated_at=? WHERE id=?",
                (status, live_url, notes[:500], int(score), _now(), item_id),
            )
        conn.commit()
    finally:
        conn.close()


def get_queue(status=None, limit=50):
    conn = connect()
    try:
        c = conn.cursor()
        if status:
            c.execute(
                "SELECT q.*, b.name AS bot_name FROM queue q JOIN bots b ON b.id=q.bot_id "
                "WHERE q.status = ? ORDER BY q.created_at ASC LIMIT ?",
                (status, limit),
            )
        else:
            c.execute(
                "SELECT q.*, b.name AS bot_name FROM queue q JOIN bots b ON b.id=q.bot_id "
                "ORDER BY q.updated_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def get_queue_item(item_id):
    conn = connect()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM queue WHERE id = ?", (item_id,))
        r = c.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def get_auto_decisions(limit=15):
    conn = connect()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT q.*, b.name AS bot_name FROM queue q JOIN bots b ON b.id=q.bot_id "
            "WHERE q.status IN ('auto_published','auto_rejected','auto_approved') "
            "ORDER BY q.updated_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def get_published():
    conn = connect()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT q.id, q.bot_id, q.title, q.live_url, q.status, b.name AS bot_name "
            "FROM queue q JOIN bots b ON b.id=q.bot_id "
            "WHERE q.status IN ('published','auto_published') ORDER BY q.updated_at DESC"
        )
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def auto_published_since(hours=24):
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = connect()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) AS n FROM queue WHERE status='auto_published' AND updated_at > ?",
            (cutoff,),
        )
        return int(c.fetchone()["n"] or 0)
    finally:
        conn.close()


def all_outcomes(since_days=None):
    """
    {bot_id: {published, rejected, pending, auto_published, auto_rejected}}

    Pass since_days to count only recent work. Lifetime totals let an old bot
    build a page count a newer, better bot can never catch, which quietly
    entrenches whoever happened to exist first.
    """
    out = {}
    conn = connect()
    try:
        c = conn.cursor()
        if since_days:
            cutoff = (datetime.utcnow() - timedelta(days=float(since_days))).isoformat()
            c.execute("SELECT bot_id, status, COUNT(*) AS n FROM queue "
                      "WHERE updated_at > ? GROUP BY bot_id, status", (cutoff,))
        else:
            c.execute("SELECT bot_id, status, COUNT(*) AS n FROM queue GROUP BY bot_id, status")
        for row in c.fetchall():
            rec = out.setdefault(row["bot_id"], {
                "published": 0, "rejected": 0, "pending": 0,
                "auto_published": 0, "auto_rejected": 0, "auto_approved": 0,
            })
            s, n = row["status"], row["n"]
            if s in rec:
                rec[s] += n
            elif s in ("needs_review", "deferred"):
                rec["pending"] += n
    finally:
        conn.close()
    return out


def outcomes_for(bot_id):
    return all_outcomes().get(bot_id, {
        "published": 0, "rejected": 0, "pending": 0,
        "auto_published": 0, "auto_rejected": 0, "auto_approved": 0,
    })


# ----------------------------------------------------------------- earnings

def add_earning(queue_id, amount, source="", note=""):
    """Revenue attaches to the PAGE that earned it, then credits that page's bot."""
    item = get_queue_item(queue_id)
    if not item:
        raise ValueError("Published page not found")
    bot_id = item["bot_id"]
    conn = connect()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO earnings (bot_id, queue_id, amount, source, note, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (bot_id, queue_id, amount, source, note or item["title"], _now()),
        )
        c.execute(
            "UPDATE bots SET revenue = revenue + ?, last_active = ? WHERE id = ?",
            (amount, _now(), bot_id),
        )
        conn.commit()
    finally:
        conn.close()
    return bot_id


def get_earnings(limit=20):
    conn = connect()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT e.*, b.name AS bot_name FROM earnings e JOIN bots b ON b.id=e.bot_id "
            "ORDER BY e.created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def earnings_by_bot():
    out = defaultdict(float)
    conn = connect()
    try:
        c = conn.cursor()
        c.execute("SELECT bot_id, SUM(amount) AS total FROM earnings GROUP BY bot_id")
        for r in c.fetchall():
            out[r["bot_id"]] = float(r["total"] or 0)
    finally:
        conn.close()
    return dict(out)


# ----------------------------------------------------------------- log, orders

def log(bot_id, action, detail=""):
    def _go():
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO actions (bot_id, action, detail, created_at) VALUES (?,?,?,?)",
                (bot_id, action, str(detail)[:600], _now()),
            )
            conn.commit()
        finally:
            conn.close()
    try:
        retry(_go)
    except Exception:
        pass


def recent_actions(limit=30):
    conn = connect()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT a.*, b.name AS bot_name FROM actions a LEFT JOIN bots b ON b.id=a.bot_id "
            "ORDER BY a.id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def rate_limited_bots(bot_ids, cap, hours=1):
    """One grouped query for the whole fleet."""
    if not bot_ids or cap <= 0:
        return set()
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = connect()
    try:
        c = conn.cursor()
        marks = ",".join("?" for _ in bot_ids)
        c.execute(
            f"SELECT bot_id, COUNT(*) AS n FROM actions WHERE action='wrote_article' "
            f"AND created_at > ? AND bot_id IN ({marks}) GROUP BY bot_id",
            [cutoff] + list(bot_ids),
        )
        return {r["bot_id"] for r in c.fetchall() if r["n"] >= cap}
    except Exception:
        return set()
    finally:
        conn.close()


def set_order(bot_id, text):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO orders (bot_id, text, created_at) VALUES (?,?,?)",
            (bot_id, text, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_order(bot_id):
    conn = connect()
    try:
        c = conn.cursor()
        c.execute("SELECT text FROM orders WHERE bot_id=? ORDER BY id DESC LIMIT 1", (bot_id,))
        r = c.fetchone()
        return r["text"] if r else ""
    finally:
        conn.close()


# ----------------------------------------------------------------- settings

def get_setting(key, default=None):
    conn = connect()
    try:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        r = c.fetchone()
        return json.loads(r["value"]) if r else default
    except Exception:
        return default
    finally:
        conn.close()


def set_setting(key, value):
    def _go():
        conn = connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                (key, json.dumps(value)),
            )
            conn.commit()
        finally:
            conn.close()
    return retry(_go)


def get_secret(name, default=""):
    """
    Settings set in the dashboard win over environment variables.

    Typing GitHub tokens into a host's env-var screen on a phone is the single
    hardest step of setup. This lets the app collect them itself.
    """
    val = get_setting(f"secret_{name}")
    if val:
        return str(val)
    import os
    return os.getenv(name, default).strip()


def set_secret(name, value):
    set_setting(f"secret_{name}", (value or "").strip())


def has_secrets(*names):
    return all(get_secret(n) for n in names)


def export_backup(dest=None):
    """
    Everything that matters, in one JSON file.

    If the host's disk goes, the bots, your verdicts, earnings history and
    every article go with it. Secrets are deliberately excluded.
    """
    from pathlib import Path as _P
    conn = connect()
    try:
        c = conn.cursor()
        data = {"exported": _now(), "bots": [], "queue": [], "earnings": [],
                "orders": [], "settings": {}, "pages": {}}
        for table in ("bots", "queue", "earnings", "orders"):
            c.execute(f"SELECT * FROM {table}")
            data[table] = [dict(r) for r in c.fetchall()]
        c.execute("SELECT key, value FROM settings")
        data["settings"] = {r["key"]: r["value"] for r in c.fetchall()
                            if not r["key"].startswith("secret_")}
    finally:
        conn.close()

    sites = PROJECT_ROOT / "sites"
    if sites.is_dir():
        for d in sites.iterdir():
            f = d / "pages.json"
            if f.is_file():
                try:
                    data["pages"][d.name] = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    pass

    dest = _P(dest) if dest else (db_path().parent / f"backup-{_now()[:10]}.json")
    dest.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return str(dest)


# ----------------------------------------------------------------- snapshot

def kpis():
    """Whole business snapshot in one connection."""
    conn = connect()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT "
            " (SELECT COUNT(*) FROM bots WHERE status != 'dead') AS live_bots,"
            " (SELECT COUNT(*) FROM queue WHERE status='needs_review') AS needs_review,"
            " (SELECT COUNT(*) FROM queue WHERE status IN ('published','auto_published')) AS published,"
            " (SELECT COUNT(*) FROM queue WHERE status IN ('rejected','auto_rejected')) AS rejected,"
            " (SELECT COUNT(*) FROM queue) AS articles,"
            " (SELECT COALESCE(SUM(amount),0) FROM earnings) AS revenue"
        )
        return dict(c.fetchone())
    except Exception:
        return {"live_bots": 0, "needs_review": 0, "published": 0,
                "rejected": 0, "articles": 0, "revenue": 0.0}
    finally:
        conn.close()


# ----------------------------------------------------------------- trading
# Separate subsystem from the content bots above — its own tables, its own
# money, its own safety gates. See core/trading/.

def record_trade(pair, side, amount, price, usd_value, order_id, status, reasoning):
    def _go():
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO trades (pair, side, amount, price, usd_value, order_id, "
                "status, reasoning, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (pair, side, amount, price, usd_value, order_id, status, reasoning, _now()),
            )
            conn.commit()
        finally:
            conn.close()
    return retry(_go)


def list_trades(limit=50):
    conn = connect()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def log_trading_activity(kind, detail=""):
    def _go():
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO trading_activity (kind, detail, created_at) VALUES (?,?,?)",
                (kind, detail, _now()),
            )
            conn.commit()
        finally:
            conn.close()
    return retry(_go)


def get_recent_trading_activity(limit=30):
    conn = connect()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM trading_activity ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def get_trading_state():
    conn = connect()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM trading_state WHERE id=1")
        r = c.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def reset_trading_day(daily_date, start_value_usd):
    """Called once per UTC day: records the portfolio's starting value so
    the circuit breaker has a baseline, and clears any previous trip."""
    def _go():
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO trading_state (id, daily_date, daily_start_value_usd, tripped, reason) "
                "VALUES (1,?,?,0,NULL) ON CONFLICT(id) DO UPDATE SET "
                "daily_date=excluded.daily_date, "
                "daily_start_value_usd=excluded.daily_start_value_usd, tripped=0, reason=NULL",
                (daily_date, start_value_usd),
            )
            conn.commit()
        finally:
            conn.close()
    return retry(_go)


def trip_circuit_breaker(reason):
    def _go():
        conn = connect()
        try:
            conn.execute("UPDATE trading_state SET tripped=1, reason=? WHERE id=1", (reason,))
            conn.commit()
        finally:
            conn.close()
    return retry(_go)


def clear_circuit_breaker():
    """Manual override from the dashboard — does not touch
    daily_start_value_usd, so loss is still measured against the same
    baseline for the rest of the day."""
    def _go():
        conn = connect()
        try:
            conn.execute("UPDATE trading_state SET tripped=0, reason=NULL WHERE id=1")
            conn.commit()
        finally:
            conn.close()
    return retry(_go)


# ----------------------------------------------------------------- operator
# The operator's two memories: skills (lessons it learned, or recipes the
# owner taught it) and mistakes. Both are read back into every later plan.

def add_memory(kind, title, body, source):
    """Same title and kind replaces the old entry, so repeating a lesson
    updates it instead of filling the prompt with near-duplicates."""
    def _go():
        conn = connect()
        try:
            conn.execute("DELETE FROM operator_memory WHERE kind=? AND lower(title)=lower(?)",
                         (kind, title))
            c = conn.execute(
                "INSERT INTO operator_memory (kind, title, body, source, created_at) "
                "VALUES (?,?,?,?,?)", (kind, title, body, source, _now()))
            conn.commit()
            return c.lastrowid
        finally:
            conn.close()
    return retry(_go)


def get_memory(kind, limit=20):
    conn = connect()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM operator_memory WHERE kind=? ORDER BY id DESC LIMIT ?",
                  (kind, limit))
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def delete_memory(memory_id):
    def _go():
        conn = connect()
        try:
            conn.execute("DELETE FROM operator_memory WHERE id=?", (memory_id,))
            conn.commit()
        finally:
            conn.close()
    return retry(_go)


def create_operator_task(goal):
    def _go():
        conn = connect()
        try:
            c = conn.execute(
                "INSERT INTO operator_tasks (goal, status, answer, transcript, created_at, "
                "updated_at) VALUES (?, 'running', '', '[]', ?, ?)", (goal, _now(), _now()))
            conn.commit()
            return c.lastrowid
        finally:
            conn.close()
    return retry(_go)


def update_operator_task(task_id, status, answer, transcript):
    def _go():
        conn = connect()
        try:
            conn.execute(
                "UPDATE operator_tasks SET status=?, answer=?, transcript=?, updated_at=? "
                "WHERE id=?", (status, answer, json.dumps(transcript), _now(), task_id))
            conn.commit()
        finally:
            conn.close()
    return retry(_go)


def get_operator_tasks(limit=10):
    conn = connect()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM operator_tasks ORDER BY id DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in c.fetchall()]
        for r in rows:
            try:
                r["transcript"] = json.loads(r["transcript"] or "[]")
            except Exception:
                r["transcript"] = []
        return rows
    finally:
        conn.close()


def get_setting_age_seconds(key):
    """Generic heartbeat-age check, used by the realtime trading stream —
    a separate process from the main worker, so it can't use the file-based
    heartbeat() in core/worker.py.

    Values stored here may be naive (this module's own _now(), which uses
    utcnow()) or timezone-aware (core/trading writes datetime.now(timezone.utc)
    isoformat, which includes a +00:00 offset). Subtracting a naive datetime
    from an aware one raises TypeError — silently caught below, which would
    make this always return None for an aware timestamp otherwise. Both are
    normalized to aware-UTC before comparing."""
    from datetime import datetime, timezone
    value = get_setting(key)
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None
