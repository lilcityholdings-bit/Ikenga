#!/usr/bin/env python3
"""Auto Body Shop — staging, approval and rollback of AI agents' system instructions.

    python3 server.py            # listens on :8090, stores state in data/autobodyshop.db

Implements openapi.yaml. Stdlib only (http.server + sqlite3), same no-dependency approach as the
rest of this repo; the one optional exception is the Claude optimizer in optimizer.py, which
needs `pip install anthropic` only if you turn it on.

Lifecycle of a configuration version, per agent:

    STAGED ──approve──▶ ACTIVE ──(next approve)──▶ ARCHIVED ──rollback──▶ ACTIVE
                          │
                          └──rollback──▶ ROLLED_BACK   (never restored automatically again)

Exactly one ACTIVE version per agent at any time (enforced by a partial unique index, not just
by the code paths). Nothing becomes ACTIVE without an explicit POST /v1/approve: the optimizer
only ever *stages* candidates. That's deliberate — telemetry carries latency and success only,
not the failing inputs, so an optimizer is working from thin evidence and a human (or a separate
eval gate) has to be the one to promote.
"""
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import optimizer as optimizer_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS configs (
    id                 INTEGER PRIMARY KEY,
    agent_id           TEXT NOT NULL,
    version            TEXT NOT NULL,
    system_instruction TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('STAGED','ACTIVE','ARCHIVED','ROLLED_BACK')),
    source             TEXT NOT NULL,          -- 'manual' or 'optimizer:<job id>'
    base_version       TEXT,                   -- the ACTIVE version a candidate was derived from
    created_at         REAL NOT NULL,
    activated_at       REAL,
    UNIQUE (agent_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_agent ON configs(agent_id) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS telemetry (
    id         INTEGER PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    version    TEXT,                           -- version the trace ran against (NULL: no config yet)
    latency_ms REAL NOT NULL,
    success    INTEGER NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS telemetry_agent ON telemetry(agent_id, id);

CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY,
    agent_id          TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('QUEUED','RUNNING','STAGED','SKIPPED','FAILED')),
    first_trace_id    INTEGER NOT NULL,
    last_trace_id     INTEGER NOT NULL,
    candidate_version TEXT,
    detail            TEXT,
    created_at        REAL NOT NULL,
    finished_at       REAL
);
"""


class ApiError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


class Store:
    """All state lives in SQLite. One connection behind one lock: the workload is tiny writes,
    and serialising them makes every multi-row transition (approve, rollback) trivially atomic."""

    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def tx(self):
        store = self

        class _Tx:
            def __enter__(self):
                store.lock.acquire()
                store.db.execute("BEGIN IMMEDIATE")
                return store.db

            def __exit__(self, exc_type, *_):
                try:
                    store.db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    store.lock.release()
                return False

        return _Tx()

    # ---- versions -------------------------------------------------------------------------

    @staticmethod
    def _new_version(db, agent_id):
        # "v<unix seconds>", as in the spec's examples; bumped if two land in the same second.
        n = int(time.time())
        while db.execute("SELECT 1 FROM configs WHERE agent_id=? AND version=?",
                         (agent_id, f"v{n}")).fetchone():
            n += 1
        return f"v{n}"

    def active(self, agent_id):
        with self.tx() as db:
            return db.execute("SELECT * FROM configs WHERE agent_id=? AND status='ACTIVE'",
                              (agent_id,)).fetchone()

    def stage(self, agent_id, instruction, source, base_version=None, db=None):
        def do(db):
            version = self._new_version(db, agent_id)
            db.execute("INSERT INTO configs (agent_id, version, system_instruction, status, source,"
                       " base_version, created_at) VALUES (?,?,?,'STAGED',?,?,?)",
                       (agent_id, version, instruction, source, base_version, time.time()))
            return version
        if db is not None:
            return do(db)
        with self.tx() as db:
            return do(db)

    def approve(self, agent_id, version):
        with self.tx() as db:
            row = db.execute("SELECT status FROM configs WHERE agent_id=? AND version=?",
                             (agent_id, version)).fetchone()
            if row is None or row["status"] != "STAGED":
                state = "not found" if row is None else f"is {row['status']}, not STAGED"
                raise ApiError(404, f"Candidate {version} for {agent_id} {state}")
            db.execute("UPDATE configs SET status='ARCHIVED' WHERE agent_id=? AND status='ACTIVE'",
                       (agent_id,))
            db.execute("UPDATE configs SET status='ACTIVE', activated_at=? WHERE agent_id=? AND version=?",
                       (time.time(), agent_id, version))

    def rollback(self, agent_id, version):
        with self.tx() as db:
            cur = db.execute("SELECT version FROM configs WHERE agent_id=? AND status='ACTIVE'",
                             (agent_id,)).fetchone()
            if cur is None:
                raise ApiError(404, f"No active configuration found for {agent_id}")
            # The caller names the version it is rolling back. If that's no longer what's live
            # (someone approved or rolled back in between), refuse rather than demote the wrong one.
            if cur["version"] != version:
                raise ApiError(409, f"{version} is not the active version of {agent_id} "
                                    f"(active is {cur['version']}); refusing to roll back")
            prev = db.execute("SELECT version FROM configs WHERE agent_id=? AND status='ARCHIVED'"
                              " ORDER BY activated_at DESC, id DESC LIMIT 1", (agent_id,)).fetchone()
            if prev is None:
                raise ApiError(409, f"No archived stable version of {agent_id} to restore")
            db.execute("UPDATE configs SET status='ROLLED_BACK' WHERE agent_id=? AND version=?",
                       (agent_id, version))
            db.execute("UPDATE configs SET status='ACTIVE', activated_at=? WHERE agent_id=? AND version=?",
                       (time.time(), agent_id, prev["version"]))
            return prev["version"]

    def versions(self, agent_id):
        with self.tx() as db:
            rows = db.execute(
                "SELECT c.version, c.status, c.source, c.base_version, c.created_at, c.activated_at,"
                "       c.system_instruction,"
                "       COUNT(t.id) AS traces, AVG(t.success) AS success_rate, AVG(t.latency_ms) AS mean_latency_ms"
                "  FROM configs c LEFT JOIN telemetry t ON t.agent_id=c.agent_id AND t.version=c.version"
                " WHERE c.agent_id=? GROUP BY c.id ORDER BY c.id", (agent_id,)).fetchall()
            jobs = db.execute("SELECT id, status, first_trace_id, last_trace_id, candidate_version, detail,"
                              " created_at, finished_at FROM jobs WHERE agent_id=? ORDER BY id",
                              (agent_id,)).fetchall()
        return [dict(r) for r in rows], [dict(j) for j in jobs]

    # ---- telemetry + optimization jobs ------------------------------------------------------

    def ingest(self, agent_id, latency_ms, success, version, batch_size):
        """Returns (total_traces, queued_job_id or None)."""
        with self.tx() as db:
            if version is None:
                row = db.execute("SELECT version FROM configs WHERE agent_id=? AND status='ACTIVE'",
                                 (agent_id,)).fetchone()
                version = row["version"] if row else None
            elif not db.execute("SELECT 1 FROM configs WHERE agent_id=? AND version=?",
                                (agent_id, version)).fetchone():
                raise ApiError(404, f"Version {version} not found for {agent_id}")
            cur = db.execute("INSERT INTO telemetry (agent_id, version, latency_ms, success, ts)"
                             " VALUES (?,?,?,?,?)", (agent_id, version, latency_ms, int(success), time.time()))
            trace_id = cur.lastrowid
            total = db.execute("SELECT COUNT(*) FROM telemetry WHERE agent_id=?", (agent_id,)).fetchone()[0]
            job_id = None
            if total % batch_size == 0:
                first = db.execute("SELECT id FROM telemetry WHERE agent_id=? ORDER BY id DESC"
                                   " LIMIT 1 OFFSET ?", (agent_id, batch_size - 1)).fetchone()[0]
                job_id = db.execute("INSERT INTO jobs (agent_id, status, first_trace_id, last_trace_id,"
                                    " created_at) VALUES (?, 'QUEUED', ?, ?, ?)",
                                    (agent_id, first, trace_id, time.time())).lastrowid
        return total, job_id

    def claim_job(self, job_id):
        with self.tx() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=? AND status='QUEUED'", (job_id,)).fetchone()
            if job is None:
                return None
            db.execute("UPDATE jobs SET status='RUNNING' WHERE id=?", (job_id,))
            active = db.execute("SELECT version, system_instruction FROM configs"
                                " WHERE agent_id=? AND status='ACTIVE'", (job["agent_id"],)).fetchone()
            traces = []
            if active is not None:
                # Only traces that ran against the live version say anything about it; shadow
                # traces reported against a STAGED version are excluded.
                traces = db.execute("SELECT latency_ms, success FROM telemetry WHERE agent_id=?"
                                    " AND id BETWEEN ? AND ? AND version=?",
                                    (job["agent_id"], job["first_trace_id"], job["last_trace_id"],
                                     active["version"])).fetchall()
            return dict(job), (dict(active) if active else None), [dict(t) for t in traces]

    def finish_job(self, job_id, status, detail, candidate=None, base_version=None):
        with self.tx() as db:
            version = None
            if candidate is not None:
                agent_id = db.execute("SELECT agent_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
                version = self.stage(agent_id, candidate, f"optimizer:{job_id}", base_version, db=db)
            db.execute("UPDATE jobs SET status=?, detail=?, candidate_version=?, finished_at=? WHERE id=?",
                       (status, detail, version, time.time(), job_id))
            return version

    def pending_jobs(self):
        with self.tx() as db:
            # RUNNING at startup means the process died mid-job; the optimizer call is repeatable.
            db.execute("UPDATE jobs SET status='QUEUED' WHERE status='RUNNING'")
            return [r[0] for r in db.execute("SELECT id FROM jobs WHERE status='QUEUED' ORDER BY id")]


def batch_stats(traces):
    lat = sorted(t["latency_ms"] for t in traces)

    def pct(p):
        return lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None

    return {
        "traces": len(traces),
        "success_rate": (sum(t["success"] for t in traces) / len(traces)) if traces else None,
        "latency_ms": {"mean": (sum(lat) / len(lat)) if lat else None, "p50": pct(0.50), "p95": pct(0.95)},
    }


class Worker(threading.Thread):
    """Runs optimization jobs off the request path, one at a time."""

    def __init__(self, store, optimize):
        super().__init__(daemon=True, name="optimizer")
        self.store, self.optimize, self.q = store, optimize, queue.Queue()

    def submit(self, job_id):
        self.q.put(job_id)

    def run(self):
        while True:
            job_id = self.q.get()
            if job_id is None:
                return
            try:
                self.run_job(job_id)
            except Exception as e:  # a bug here must not kill the worker thread
                self.store.finish_job(job_id, "FAILED", f"internal error: {e!r}")

    def run_job(self, job_id):
        claimed = self.store.claim_job(job_id)
        if claimed is None:
            return
        job, active, traces = claimed
        if active is None:
            self.store.finish_job(job_id, "SKIPPED", "agent has no ACTIVE configuration to optimize")
            return
        if not traces:
            self.store.finish_job(job_id, "SKIPPED", f"no traces in this batch ran against {active['version']}")
            return
        if self.optimize is None:
            self.store.finish_job(job_id, "SKIPPED", "no optimizer configured (set ABS_OPTIMIZER)")
            return
        ctx = {"agent_id": job["agent_id"], "version": active["version"],
               "system_instruction": active["system_instruction"], "batch": batch_stats(traces)}
        try:
            candidate = self.optimize(ctx)
        except Exception as e:
            self.store.finish_job(job_id, "FAILED", f"optimizer error: {e}")
            return
        candidate = (candidate or "").strip()
        if not candidate or candidate == active["system_instruction"].strip():
            self.store.finish_job(job_id, "SKIPPED", "optimizer proposed no change")
            return
        version = self.store.finish_job(job_id, "STAGED", "candidate staged for approval",
                                        candidate=candidate, base_version=active["version"])
        print(f"[optimizer] job {job_id}: staged {version} for {job['agent_id']}", flush=True)


# ---- HTTP ------------------------------------------------------------------------------------

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_BODY = 1 << 20


def require(body, field, kind):
    if field not in body:
        raise ApiError(422, f"Field required: {field}")
    val = body[field]
    ok = {"str": isinstance(val, str) and val != "",
          "bool": isinstance(val, bool),
          "num": isinstance(val, (int, float)) and not isinstance(val, bool)}[kind]
    if not ok:
        raise ApiError(422, f"Invalid value for {field}")
    return val


def agent_id_of(val):
    if not AGENT_ID_RE.match(val):
        raise ApiError(422, "Invalid agent_id")
    return val


class App:
    def __init__(self, store, worker, batch_size, admin_key):
        self.store, self.worker, self.batch_size, self.admin_key = store, worker, batch_size, admin_key

    def check_admin(self, headers):
        if self.admin_key is not None and headers.get("X-Admin-Key") != self.admin_key:
            raise ApiError(401, "Missing or invalid X-Admin-Key")

    def handle(self, method, path, headers, body):
        m = re.fullmatch(r"/v1/config/([^/]+)", path)
        if method == "GET" and m:
            row = self.store.active(agent_id_of(m.group(1)))
            if row is None:
                raise ApiError(404, f"No active configuration found for {m.group(1)}")
            return 200, {"version": row["version"], "system_instruction": row["system_instruction"]}

        m = re.fullmatch(r"/v1/versions/([^/]+)", path)
        if method == "GET" and m:
            self.check_admin(headers)
            versions, jobs = self.store.versions(agent_id_of(m.group(1)))
            return 200, {"agent_id": m.group(1), "versions": versions, "jobs": jobs}

        if method == "GET" and path == "/health":
            return 200, {"status": "ok", "optimizer": optimizer_mod.describe(),
                         "batch_size": self.batch_size, "admin_key_required": self.admin_key is not None}

        if method == "POST" and path == "/v1/telemetry":
            agent_id = agent_id_of(require(body, "agent_id", "str"))
            latency = require(body, "latency_ms", "num")
            if latency < 0 or latency != latency or latency == float("inf"):
                raise ApiError(422, "Invalid value for latency_ms")
            success = require(body, "success", "bool")
            version = require(body, "version", "str") if "version" in body else None
            total, job_id = self.store.ingest(agent_id, float(latency), success, version, self.batch_size)
            if job_id is not None:
                self.worker.submit(job_id)
            return 200, {"status": "INGESTED", "total_traces": total}

        if method == "POST" and path == "/v1/approve":
            self.check_admin(headers)
            agent_id = agent_id_of(require(body, "agent_id", "str"))
            version = require(body, "version", "str")
            self.store.approve(agent_id, version)
            return 200, {"status": "SUCCESS", "message": f"Version {version} promoted to production."}

        if method == "POST" and path == "/v1/rollback":
            self.check_admin(headers)
            agent_id = agent_id_of(require(body, "agent_id", "str"))
            restored = self.store.rollback(agent_id, require(body, "version", "str"))
            return 200, {"status": "ROLLED_BACK",
                         "message": f"Agent restored to last stable configuration ({restored}).",
                         "restored_version": restored}

        if method == "POST" and path == "/v1/candidates":
            self.check_admin(headers)
            agent_id = agent_id_of(require(body, "agent_id", "str"))
            version = self.store.stage(agent_id, require(body, "system_instruction", "str"), "manual")
            return 201, {"status": "STAGED", "version": version}

        raise ApiError(404, "Not Found")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AutoBodyShop/1.0"

        def _dispatch(self, method):
            try:
                body = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY:
                        raise ApiError(413, "Request body too large")
                    raw = self.rfile.read(length) if length else b""
                    try:
                        body = json.loads(raw or b"null")
                    except ValueError:
                        raise ApiError(400, "Body is not valid JSON")
                    if not isinstance(body, dict):
                        raise ApiError(422, "Body must be a JSON object")
                path = self.path.split("?", 1)[0]
                status, payload = app.handle(method, path, self.headers, body)
            except ApiError as e:
                status, payload = e.status, {"detail": e.detail}
            except Exception as e:
                print(f"[error] {method} {self.path}: {e!r}", file=sys.stderr, flush=True)
                status, payload = 500, {"detail": "Internal Server Error"}
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, fmt, *args):
            if os.environ.get("ABS_ACCESS_LOG") == "1":
                super().log_message(fmt, *args)

    return Handler


def build(db_path, batch_size, admin_key, optimize):
    store = Store(db_path)
    worker = Worker(store, optimize)
    worker.start()
    for job_id in store.pending_jobs():
        worker.submit(job_id)
    return App(store, worker, batch_size, admin_key)


def main():
    port = int(os.environ.get("ABS_PORT", "8090"))
    batch_size = int(os.environ.get("ABS_BATCH_SIZE", "100"))
    if batch_size < 1:
        sys.exit("FATAL: ABS_BATCH_SIZE must be >= 1")
    admin_key = os.environ.get("ABS_ADMIN_KEY") or None
    if os.environ.get("ABS_ENV") == "production" and admin_key is None:
        # Without it, anyone who can reach the port can promote or roll back production prompts.
        sys.exit("FATAL: ABS_ENV=production requires ABS_ADMIN_KEY")
    if admin_key is None:
        print("WARNING: ABS_ADMIN_KEY unset — approve/rollback/candidates are unauthenticated", flush=True)
    app = build(os.environ.get("ABS_DB", "data/autobodyshop.db"), batch_size, admin_key,
                optimizer_mod.from_env())
    server = ThreadingHTTPServer((os.environ.get("ABS_HOST", "0.0.0.0"), port), make_handler(app))
    print(f"Auto Body Shop listening on :{port} (batch size {batch_size}, optimizer: "
          f"{optimizer_mod.describe()})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
