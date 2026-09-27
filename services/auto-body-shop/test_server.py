#!/usr/bin/env python3
"""End-to-end tests over real HTTP. Stdlib only, no setup:

    python3 test_server.py

The referee's model is a deterministic fake, so repairs can be scored exactly:
  - inputs starting "tricky" break the baseline (it apologises instead of returning JSON),
    unless the instruction contains HANDLE_TRICKY;
  - an instruction containing BREAK_NORMAL makes normal inputs fail (a fix that breaks things);
  - an instruction containing BREAK_LIVE makes inputs starting "live" fail: a fix that passes
    every test case but is genuinely worse on production traffic.
"""
import base64
import hashlib
import http.client
import json
import math
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
from client import Client as AgentClient  # noqa: E402
from db import Clock, canonical  # noqa: E402

sys.path.insert(0, os.path.join(HERE, "..", "..", "bots", "repair-bot"))
import repair_bot  # noqa: E402

ADMIN = "test-admin-key"
CONTRACT = {"type": "json_schema", "schema": {"type": "object", "required": ["name"], "additionalProperties": False,
                                              "properties": {"name": {"type": "string"}}}}


def fake_model(config, text):
    si = config["system_instruction"]
    if text.startswith("tricky") and "HANDLE_TRICKY" not in si:
        return "Sorry, I can't help with that."
    if not text.startswith("tricky") and "BREAK_NORMAL" in si:
        return "{broken"
    if text.startswith("live") and "BREAK_LIVE" in si:
        return "{broken"
    return json.dumps({"name": text})


def good(text):
    return json.dumps({"name": text})


class Api:
    def __init__(self, port):
        self.port = port

    def call(self, method, path, body=None, key=None, admin=False, headers=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json", **(headers or {})}
        if key:
            h["Authorization"] = f"Bearer {key}"
        if admin:
            h["X-Admin-Key"] = ADMIN
        conn.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        out = json.loads(data) if data else None
        return (resp.status, out, dict(resp.getheaders())) if raw else (resp.status, out)


class Harness:
    def __init__(self, env=None, runner=fake_model):
        self.clock = Clock()
        e = {"ABS_ADMIN_KEY": ADMIN, "ABS_GUARD_EVERY": "1", "ABS_FEE_PER_RUN": "10", "ABS_RUNNER": "fake"}
        e.update(env or {})
        self.app = server.build(os.path.join(tempfile.mkdtemp(), "abs.db"), env=e, runner=runner, clock=self.clock)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.api = Api(self.httpd.server_address[1])

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, *a, **kw):
        return self.api.call(*a, **kw)

    def account(self, name, grant=0):
        s, out = self.call("POST", "/v1/accounts", {"name": name})
        assert s == 201, out
        if grant:
            s, g = self.call("POST", "/v1/admin/grants", {"account_id": out["account_id"], "amount": grant}, admin=True)
            assert s == 200, g
        return out

    def agent(self, owner_key, agent_id="bot_alpha", policy=None, contract=CONTRACT, si="Extract the name as JSON."):
        s, out = self.call("POST", "/v1/agents", {"agent_id": agent_id, "config": {"system_instruction": si, "model": "fake"},
                                                   "contract": contract, "policy": policy}, key=owner_key)
        assert s == 201, out
        return out

    def trace(self, key, text, output=None, success=True, agent_id="bot_alpha", **kw):
        body = {"agent_id": agent_id, "latency_ms": 12.5, "success": success, "input": text,
                "output": fake_model({"system_instruction": ""}, text) if output is None else output, **kw}
        s, out = self.call("POST", "/v1/telemetry", body, key=key)
        assert s == 200, out
        return out

    def balance(self, key):
        return self.call("GET", "/v1/accounts/me", key=key)[1]["balance"]

    def ledger_ok(self):
        s, out = self.call("GET", "/v1/admin/ledger", admin=True)
        return s == 200 and out["sum"] == 0 and all(v >= 0 for k, v in out["balances"].items() if not k.startswith("world:"))

    def wait(self, pred, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = pred()
            if v:
                return v
            time.sleep(0.02)
        raise AssertionError("condition not met in time")


class Base(unittest.TestCase):
    env = None

    def setUp(self):
        self.h = Harness(self.env)

    def tearDown(self):
        self.h.close()


class AuthAndSpec(Base):
    def test_config_endpoints_are_scoped_to_the_owning_account(self):
        a = self.h.account("alice")
        ag = self.h.agent(a["owner_key"])
        s, out = self.h.call("GET", "/v1/config/bot_alpha", key=ag["agent_key"])
        self.assertEqual(s, 200)
        self.assertEqual(out["system_instruction"], "Extract the name as JSON.")   # the original spec's fields
        self.assertEqual(out["version"], ag["version"])
        self.assertEqual(self.h.call("GET", "/v1/config/bot_alpha")[0], 401)       # prompts are no longer public
        mallory = self.h.account("mallory")
        self.assertEqual(self.h.call("GET", "/v1/config/bot_alpha", key=mallory["owner_key"])[0], 404)
        self.assertEqual(self.h.call("POST", "/v1/telemetry", {"agent_id": "bot_alpha", "latency_ms": 1, "success": True},
                                     key=mallory["owner_key"])[0], 404)
        # The agent's own key can't promote or roll back anything.
        self.assertEqual(self.h.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": "v1"},
                                     key=ag["agent_key"])[0], 403)
        self.assertEqual(self.h.call("GET", "/v1/config/bot_alpha", key="abs_nonsense")[0], 401)

    def test_bound_bot_key_cannot_touch_another_agent(self):
        a = self.h.account("alice")
        self.h.agent(a["owner_key"], "one")
        two = self.h.agent(a["owner_key"], "two")
        self.assertEqual(self.h.call("GET", "/v1/config/one", key=two["agent_key"])[0], 403)

    def test_approve_rollback_lifecycle(self):
        a = self.h.account("alice")
        ag = self.h.agent(a["owner_key"])
        v1 = ag["version"]
        s, st = self.h.call("POST", "/v1/candidates", {"agent_id": "bot_alpha", "system_instruction": "two"}, key=a["owner_key"])
        self.assertEqual(s, 201)
        self.assertEqual(self.h.call("POST", "/v1/approve", {"agent_id": "bot_alpha", "version": st["version"]},
                                     key=a["owner_key"])[1]["status"], "SUCCESS")
        self.assertEqual(self.h.call("POST", "/v1/rollback", {"agent_id": "bot_alpha", "version": v1}, key=a["owner_key"])[0], 409)
        s, rb = self.h.call("POST", "/v1/rollback", {"agent_id": "bot_alpha", "version": st["version"]}, key=a["owner_key"])
        self.assertEqual((s, rb["restored_version"]), (200, v1))
        self.assertEqual(self.h.call("POST", "/v1/rollback", {"agent_id": "bot_alpha", "version": v1}, key=a["owner_key"])[0], 409)

    def test_etag_revalidation(self):
        a = self.h.account("alice")
        ag = self.h.agent(a["owner_key"])
        s, out, headers = self.h.call("GET", "/v1/config/bot_alpha", key=ag["agent_key"], raw=True)
        s2, out2 = self.h.call("GET", "/v1/config/bot_alpha", key=ag["agent_key"], headers={"If-None-Match": headers["ETag"]})
        self.assertEqual((s2, out2), (304, None))

    def test_validation(self):
        a = self.h.account("alice")
        ag = self.h.agent(a["owner_key"])
        for body in ({"latency_ms": 1, "success": True}, {"agent_id": "bot_alpha", "latency_ms": "x", "success": True},
                     {"agent_id": "bot_alpha", "latency_ms": -1, "success": True},
                     {"agent_id": "bot_alpha", "latency_ms": 1, "success": "yes"},
                     {"agent_id": "bot_alpha", "latency_ms": 1, "success": True, "input": 5}):
            self.assertEqual(self.h.call("POST", "/v1/telemetry", body, key=ag["agent_key"])[0], 422, body)
        bad = {"type": "json_schema", "schema": {"type": "object", "oneOf": []}}
        s, out = self.h.call("POST", "/v1/agents", {"agent_id": "x", "config": {"system_instruction": "s"}, "contract": bad},
                             key=a["owner_key"])
        self.assertEqual(s, 422)
        self.assertIn("oneOf", out["detail"])  # unsupported keywords are refused, not silently ignored


