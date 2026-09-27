"""SQLite storage. One connection behind one lock: writes are small, and serialising them makes
every multi-row transition (settlement, promotion, rollback) atomic without further thought.
Every write is fsynced (synchronous=FULL) before the HTTP response goes out."""
import hashlib
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS keys (
    id         TEXT PRIMARY KEY,               -- public id, safe to show; the secret is never stored
    hash       TEXT NOT NULL UNIQUE,           -- sha256 of the secret
    account_id TEXT NOT NULL,
    scope      TEXT NOT NULL CHECK (scope IN ('owner','bot')),
    agent_id   TEXT,                           -- bot keys may be bound to one agent
    label      TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

-- Double-entry ledger. Balances are integer atomic units (1 unit = 1e-6 of the settlement asset,
-- matching USDC's 6 decimals). Only 'world:' accounts, which represent money outside the system,
-- may go negative; every transfer moves value between two accounts, so all balances sum to zero.
CREATE TABLE IF NOT EXISTS balances (
    account TEXT PRIMARY KEY,
    amount  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transfers (
    id         INTEGER PRIMARY KEY,
    from_acct  TEXT NOT NULL,
    to_acct    TEXT NOT NULL,
    amount     INTEGER NOT NULL CHECK (amount > 0),
    kind       TEXT NOT NULL,
    ref        TEXT,
    mandate_id TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mandates (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL,
    agent_id    TEXT,                          -- NULL: any of the account's bot keys
    purpose     TEXT NOT NULL CHECK (purpose IN ('bounty','eval_fee','any')),
    max_per_tx  INTEGER NOT NULL,
    max_per_day INTEGER NOT NULL,
    expires_at  REAL NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    terms_hash  TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id   TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    contract   TEXT NOT NULL,                  -- JSON output contract, checked on every trace
    policy     TEXT NOT NULL,                  -- JSON: auto_bounty, auto_promote, canary settings
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS configs (
    id           INTEGER PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    version      TEXT NOT NULL,
    config       TEXT NOT NULL,                -- JSON: system_instruction, model, params, tools, examples
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('STAGED','ACTIVE','ARCHIVED','ROLLED_BACK')),
    source       TEXT NOT NULL,
    base_version TEXT,
    created_at   REAL NOT NULL,
    activated_at REAL,
    UNIQUE (agent_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_agent ON configs(agent_id) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS traces (
    id             INTEGER PRIMARY KEY,
    agent_id       TEXT NOT NULL,
    version        TEXT,
    session        TEXT,
    latency_ms     REAL NOT NULL,
    success        INTEGER NOT NULL,           -- what the agent claimed
    input          TEXT,
    output         TEXT,
    error          TEXT,
    verdict        TEXT NOT NULL CHECK (verdict IN ('fail','pass','unverified')),
    verdict_source TEXT NOT NULL,              -- self_fail | contract | consumer | owner | none
    feedback_hash  TEXT,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_agent ON traces(agent_id, id);
CREATE INDEX IF NOT EXISTS traces_feedback ON traces(feedback_hash);

-- A case is a replayable test built from a verified trace: an input plus a grader.
--   failure:    a verified failure the agent should stop making
--   guard:      a verified pass the agent must not start failing
--   regression: a failure some past repair fixed; must keep passing forever
CREATE TABLE IF NOT EXISTS cases (
    id         INTEGER PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    trace_id   INTEGER NOT NULL UNIQUE,
    kind       TEXT NOT NULL CHECK (kind IN ('failure','guard','regression')),
    input      TEXT NOT NULL,
    grader     TEXT NOT NULL,
    source     TEXT NOT NULL,
    bounty_id  INTEGER,                        -- first bounty that used it (failures are used once)
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bounties (
    id               INTEGER PRIMARY KEY,
    agent_id         TEXT NOT NULL,
    account_id       TEXT NOT NULL,
    amount           INTEGER NOT NULL,
    baseline_fee     INTEGER NOT NULL,
    terms            TEXT NOT NULL,            -- JSON, committed
    commitment       TEXT NOT NULL,
    seed             TEXT NOT NULL,            -- secret until settlement
    baseline_version TEXT NOT NULL,
    baseline_results TEXT,                     -- JSON {case_id: pass}; secret until settlement
    status           TEXT NOT NULL CHECK (status IN ('PREPARING','OPEN','SETTLED','NO_WINNER','FAILED')),
    closes_at        REAL,                     -- set when baseline evaluation finishes
    result           TEXT,
    warranty_state   TEXT,                     -- NULL | HELD | RELEASED | REFUNDED
    warranty_until   REAL,
    created_at       REAL NOT NULL,
    settled_at       REAL
);
CREATE TABLE IF NOT EXISTS bounty_cases (
    bounty_id    INTEGER NOT NULL,
    case_id      INTEGER NOT NULL,
    split        TEXT NOT NULL CHECK (split IN ('visible','hidden')),
    role         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (bounty_id, case_id)
);
CREATE TABLE IF NOT EXISTS submissions (
    id             INTEGER PRIMARY KEY,
    bounty_id      INTEGER NOT NULL,
    account_id     TEXT NOT NULL,
    config         TEXT NOT NULL,              -- sealed: never shown to the bounty's owner unless it wins
    fingerprint    TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('EVALUATING','EVALUATED','FAILED')),
    fee            INTEGER NOT NULL,           -- referee fee paid; refunded if the referee itself fails
    visible_result TEXT,
    hidden_result  TEXT,                       -- secret until settlement
    error          TEXT,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rollouts (
    id               INTEGER PRIMARY KEY,
    agent_id         TEXT NOT NULL,
    candidate        TEXT NOT NULL,
    baseline         TEXT NOT NULL,
    fraction         REAL NOT NULL,
    min_samples      INTEGER NOT NULL,
    bounty_id        INTEGER,
    status           TEXT NOT NULL CHECK (status IN ('RUNNING','PROMOTED','ROLLED_BACK','REVERTED','CANCELLED')),
    decision         TEXT,
    started_at       REAL NOT NULL,
    ends_at          REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_rollout_per_agent ON rollouts(agent_id) WHERE status = 'RUNNING';

CREATE TABLE IF NOT EXISTS deposits (
    id          INTEGER PRIMARY KEY,
    account_id  TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    rail        TEXT NOT NULL,
    reference   TEXT NOT NULL UNIQUE,          -- on-chain tx hash: the same payment can't credit twice
    payer       TEXT,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id          INTEGER PRIMARY KEY,
    account_id  TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    destination TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('PENDING','PAID','FAILED')),
    reference   TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    agent_id   TEXT,
    account_id TEXT,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_agent ON events(agent_id, id);
"""


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


class ApiError(Exception):
    def __init__(self, status, detail, headers=None, body=None):
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers or {}
        self.body = body  # full response body, when {"detail": ...} isn't enough (x402's 402)


class Clock:
    """Wall clock with an offset tests can advance, so time-based rules (bounty deadlines,
    warranty expiry, canary windows) are testable without sleeping."""

    def __init__(self):
        self.offset = 0.0

    def now(self):
        return time.time() + self.offset

    def advance(self, seconds):
        self.offset += seconds


class Store:
    def __init__(self, path, clock=None):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.depth = 0
        self.clock = clock or Clock()

    def now(self):
        return self.clock.now()

    def tx(self):
        """Re-entrant transaction: nested calls join the outer one, so helpers can be composed
        into a single atomic settlement."""
        store = self

        class _Tx:
            def __enter__(self):
                store.lock.acquire()
                if store.depth == 0:
                    store.db.execute("BEGIN IMMEDIATE")
                store.depth += 1
                return store.db

            def __exit__(self, exc_type, *_):
                store.depth -= 1
                try:
                    if store.depth == 0:
                        store.db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    store.lock.release()
                return False

        return _Tx()

    def event(self, kind, detail, agent_id=None, account_id=None):
        with self.tx() as db:
            db.execute("INSERT INTO events (agent_id, account_id, kind, detail, created_at) VALUES (?,?,?,?,?)",
                       (agent_id, account_id, kind, canonical(detail), self.now()))
