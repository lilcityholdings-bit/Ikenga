#!/usr/bin/env python3
"""Pull recent trades for every app in sources.json from Solana and cache them on disk.

    python3 collect.py --per-app 150

Uses Helius when HELIUS_API_KEY is set, and Solana's free public server otherwise. The public
server is rate-limited, so this goes slowly and backs off when told to; a sample of a few hundred
trades per app takes a few minutes.

Only successful transactions are kept. Raw responses are cached under data/raw/ by signature, so
re-running never fetches the same transaction twice and anyone can re-check a number against the
exact transaction it came from.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "raw")
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def rpc_url():
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    return f"https://mainnet.helius-rpc.com/?api-key={key}" if key else PUBLIC_RPC


def call(method, params, min_gap=[0.0]):
    """One JSON-RPC call, paced and retried. The public server allows roughly a few calls a second
    per caller; going faster just earns 429s, so the pace is set below that rather than above it."""
    gap = 0.05 if "helius" in rpc_url() else 0.35
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(8):
        wait = min_gap[0] + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        min_gap[0] = time.time()
        req = urllib.request.Request(rpc_url(), data=body, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(min(30, 2 ** attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(min(30, 2 ** attempt))
            continue
        if "error" in out:
            # Rate limits sometimes come back as a JSON-RPC error rather than an HTTP status.
            if out["error"].get("code") in (-32005, 429, -32429):
                time.sleep(min(30, 2 ** attempt))
                continue
            raise RuntimeError(f"{method}: {out['error']}")
        return out["result"]
    raise RuntimeError(f"{method}: gave up after repeated rate limits")


def recent_signatures(wallet, limit):
    sigs = call("getSignaturesForAddress", [wallet, {"limit": min(limit, 1000)}])
    return [s for s in sigs if s.get("err") is None]


def fetch_tx(sig):
    path = os.path.join(RAW, sig + ".json")
    if os.path.exists(path):
        return True
    tx = call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                                        "commitment": "finalized"}])
    if tx is None:
        return False
    with open(path, "w") as f:
        json.dump(tx, f)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-app", type=int, default=150, help="most recent fee-paying transactions to sample per app")
    ap.add_argument("--apps", nargs="*", help="only these apps (default: all in sources.json)")
    args = ap.parse_args()

    os.makedirs(RAW, exist_ok=True)
    with open(os.path.join(HERE, "sources.json")) as f:
        sources = json.load(f)

    print(f"source: {'Helius' if 'helius' in rpc_url() else 'Solana public RPC (slow; set HELIUS_API_KEY for full days)'}")
    index = {}
    for app, spec in sources["apps"].items():
        if args.apps and app not in args.apps:
            continue
        # Ask each fee wallet for its most recent payments and keep the newest overall, so an app
        # that spreads fees across many wallets is sampled across all of them.
        seen = {}
        per_wallet = max(10, args.per_app // max(1, len(spec["fee_wallets"])) + 10)
        for w in spec["fee_wallets"]:
            try:
                for s in recent_signatures(w, per_wallet):
                    seen.setdefault(s["signature"], s.get("blockTime") or 0)
            except RuntimeError as e:
                print(f"  {app}: {w[:8]}... skipped ({e})", file=sys.stderr)
        newest = sorted(seen.items(), key=lambda kv: -kv[1])[: args.per_app]
        got = 0
        for sig, _ in newest:
            try:
                if fetch_tx(sig):
                    got += 1
            except RuntimeError as e:
                print(f"  {app}: {sig[:10]}... skipped ({e})", file=sys.stderr)
        index[app] = [sig for sig, _ in newest]
        print(f"{app:12s} {got} transactions")

    with open(os.path.join(HERE, "data", "index.json"), "w") as f:
        json.dump({"collected_at": int(time.time()), "rpc": "helius" if "helius" in rpc_url() else "public",
                   "apps": index}, f)


if __name__ == "__main__":
    main()