class Verdicts(Base):
    def setUp(self):
        super().setUp()
        self.a = self.h.account("alice")
        self.ag = self.h.agent(self.a["owner_key"])

    def test_claimed_success_is_checked_against_the_contract(self):
        out = self.h.trace(self.ag["agent_key"], "tricky one", output="Sorry, I can't help.", success=True)
        self.assertEqual((out["verdict"], out["verdict_source"]), ("fail", "contract"))
        out = self.h.trace(self.ag["agent_key"], "bob")
        self.assertEqual((out["verdict"], out["verdict_source"]), ("pass", "contract"))
        out = self.h.trace(self.ag["agent_key"], "x", output="", success=False, error="timeout")
        self.assertEqual((out["verdict"], out["verdict_source"]), ("fail", "self_fail"))
        cases = self.h.call("GET", "/v1/versions/bot_alpha", key=self.a["owner_key"])[1]["cases"]
        self.assertEqual(cases["failure"]["total"], 2)
        self.assertEqual(cases["guard"]["total"], 1)

    def test_feedback_is_independent_and_single_use(self):
        t = self.h.trace(self.ag["agent_key"], "carol", output=json.dumps({"name": "Karol"}))
        self.assertEqual(t["verdict"], "pass")  # schema-valid, but wrong
        s, _ = self.h.call("POST", "/v1/feedback", {"feedback_token": t["feedback_token"], "verdict": "pass"},
                           key=self.ag["agent_key"])
        self.assertEqual(s, 403)  # an agent can't grade itself
        consumer = self.h.account("consumer")
        s, out = self.h.call("POST", "/v1/feedback", {"feedback_token": t["feedback_token"], "verdict": "fail",
                                                      "expected": {"name": "carol"}}, key=consumer["owner_key"])
        self.assertEqual((s, out["source"], out["case"]), (200, "consumer", "failure"))
        self.assertEqual(self.h.call("POST", "/v1/feedback", {"feedback_token": t["feedback_token"], "verdict": "pass"},
                                     key=consumer["owner_key"])[0], 404)


