#!/usr/bin/env python3
"""Ikenga Clear demo: test agents call a test service, Clear counts the calls and writes the bills.

    python3 clear/demo.py            # run once, print the bills, write clear/demo_page.html
    python3 clear/demo.py --serve    # ...then keep the page up at http://127.0.0.1:8400/

What runs, all on this machine, test mode only:
  1. Clear, on port 8400.
  2. "Weather Lookup", a pretend MCP-style service on port 8401. Each time an agent calls one of
     its tools it reports the call to Clear with ClearClient.report(). That one line is the whole
     integration for a service owner.
  3. Six test agents, belonging to three companies, calling the service over HTTP.
"""
import json
import os
import random
import secrets
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clear import Agent, ClearClient, Meter, Service, make_server, money, render_page, require_test_mode  # noqa: E402

CLEAR_PORT = int(os.environ.get("CLEAR_PORT", "8400"))
SERVICE_PORT = int(os.environ.get("CLEAR_DEMO_SERVICE_PORT", "8401"))

SERVICE = Service(
    id="svc_weather", name="Weather Lookup", owner="Skyline Data (test)",
    stripe_account="acct_TEST_skyline",
    prices={"get_forecast": 2, "get_alerts": 1, "get_history": 5},
)
AGENTS = [
    Agent("ag_trip1", "TripPlanner-1", "Northwind Travel"),
    Agent("ag_trip2", "TripPlanner-2", "Northwind Travel"),
    Agent("ag_farm", "CropWatch", "Greenfield Farms"),
    Agent("ag_farm2", "IrrigationBot", "Greenfield Farms"),
    Agent("ag_ship", "RouteOptimizer", "Harbor Logistics"),
    Agent("ag_ship2", "DockScheduler", "Harbor Logistics"),
]


def start_weather_service(clear):
    """The pretend service. A real one would check the agent's identity; this one trusts the header."""

    class Weather(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            tool = self.path.rsplit("/", 1)[-1]
            agent_id = self.headers.get("X-Agent-Id", "")
            request_id = self.headers.get("X-Request-Id") or secrets.token_hex(8)
            if tool not in SERVICE.prices:
                self.send_response(404)
                self.end_headers()
                return
            result = {"tool": tool, "result": {"city": "Lagos", "high_c": 31, "rain_chance": 0.4}}
            clear.report(agent_id, tool, ref=request_id)  # <- the integration
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", SERVICE_PORT), Weather)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run_agent(agent, calls, rng_seed, retry_every=10):
    rng = random.Random(rng_seed)
    tools = list(SERVICE.prices)
    weights = [6, 3, 1]
    for i in range(calls):
        tool = rng.choices(tools, weights)[0]
        req_id = f"{agent.id}-{i}"
        # Every tenth call is sent twice, like a network retry. Clear must count it once.
        for _ in range(2 if i % retry_every == 0 else 1):
            req = urllib.request.Request(
                f"http://127.0.0.1:{SERVICE_PORT}/tools/{tool}", method="POST", data=b"{}",
                headers={"X-Agent-Id": agent.id, "X-Request-Id": req_id})
            urllib.request.urlopen(req, timeout=5).read()


def run(seed=7):
    """Run the whole demo. Returns (meter, the Clear server, the number of distinct calls the agents made)."""
    require_test_mode()
    meter = Meter()
    meter.add_service(SERVICE)
    for a in AGENTS:
        meter.add_agent(a)
    # The service's key comes from the environment; for a throwaway demo, make one up.
    key = os.environ.get("CLEAR_DEMO_SERVICE_KEY") or "test_" + secrets.token_hex(16)
    clear_srv = make_server(meter, {key: SERVICE.id}, port=CLEAR_PORT)
    threading.Thread(target=clear_srv.serve_forever, daemon=True).start()
    svc = start_weather_service(ClearClient(f"http://127.0.0.1:{CLEAR_PORT}", key))

    rng = random.Random(seed)
    plan = [(a, rng.randint(20, 60), rng.random()) for a in AGENTS]
    threads = [threading.Thread(target=run_agent, args=p) for p in plan]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    svc.shutdown()
    return meter, clear_srv, sum(p[1] for p in plan)


def main():
    meter, clear_srv, made = run()
    print("Ikenga Clear demo (test mode: no money moves, nothing is sent to Stripe)\n")
    for b in meter.bills(SERVICE.id):
        print(f"Bill to {b['bill_to']}  ->  paid to {b['pay_to']} ({b['pay_to_stripe_account']})")
        for l in b["lines"]:
            print(f"   {l['agent']:<16} {l['tool']:<13} {l['calls']:>4} x {l['unit_cents']}c = {money(l['amount_cents'])}")
        print(f"   {'Total':<36}{money(b['total_cents']):>10}\n")
    s = meter.summary(SERVICE.id)
    print(f"Agents made {made} calls (plus retries); Clear counted {s['calls']}.")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_page.html")
    with open(out, "w") as f:
        f.write(render_page(meter, SERVICE.id, full_document=True))
    print(f"Page written to {out}")
    if "--serve" in sys.argv:
        print(f"Serving the page at http://127.0.0.1:{CLEAR_PORT}/  (Ctrl-C to stop)")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    clear_srv.shutdown()


if __name__ == "__main__":
    main()
