"""Everything Clear remembers, in one SQLite file.

Money is kept in integer micro-dollars (1 = $0.000001) so a price like $0.0004 per call adds up
exactly. Bills are rounded to whole cents only at the end, because that is what Stripe charges.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_name TEXT NOT NULL, email TEXT,
  key_hash TEXT UNIQUE NOT NULL, stripe_account TEXT,
  prices TEXT NOT NULL DEFAULT '{}',          -- tool -> micro-dollars per unit
  policy TEXT NOT NULL DEFAULT '{}',          -- who may use it, see Policy in billing.py
  created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS owners (            -- whoever pays for some agents
  id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, key_hash TEXT UNIQUE NOT NULL,
  created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  keptvow_id TEXT,                            -- the agent's Keptvow id, as claimed by its owner
  keptvow_verified INTEGER NOT NULL DEFAULT 0, -- only a confirmed link borrows the Keptvow record
  owner_id TEXT NOT NULL REFERENCES owners(id), name TEXT NOT NULL,
  key_hash TEXT UNIQUE NOT NULL,
  monthly_cap INTEGER,                        -- micro-dollars across all services; NULL = none
  active INTEGER NOT NULL DEFAULT 1, created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
  service_id TEXT NOT NULL, ref TEXT NOT NULL, agent_id TEXT NOT NULL, owner_id TEXT NOT NULL,
  tool TEXT NOT NULL, units INTEGER NOT NULL, amount INTEGER NOT NULL, period TEXT NOT NULL,
  at_ms INTEGER NOT NULL, PRIMARY KEY (service_id, ref)
);
CREATE INDEX IF NOT EXISTS usage_period ON usage(period, service_id, owner_id);
CREATE INDEX IF NOT EXISTS usage_agent ON usage(agent_id, period);
CREATE TABLE IF NOT EXISTS bills (
  id TEXT PRIMARY KEY, service_id TEXT NOT NULL, owner_id TEXT NOT NULL, period TEXT NOT NULL,
  lines TEXT NOT NULL, usage_cents INTEGER NOT NULL, carried_in_cents INTEGER NOT NULL,
  total_cents INTEGER NOT NULL,
  status TEXT NOT NULL,      -- carried_forward | ready | sent | paid | overdue | void
  note TEXT, stripe_invoice_id TEXT, invoice_url TEXT, due_ms INTEGER,
  reported_to_keptvow TEXT, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
  UNIQUE (service_id, owner_id, period)
);
CREATE TABLE IF NOT EXISTS stripe_customers (
  stripe_account TEXT NOT NULL, owner_id TEXT NOT NULL, customer_id TEXT NOT NULL,
  PRIMARY KEY (stripe_account, owner_id)
);
CREATE TABLE IF NOT EXISTS closed_periods (period TEXT PRIMARY KEY, closed_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, service_id TEXT NOT NULL, expires_ms INTEGER NOT NULL);
"""


def now_ms():
    return int(time.time() * 1000)


def period_of(ms):
    t = time.gmtime(ms / 1000)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}"


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(prefix):
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def new_id(prefix):
    return f"{prefix}_{secrets.token_hex(6)}"


class Store:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    # -- tiny helpers ---------------------------------------------------------------------------
    def one(self, sql, *args):
        with self.lock:
            r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    def all(self, sql, *args):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def run(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args)

    # -- services -------------------------------------------------------------------------------
    def create_service(self, name, owner_name, email=None):
        sid, key = new_id("svc"), new_key("svc_key")
        self.run("INSERT INTO services (id,name,owner_name,email,key_hash,created_ms) VALUES (?,?,?,?,?,?)",
                 sid, name, owner_name, email, hash_key(key), now_ms())
        return sid, key

    def service_by_key(self, key):
        return self._service(self.one("SELECT * FROM services WHERE key_hash=?", hash_key(key or "")))

    def service(self, sid):
        return self._service(self.one("SELECT * FROM services WHERE id=?", sid))

    @staticmethod
    def _service(r):
        if r:
            r["prices"], r["policy"] = json.loads(r["prices"]), json.loads(r["policy"])
        return r

    def update_service(self, sid, **fields):
        for k, v in fields.items():
            if k in ("prices", "policy"):
                v = json.dumps(v)
            if k not in ("prices", "policy", "stripe_account", "email", "name", "owner_name"):
                raise ValueError(k)
            self.run(f"UPDATE services SET {k}=? WHERE id=?", v, sid)

    # -- owners and agents ----------------------------------------------------------------------
    def create_owner(self, name, email=None):
        oid, key = new_id("own"), new_key("own_key")
        self.run("INSERT INTO owners (id,name,email,key_hash,created_ms) VALUES (?,?,?,?,?)",
                 oid, name, email, hash_key(key), now_ms())
        return oid, key

    def owner_by_key(self, key):
        return self.one("SELECT * FROM owners WHERE key_hash=?", hash_key(key or ""))

    def owner(self, oid):
        return self.one("SELECT * FROM owners WHERE id=?", oid)

    def create_agent(self, owner_id, name, monthly_cap=None, keptvow_id=None):
        aid, key = new_id("agt"), new_key("agt_key")
        self.run("INSERT INTO agents (id,keptvow_id,owner_id,name,key_hash,monthly_cap,created_ms) VALUES (?,?,?,?,?,?,?)",
                 aid, keptvow_id, owner_id, name, hash_key(key), monthly_cap, now_ms())
        return aid, key

    def agent(self, aid):
        return self.one("SELECT * FROM agents WHERE id=?", aid)

    def agent_by_key(self, key):
        return self.one("SELECT * FROM agents WHERE key_hash=?", hash_key(key or ""))

    # -- dashboard sessions ---------------------------------------------------------------------
    def new_session(self, service_id, hours=12):
        tok = secrets.token_urlsafe(32)
        self.run("INSERT INTO sessions VALUES (?,?,?)", hash_key(tok), service_id, now_ms() + hours * 3600_000)
        return tok

    def session_service(self, tok):
        r = self.one("SELECT service_id FROM sessions WHERE token_hash=? AND expires_ms>?", hash_key(tok or ""), now_ms())
        return r and r["service_id"]