class Market(Base):
    """The whole loop: failures -> auto bounty under a mandate -> sealed submissions -> automatic
    settlement -> canary -> warranty. No human approves any individual payment or deployment."""

    POLICY = {"auto_bounty": {"amount": 100_000, "threshold": 0.5, "warranty_bps": 3000, "duration_s": 3600,
                              "warranty_s": 86400, "min_failures": 4},
              "auto_promote": True, "canary": {"fraction": 0.5, "min_samples": 40, "duration_s": 86400}}

    def setUp(self):
        super().setUp()
        h = self.h
        self.owner = h.account("owner", grant=1_000_000)
        self.agent = h.agent(self.owner["owner_key"], policy=self.POLICY)
        for i in range(8):
            h.trace(self.agent["agent_key"], f"tricky {i}", output="Sorry, I can't help with that.")
        for i in range(6):
            h.trace(self.agent["agent_key"], f"person {i}")

    def open_bounty(self):
        h = self.h
        h.app.shop.tick()
        ev = h.call("GET", "/v1/agents/bot_alpha/events", key=self.owner["owner_key"])[1]["events"]
        self.assertEqual(ev[-1]["kind"], "auto_bounty_blocked")
        self.assertIn("No active mandate", ev[-1]["detail"]["reason"])
        s, m = h.call("POST", "/v1/mandates", {"purpose": "bounty", "agent_id": "bot_alpha", "max_per_tx": 200_000,
                                               "max_per_day": 500_000}, key=self.owner["owner_key"])
        self.assertEqual(s, 200, m)
        h.app.shop.tick()
        b = h.wait(lambda: [x for x in h.call("GET", "/v1/bounties")[1]["bounties"] if x["agent_id"] == "bot_alpha"])[0]
        self.assertEqual(b["status"], "OPEN")
        return b["id"], m

    def repairer(self, name, grant=50_000):
        r = self.h.account(name, grant=grant)
        s, k = self.h.call("POST", "/v1/keys", {"label": "repair bot"}, key=r["owner_key"])
        self.assertEqual(s, 200)
        return r, k["key"]

    def submit(self, key, bid, si):
        return self.h.call("POST", f"/v1/bounties/{bid}/submissions", {"config": {"system_instruction": si, "model": "fake"}},
                           key=key)

    def evaluated(self, key, sid):
        return self.h.wait(lambda: (lambda o: o["status"] != "EVALUATING" and o)(
            self.h.call("GET", f"/v1/submissions/{sid}", key=key)[1]))

    def test_full_loop(self):
        h = self.h
        before = h.balance(self.owner["owner_key"])
        bid, mandate = self.open_bounty()
        s, view = h.call("GET", f"/v1/bounties/{bid}", key=self.owner["owner_key"])
        n_cases = len(view["visible_cases"]) + view["hidden_case_count"]
        self.assertTrue(view["visible_cases"] and view["hidden_case_count"])
        self.assertNotIn("seed", view)
        baseline_fee = n_cases * 10
        self.assertEqual(h.balance(self.owner["owner_key"]), before - 100_000 - baseline_fee)
        self.assertEqual(view["baseline_config"]["system_instruction"], "Extract the name as JSON.")

        # Repair bots pay the referee from their own balance, under their own mandate.
        r1, bot1 = self.repairer("fixer")
        s, out = self.submit(bot1, bid, "Extract the name as JSON. HANDLE_TRICKY")
        self.assertEqual(s, 403)  # a bot key with no mandate can't spend
        h.call("POST", "/v1/mandates", {"purpose": "eval_fee", "max_per_tx": 1000, "max_per_day": 5000}, key=r1["owner_key"])
        s, good_sub = self.submit(bot1, bid, "Extract the name as JSON. HANDLE_TRICKY")
        self.assertEqual((s, good_sub["fee"]), (202, n_cases * 10))
        r2, bot2 = self.repairer("cheater")
        h.call("POST", "/v1/mandates", {"purpose": "any", "max_per_tx": 1000, "max_per_day": 5000}, key=r2["owner_key"])
        s, cheat_sub = self.submit(bot2, bid, "HANDLE_TRICKY BREAK_NORMAL")
        self.assertEqual(s, 202)
        self.assertEqual(self.submit(self.owner["owner_key"], bid, "x")[0], 403)  # the owner can't bid on its own bounty

        vis = self.evaluated(bot1, good_sub["submission_id"])["visible_result"]
        self.assertEqual(vis["passed"], vis["total"])  # the good fix passes everything it can see
        self.evaluated(bot2, cheat_sub["submission_id"])
        # Sealed: the owner can't read either submission before settlement.
        self.assertEqual(h.call("GET", f"/v1/submissions/{good_sub['submission_id']}", key=self.owner["owner_key"])[0], 404)
        self.assertNotIn("HANDLE_TRICKY", json.dumps(h.call("GET", f"/v1/bounties/{bid}", key=self.owner["owner_key"])[1]))

        h.clock.advance(3601)
        h.app.shop.tick()
        s, view = h.call("GET", f"/v1/bounties/{bid}", key=self.owner["owner_key"])
        self.assertEqual(view["status"], "SETTLED")
        res = view["result"]
        w = res["winner"]
        self.assertEqual(w["submission_id"], good_sub["submission_id"])
        cheat = next(x for x in res["scores"] if x["submission_id"] == cheat_sub["submission_id"])
        self.assertGreater(cheat["regressions"], 0)
        self.assertFalse(cheat["eligible"])  # fixed failures but broke guards: paid nothing

        # The award follows the committed formula exactly.
        award = 100_000 * min(w["fixed"], res["required_fixes"]) // res["required_fixes"]
        take = award * 1000 // 10000
        warranty = (award - take) * 3000 // 10000
        self.assertEqual((w["award"], w["take"], w["warranty"], w["paid_now"], w["refund"]),
                         (award, take, warranty, award - take - warranty, 100_000 - award))
        self.assertEqual(h.balance(r1["owner_key"]), 50_000 - n_cases * 10 + w["paid_now"])
        self.assertTrue(h.ledger_ok())

        # Anyone can check the referee: recompute the commitment and the split from the reveal.
        cases = sorted([{"case_id": c["case_id"], "role": c["role"], "split": "visible", "content_hash": c["content_hash"]}
                        for c in view["visible_cases"]] +
                       [{"case_id": c["case_id"], "role": c["role"], "split": "hidden", "content_hash": c["content_hash"]}
                        for c in view["hidden_cases"]], key=lambda x: x["case_id"])
        for c in view["visible_cases"]:
            self.assertEqual(c["content_hash"], hashlib.sha256(canonical({"input": c["input"], "grader": c["grader"]}).encode()).hexdigest())
        recomputed = hashlib.sha256(canonical({"terms": view["terms"], "cases": cases, "seed": view["seed"]}).encode()).hexdigest()
        self.assertEqual(recomputed, view["commitment"])
        for role in ("failure", "guard"):
            group = sorted([c for c in cases if c["role"] == role],
                           key=lambda c: hashlib.sha256(f"{view['seed']}:{c['case_id']}".encode()).hexdigest())
            n_vis = min(len(group) - 1, math.ceil(len(group) * 0.5))
            self.assertEqual([c["split"] for c in group], ["visible"] * n_vis + ["hidden"] * (len(group) - n_vis))

        # Now the owner can see the fix: it's staged and running as a canary on half the sessions.
        versions = h.call("GET", "/v1/versions/bot_alpha", key=self.owner["owner_key"])[1]
        self.assertEqual(versions["rollouts"][-1]["status"], "RUNNING")
        self.assertIn("HANDLE_TRICKY", next(v for v in versions["versions"] if v["version"] == w["version"])["config"]["system_instruction"])
        self.assertGreater(versions["cases"]["regression"]["total"], 0)  # fixed failures are permanent tests now

        arms = {"candidate": 0, "baseline": 0}
        for i in range(400):
            sess = f"s{i}"
            cfg = h.call("GET", f"/v1/config/bot_alpha?session={sess}", key=self.agent["agent_key"])[1]
            arms[cfg["rollout"]["arm"]] += 1
            text = f"tricky live {i}" if i % 4 == 0 else f"live {i}"
            out = fake_model(cfg["config"], text)
            ok = out.startswith("{")
            h.trace(self.agent["agent_key"], text, output=out, success=True, version=cfg["version"], session=sess)
        self.assertTrue(120 < arms["candidate"] < 280, arms)  # sticky hash routing at ~50%

        h.app.shop.tick()
        versions = h.call("GET", "/v1/versions/bot_alpha", key=self.owner["owner_key"])[1]
        self.assertEqual(versions["rollouts"][-1]["status"], "PROMOTED", versions["rollouts"][-1])
        self.assertEqual(h.call("GET", "/v1/config/bot_alpha", key=self.agent["agent_key"])[1]["version"], w["version"])
        h.app.shop.tick()
        self.assertEqual(h.call("GET", f"/v1/bounties/{bid}", key=r1["owner_key"])[1]["warranty"]["state"], "RELEASED")
        self.assertEqual(h.balance(r1["owner_key"]), 50_000 - n_cases * 10 + w["paid_now"] + w["warranty"])
        rep = h.call("GET", f"/v1/accounts/{r1['account_id']}/reputation")[1]["as_repairer"]
        self.assertEqual((rep["wins"], rep["warranties_released"]), (1, 1))
        self.assertTrue(h.ledger_ok())

    def canary_with(self, fix):
        """Wins a bounty with `fix`, then runs live traffic through the canary: each arm answers
        with what its config really produces, except that `forge` makes the owner's agent report
        failures on the candidate arm regardless."""
        h = self.h
        bid, _ = self.open_bounty()
        r1, bot1 = self.repairer("fixer")
        h.call("POST", "/v1/mandates", {"purpose": "eval_fee", "max_per_tx": 1000, "max_per_day": 5000}, key=r1["owner_key"])
        s, sub = self.submit(bot1, bid, fix)
        self.evaluated(bot1, sub["submission_id"])
        h.clock.advance(3601)
        h.app.shop.tick()
        w = h.call("GET", f"/v1/bounties/{bid}", key=r1["owner_key"])[1]["result"]["winner"]
        # Stop the agent re-posting a bounty for the live failures, so balance changes below are
        # only the warranty.
        h.call("POST", "/v1/agents/bot_alpha", {"policy": {"auto_bounty": None}}, key=self.owner["owner_key"])
        return bid, r1, w

    def live_traffic(self, forge):
        h = self.h
        for i in range(120):
            sess = f"s{i}"
            cfg = h.call("GET", f"/v1/config/bot_alpha?session={sess}", key=self.agent["agent_key"])[1]
            out = fake_model(cfg["config"], f"live {i}")
            if forge and cfg["rollout"]["arm"] == "candidate":
                out = "{bad"
            h.trace(self.agent["agent_key"], f"live {i}", output=out, success=True, version=cfg["version"], session=sess)

    def settle_warranty(self, bid, key):
        self.h.app.shop.tick()  # canary decides; the warranty claim is queued for the referee
        return self.h.wait(lambda: (lambda v: v["warranty"]["state"] in ("REFUNDED", "RELEASED") and v)(
            self.h.call("GET", f"/v1/bounties/{bid}", key=key)[1]))

    def test_fix_that_is_really_worse_in_production_refunds_the_warranty(self):
        h = self.h
        bid, r1, w = self.canary_with("HANDLE_TRICKY BREAK_LIVE")  # passes every test case
        owner_before, repairer_before = h.balance(self.owner["owner_key"]), h.balance(r1["owner_key"])
        self.live_traffic(forge=False)
        self.settle_warranty(bid, r1["owner_key"])
        versions = h.call("GET", "/v1/versions/bot_alpha", key=self.owner["owner_key"])[1]
        self.assertEqual(versions["rollouts"][-1]["status"], "ROLLED_BACK")
        self.assertNotEqual(h.call("GET", "/v1/config/bot_alpha", key=self.agent["agent_key"])[1]["version"], w["version"])
        ev = [e for e in h.call("GET", "/v1/agents/bot_alpha/events", key=self.owner["owner_key"])[1]["events"]
              if e["kind"].startswith("warranty_")][-1]
        self.assertEqual(ev["kind"], "warranty_refunded")
        self.assertGreater(ev["detail"]["reproduced"], 0)
        self.assertEqual(h.balance(self.owner["owner_key"]), owner_before + ev["detail"]["amount"])
        self.assertEqual(ev["detail"]["amount"] + ev["detail"]["referee_fee"], w["warranty"])
        self.assertEqual(h.balance(r1["owner_key"]), repairer_before)
        self.assertTrue(h.ledger_ok())

    def test_forged_production_failures_do_not_claw_back_the_warranty(self):
        h = self.h
        bid, r1, w = self.canary_with("Extract the name as JSON. HANDLE_TRICKY")  # a genuinely good fix
        repairer_before = h.balance(r1["owner_key"])
        self.live_traffic(forge=True)   # the owner's agent reports failures the fix never produced
        self.settle_warranty(bid, r1["owner_key"])
        # Deployment safety still wins: the canary is rolled back on the reported numbers...
        versions = h.call("GET", "/v1/versions/bot_alpha", key=self.owner["owner_key"])[1]
        self.assertEqual(versions["rollouts"][-1]["status"], "ROLLED_BACK")
        # ...but the money follows reproducible evidence, and there is none.
        ev = [e for e in h.call("GET", "/v1/agents/bot_alpha/events", key=self.owner["owner_key"])[1]["events"]
              if e["kind"].startswith("warranty_")][-1]
        self.assertEqual((ev["kind"], ev["detail"]["reproduced"]), ("warranty_released", 0))
        self.assertEqual(h.balance(r1["owner_key"]), repairer_before + ev["detail"]["amount"])
        self.assertTrue(h.ledger_ok())

    def test_reference_repair_bot_wins_using_only_visible_feedback(self):
        h = self.h
        bid, _ = self.open_bounty()
        r1, bot = self.repairer("repair-bot")
        h.call("POST", "/v1/mandates", {"purpose": "eval_fee", "max_per_tx": 1000, "max_per_day": 5000}, key=r1["owner_key"])
        jobs = []

        def propose(job):
            # First try misses; the second reads which visible inputs still fail and handles them.
            jobs.append(job)
            return job["instruction"] + (" HANDLE_TRICKY" if "still failed" in job["prompt"] else " Be careful.")

        api = repair_bot.Api(f"http://127.0.0.1:{h.api.port}", bot)
        repair_bot.run_once(api, propose, set(), log=lambda *_: None)
        self.assertEqual(len(jobs), 2)
        self.assertIn("tricky", jobs[0]["prompt"])          # it was shown the visible failures...
        self.assertIn("Extract the name as JSON.", jobs[0]["prompt"])
        h.clock.advance(3601)
        h.app.shop.tick()
        view = h.call("GET", f"/v1/bounties/{bid}", key=r1["owner_key"])[1]
        self.assertEqual(view["result"]["winner"]["account_id"], r1["account_id"])

    def test_no_winner_refunds_everything_and_returns_hidden_cases(self):
        h = self.h
        before = h.balance(self.owner["owner_key"])
        bid, _ = self.open_bounty()
        baseline_fee = before - h.balance(self.owner["owner_key"]) - 100_000
        r1, _ = self.repairer("fixer")
        s, sub = self.submit(r1["owner_key"], bid, "no real change")  # owner keys spend without a mandate
        self.assertEqual(s, 202)
        self.evaluated(r1["owner_key"], sub["submission_id"])
        h.clock.advance(3601)
        h.app.shop.tick()
        view = h.call("GET", f"/v1/bounties/{bid}", key=self.owner["owner_key"])[1]
        self.assertEqual(view["status"], "NO_WINNER")
        self.assertEqual(h.balance(self.owner["owner_key"]), before - baseline_fee)  # reward back; baseline fee spent
        h.app.shop.tick()  # the unsolved failures went back to the pool, but the agent cools down before re-posting
        self.assertEqual(h.call("GET", "/v1/agents/bot_alpha/events", key=self.owner["owner_key"])[1]["events"][-1]["kind"],
                         "bounty_no_winner")
        cases = h.call("GET", "/v1/versions/bot_alpha", key=self.owner["owner_key"])[1]["cases"]["failure"]
        self.assertEqual(cases["unassigned"], view["hidden_case_count"] - sum(1 for c in view["hidden_cases"] if c["role"] != "failure"))
        self.assertTrue(h.ledger_ok())

    def test_submission_limits_and_deadline(self):
        h = self.h
        bid, _ = self.open_bounty()
        r1, _ = self.repairer("fixer")
        for _ in range(3):
            self.assertEqual(self.submit(r1["owner_key"], bid, "try")[0], 202)
        self.assertEqual(self.submit(r1["owner_key"], bid, "try")[0], 429)
        h.clock.advance(3601)
        r2, _ = self.repairer("late")
        self.assertEqual(self.submit(r2["owner_key"], bid, "try")[0], 409)


