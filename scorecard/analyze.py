#!/usr/bin/env python3
"""Turn cached transactions into what each trade really cost the trader.

    python3 analyze.py            # writes data/summary.json and data/trades.jsonl

For every transaction that paid an app's fee wallet, this finds the trader, how much SOL the trade
was worth, and four costs, all read straight off the transaction's balance changes:

  app fee      SOL that landed in the app's published fee wallets
  network fee  Solana's base + priority fee (only when the trader paid it)
  tips         SOL sent to known landing-service tip accounts (Jito, Nozomi, Axiom's published one)
  total        the three added up

What it does NOT measure yet, and says so on the page: slippage, losses to front-running, fees
taken in tokens other than SOL, and tips to landing services whose accounts are not published.
Every cost here is therefore a floor: the real cost is at least this much.
"""
import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "raw")
WSOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
# Trades below this are dust or account housekeeping, not trades anyone would compare apps on.
MIN_TRADE_LAMPORTS = 1_000_000  # 0.001 SOL


def keys_of(tx):
    msg = tx["transaction"]["message"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
    signers = {k["pubkey"] for k in msg["accountKeys"] if isinstance(k, dict) and k.get("signer")}
    n = len(tx["meta"]["preBalances"])
    if len(keys) < n:  # some encodings list lookup-table accounts separately
        la = tx["meta"].get("loadedAddresses") or {}
        keys += la.get("writable", []) + la.get("readonly", [])
    return keys[:n], signers


def analyze_tx(tx, fee_wallets, tip_accounts):
    """Returns one trade record, or (None, reason) when the transaction is not a priceable trade."""
    meta = tx["meta"]
    if meta.get("err") is not None:
        return None, "failed"
    keys, signers = keys_of(tx)
    pre, post = meta["preBalances"], meta["postBalances"]
    delta = {keys[i]: post[i] - pre[i] for i in range(len(keys))}

    # Token balances, keyed by account index.
    tok_pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or []}
    tok_post = {b["accountIndex"]: b for b in meta.get("postTokenBalances") or []}

    def amt(b):
        return int(b["uiTokenAmount"]["amount"]) if b else 0

    # The trader is the signer whose non-SOL token holdings changed. Pools and vaults change too,
    # but they never sign.
    trader, best = None, 0
    for owner in signers:
        moved = 0
        for idx in set(tok_pre) | set(tok_post):
            b = tok_post.get(idx) or tok_pre.get(idx)
            if b["owner"] == owner and b["mint"] != WSOL:
                moved += abs(amt(tok_post.get(idx)) - amt(tok_pre.get(idx)))
        if moved > best:
            trader, best = owner, moved
    if trader is None:
        return None, "no token swap by a signer"

    # The trader's SOL position: native lamports plus wrapped SOL held in token accounts.
    net = delta.get(trader, 0)
    rent_adj = 0
    for idx in set(tok_pre) | set(tok_post):
        b = tok_post.get(idx) or tok_pre.get(idx)
        if b["owner"] != trader:
            continue
        is_wsol = b["mint"] == WSOL
        if is_wsol:
            net += amt(tok_post.get(idx)) - amt(tok_pre.get(idx))
        # A token account opened in this transaction holds a refundable deposit (rent), which is not
        # a trading cost; one closed in it hands that deposit back, which is not trade proceeds.
        if idx not in tok_pre and idx in tok_post:
            rent_adj += post[idx] - (amt(tok_post[idx]) if is_wsol else 0)
        elif idx in tok_pre and idx not in tok_post:
            rent_adj -= pre[idx] - (amt(tok_pre[idx]) if is_wsol else 0)

    app_fee = sum(max(0, delta.get(w, 0)) for w in fee_wallets)
    tips = sum(max(0, delta.get(t, 0)) for t in tip_accounts)
    fee_payer = keys[0]

    # Other accounts created in this transaction (e.g. a DEX's per-user tracking account) also take
    # a refundable deposit from whoever pays for the transaction. Not a trade, not a fee.
    if fee_payer == trader:
        token_idx = set(tok_pre) | set(tok_post)
        for i, k in enumerate(keys):
            if pre[i] == 0 and post[i] > 0 and i not in token_idx and k not in fee_wallets \
                    and k not in tip_accounts and k != trader:
                rent_adj += post[i]
    network_fee = meta["fee"] if fee_payer == trader else 0

    x = net + app_fee + tips + network_fee + rent_adj
    trade = abs(x)
    if trade < MIN_TRADE_LAMPORTS:
        return None, "no SOL leg (token-to-token or housekeeping)"
    if app_fee == 0:
        return None, "app fee not paid in SOL"
    return {
        "sig": tx["transaction"]["signatures"][0],
        "time": tx.get("blockTime"),
        "side": "buy" if x < 0 else "sell",
        "trade_sol": trade / LAMPORTS,
        "app_fee_sol": app_fee / LAMPORTS,
        "network_fee_sol": network_fee / LAMPORTS,
        "tips_sol": tips / LAMPORTS,
        "trader_paid_network_fee": fee_payer == trader,
    }, None


