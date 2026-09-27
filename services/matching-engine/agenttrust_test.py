#!/usr/bin/env python3
"""Agenttrust as the dispute referee and trust-score source, end to end.

    cargo build --release && python3 agenttrust_test.py

Runs the real server against a stand-in Agenttrust on localhost that records what Ikenga sends
and rules for whoever disputed. Checks that a dispute is referred with both sides' answers, in
free points only, that the ruling is what gets paid out, and that trust scores show up.
"""
import http.server
import json
import os
import threading
import time

# Reuse market_test's signing and HTTP helpers rather than a third copy of them.
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_test.py")).read()
exec(_src[: _src.index('WAL = "/tmp/ikenga-market-test.wal"')])
PORT = 8371
FAKE_PORT = 8372

seen = []


class FakeAgenttrust(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        seen.append((self.path, body, self.headers.get("Authorization")))
        if self.path == "/v1/agreements":
            return self._send(201, {"agreement_id": "agr_1"})
        return self._send(200, {"result": "ok"})

    def do_GET(self):
        if self.path == "/v1/agreements/agr_1":
            return self._send(200, {"settlement": {"instruction": "pay_out", "outcome": 1}})
        if self.path.startswith("/v1/trust/"):
            return self._send(200, {"score": 104, "trust_level": "fair"})
        return self._send(404, {"error": "no"})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), FakeAgenttrust)
threading.Thread(target=srv.serve_forever, daemon=True).start()

WAL = "/tmp/ikenga-agenttrust-test.wal"
if os.path.exists(WAL):
    os.remove(WAL)
os.environ["AGENTTRUST_URL"] = f"http://127.0.0.1:{FAKE_PORT}"
os.environ["AGENTTRUST_API_KEY"] = "at_live_test"
proc, _ = start_server()
try:
    print("=== 1. starter markets ===")
    st, listing = call("GET", "/v1/markets?limit=40")
    ids = {m["market_id"] for m in listing.get("markets", [])}
    starters = {"mkt_ai_arena_october", "mkt_ai_big3_flagship", "mkt_ai_agenttrust_150",
                "mkt_ai_open_weights_top5", "mkt_ai_agent_science"}
    check("all five AI/agent-news markets are open", starters <= ids, sorted(ids))
    check("and every one is in free points",
          all(m["asset"] == "PTS" for m in listing["markets"] if m["market_id"] in starters))

    print("=== 2. a dispute goes to Agenttrust and its ruling is paid ===")
    seed_a, agent_a, _ = register()
    seed_b, agent_b, _ = register()
    mkt, detail = open_market("Will the referee agree?", closes_in_ms=1_500, dispute_window_ms=600_000)
    call("POST", f"/v1/markets/{mkt}/stakes", seed=seed_a, agent_id=agent_a, body_obj={"outcome": 0, "amount": 100})
    call("POST", f"/v1/markets/{mkt}/stakes", seed=seed_b, agent_id=agent_b, body_obj={"outcome": 1, "amount": 100})
    wait_for_observation(detail)
    st, _ = call("POST", f"/v1/markets/{mkt}/propose", body_obj={"outcome": 0, "evidence": "looked it up"}, owner=True)
    check("the operator proposes YES", st == 200, st)
    st, d = call("POST", f"/v1/markets/{mkt}/dispute", seed=seed_b, agent_id=agent_b,
                 body_obj={"reason": "it was NO", "outcome": 1})
    check("B's dispute is accepted", st == 200, d)
    check("and says Agenttrust will resolve it", d.get("resolver") == "agenttrust", d)

    deadline = time.time() + 70
    settled = None
    while time.time() < deadline:
        st, m = call("GET", f"/v1/markets/{mkt}")
        if m.get("status") in ("Resolved", "Settled", "Voided") or m.get("winning_outcome") is not None:
            settled = m
            break
        time.sleep(2)
    create = next((b for p, b, _ in seen if p == "/v1/agreements"), None)
    check("an agreement was opened on Agenttrust", create is not None, seen)
    if create:
        check("between the resolver and the disputer",
              create["parties"] == ["ikenga-resolver", f"ikenga-{agent_b}"], create["parties"])
        check("in free play points", create["asset"] == "IKENGA_POINTS", create)
        check("with the platform key", seen[0][2] == "Bearer at_live_test", seen[0][2])
    reports = [b for p, b, _ in seen if p == "/v1/agreements/agr_1/report"]
    check("both sides' answers were filed", sorted(r["outcome"] for r in reports) == [0, 1], reports)
    check("the market settled on Agenttrust's ruling (NO)",
          settled is not None and settled.get("winning_outcome") == 1, settled)
    check("B, who was right, got paid", points(seed_b, agent_b) > 1000.0, points(seed_b, agent_b))

    print("=== 3. trust scores ===")
    st, t = call("GET", f"/v1/agents/{agent_a}/agenttrust")
    check("an agent's Agenttrust score is shown", st == 200 and t.get("score") == 104, t)
    check("with its level and profile link",
          t.get("trust_level") == "fair" and t.get("profile_url", "").endswith(f"/trust/ikenga-{agent_a}"), t)
    st, acct = call("GET", "/v1/account", seed=seed_a, agent_id=agent_a)
    check("the account links to it", acct.get("agenttrust", {}).get("agenttrust_id") == f"ikenga-{agent_a}", acct.get("agenttrust"))
finally:
    stop(proc)
    srv.shutdown()

if failures:
    print(f"\n{len(failures)} FAILED")
    raise SystemExit(1)
print("\nALL AGENTTRUST CHECKS PASSED")