class Mandates(Base):
    def setUp(self):
        super().setUp()
        self.owner = self.h.account("owner", grant=1_000_000)
        self.ag = self.h.agent(self.owner["owner_key"])
        for i in range(3):
            self.h.trace(self.ag["agent_key"], f"tricky {i}", output="nope")

    def post(self, amount):
        return self.h.call("POST", "/v1/bounties", {"agent_id": "bot_alpha", "amount": amount, "duration_s": 600},
                           key=self.ag["agent_key"])

    def mandate(self, **kw):
        body = {"purpose": "bounty", "agent_id": "bot_alpha", "max_per_tx": 10_000, "max_per_day": 15_000, **kw}
        return self.h.call("POST", "/v1/mandates", body, key=self.owner["owner_key"])[1]

    def test_limits_are_enforced_and_explained(self):
        m = self.mandate()
        s, out = self.post(20_000)
        self.assertEqual(s, 403)
        self.assertIn("exceeds max_per_tx", out["detail"])
        s, b = self.post(9_000)  # the agent pays for its own repair, no human asked
        self.assertEqual((s, b["mandate_id"]), (201, m["id"]))
        self.h.wait(lambda: self.h.call("GET", f"/v1/bounties/{b['bounty_id']}", key=self.owner["owner_key"])[1]["status"] == "OPEN")
        self.h.clock.advance(601)
        self.h.app.shop.tick()  # no submissions: settles as NO_WINNER, cases return to the pool
        s, out = self.post(9_000)
        self.assertEqual(s, 403)
        self.assertIn("max_per_day", out["detail"])  # 9,000 + fee already spent today; another 9,000 won't fit
        self.h.clock.advance(86401)
        self.assertEqual(self.h.call("POST", f"/v1/mandates/{m['id']}/revoke", key=self.owner["owner_key"])[0], 200)
        self.assertEqual(self.post(1_000)[0], 403)

    def test_expiry_and_bot_key_scope(self):
        self.mandate(ttl_s=60)
        self.h.clock.advance(61)
        self.assertEqual(self.post(1_000)[0], 403)
        self.assertEqual(self.h.call("POST", "/v1/mandates", {"purpose": "any", "max_per_tx": 1, "max_per_day": 1},
                                     key=self.ag["agent_key"])[0], 403)  # bots can't write their own mandates

    def test_insufficient_balance(self):
        poor = self.h.account("poor", grant=100)
        self.h.agent(poor["owner_key"], "poor_bot")
        self.h.trace(self.ag["agent_key"], "tricky x", output="nope")
        s, out = self.h.call("POST", "/v1/bounties", {"agent_id": "bot_alpha", "amount": 10**9}, key=self.owner["owner_key"])
        self.assertEqual(s, 402)


