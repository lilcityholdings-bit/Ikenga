#!/usr/bin/env python3
"""Tests for the Ikenga Clear demo.   python3 clear/test_clear.py"""
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from clear import Agent, ClearError, Meter, Service, make_server  # noqa: E402


def sample_meter():
    m = Meter()
    m.add_service(Service("s", "Svc", "Owner", "acct_TEST", {"a": 2, "b": 5}))
    m.add_agent(Agent("x1", "X one", "Acme"))
    m.add_agent(Agent("x2", "X two", "Acme"))
    m.add_agent(Agent("y1", "Y one", "Beta"))
    return m


class MeterTests(unittest.TestCase):
    def test_a_retry_with_the_same_ref_is_counted_once(self):
        m = sample_meter()
        _, first = m.record("s", "x1", "a", "r1")
        _, again = m.record("s", "x1", "a", "r1")
        self.assertTrue(first)
        self.assertFalse(again)
        self.assertEqual(m.summary("s")["calls"], 1)

    def test_unknown_tool_agent_and_bad_units_are_rejected(self):
        m = sample_meter()
        for args in [("s", "x1", "zzz", "r"), ("s", "nobody", "a", "r"), ("s", "x1", "a", "")]:
            with self.assertRaises(ClearError):
                m.record(*args)
        for units in (0, -3, 1.5, True):
            with self.assertRaises(ClearError):
                m.record("s", "x1", "a", "r", units=units)

    def test_one_bill_per_agent_owner_and_the_totals_add_up(self):
        m = sample_meter()
        for i in range(3):
            m.record("s", "x1", "a", f"x1-{i}")
        m.record("s", "x2", "b", "x2-0", units=2)
        m.record("s", "y1", "a", "y1-0")
        bills = {b["bill_to"]: b for b in m.bills("s")}
        self.assertEqual(set(bills), {"Acme", "Beta"})
        self.assertEqual(bills["Acme"]["total_cents"], 3 * 2 + 2 * 5)
        self.assertEqual(bills["Beta"]["total_cents"], 2)
        self.assertEqual(bills["Acme"]["pay_to_stripe_account"], "acct_TEST")
        self.assertTrue(all("test mode" in b["status"] for b in bills.values()))
        self.assertEqual(m.summary("s")["ikenga_fee_if_over_plan_cents"], 2)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.m = sample_meter()
        self.srv = make_server(self.m, {"k_test": "s"}, port=0)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()

    def call(self, method, path, body=None, key="k_test"):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {key}"} if key else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_usage_needs_the_service_key(self):
        self.assertEqual(self.call("POST", "/v1/usage", {"agent_id": "x1", "tool": "a", "ref": "1"}, key=None)[0], 401)
        self.assertEqual(self.call("POST", "/v1/usage", {"agent_id": "x1", "tool": "a", "ref": "1"}, key="wrong")[0], 401)
        self.assertEqual(self.call("GET", "/v1/bills", key=None)[0], 401)

    def test_report_then_read_the_bills(self):
        self.assertEqual(self.call("POST", "/v1/usage", {"agent_id": "x1", "tool": "b", "ref": "1"})[0], 201)
        self.assertEqual(self.call("POST", "/v1/usage", {"agent_id": "x1", "tool": "b", "ref": "1"})[0], 200)
        status, body = self.call("GET", "/v1/bills")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "test")
        self.assertEqual(body["summary"]["billed_cents"], 5)


class ModeTests(unittest.TestCase):
    def run_demo(self, **env):
        return subprocess.run([sys.executable, os.path.join(HERE, "demo.py")], capture_output=True, text=True,
                              env={**os.environ, "CLEAR_PORT": "8410", "CLEAR_DEMO_SERVICE_PORT": "8411", **env})

    def test_live_mode_and_live_stripe_keys_are_refused(self):
        self.assertNotEqual(self.run_demo(CLEAR_MODE="live").returncode, 0)
        self.assertNotEqual(self.run_demo(STRIPE_SECRET_KEY="sk_live_x").returncode, 0)

    def test_the_demo_counts_every_call_once_despite_retries(self):
        r = self.run_demo()
        self.assertEqual(r.returncode, 0, r.stderr)
        line = [l for l in r.stdout.splitlines() if l.startswith("Agents made")][0]
        made = int(line.split()[2])
        counted = int(line.rstrip(".").split()[-1])
        self.assertEqual(made, counted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
