#!/usr/bin/env python3
"""End-to-end tests over real HTTP. Stdlib only, no setup:

    python3 test_server.py
"""
import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server  # noqa: E402

ADMIN = "test-admin-key"


class Client:
    def __init__(self, port):
        self.port = port

    def call(self, method, path, body=None, admin=True, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if admin:
            headers["X-Admin-Key"] = ADMIN
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        out = json.loads(resp.read())
        conn.close()
        return resp.status, out


def start(optimize=None, batch_size=5, db=None):
    db = db or os.path.join(tempfile.mkdtemp(), "abs.db")
    app = server.build(db, batch_size, ADMIN, optimize)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, Client(httpd.server_address[1]), app


def wait_for(pred, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.httpd, self.c, self.app = start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()

    def stage(self, text, agent="bot_alpha"):
        s, out = self.c.call("POST", "/v1/candidates", {"agent_id": agent, "system_instruction": text})
        self.assertEqual(s, 201, out)
        return out["version"]

    def test_no_active_config_is_404_with_detail(self):
        s, out = self.c.call("GET", "/v1/config/bot_alpha")
        self.assertEqual(s, 404)
        self.assertIn("detail", out)

    def test_approve_promotes_and_archives_previous(self):
        v1 = self.stage("instruction one")
        v2 = self.stage("instruction two")
        self.assertNotEqual(v1, v2)
        # Staged is not live.
        self.assertEqual(self.c.call("GET", "/v1/config/bot_alpha")[0], 404)

        s, out = self.c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": v1})
        self.assertEqual((s, out["status"]), (200, "SUCCESS"))
        self.assertEqual(self.c.call("GET", "/v1/config/bot_alpha")[1],
                         {"version": v1, "system_instruction": "instruction one"})

        self.c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": v2})
        self.assertEqual(self.c.call("GET", "/v1/config/bot_alpha")[1]["version"], v2)
        statuses = {v["version"]: v["status"] for v in self.c.call("GET", "/v1/versions/bot_alpha")[1]["versions"]}
        self.assertEqual(statuses, {v1: "ARCHIVED", v2: "ACTIVE"})

    def test_approve_rejects_non_staged(self):
        v1 = self.stage("one")
        self.c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": v1})
        self.assertEqual(self.c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": v1})[0], 404)
        self.assertEqual(self.c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": "v1"})[0], 404)
        # A version belongs to one agent only.
        self.assertEqual(self.c.call("POST", "/v1/approve", {"agent_id": "bot_beta", "version": v1})[0], 404)

    def test_rollback_restores_last_archived_and_never_reinstates_rolled_back(self):
        v1, v2, v3 = self.stage("one"), self.stage("two"), self.stage("three")
        for v in (v1, v2, v3):
            self.c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": v})

        s, out = self.c.call("POST", "/v1/rollback", {"agent_id": "bot_alpha", "version": v3})
        self.assertEqual((s, out["status"], out["restored_version"]), (200, "ROLLED_BACK", v2))
        self.assertEqual(self.c.call("GET", "/v1/config/bot_alpha")[1]["version"], v2)

        s, out = self.c.call("POST", "/v1/rollback", {"agent_id": "bot_alpha", "version": v2})
        self.assertEqual(out["restored_version"], v1)  # not v3, which was rolled back

        s, out = self.c.call("POST", "/v1/rollback", {"agent_id": "bot_alpha", "version": v1})
        self.assertEqual(s, 409)  # nothing left to restore; v1 stays live
        self.assertEqual(self.c.call("GET", "/v1/config/bot_alpha")[1]["version"], v1)

    def test_rollback_refuses_stale_version(self):
        v1, v2 = self.stage("one"), self.stage("two")
        self.c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": v1})
        self.c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": v2})
        self.assertEqual(self.c.call("POST", "/v1/rollback", {"agent_id": "bot_alpha", "version": v1})[0], 409)
        self.assertEqual(self.c.call("GET", "/v1/config/bot_alpha")[1]["version"], v2)
        self.assertEqual(self.c.call("POST", "/v1/rollback", {"agent_id": "nobody", "version": v2})[0], 404)

    def test_admin_key_enforced(self):
        v = self.stage("one")
        for path, body in (("/v1/approve", {"agent_id": "bot_alpha", "version": v}),
                           ("/v1/rollback", {"agent_id": "bot_alpha", "version": v}),
                           ("/v1/candidates", {"agent_id": "bot_alpha", "system_instruction": "x"})):
            self.assertEqual(self.c.call("POST", path, body, admin=False)[0], 401, path)
        self.assertEqual(self.c.call("GET", "/v1/versions/bot_alpha", admin=False)[0], 401)
        # Agents fetching their config and reporting telemetry don't need it.
        self.assertEqual(self.c.call("GET", "/v1/config/bot_alpha", admin=False)[0], 404)
        self.assertEqual(self.c.call("POST", "/v1/telemetry",
                                     {"agent_id": "bot_alpha", "latency_ms": 1, "success": True},
                                     admin=False)[0], 200)

    def test_telemetry_validation(self):
        bad = [
            {"latency_ms": 1, "success": True},
            {"agent_id": "bot_alpha", "success": True},
            {"agent_id": "bot_alpha", "latency_ms": "fast", "success": True},
            {"agent_id": "bot_alpha", "latency_ms": True, "success": True},
            {"agent_id": "bot_alpha", "latency_ms": -1, "success": True},
            {"agent_id": "bot_alpha", "latency_ms": 1, "success": "yes"},
            {"agent_id": "../etc", "latency_ms": 1, "success": True},
            [1, 2],
        ]
        for body in bad:
            self.assertEqual(self.c.call("POST", "/v1/telemetry", body)[0], 422, body)
        self.assertEqual(self.c.call("POST", "/v1/telemetry", raw=b"{not json")[0], 400)
        self.assertEqual(self.c.call("POST", "/v1/telemetry",
                                     {"agent_id": "bot_alpha", "latency_ms": 1, "success": True,
                                      "version": "v_nope"})[0], 404)

    def test_telemetry_counts_per_agent(self):
        for i in range(3):
            s, out = self.c.call("POST", "/v1/telemetry", {"agent_id": "bot_alpha", "latency_ms": 245.5, "success": True})
        self.assertEqual((s, out), (200, {"status": "INGESTED", "total_traces": 3}))
        out = self.c.call("POST", "/v1/telemetry", {"agent_id": "bot_beta", "latency_ms": 1, "success": False})[1]
        self.assertEqual(out["total_traces"], 1)


class Optimization(unittest.TestCase):
    def run_batch(self, c, n, success=True, agent="bot_alpha"):
        for _ in range(n):
            c.call("POST", "/v1/telemetry", {"agent_id": agent, "latency_ms": 100, "success": success})

    def jobs(self, c, agent="bot_alpha"):
        return c.call("GET", f"/v1/versions/{agent}")[1]["jobs"]

    def seed(self, c, text="base instruction"):
        v = c.call("POST", "/v1/candidates", {"agent_id": "bot_alpha", "system_instruction": text})[1]["version"]
        c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": v})
        return v

    def test_batch_threshold_stages_candidate_but_never_promotes(self):
        seen = []

        def optimize(ctx):
            seen.append(ctx)
            return ctx["system_instruction"] + " Always return valid JSON."

        httpd, c, _ = start(optimize, batch_size=5)
        try:
            base = self.seed(c)
            self.run_batch(c, 4)
            self.assertEqual(self.jobs(c), [])
            self.run_batch(c, 1, success=False)
            job = wait_for(lambda: [j for j in self.jobs(c) if j["status"] == "STAGED"])[0]
            ctx = seen[0]
            self.assertEqual(ctx["version"], base)
            self.assertEqual(ctx["batch"]["traces"], 5)
            self.assertAlmostEqual(ctx["batch"]["success_rate"], 0.8)
            # Staged, not live.
            self.assertEqual(c.call("GET", "/v1/config/bot_alpha")[1]["version"], base)
            cand = job["candidate_version"]
            versions = {v["version"]: v for v in c.call("GET", "/v1/versions/bot_alpha")[1]["versions"]}
            self.assertEqual(versions[cand]["status"], "STAGED")
            self.assertEqual(versions[cand]["base_version"], base)
            # Shadow traces against the candidate are attributed to it, not to the live version.
            c.call("POST", "/v1/telemetry", {"agent_id": "bot_alpha", "latency_ms": 50, "success": True,
                                             "version": cand})
            versions = {v["version"]: v for v in c.call("GET", "/v1/versions/bot_alpha")[1]["versions"]}
            self.assertEqual((versions[cand]["traces"], versions[base]["traces"]), (1, 5))
            # And approving it is what makes it live.
            c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": cand})
            self.assertEqual(c.call("GET", "/v1/config/bot_alpha")[1]["system_instruction"],
                             "base instruction Always return valid JSON.")
        finally:
            httpd.shutdown(); httpd.server_close()

    def test_skips_and_failures_are_recorded(self):
        calls = {"n": 0}

        def optimize(ctx):
            calls["n"] += 1
            if calls["n"] == 1:
                return ctx["system_instruction"]  # no change
            raise RuntimeError("upstream down")

        httpd, c, _ = start(optimize, batch_size=2)
        try:
            self.run_batch(c, 2)  # no ACTIVE config yet
            wait_for(lambda: len(self.jobs(c)) == 1 and self.jobs(c)[0]["status"] == "SKIPPED")
            self.seed(c)
            self.run_batch(c, 2)
            self.run_batch(c, 2)
            jobs = wait_for(lambda: (j := self.jobs(c)) and all(x["status"] not in ("QUEUED", "RUNNING") for x in j)
                            and len(j) == 3 and j)
            self.assertEqual([j["status"] for j in jobs], ["SKIPPED", "SKIPPED", "FAILED"])
            self.assertIn("upstream down", jobs[2]["detail"])
            self.assertEqual(len(c.call("GET", "/v1/versions/bot_alpha")[1]["versions"]), 1)
        finally:
            httpd.shutdown(); httpd.server_close()

    def test_no_optimizer_configured(self):
        httpd, c, _ = start(None, batch_size=2)
        try:
            self.seed(c)
            self.run_batch(c, 2)
            job = wait_for(lambda: [j for j in self.jobs(c) if j["status"] == "SKIPPED"])[0]
            self.assertIn("no optimizer", job["detail"])
        finally:
            httpd.shutdown(); httpd.server_close()


class Process(unittest.TestCase):
    """The real entry point: env config, the command optimizer, and state surviving kill -9."""

    def launch(self, db, port, **env):
        e = dict(os.environ, ABS_DB=db, ABS_PORT=str(port), ABS_HOST="127.0.0.1", ABS_ADMIN_KEY=ADMIN, **env)
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], env=e,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        c = Client(port)

        def up():
            try:
                return c.call("GET", "/health")[0] == 200
            except OSError:
                return False
        wait_for(up)
        return p, c

    def test_command_optimizer_and_crash_recovery(self):
        db = os.path.join(tempfile.mkdtemp(), "abs.db")
        script = os.path.join(tempfile.mkdtemp(), "opt.py")
        with open(script, "w") as f:
            f.write("import json,sys\nctx=json.load(sys.stdin)\n"
                    "print(ctx['system_instruction']+' (rate '+str(ctx['batch']['success_rate'])+')')\n")
        p, c = self.launch(db, 8191, ABS_BATCH_SIZE="3", ABS_OPTIMIZER="command",
                           ABS_OPTIMIZER_CMD=f"{sys.executable} {script}")
        try:
            v = c.call("POST", "/v1/candidates", {"agent_id": "bot_alpha", "system_instruction": "base"})[1]["version"]
            c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": v})
            for ok in (True, True, False):
                c.call("POST", "/v1/telemetry", {"agent_id": "bot_alpha", "latency_ms": 10, "success": ok})
            job = wait_for(lambda: [j for j in c.call("GET", "/v1/versions/bot_alpha")[1]["jobs"]
                                    if j["status"] == "STAGED"])[0]
        finally:
            p.send_signal(signal.SIGKILL)
            p.wait()

        p, c = self.launch(db, 8192)
        try:
            self.assertEqual(c.call("GET", "/v1/config/bot_alpha")[1], {"version": v, "system_instruction": "base"})
            s, out = c.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": job["candidate_version"]})
            self.assertEqual(s, 200, out)
            self.assertEqual(c.call("GET", "/v1/config/bot_alpha")[1]["system_instruction"],
                             "base (rate 0.6666666666666666)")
            self.assertEqual(c.call("POST", "/v1/telemetry", {"agent_id": "bot_alpha", "latency_ms": 1,
                                                              "success": True})[1]["total_traces"], 4)
        finally:
            p.send_signal(signal.SIGKILL)
            p.wait()

    def test_production_requires_admin_key(self):
        e = dict(os.environ, ABS_ENV="production", ABS_DB=os.path.join(tempfile.mkdtemp(), "x.db"))
        e.pop("ABS_ADMIN_KEY", None)
        p = subprocess.run([sys.executable, os.path.join(HERE, "server.py")], env=e,
                           capture_output=True, timeout=10)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(b"ABS_ADMIN_KEY", p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