class RefereeFailure(unittest.TestCase):
    def test_broken_runner_refunds_fees(self):
        state = {"broken": False}

        def runner(config, text):
            if state["broken"]:
                raise RuntimeError("model API down")
            return fake_model(config, text)

        h = Harness(runner=runner)
        try:
            owner = h.account("owner", grant=1_000_000)
            ag = h.agent(owner["owner_key"])
            for i in range(3):
                h.trace(ag["agent_key"], f"tricky {i}", output="nope")
            s, b = h.call("POST", "/v1/bounties", {"agent_id": "bot_alpha", "amount": 1000}, key=owner["owner_key"])
            h.wait(lambda: h.call("GET", f"/v1/bounties/{b['bounty_id']}", key=owner["owner_key"])[1]["status"] == "OPEN")
            state["broken"] = True
            r = h.account("fixer", grant=10_000)
            s, sub = h.call("POST", f"/v1/bounties/{b['bounty_id']}/submissions",
                            {"config": {"system_instruction": "HANDLE_TRICKY"}}, key=r["owner_key"])
            out = h.wait(lambda: (lambda o: o["status"] != "EVALUATING" and o)(
                h.call("GET", f"/v1/submissions/{sub['submission_id']}", key=r["owner_key"])[1]))
            self.assertEqual(out["status"], "FAILED")
            self.assertEqual(h.balance(r["owner_key"]), 10_000)
            self.assertTrue(h.ledger_ok())
        finally:
            h.close()

    def test_no_runner_means_no_bounties(self):
        h = Harness(runner=None)
        try:
            owner = h.account("owner", grant=1000)
            h.agent(owner["owner_key"])
            s, out = h.call("POST", "/v1/bounties", {"agent_id": "bot_alpha", "amount": 100}, key=owner["owner_key"])
            self.assertEqual(s, 503)
        finally:
            h.close()


