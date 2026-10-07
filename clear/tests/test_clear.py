#!/usr/bin/env python3
"""Keptvow Clear tests.   python3 clear/tests/test_clear.py"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from keptvow_clear import build, tokens  # noqa: E402
from keptvow_clear.billing import DAY_MS, Denied, split_cents  # noqa: E402
from keptvow_clear.config import Config  # noqa: E402
from keptvow_clear.fakes import fake_keptvow, fake_stripe  # noqa: E402
from keptvow_clear.server import make_server  # noqa: E402
from keptvow_clear.store import Store, now_ms, period_of  # noqa: E402

ADMIN = "test-admin"


def make_cfg(**over):
    base = dict(mode="test", db_path=":memory:", admin_secret=ADMIN, token_secret=b"k" * 32, stripe_key="",
                stripe_api="", keptvow_url="http://127.0.0.1:9", keptvow_source="clear", keptvow_source_secret="",
                days_until_due=14, grace_days=30, included_settlements=0, sync_secs=0)
    base.update(over)
    return Config(**base)


class Base(unittest.TestCase):
    levels = {"kv-good": "good", "kv-bad": "caution"}

    def setUp(self):
        self.kv, kv_url = fake_keptvow(self.levels)
        self.stripe, st_url = fake_stripe()
        self.cfg = make_cfg(keptvow_url=kv_url, stripe_key="sk_test_x", stripe_api=st_url, keptvow_source_secret="s")
        self.c = build(self.cfg, Store(":memory:"))
        st = self.c.store
        self.sid, _ = st.create_service("Svc", "Owner Co", "o@x.test")
        st.update_service(self.sid, prices={"a": 20_000, "b": 4_000}, stripe_account="acct_TEST_1")
        self.oid, _ = st.create_owner("Acme", "ap@acme.test")
        self.aid, self.akey = st.create_agent(self.oid, "bot")
        self.svc = st.service(self.sid)

    def tearDown(self):
        self.kv.shutdown()
        self.stripe.shutdown()

    def pass_(self, aid=None, sid=None):
        return tokens.issue(self.cfg.token_secret, aid or self.aid, sid or self.sid)[0]

    def use(self, n, tool="a", aid=None, prefix="r"):
        p = self.pass_(aid)
        for i in range(n):
            self.c.record(self.svc, p, tool, f"{prefix}{i}")


class Passes(Base):
    def test_a_pass_for_one_service_is_refused_by_another(self):
        other, _ = self.c.store.create_service("Other", "X")
        self.c.store.update_service(other, prices={"a": 1})
        with self.assertRaises(Denied) as e:
            self.c.record(self.c.store.service(other), self.pass_(), "a", "r1")
        self.assertEqual(e.exception.code, "BAD_PASS")

    def test_expired_and_tampered_passes_are_refused(self):
        old = tokens.issue(self.cfg.token_secret, self.aid, self.sid, ttl=-1)[0]
        forged = tokens.issue(b"wrong" * 8, self.aid, self.sid)[0]
        for p in (old, forged, "junk", ""):
            with self.assertRaises(Denied):
                self.c.record(self.svc, p, "a", "r")


class Counting(Base):
    def test_a_retry_with_the_same_ref_is_counted_once(self):
        p = self.pass_()
        _, first = self.c.record(self.svc, p, "a", "same")
        _, again = self.c.record(self.svc, p, "a", "same")
        self.assertEqual((first, again), (True, False))
        self.assertEqual(self.c._spent(self.aid, self.c.open_period()), 20_000)

    def test_a_ref_from_another_agent_does_not_get_a_free_call(self):
        self.use(1, prefix="shared")
        other, _ = self.c.store.create_agent(self.oid, "other")
        self.assertTrue(self.c.check(self.svc, self.pass_(), "a", ref="shared0")["already_counted"])
        self.assertEqual(self.c.check(self.svc, self.pass_(other), "a", ref="shared0")["code"], "REF_TAKEN")

    def test_the_owner_cap_holds_even_if_the_service_skips_the_check(self):
        self.c.store.run("UPDATE agents SET monthly_cap=50000 WHERE id=?", self.aid)
        self.use(2)  # $0.04
        with self.assertRaises(Denied) as e:
            self.use(1, prefix="x")
        self.assertEqual(e.exception.code, "OWNER_LIMIT")

    def test_bad_units_and_unknown_tools_are_refused(self):
        for tool, units in (("zzz", 1), ("a", 0), ("a", 1.5), ("a", True), ("a", 10**9)):
            with self.assertRaises(Denied):
                self.c.record(self.svc, self.pass_(), tool, "r", units)


class Trust(Base):
    def agent(self, kv, verified):
        aid, _ = self.c.store.create_agent(self.oid, kv, keptvow_id=kv)
        if verified:
            self.c.store.run("UPDATE agents SET keptvow_verified=1 WHERE id=?", aid)
        return aid

    def test_a_flagged_agent_is_refused(self):
        r = self.c.check(self.svc, self.pass_(self.agent("kv-bad", True)), "a")
        self.assertEqual(r["code"], "TRUST_TOO_LOW")

    def test_claiming_someone_elses_good_record_gets_you_nothing(self):
        r = self.c.check(self.svc, self.pass_(self.agent("kv-good", False)), "a")
        self.assertEqual(r["trust_level"], "unknown")
        r = self.c.check(self.svc, self.pass_(self.agent("kv-good", True)), "a")
        self.assertEqual(r["trust_level"], "good")

    def test_agents_with_no_record_have_a_starting_limit(self):
        self.c.store.update_service(self.sid, policy={"unknown_monthly_cap": 40_000})
        self.svc = self.c.store.service(self.sid)
        self.use(2)
        self.assertEqual(self.c.check(self.svc, self.pass_(), "a")["code"], "NEW_AGENT_LIMIT")
        good = self.agent("kv-good", True)
        self.use(3, aid=good, prefix="g")  # a proven record isn't held to the starting limit

    def test_keptvow_down_means_stricter_not_looser(self):
        c = build(make_cfg(keptvow_url="http://127.0.0.1:9"), self.c.store)
        aid = self.agent("kv-good", True)
        self.assertEqual(c.check(self.svc, tokens.issue(c.cfg.token_secret, aid, self.sid)[0], "a")["trust_level"], "unknown")


class Bills(Base):
    def close(self):
        self.c.close_period(period_of(now_ms()), force=True)
        return self.c.bills(service_id=self.sid)

    def test_cents_split_adds_up(self):
        for parts in ([15_000] * 3, [4_999, 1], [123_456, 7, 99_999, 5_000]):
            self.assertEqual(sum(split_cents(parts)), (sum(parts) + 5_000) // 10_000)

    def test_one_bill_per_owner_and_the_invoice_matches_it(self):
        self.use(30)               # 30 x $0.02
        self.use(5, "b", prefix="b")  # 5 x $0.004
        [b] = self.close()
        self.assertEqual(b["total_cents"], 62)
        self.c.send_bills()
        b = self.c.bill(b["id"])
        self.assertEqual(b["status"], "sent")
        inv = self.stripe.state["invoices"][b["stripe_invoice_id"]]
        self.assertEqual((inv["amount_due"], inv["account"]), (62, "acct_TEST_1"))

    def test_sending_twice_makes_one_invoice(self):
        self.use(30)
        self.close()
        self.c.send_bills()
        self.c.send_bills()
        self.assertEqual(len(self.stripe.state["invoices"]), 1)

    def test_tiny_bills_roll_into_next_month(self):
        self.use(1)
        [b] = self.close()
        self.assertEqual(b["status"], "carried_forward")
        self.use(30, prefix="n")  # lands in the next open month
        nxt = [p for p in [self.c.open_period()]][0]
        self.c.close_period(nxt, force=True)
        new = [x for x in self.c.bills(service_id=self.sid) if x["period"] == nxt][0]
        self.assertEqual((new["carried_in_cents"], new["total_cents"]), (2, 62))
        self.assertEqual(self.c.bill(b["id"])["status"], "rolled_over")

    def test_paid_is_reported_and_overdue_blocks_everywhere(self):
        self.use(30)
        [b] = self.close()
        self.c.send_bills()
        self.c.sync(now=now_ms() + 15 * DAY_MS)
        self.assertEqual(self.c.bill(b["id"])["status"], "overdue")
        other, _ = self.c.store.create_service("Elsewhere", "Y")
        self.c.store.update_service(other, prices={"a": 1})
        r = self.c.check(self.c.store.service(other), self.pass_(sid=other), "a")
        self.assertEqual(r["code"], "OWNER_OVERDUE")
        self.stripe.pay(self.c.bill(b["id"])["stripe_invoice_id"])
        self.c.sync()
        self.assertEqual(self.c.bill(b["id"])["status"], "paid")
        self.assertTrue(self.c.check(self.c.store.service(other), self.pass_(sid=other), "a")["allow"])

    def test_no_stripe_account_means_no_invoice_and_a_reason(self):
        self.c.store.run("UPDATE services SET stripe_account=NULL WHERE id=?", self.sid)
        self.use(30)
        [b] = self.close()
        self.c.send_bills()
        b = self.c.bill(b["id"])
        self.assertEqual(b["status"], "ready")
        self.assertIn("Stripe account", b["note"])


class Http(Base):
    def setUp(self):
        super().setUp()
        self.srv = make_server(self.c, port=0)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def call(self, method, path, body=None, key=None, admin=None, raw=False):
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        if admin:
            h["X-Admin-Secret"] = admin
        req = urllib.request.Request(self.url + path, method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                data = r.read()
                return r.status, (data.decode() if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            data = e.read()
            return e.code, (data.decode() if raw else json.loads(data))

    def test_keys_are_required_and_scoped(self):
        self.assertEqual(self.call("POST", "/v1/usage", {})[0], 401)
        self.assertEqual(self.call("POST", "/v1/admin/close", {})[0], 401)
        self.assertEqual(self.call("POST", "/v1/admin/close", {}, admin="wrong")[0], 401)
        _, a = self.call("POST", "/v1/owners", {"name": "A"})
        _, b = self.call("POST", "/v1/owners", {"name": "B"})
        _, agent = self.call("POST", "/v1/agents", {"name": "bot"}, key=a["owner_key"])
        self.assertEqual(self.call("PATCH", f"/v1/agents/{agent['agent_id']}", {"active": False}, key=b["owner_key"])[0], 404)

    def test_full_flow_over_http(self):
        _, s = self.call("POST", "/v1/services", {"name": "S", "owner_name": "O"})
        k = s["service_key"]
        self.assertEqual(self.call("PUT", "/v1/service/prices", {"t": 10_000}, key=k)[0], 200)
        self.assertEqual(self.call("PUT", "/v1/service/prices", {"t": -1}, key=k)[0], 400)
        self.assertEqual(self.call("PUT", "/v1/service/policy", {"min_trust": "nonsense"}, key=k)[0], 400)
        _, o = self.call("POST", "/v1/owners", {"name": "Own"})
        _, ag = self.call("POST", "/v1/agents", {"name": "bot"}, key=o["owner_key"])
        _, p = self.call("POST", "/v1/passes", {"service_id": s["service_id"]}, key=ag["agent_key"])
        self.assertTrue(self.call("POST", "/v1/check", {"pass": p["pass"], "tool": "t"}, key=k)[1]["allow"])
        self.assertEqual(self.call("POST", "/v1/usage", {"pass": p["pass"], "tool": "t", "ref": "1"}, key=k)[0], 201)
        self.assertEqual(self.call("POST", "/v1/usage", {"pass": p["pass"], "tool": "t", "ref": "1"}, key=k)[0], 200)
        _, owner = self.call("GET", "/v1/owner", key=o["owner_key"])
        self.assertEqual(owner["agents"][0]["spent_this_month"], "$0.01")

    def test_dashboard_needs_a_key_and_escapes_names(self):
        _, s = self.call("POST", "/v1/services", {"name": "<script>alert(1)</script>", "owner_name": "O"})
        status, page = self.call("GET", "/dashboard", raw=True)
        self.assertIn("Sign in", page)
        req = urllib.request.Request(self.url + "/login", method="POST", data=f"key={s['service_key']}".encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a):
                return None
        try:
            urllib.request.build_opener(NoRedirect).open(req)
        except urllib.error.HTTPError as e:
            cookie = e.headers["Set-Cookie"].split(";")[0]
        page = urllib.request.urlopen(urllib.request.Request(self.url + "/dashboard", headers={"Cookie": cookie})).read().decode()
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)


class Modes(unittest.TestCase):
    def test_live_mode_and_live_stripe_keys_are_refused(self):
        with self.assertRaises(SystemExit):
            make_cfg(mode="live").check_test_mode()
        with self.assertRaises(SystemExit):
            make_cfg(stripe_key="sk_live_abc").check_test_mode()
        make_cfg(stripe_key="sk_test_abc").check_test_mode()

    def test_the_demo_runs_end_to_end(self):
        sys.path.insert(0, os.path.dirname(HERE))
        import demo
        clear, sid, srv, outcomes, late = demo.run(quiet=True)
        srv.shutdown()
        self.assertEqual(outcomes["Scraper-X"]["served"], 0)
        self.assertIn("OWNER_LIMIT", outcomes["IrrigationBot"])
        self.assertIn("overdue", late.values())
        served = sum(c["served"] for c in outcomes.values())
        counted = clear.store.one("SELECT SUM(units) AS n FROM usage")["n"]
        self.assertEqual(served, counted, "every served call counted once, retries included")


if __name__ == "__main__":
    unittest.main(verbosity=1)
