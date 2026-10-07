#!/usr/bin/env python3
"""Keptvow Clear, the whole loop, on this machine, in test mode.

    python3 clear/demo.py            # run it, print what happened, write clear/demo_page.html
    python3 clear/demo.py --serve    # ...then keep the dashboard up at http://127.0.0.1:8400/dashboard

What happens:
  1. A service owner signs up "Weather Lookup", sets prices and who it will serve, and connects
     a (stand-in) Stripe account.
  2. Five companies register their agents. Some link a Keptvow record; one sets a spending cap.
  3. The agents call the service. Before each call the service asks Clear "should I serve this
     agent?"; after it serves, it reports the call. Some calls are refused, for real reasons.
  4. The month closes. Clear writes one bill per company and turns each into a Stripe test
     invoice on the service owner's own account.
  5. Two companies pay. One doesn't. When its bill goes overdue, its agents are refused.

Stripe and Keptvow are local stand-ins (keptvow_clear/fakes.py): this machine can't reach either.
"""
import json
import os
import sys
import tempfile
import threading
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from keptvow_clear import build  # noqa: E402
from keptvow_clear.billing import DAY_MS, cents_str  # noqa: E402
from keptvow_clear.config import Config  # noqa: E402
from keptvow_clear.dashboard import service_page  # noqa: E402
from keptvow_clear.fakes import fake_keptvow, fake_stripe  # noqa: E402
from keptvow_clear.sdk import ClearAgent, ClearService  # noqa: E402
from keptvow_clear.server import make_server  # noqa: E402
from keptvow_clear.store import Store, now_ms, period_of  # noqa: E402

ADMIN = "demo-admin-secret"  # throwaway, for this local demo only


def http(base, method, path, body=None, key=None, admin=False):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if admin:
        headers["X-Admin-Secret"] = ADMIN
    req = urllib.request.Request(base + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def weather_service(clear_url, service_key):
    """The pretend MCP-style service. Its only Clear code is the gate() block."""
    clear = ClearService(clear_url, service_key)

    class Weather(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            tool = self.path.rsplit("/", 1)[-1]
            with clear.gate(self.headers.get("X-Clear-Pass", ""), tool, ref=self.headers.get("X-Request-Id")) as call:
                if not call.allowed:
                    body, code = {"error": call.code, "reason": call.reason}, 402
                else:
                    body, code = {"tool": tool, "city": "Lagos", "high_c": 31}, 200
            data = json.dumps(body).encode()
            self.send_response(code)
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Weather)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def call_service(svc_url, agent, service_id, tool, req_id):
    req = urllib.request.Request(f"{svc_url}/tools/{tool}", method="POST", data=b"{}",
                                 headers={"X-Clear-Pass": agent.pass_for(service_id), "X-Request-Id": req_id})
    try:
        with urllib.request.urlopen(req) as r:
            return "served", r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read())["error"], e.code