class X402(Base):
    env = {"ABS_REAL_MONEY": "1", "ABS_X402_FACILITATOR_URL": "https://facilitator.test", "ABS_X402_PAY_TO": "0xShop"}

    def setUp(self):
        super().setUp()
        self.calls = []

        def facilitator(url, body):
            self.calls.append((url, body))
            sig = body["paymentPayload"]["payload"]["signature"]
            if url.endswith("/verify"):
                return {"isValid": sig != "bad", "invalidReason": "invalid_signature", "payer": "0xBot"}
            return {"success": True, "transaction": "0xtx-" + sig, "network": body["paymentRequirements"]["network"],
                    "payer": "0xBot"}

        self.h.app.payments.post = facilitator

    def pay(self, key, amount, accepted, sig="0xsig"):
        payload = {"x402Version": 2, "accepted": accepted, "payload": {"signature": sig, "authorization": {}}}
        hdr = base64.b64encode(json.dumps(payload).encode()).decode()
        return self.h.call("POST", "/v1/deposits/x402", {"amount": amount}, key=key, headers={"PAYMENT-SIGNATURE": hdr}, raw=True)

    def test_bot_tops_up_over_http_402(self):
        acct = self.h.account("bot")
        s, k = self.h.call("POST", "/v1/keys", {}, key=acct["owner_key"])
        bot = k["key"]
        s, body, headers = self.h.call("POST", "/v1/deposits/x402", {"amount": 5_000_000}, key=bot, raw=True)
        self.assertEqual(s, 402)
        required = json.loads(base64.b64decode(headers["PAYMENT-REQUIRED"]))
        self.assertEqual(required, body)
        req = required["accepts"][0]
        self.assertEqual((required["x402Version"], req["scheme"], req["amount"], req["payTo"]), (2, "exact", "5000000", "0xShop"))
        s, out, _ = self.pay(bot, 5_000_000, dict(req, amount="1"))
        self.assertEqual(s, 402)  # signed for different terms: refused before touching the facilitator
        self.assertEqual(self.calls, [])
        s, out, _ = self.pay(bot, 5_000_000, req, sig="bad")
        self.assertEqual(s, 402)
        s, out, headers = self.pay(bot, 5_000_000, req)
        self.assertEqual((s, out["status"], out["balance"]), (200, "CREDITED", 5_000_000))
        self.assertTrue(json.loads(base64.b64decode(headers["PAYMENT-RESPONSE"]))["success"])
        self.assertEqual(self.pay(bot, 5_000_000, req)[0], 409)  # same on-chain payment can't credit twice
        self.assertEqual(self.h.balance(bot), 5_000_000)
        self.assertTrue(self.h.ledger_ok())

    def test_real_money_barrier_and_withdrawals(self):
        acct = self.h.account("bot")
        s, _ = self.h.call("POST", "/v1/admin/grants", {"account_id": acct["account_id"], "amount": 5}, admin=True)
        self.assertEqual(s, 403)  # no free money in real-money mode
        req = self.h.app.payments.requirements(1000)
        self.pay(acct["owner_key"], 1000, req)
        s, k = self.h.call("POST", "/v1/keys", {}, key=acct["owner_key"])
        self.assertEqual(self.h.call("POST", "/v1/withdrawals", {"amount": 10, "destination": "0xme"}, key=k["key"])[0], 403)
        s, w = self.h.call("POST", "/v1/withdrawals", {"amount": 400, "destination": "0xme"}, key=acct["owner_key"])
        self.assertEqual((s, w["status"]), (202, "PENDING"))
        self.assertEqual(self.h.balance(acct["owner_key"]), 600)
        self.h.call("POST", f"/v1/admin/withdrawals/{w['withdrawal_id']}", {"status": "FAILED"}, admin=True)
        self.assertEqual(self.h.balance(acct["owner_key"]), 1000)
        self.assertTrue(self.h.ledger_ok())


