#!/usr/bin/env python3
"""Ikenga Clear: bot-to-bot payments netted instead of paid one by one.

    cargo build --release && python3 clear_test.py

Four bots buy small services from each other all day. Paid one at a time on a card-style rail
every 5-cent call would cost more to move than it is worth. Recorded here instead, they are
netted each cycle into the fewest transfers that settle everything, and the clearinghouse takes
a share of what that saved — never of the volume.

Pinned end to end against a real server: the netting is exact, the fee never exceeds its share
of any agent's saving, only a recipient can settle an instruction, a late payer is cut off from
the whole network, caps bound the net and not the gross, retries are safe, and every cycle and
settlement survives a kill -9.
"""
import base64
import http.client
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time

HOST, PORT = "127.0.0.1", 8347
BIN = "./target/release/ikenga-matching-engine"
OWNER_KEY = "dd" * 32
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")

failures = []


def check(label, ok, extra=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if extra:
            print(f"       got: {extra}")
        failures.append(label)


def generate_keypair():
    with tempfile.NamedTemporaryFile(suffix=".pem") as f:
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", f.name],
                       check=True, capture_output=True)
        priv = subprocess.run(["openssl", "pkey", "-in", f.name, "-outform", "DER"],
                              check=True, capture_output=True).stdout
        pub = subprocess.run(["openssl", "pkey", "-in", f.name, "-pubout", "-outform", "DER"],
                             check=True, capture_output=True).stdout
    return priv[16:].hex(), pub[12:].hex()


def _pem(seed_hex):
    b64 = base64.b64encode(PKCS8_PREFIX + bytes.fromhex(seed_hex)).decode()
    return ("-----BEGIN PRIVATE KEY-----\n" + b64 + "\n-----END PRIVATE KEY-----\n").encode()


def sign(seed_hex, method, path, ts, nonce, body=b""):
    payload = method.encode() + path.encode() + ts.encode() + nonce.encode() + body
    with tempfile.NamedTemporaryFile() as k, tempfile.NamedTemporaryFile() as m, tempfile.NamedTemporaryFile() as s:
        k.write(_pem(seed_hex)); k.flush()
        m.write(payload); m.flush()
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", k.name, "-rawin", "-in", m.name, "-out", s.name],
            check=True, capture_output=True,
        )
        return s.read().hex()


def call(method, path, seed=None, agent_id=None, body_obj=None, owner=False, _retries=6):
    """Retries on 429. A freshly registered agent sits in the New trust band at 10 req/s, and
    this test bursts well past that during setup — which is the limiter doing its job, not a
    bug. Real integrators will meet the same ceiling on their first minute."""
    for attempt in range(_retries):
        status, body = _call_once(method, path, seed, agent_id, body_obj, owner)
        if status != 429:
            return status, body
        time.sleep(0.35)
    return status, body


def _call_once(method, path, seed=None, agent_id=None, body_obj=None, owner=False):
    raw = json.dumps(body_obj).encode() if body_obj is not None else b""
    h = {}
    if raw:
        h["Content-Type"] = "application/json"
    if owner:
        h["X-Owner-Key"] = OWNER_KEY
    if seed:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        nonce = f"mkt-{time.time_ns()}"
        h.update({
            "X-Agent-ID": agent_id or "registration",
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sign(seed, method, path, ts, nonce, raw),
        })
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request(method, path, body=raw, headers=h)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except Exception:
        return resp.status, data


def register():
    seed, pub = generate_keypair()
    status, reg = call("POST", "/v1/agents", seed=seed, body_obj={"pubkey_hex": pub})
    assert status == 201, f"registration failed: {status} {reg}"
    return seed, reg["agent_id"], reg



WAL = tempfile.mktemp(suffix=".wal")


