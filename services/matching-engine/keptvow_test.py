#!/usr/bin/env python3
"""Keptvow as the dispute referee and trust-score source, end to end.

    cargo build --release && python3 keptvow_test.py

Runs the real server against a stand-in Keptvow on localhost that records what Ikenga sends
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
created = []


class FakeKeptvow(http.server.BaseHTTPRequestHandler):
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
            created.append(body)
            return self._send(201, {"agreement_id": f"agr_{len(created)}"})
        return self._send(200, {"result": "ok"})

    def do_GET(self):
        if self.path.startswith("/v1/agreements/agr_"):
            # The referee rules for outcome 1 ("NO") on every case.
            return self._send(200, {"settlement": {"instruction": "pay_out", "outcome": 1}})
        if self.path.startswith("/v1/trust/"):
            return self._send(200, {"score": 104, "trust_level": "fair"})
        return self._send(404, {"error": "no"})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), FakeKeptvow)
threading.Thread(target=srv.serve_forever, daemon=True).start()

WAL = "/tmp/ikenga-keptvow-test.wal"
if os.path.exists(WAL):
    os.remove(WAL)
os.environ["KEPTVOW_URL"] = f"http://127.0.0.1:{FAKE_PORT}"
# No API key: Keptvow's free tier opens deals without one.
os.environ.pop("KEPTVOW_API_KEY", None)
os.environ.pop("AGENTTRUST_API_KEY", None)
os.environ["IKENGA_KEPTVOW_TICK_MS"] = "1000"
os.environ["IKENGA_KEPTVOW_POLL_MS"] = "2000"
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

    print("=== 2. a dispute goes to Keptvow and its ruling is paid ===")
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
    check("and says Keptvow will resolve it", d.get("resolver") == "keptvow", d)

    deadline = time.time() + 70
    settled = None
    while time.time() < deadline:
        st, m = call("GET", f"/v1/markets/{mkt}")
        if m.get("status") in ("Resolved", "Settled", "Voided") or m.get("winning_outcome") is not None:
            settled = m
            break
        time.sleep(2)
    create = next((b for p, b, _ in seen if p == "/v1/agreements"), None)
    check("an agreement was opened on Keptvow", create is not None, seen)
    if create:
        st, tb = call("GET", f"/v1/agents/{agent_b}/keptvow")
        check("between the resolver and the disputer's Keptvow id",
              len(create["parties"]) == 2 and create["parties"][1] == tb.get("keptvow_id"), create["parties"])
        check("and no real agent id is sent to Keptvow",
              not any(agent_a in json.dumps(b) or agent_b in json.dumps(b) for _, b, _ in seen), seen)
        check("in free play points", create["asset"] == "IKENGA_POINTS", create)
        check("with no API key needed", seen[0][2] is None, seen[0][2])
    reports = [b for p, b, _ in seen if p == "/v1/agreements/agr_1/report"]
    check("both sides' answers were filed", sorted(r["outcome"] for r in reports) == [0, 1], reports)
    check("the market settled on Keptvow's ruling (NO)",
          settled is not None and settled.get("winning_outcome") == 1, settled)
    check("B, who was right, got paid", points(seed_b, agent_b) > 1000.0, points(seed_b, agent_b))

    print("=== 3. head-to-head bets go on both bots' Keptvow records ===")
    seed_c, agent_c, _ = register()
    seed_d, agent_d, _ = register()

    def head_to_head(question):
        now = int(time.time() * 1000)
        st, ch = call("POST", "/v1/challenges", seed=seed_c, agent_id=agent_c, body_obj={
            "question": question, "my_outcome": 0, "my_stake": 100, "their_stake": 100,
            "closes_at_ms": now + 1_500, "observed_at_ms": now + 2_000,
            "expires_at_ms": now + 1_500, "dispute_window_ms": 60_000,
            "resolution": {"kind": "mutual_agreement", "criteria": "whatever the two of us saw"},
        })
        assert st in (200, 201), (st, ch)
        st, taken = call("POST", f"/v1/challenges/{ch['challenge']['challenge_id']}/accept", seed=seed_d, agent_id=agent_d, body_obj={})
        assert st == 200, (st, taken)
        time.sleep(2.2)
        return taken["market_id"]

    def wait_settled(mkt, seconds=40):
        deadline = time.time() + seconds
        while time.time() < deadline:
            st, m = call("GET", f"/v1/markets/{mkt}")
            if m.get("winning_outcome") is not None or m.get("status") in ("Resolved", "Voided"):
                return m
            time.sleep(1)
        return m

    def wait_created(n, seconds=30):
        deadline = time.time() + seconds
        while time.time() < deadline and len(created) < n:
            time.sleep(0.5)
        return len(created) >= n

    # Both agree: paid at once on Ikenga, and a clean deal on both Keptvow records.
    before = len(created)
    agree = head_to_head("Agree on this one?")
    call("POST", f"/v1/markets/{agree}/report", seed=seed_c, agent_id=agent_c, body_obj={"outcome": 0})
    st, r = call("POST", f"/v1/markets/{agree}/report", seed=seed_d, agent_id=agent_d, body_obj={"outcome": 0})
    check("when both agree, Ikenga pays at once", r.get("status") == "settled", r)
    check("and the bet is recorded on Keptvow", wait_created(before + 1), len(created))
    if len(created) > before:
        rec = created[before]
        st, tc = call("GET", f"/v1/agents/{agent_c}/keptvow")
        st, td = call("GET", f"/v1/agents/{agent_d}/keptvow")
        check("between the two bettors' Keptvow ids",
              sorted(rec["parties"]) == sorted([tc.get("keptvow_id"), td.get("keptvow_id")]),
              (rec["parties"], tc.get("keptvow_id"), td.get("keptvow_id")))
        rid = f"agr_{before + 1}"
        time.sleep(1)
        outs = [b["outcome"] for p, b, _ in seen if p == f"/v1/agreements/{rid}/report"]
        check("with both sides' matching answers", outs == [0, 0], outs)

    # They disagree: frozen on Ikenga, then Keptvow's ruling (NO) is paid.
    before = len(created)
    fight = head_to_head("Disagree on this one?")
    c_before, d_before = points(seed_c, agent_c), points(seed_d, agent_d)
    call("POST", f"/v1/markets/{fight}/report", seed=seed_c, agent_id=agent_c, body_obj={"outcome": 0})
    st, r = call("POST", f"/v1/markets/{fight}/report", seed=seed_d, agent_id=agent_d, body_obj={"outcome": 1})
    check("when they disagree, Keptvow decides", r.get("status") == "disagreed" and r.get("resolver") == "keptvow", r)
    check("the disagreement is recorded on Keptvow", wait_created(before + 1), len(created))
    m = wait_settled(fight, seconds=150)
    check("and Ikenga pays out on Keptvow's ruling (NO)", m.get("winning_outcome") == 1, m)
    c_gain = points(seed_c, agent_c) - c_before
    d_gain = points(seed_d, agent_d) - d_before
    check("so the bot that said NO was paid and the other wasn't", d_gain > 100 and c_gain < 1,
          (c_gain, d_gain))

    print("=== 4. trust scores ===")
    st, old = call("GET", f"/v1/agents/{agent_a}/agenttrust")
    check("the old /agenttrust address still answers", st == 200, st)
    time.sleep(1.1)  # leave the per-IP lookup budget for the checks below
    st, t = call("GET", f"/v1/agents/{agent_a}/keptvow")
    check("an agent's Keptvow score is shown", st == 200 and t.get("score") == 104, t)
    check("with its level and profile link",
          t.get("trust_level") == "fair" and t.get("profile_url", "").endswith("/trust/" + t.get("keptvow_id", "?")), t)
    check("under a pseudonym, not the agent's real id",
          t.get("keptvow_id", "").startswith("ikenga-") and agent_a not in t.get("keptvow_id", ""), t)
    st, acct = call("GET", "/v1/account", seed=seed_a, agent_id=agent_a)
    check("the account links to it", acct.get("keptvow", {}).get("keptvow_id") == t.get("keptvow_id"), acct.get("keptvow"))
    st, _ = call("GET", "/v1/agents/agent_DOESNOTEXIST/keptvow")
    check("a made-up agent id is refused before any lookup", st == 404, st)
    codes = [call("GET", f"/v1/agents/{agent_a}/keptvow", _retries=1)[0] for _ in range(20)]
    check("lookups are rate-limited per IP", 429 in codes, codes)
finally:
    stop(proc)
    srv.shutdown()

if failures:
    print(f"\n{len(failures)} FAILED")
    raise SystemExit(1)
print("\nALL KEPTVOW CHECKS PASSED")