class PlayMoney(Base):
    def test_play_credits_cannot_leave(self):
        acct = self.h.account("a", grant=1000)
        s, out = self.h.call("POST", "/v1/withdrawals", {"amount": 10, "destination": "0x"}, key=acct["owner_key"])
        self.assertEqual(s, 403)
        s, out = self.h.call("POST", "/v1/deposits/x402", {"amount": 10}, key=acct["owner_key"])
        self.assertEqual(s, 403)


class RateLimits(Base):
    env = {"ABS_SIGNUPS_PER_HOUR": "2"}

    def test_signup_limit(self):
        self.assertEqual(self.h.call("POST", "/v1/accounts", {"name": "a"})[0], 201)
        self.assertEqual(self.h.call("POST", "/v1/accounts", {"name": "b"})[0], 201)
        s, out, headers = self.h.call("POST", "/v1/accounts", {"name": "c"}, raw=True)
        self.assertEqual(s, 429)
        self.assertIn("Retry-After", headers)


class ClientFailStatic(unittest.TestCase):
    def test_agent_keeps_running_when_the_shop_is_down(self):
        h = Harness()
        owner = h.account("a")
        ag = h.agent(owner["owner_key"])
        c = AgentClient(f"http://127.0.0.1:{h.api.port}", ag["agent_key"], "bot_alpha", cache_dir=tempfile.mkdtemp())
        cfg = c.get_config()
        self.assertEqual(cfg["version"], ag["version"])
        self.assertEqual(c.report_sync(cfg, latency_ms=3, success=True, input="x", output=good("x"))["verdict"], "pass")
        self.assertEqual(c.get_config()["version"], ag["version"])  # 304 path
        h.close()
        self.assertEqual(c.get_config()["version"], ag["version"])  # shop gone: cached config
        c.report(cfg, latency_ms=1, success=True)                   # never raises
        self.assertIsNotNone(c.last_error)