def summarize(app, trades):
    if not trades:
        return {"app": app, "trades": 0}
    vol = sum(t["trade_sol"] for t in trades)
    cost = lambda t: t["app_fee_sol"] + t["network_fee_sol"] + t["tips_sol"]
    pct = sorted(100 * cost(t) / t["trade_sol"] for t in trades)
    sizes = sorted(t["trade_sol"] for t in trades)
    times = [t["time"] for t in trades if t["time"]]
    return {
        "app": app,
        "trades": len(trades),
        "volume_sol": vol,
        "median_trade_sol": statistics.median(sizes),
        # What the whole sample paid per 100 SOL traded: the app's real take on its volume.
        "cost_per_100": {
            "app_fee": 100 * sum(t["app_fee_sol"] for t in trades) / vol,
            "network_fee": 100 * sum(t["network_fee_sol"] for t in trades) / vol,
            "tips": 100 * sum(t["tips_sol"] for t in trades) / vol,
            "total": 100 * sum(cost(t) for t in trades) / vol,
        },
        # What a typical trade paid, as a share of its size. Small trades pay far more than the
        # headline fee because tips and network fees are flat, and this is where that shows.
        "typical_trade_cost_pct": statistics.median(pct),
        "worst_10pct_trade_cost_pct": pct[int(0.9 * (len(pct) - 1))],
        "window": [min(times), max(times)] if times else None,
        "examples": [t["sig"] for t in trades[:5]],
    }


def main():
    with open(os.path.join(HERE, "sources.json")) as f:
        sources = json.load(f)
    with open(os.path.join(HERE, "data", "index.json")) as f:
        index = json.load(f)
    tips = {a for spec in sources["tip_accounts"].values() for a in spec["accounts"]}

    summary, skipped = [], {}
    with open(os.path.join(HERE, "data", "trades.jsonl"), "w") as out:
        for app, sigs in index["apps"].items():
            wallets = set(sources["apps"][app]["fee_wallets"])
            trades = []
            for sig in sigs:
                path = os.path.join(RAW, sig + ".json")
                if not os.path.exists(path):
                    continue
                with open(path) as f:
                    rec, why = analyze_tx(json.load(f), wallets, tips)
                if rec:
                    rec["app"] = app
                    trades.append(rec)
                    out.write(json.dumps(rec) + "\n")
                else:
                    skipped.setdefault(app, {}).setdefault(why, 0)
                    skipped[app][why] += 1
            s = summarize(app, trades)
            s["skipped"] = skipped.get(app, {})
            summary.append(s)

    ranked = sorted((s for s in summary if s["trades"]), key=lambda s: s["cost_per_100"]["total"])
    result = {"collected_at": index["collected_at"], "rpc": index["rpc"], "apps": ranked,
              "not_enough_data": [s["app"] for s in summary if not s["trades"]]}
    with open(os.path.join(HERE, "data", "summary.json"), "w") as f:
        json.dump(result, f, indent=1)

    print(f"{'app':12s} {'trades':>6s} {'median SOL':>10s} {'per 100 SOL':>11s} {'typical %':>9s}  app fee / network / tips")
    for s in ranked:
        c = s["cost_per_100"]
        print(f"{s['app']:12s} {s['trades']:6d} {s['median_trade_sol']:10.3f} {c['total']:11.3f} "
              f"{s['typical_trade_cost_pct']:9.3f}  {c['app_fee']:.3f} / {c['network_fee']:.3f} / {c['tips']:.3f}")
    for app in result["not_enough_data"]:
        print(f"{app:12s} no priceable trades ({skipped.get(app)})")


if __name__ == "__main__":
    main()