def run(port=0, quiet=False):
    say = (lambda *a: None) if quiet else print
    kv_srv, kv_url = fake_keptvow({"kv-trip-1": "good", "kv-crop": "fair", "kv-scraper": "caution"})
    st_srv, st_url = fake_stripe()
    cfg = Config(mode="test", db_path=os.path.join(tempfile.mkdtemp(), "clear.db"), admin_secret=ADMIN,
                 token_secret=os.urandom(32), stripe_key="sk_test_demo_not_a_real_key", stripe_api=st_url,
                 keptvow_url=kv_url, keptvow_source="clear", keptvow_source_secret="demo-source-secret",
                 days_until_due=14, grace_days=30, included_settlements=0, sync_secs=0)
    clear = build(cfg, Store(cfg.db_path))
    srv = make_server(clear, port=port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    # 1. The service owner.
    s = http(base, "POST", "/v1/services", {"name": "Weather Lookup", "owner_name": "Skyline Data", "email": "billing@skyline.test"})
    sid, skey = s["service_id"], s["service_key"]
    http(base, "PUT", "/v1/service/prices", {"get_forecast": 20_000, "get_alerts": 10_000, "get_history": 50_000, "get_now": 4_000}, skey)
    http(base, "PUT", "/v1/service/policy", {"min_trust": "unknown", "unknown_monthly_cap": 1_000_000}, skey)
    http(base, "PUT", "/v1/service/stripe", {"stripe_account": "acct_TEST_skyline"}, skey)
    say("1. Skyline Data signed up Weather Lookup: forecast $0.02, alerts $0.01, history $0.05, now $0.004 a call.")
    say("   It serves any agent Keptvow doesn't flag; agents with no record get $1.00 a month to start.\n")

    # 2. Agent owners and their agents.
    plan = [  # owner, email, agent, keptvow id, verified, monthly cap (micro-dollars), calls
        ("Northwind Travel", "ap@northwind.test", "TripPlanner-1", "kv-trip-1", True, None, 70),
        ("Northwind Travel", "ap@northwind.test", "TripPlanner-2", "kv-trip-1", False, None, 70),
        ("Greenfield Farms", "pay@greenfield.test", "CropWatch", "kv-crop", True, None, 60),
        ("Greenfield Farms", "pay@greenfield.test", "IrrigationBot", None, False, 300_000, 60),
        ("Harbor Logistics", "finance@harbor.test", "RouteOptimizer", None, False, None, 45),
        ("Solo Dev", "me@solo.test", "WeekendBot", None, False, None, 6),
        ("Scrapers Inc", "x@scrapers.test", "Scraper-X", "kv-scraper", True, None, 10),
    ]
    owners, agents = {}, []
    for owner, email, name, kv, verified, cap, calls in plan:
        if owner not in owners:
            owners[owner] = http(base, "POST", "/v1/owners", {"name": owner, "email": email})
        a = http(base, "POST", "/v1/agents", {"name": name, "keptvow_id": kv, "monthly_cap": cap}, owners[owner]["owner_key"])
        if verified:
            http(base, "POST", f"/v1/admin/agents/{a['agent_id']}/keptvow-verified", {}, admin=True)
        agents.append((name, ClearAgent(base, a["agent_key"]), calls))
    say("2. Five companies registered seven agents. TripPlanner-2 *claims* TripPlanner-1's Keptvow record")
    say("   but hasn't proven it, so it counts as having no record. IrrigationBot's owner capped it at $0.30.\n")

    # 3. The agents use the service.
    svc_srv, svc_url = weather_service(base, skey)
    tools = ["get_forecast"] * 5 + ["get_alerts"] * 3 + ["get_history"] + ["get_now"] * 3
    outcomes = {}
    for name, agent, calls in agents:
        c = Counter()
        for i in range(calls):
            tool = tools[(i * 7 + len(name)) % len(tools)]
            for _ in range(2 if i % 10 == 0 else 1):  # every tenth call is retried, like a dropped connection
                result, _ = call_service(svc_url, agent, sid, tool, f"{name}-{i}")
            c[result] += 1
        outcomes[name] = c
    say("3. The agents called Weather Lookup:")
    for name, c in outcomes.items():
        refused = ", ".join(f"{n} refused ({k})" for k, n in c.items() if k != "served")
        say(f"   {name:<15} {c['served']:>3} served" + (f"; {refused}" if refused else ""))
    say()

    # 4. Close the month, send invoices.
    period = period_of(now_ms())
    http(base, "POST", "/v1/admin/close", {"period": period, "force": True}, admin=True)
    sent = http(base, "POST", "/v1/admin/send", {}, admin=True)
    say(f"4. Closed {period}. Bills:")
    for b in clear.bills(service_id=sid):
        o = clear.store.owner(b["owner_id"])["name"]
        say(f"   {o:<17} {cents_str(b['total_cents']):>7}  {b['status']:<16} {b['note'] or ''}")
    say(f"   {sum(v == 'sent' for v in sent.values())} Stripe test invoices created on acct_TEST_skyline.\n")

    # 5. Two pay, one doesn't.
    for b in clear.bills(service_id=sid):
        if b["stripe_invoice_id"] and clear.store.owner(b["owner_id"])["name"] in ("Northwind Travel", "Greenfield Farms"):
            st_srv.pay(b["stripe_invoice_id"])
    clear.sync()
    late = clear.sync(now=now_ms() + 15 * DAY_MS)  # pretend two weeks pass
    say("5. Northwind and Greenfield paid their invoices. Harbor didn't; two weeks later its bill is overdue.")
    harbor = next(a for n, a, _ in agents if n == "RouteOptimizer")
    result, code = call_service(svc_url, harbor, sid, "get_forecast", "RouteOptimizer-after")
    say(f"   RouteOptimizer tries again: {result} (HTTP {code}).")
    say(f"   Keptvow was told about {len(kv_srv.reports)} on-time payments (it ignores them until it trusts Clear as a source).\n")

    svc_srv.shutdown()
    return clear, sid, srv, outcomes, late


def main():
    serve = "--serve" in sys.argv
    clear, sid, srv, _, _ = run(port=int(os.environ.get("CLEAR_PORT", "8400")) if serve else 0)
    out = os.path.join(HERE, "demo_page.html")
    with open(out, "w") as f:
        f.write(service_page(clear, sid, full_document=True))
    print(f"Dashboard snapshot written to {out}")
    if serve:
        print(f"Dashboard at http://127.0.0.1:{srv.server_address[1]}/dashboard  (sign in with the service key)")
        threading.Event().wait()


if __name__ == "__main__":
    main()