class Process(unittest.TestCase):
    def launch(self, db, port, **env):
        e = dict(os.environ, ABS_DB=db, ABS_PORT=str(port), ABS_HOST="127.0.0.1", ABS_ADMIN_KEY=ADMIN, ABS_TICK_S="0.2", **env)
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], env=e,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        api = Api(port)

        def up():
            try:
                return api.call("GET", "/health")[0] == 200
            except OSError:
                return False
        Harness.wait(None, up)
        return p, api

    def test_command_runner_settlement_and_crash_recovery(self):
        db = os.path.join(tempfile.mkdtemp(), "abs.db")
        script = os.path.join(tempfile.mkdtemp(), "runner.py")
        with open(script, "w") as f:
            f.write("import json,sys\nj=json.load(sys.stdin)\nsi=j['config']['system_instruction'];t=j['input']\n"
                    "print('Sorry' if t.startswith('tricky') and 'HANDLE_TRICKY' not in si else json.dumps({'name':t}))\n")
        env = {"ABS_RUNNER": "command", "ABS_RUNNER_CMD": f"{sys.executable} {script}", "ABS_FEE_PER_RUN": "1"}
        p, api = self.launch(db, 8193, **env)
        try:
            owner = api.call("POST", "/v1/accounts", {"name": "o"})[1]
            api.call("POST", "/v1/admin/grants", {"account_id": owner["account_id"], "amount": 10_000}, admin=True)
            ag = api.call("POST", "/v1/agents", {"agent_id": "a1", "config": {"system_instruction": "base"}, "contract": CONTRACT},
                          key=owner["owner_key"])[1]
            for i in range(4):
                api.call("POST", "/v1/telemetry", {"agent_id": "a1", "latency_ms": 1, "success": True, "input": f"tricky {i}",
                                                   "output": "Sorry"}, key=ag["agent_key"])
            s, b = api.call("POST", "/v1/bounties", {"agent_id": "a1", "amount": 1000, "duration_s": 60}, key=owner["owner_key"])
            self.assertEqual(s, 201, b)
            Harness.wait(None, lambda: api.call("GET", f"/v1/bounties/{b['bounty_id']}", key=owner["owner_key"])[1]["status"] == "OPEN")
            r = api.call("POST", "/v1/accounts", {"name": "r"})[1]
            api.call("POST", "/v1/admin/grants", {"account_id": r["account_id"], "amount": 1000}, admin=True)
            s, sub = api.call("POST", f"/v1/bounties/{b['bounty_id']}/submissions",
                              {"config": {"system_instruction": "base HANDLE_TRICKY"}}, key=r["owner_key"])
            Harness.wait(None, lambda: api.call("GET", f"/v1/submissions/{sub['submission_id']}", key=r["owner_key"])[1]["status"] == "EVALUATED")
        finally:
            p.send_signal(signal.SIGKILL)
            p.wait()
        # Restart: everything is still there, including the open bounty and the sealed submission.
        p, api = self.launch(db, 8194, **env)
        try:
            view = api.call("GET", f"/v1/bounties/{b['bounty_id']}", key=owner["owner_key"])[1]
            self.assertEqual(view["status"], "OPEN")
            me = api.call("GET", "/v1/accounts/me", key=r["owner_key"])[1]
            self.assertEqual(me["balance"], 1000 - sub["fee"])
            self.assertEqual(api.call("GET", "/health")[1]["ledger_balanced"], True)
        finally:
            p.send_signal(signal.SIGKILL)
            p.wait()

    def test_production_requires_admin_key(self):
        e = dict(os.environ, ABS_ENV="production", ABS_DB=os.path.join(tempfile.mkdtemp(), "x.db"))
        e.pop("ABS_ADMIN_KEY", None)
        p = subprocess.run([sys.executable, os.path.join(HERE, "server.py")], env=e, capture_output=True, timeout=10)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(b"ABS_ADMIN_KEY", p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