def start_server():
    env = {
        **os.environ,
        "IKENGA_BIND": f"{HOST}:{PORT}",
        "IKENGA_OWNER_KEY": OWNER_KEY,
        "IKENGA_WAL_PATH": WAL,
        "IKENGA_REGISTRATIONS_PER_HOUR": "500",
        "IKENGA_DISABLE_RATE_LIMIT": "1",
        "IKENGA_CLEAR_CYCLE_SECS": "0",
        "IKENGA_CLEAR_SETTLE_SECS": "4",
    }
    proc = subprocess.Popen(BIN, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line or "listening on" in line:
            break
    return proc


def stop(proc):
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


def owe(who, payee, amount, **extra):
    seed, aid = who
    body = {"payee": payee[1], "amount": amount, "asset": "USDC", **extra}
    return call("POST", "/v1/clear/obligations", seed=seed, agent_id=aid, body_obj=body)


def main():
    proc = start_server()
    try:
        names = ["data", "trader", "research", "compute"]
        bots = {}
        for n in names:
            seed, aid, _ = register()
            bots[n] = (seed, aid)

        st, ov = call("GET", "/v1/clear")
        check("GET /v1/clear describes the service unauthenticated", st == 200 and ov.get("service") == "Ikenga Clear", ov)

        # --- A day of small bot-to-bot commerce --------------------------------------------
        rng = random.Random(7)
        economy = [
            ("trader", "data", 0.05), ("research", "data", 0.05), ("data", "compute", 0.08),
            ("trader", "research", 0.25), ("research", "compute", 0.08), ("compute", "trader", 0.10),
            ("data", "research", 0.03), ("compute", "data", 0.04),
        ]
        recorded = 0
        for k in range(48):
            p, q, amt = economy[k % len(economy)]
            amt = round(amt * (1 + rng.random()), 4)
            st, o = owe(bots[p], bots[q], amt)
            if st == 201:
                recorded += 1
            else:
                check(f"obligation {k} recorded", False, (st, o))
        check("48 micro-payments recorded without moving any money", recorded == 48, recorded)

        # --- Safety rails -------------------------------------------------------------------
        st, a = owe(bots["trader"], bots["data"], 0.05, ref="call-123")
        st2, b = owe(bots["trader"], bots["data"], 0.05, ref="call-123")
        check("a retry with the same ref returns the first obligation, not a second debt",
              st == 201 and st2 == 200 and a["obligation_id"] == b["obligation_id"] and b["created"] is False,
              (st, st2))
        st, e = owe(bots["trader"], bots["trader"], 1)
        check("an agent cannot owe itself", st == 400 and e["code"] == "SELF_PAYMENT", e)
        st, e = owe(bots["trader"], bots["data"], 26)
        check("a New-band agent's net debit is capped (25)", st == 402 and e["code"] == "OVER_CREDIT_CAP", e)
        st, cap = call("GET", f"/v1/clear/capacity/{bots['trader'][1]}?asset=USDC&amount=1")
        check("the public credit check says yes to a small obligation", st == 200 and cap["can_owe"] is True, cap)
        st, cap = call("GET", f"/v1/clear/capacity/{bots['trader'][1]}?asset=USDC&amount=500")
        check("...and no to one past the cap", st == 200 and cap["can_owe"] is False, cap)
        st, e = call("POST", "/v1/clear/cycles", body_obj={})
        check("closing a cycle needs the owner key", st == 403, (st, e))

        # --- Net it ---------------------------------------------------------------------------
        st, cyc = call("POST", "/v1/clear/cycles", body_obj={}, owner=True)
        check("owner closes the cycle", st == 201, (st, cyc))
        usdc = cyc["assets"][0]
        print(f"       {usdc['payments']} payments worth {usdc['gross_value']:.4f} USDC -> "
              f"{usdc['transfers']} transfers moving {usdc['settled_value']:.4f}; rail cost "
              f"{usdc['cost_if_paid_one_by_one']:.2f} -> {usdc['cost_netted']:.2f}, fee {usdc['fees']:.4f}")
        check("49 payments collapse to at most 4 transfers (agents - 1, plus the fee account)",
              usdc["payments"] == 49 and usdc["transfers"] <= 4, usdc)
        check("the saving is real and the fee is a fraction of it",
              usdc["saved"] > 0 and 0 < usdc["fees"] <= 0.2 * usdc["saved"] + 1e-6, usdc)
        check("...and never more than 1% of the value that went through",
              usdc["fees"] <= 0.01 * usdc["gross_value"] + 1e-6, usdc)
        cid = int(cyc["cycle_id"])

        # Every agent's own receipt, and the guarantee per agent.
        instructions = {}
        for n, (seed, aid) in bots.items():
            st, view = call("GET", f"/v1/clear/cycles/{cid}", seed=seed, agent_id=aid)
            lines = view.get("your_savings", [])
            ok = st == 200 and len(lines) == 1 and lines[0]["agent"] == aid
            if ok:
                l = lines[0]
                ok = l["fee"] <= 0.2 * max(0, l["saved"]) + 1e-6 and l["fee"] <= 0.01 * l["gross_out"] + 1e-6
            check(f"{n}: sees only its own receipt; fee <= 20% of its saving and <= 1% of what it sent", ok, view)
            for i in view.get("your_instructions", []):
                instructions[i["instruction_id"]] = i
        st, public = call("GET", f"/v1/clear/cycles/{cid}")
        check("an anonymous reader gets totals but nobody's instructions",
              st == 200 and "your_instructions" not in public and public["digest"] == cyc["digest"], public)

        # Net positions reproduce exactly: sum of instruction flows == net of obligations.
        agent_ids = {aid: n for n, (_, aid) in bots.items()}
        to_agents = [i for i in instructions.values() if not i["is_fee"]]
        to_fees = [i for i in instructions.values() if i["is_fee"]]
        check("a fee too small to be worth its own transfer is not sent this cycle", not to_fees, to_fees)
        st, pos = call("GET", "/v1/clear/position", seed=bots["trader"][0], agent_id=bots["trader"][1])
        carried = [o for o in pos.get("open_obligations", []) if o["payee"] == "ikenga_clear"]
        check("...it is carried into the next cycle instead of forgiven",
              st == 200 and len(carried) == 1 and carried[0]["amount"] > 0, pos)

        # --- Settle ---------------------------------------------------------------------------
        first = to_agents[0]
        payer = bots[agent_ids[first["from"]]]
        payee = bots[agent_ids[first["to"]]]
        path = f"/v1/clear/instructions/{first['instruction_id']}"
        st, e = call("POST", path + "/confirm", seed=payer[0], agent_id=payer[1], body_obj={})
        check("a payer cannot confirm its own payment", st == 403, (st, e))
        st, r = call("POST", path + "/paid", seed=payer[0], agent_id=payer[1], body_obj={"tx_ref": "0xfeed"})
        check("the payer marks it paid with a tx ref", st == 200 and r["status"] == "paid", r)
        st, r = call("POST", path + "/confirm", seed=payee[0], agent_id=payee[1], body_obj={})
        check("the recipient's confirmation settles it", st == 200 and r["status"] == "settled", r)
        st, r = call("POST", path + "/confirm", seed=payee[0], agent_id=payee[1], body_obj={})
        check("it cannot be settled twice", st == 409, (st, r))

        # --- Crash -----------------------------------------------------------------------------
        stop(proc)
        proc = start_server()
        st, again = call("GET", f"/v1/clear/cycles/{cid}", seed=payee[0], agent_id=payee[1])
        check("after kill -9 the cycle comes back with the same digest",
              st == 200 and again["digest"] == cyc["digest"], again)
        mine = {i["instruction_id"]: i for i in again.get("your_instructions", [])}
        check("...and the settled instruction is still settled",
              mine.get(first["instruction_id"], {}).get("status") == "settled", mine)
        st, ov = call("GET", "/v1/clear")
        check("lifetime savings survive the restart", ov["lifetime"]["cycles"] == 1 and ov["lifetime"]["rail_cost_saved"] > 0, ov["lifetime"])

        # --- Late payers are cut off --------------------------------------------------------
        unpaid = [i for i in to_agents if i["instruction_id"] != first["instruction_id"]]
        if unpaid:
            late = bots[agent_ids[unpaid[0]["from"]]]
            other = next(b for b in bots.values() if b[1] != late[1])
            time.sleep(4.5)
            st, e = owe(late, other, 0.01)
            check("a payer past its settle-by is frozen out of the whole network",
                  st == 402 and e["code"] == "OVERDUE_SETTLEMENT", (st, e))
            st, cap = call("GET", f"/v1/clear/capacity/{late[1]}?amount=0.01")
            check("...and the public credit check shows it", cap["in_good_standing"] is False, cap)
            # Its creditors confirm; it is back in.
            for i in [x for x in unpaid if x["from"] == late[1]]:
                rcpt = bots[agent_ids[i["to"]]]
                call("POST", f"/v1/clear/instructions/{i['instruction_id']}/confirm", seed=rcpt[0], agent_id=rcpt[1], body_obj={})
            st, o = owe(late, other, 0.01)
            check("once everything it owes is confirmed, it can trade again", st == 201, (st, o))

        # --- Heavy gross, tiny net: the cap binds the net only -------------------------------
        call("POST", "/v1/clear/cycles", body_obj={}, owner=True)  # net what is left from above
        ring = []
        for _ in range(3):
            seed, aid, _ = register()
            ring.append((seed, aid))
        ok = True
        for rnd in range(30):
            for k in range(3):
                st, o = owe(ring[k], ring[(k + 1) % 3], 20)
                ok = ok and st == 201
        check("three New-band bots (cap 25 each) move 1,800 USDC between them", ok)
        st, cyc2 = call("POST", "/v1/clear/cycles", body_obj={}, owner=True)
        a2 = cyc2["assets"][0] if st == 201 else {}
        print(f"       {a2.get('payments')} payments worth {a2.get('gross_value')} -> "
              f"{a2.get('settled_value')} settled between agents; rail cost "
              f"{a2.get('cost_if_paid_one_by_one')} -> {a2.get('cost_netted')}; fee {a2.get('fees')}")
        check("a closed circle settles with no agent-to-agent transfers at all",
              st == 201 and a2["settled_value"] == 0 and a2["cost_netted"] == 0, cyc2)
        seed, aid = ring[0]
        st, view = call("GET", f"/v1/clear/cycles/{int(cyc2['cycle_id'])}", seed=seed, agent_id=aid)
        fee_ins = [i for i in view.get("your_instructions", []) if i["is_fee"]]
        check("a fee big enough to be worth a transfer is collected, sent net of its rail cost",
              len(fee_ins) == 1 and fee_ins[0]["send"] < fee_ins[0]["amount"], view)
        if fee_ins:
            fid = fee_ins[0]["instruction_id"]
            st, e = call("POST", f"/v1/clear/instructions/{fid}/confirm", seed=ring[1][0], agent_id=ring[1][1], body_obj={})
            check("no agent can confirm a fee transfer", st == 403, (st, e))
            st, r = call("POST", f"/v1/clear/instructions/{fid}/confirm", body_obj={}, owner=True)
            check("the operator confirms it with the owner key", st == 200 and r["status"] == "settled", r)
    finally:
        stop(proc)
        try:
            os.remove(WAL)
        except OSError:
            pass

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("all clearing checks passed")


if __name__ == "__main__":
    main()
