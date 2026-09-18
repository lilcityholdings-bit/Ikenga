#!/usr/bin/env python3
"""Reference market maker for Ikenga — posts real two-sided liquidity so the book is never empty.

    python3 market_maker.py                       # against a local venue, BTC-USD
    python3 market_maker.py https://your.host ETH-USD

This is NOT a volume generator and it is NOT designed to be profitable. Its only job is that an
agent arriving to trade finds a quote instead of an empty book. It will lose money to
better-informed counterparties — that is the expected cost of having a market, not a bug — and
the loss limit below exists to bound that cost, not eliminate it.

Read this before pointing it at real capital:

  - The reference price defaults to a manually supplied constant (MM_REF_PRICE) so this can be
    demoed and tested without live network access. Replace it with a real external feed (Ikenga's
    own GET /v1/route against coinbase/kraken/gemini/bitstamp, or your own) before this quotes
    anything real. A market maker that skews around a stale or fabricated reference price is
    strictly worse than no market maker at all.
  - Inventory caps and the loss limit are the only things standing between this and an unbounded
    position. They are deliberately conservative defaults, not tuned parameters — tighten them,
    don't loosen them, until you have a reason grounded in real fill data.
  - Self-trade prevention on the server means this bot's own resting order gets cancelled if its
    own next order would cross it — it cannot accidentally trade with itself, but that also means
    a naive re-quote loop (cancel, then immediately re-place at a price that crosses the order you
    haven't heard was cancelled yet) can lose you a queue position. This bot cancels and waits for
    the ack before placing the replacement, for exactly that reason.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IKENGA_BASE", "http://127.0.0.1:8080")).rstrip("/")
SYMBOL = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("IKENGA_SYMBOL", "BTC-USD")
KEYFILE = os.environ.get("IKENGA_MM_KEYFILE", "market-maker-agent.json")

# ---------------------------------------------------------------------------------------------
# Risk controls. All conservative on purpose — see the module docstring.
# ---------------------------------------------------------------------------------------------
QUOTE_ASSET = SYMBOL.split("-")[-1]
BASE_ASSET = SYMBOL.split("-")[0]

HALF_SPREAD_BPS = float(os.environ.get("IKENGA_MM_HALF_SPREAD_BPS", "20"))       # 0.20% each side
SKEW_COEFF = float(os.environ.get("IKENGA_MM_SKEW_COEFF", "0.6"))               # how hard inventory bends the mid
QUOTE_SIZE = float(os.environ.get("IKENGA_MM_QUOTE_SIZE", "0.05"))              # size per side, in base asset
POSITION_CAP = float(os.environ.get("IKENGA_MM_POSITION_CAP", "0.5"))           # max |inventory|, in base asset
LOSS_LIMIT_USD = float(os.environ.get("IKENGA_MM_LOSS_LIMIT_USD", "500"))       # halt if mark-to-market drops this far
PRICE_JUMP_HALT_BPS = float(os.environ.get("IKENGA_MM_PRICE_JUMP_HALT_BPS", "150"))  # pull quotes on a >1.5% tick
REQUOTE_INTERVAL_SEC = float(os.environ.get("IKENGA_MM_INTERVAL_SEC", "5"))

_running = True


def _handle_shutdown(signum, _frame):
    global _running
    print(f"\n[shutdown] signal {signum} received — cancelling all resting orders before exit")
    _running = False


signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


# ---------------------------------------------------------------------------------------------
# Signing — identical scheme to examples/agent.py. See that file for why it's built this way.
# ---------------------------------------------------------------------------------------------

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    def new_key():
        k = Ed25519PrivateKey.generate()
        seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return seed.hex(), pub.hex()

    def sign(seed_hex, message: bytes) -> str:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex)).sign(message).hex()

except BaseException:
    # Not just ImportError: a broken/mismatched native extension for `cryptography` raises a
    # pyo3_runtime.PanicException at import time, which is a BaseException, not an Exception —
    # `except ImportError` and even `except Exception` both miss it.
    PKCS8 = bytes.fromhex("302e020100300506032b657004220420")

    def new_key():
        with tempfile.NamedTemporaryFile(suffix=".pem") as f:
            subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", f.name],
                            check=True, capture_output=True)
            priv = subprocess.run(["openssl", "pkey", "-in", f.name, "-outform", "DER"],
                                   check=True, capture_output=True).stdout
            pub = subprocess.run(["openssl", "pkey", "-in", f.name, "-pubout", "-outform", "DER"],
                                  check=True, capture_output=True).stdout
        return priv[16:].hex(), pub[12:].hex()

    def sign(seed_hex, message: bytes) -> str:
        import base64
        pem = ("-----BEGIN PRIVATE KEY-----\n"
               + base64.b64encode(PKCS8 + bytes.fromhex(seed_hex)).decode()
               + "\n-----END PRIVATE KEY-----\n").encode()
        with tempfile.NamedTemporaryFile() as k, tempfile.NamedTemporaryFile() as m, \
             tempfile.NamedTemporaryFile() as s:
            k.write(pem); k.flush()
            m.write(message); m.flush()
            subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", k.name, "-rawin",
                            "-in", m.name, "-out", s.name], check=True, capture_output=True)
            return s.read().hex()


class Ikenga:
    def __init__(self, base, seed=None, agent_id=None):
        self.base, self.seed, self.agent_id = base, seed, agent_id

    def call(self, method, path, body=None, signed=False, signing_id=None):
        raw = json.dumps(body).encode() if body is not None else b""
        headers = {"Content-Type": "application/json"} if raw else {}
        if signed:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            nonce = f"{time.time_ns()}-{os.getpid()}"
            message = method.encode() + path.encode() + ts.encode() + nonce.encode() + raw
            headers.update({
                "X-Agent-ID": signing_id or self.agent_id or "registration",
                "X-Timestamp": ts,
                "X-Nonce": nonce,
                "X-Signature": sign(self.seed, message),
            })
        req = urllib.request.Request(self.base + path, data=raw or None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {}
        except urllib.error.URLError as e:
            return 0, {"code": "UNREACHABLE", "message": str(e)}

    def get(self, path):
        return self.call("GET", path)[1]


def _load_keyfile():
    return json.load(open(KEYFILE)) if os.path.exists(KEYFILE) else {}


def _save_keyfile(data):
    json.dump(data, open(KEYFILE, "w"))
    os.chmod(KEYFILE, 0o600)


def load_or_register(base):
    saved = _load_keyfile()
    if saved:
        print(f"resuming as {saved['agent_id']}")
        return Ikenga(base, saved["seed"], saved["agent_id"])

    seed, pub = new_key()
    client = Ikenga(base, seed)
    status, reg = client.call("POST", "/v1/agents", {"pubkey_hex": pub}, signed=True, signing_id="registration")
    if status not in (200, 201):
        print(f"registration failed: {status}")
        print(json.dumps(reg, indent=2))
        sys.exit(1)
    client.agent_id = reg["agent_id"]
    _save_keyfile({"seed": seed, "agent_id": client.agent_id})
    print(f"registered market maker as {client.agent_id}")
    return client


def get_target_inventory(client: Ikenga, ref_price: float):
    """The baseline this bot skews and caps against — set once, on this bot's first-ever run
    with a usable reference price, and reused across every restart after that.

    Deliberately NOT re-derived from the current balance on every process start: a bot that
    crashes and restarts under a supervisor would otherwise treat "wherever I happen to be right
    now" as the new zero point every time, which quietly disables the position cap and skew for
    exactly the case (repeated crashes) where risk controls matter most.
    """
    saved = _load_keyfile()
    if "target_base" in saved:
        return saved["target_base"], saved["target_quote"], saved["target_ref_price"]

    base_bal, quote_bal, _ = get_inventory(client)
    saved.update({
        "target_base": base_bal, "target_quote": quote_bal, "target_ref_price": ref_price,
    })
    _save_keyfile(saved)
    print(f"baseline set: {base_bal} {BASE_ASSET} / {quote_bal} {QUOTE_ASSET} @ ref {ref_price:.2f} "
          f"(persisted — future restarts skew and cap against this, not their own start balance)")
    return base_bal, quote_bal, ref_price


# ---------------------------------------------------------------------------------------------
# Reference price. Real deployments should replace get_reference_price with a live feed — see
# the module docstring. IKENGA_MM_REF_PRICE lets this run and be tested without one.
# ---------------------------------------------------------------------------------------------

def get_reference_price(client: Ikenga):
    """Returns (price, source) or (None, reason) if no reference is available right now."""
    static = os.environ.get("IKENGA_MM_REF_PRICE")
    if static:
        try:
            return float(static), "IKENGA_MM_REF_PRICE (static/manual)"
        except ValueError:
            pass

    status, route = client.call(
        "GET", f"/v1/route?sell={BASE_ASSET}&buy={QUOTE_ASSET}&amount=1", signed=False,
    )
    if status == 200 and route.get("net_buy_amount"):
        return float(route["net_buy_amount"]), f"/v1/route ({route.get('source', 'external')})"
    return None, f"no reference available (route status {status}: {route.get('message', route.get('code', ''))})"


def get_account(client: Ikenga):
    # /v1/account is a signed endpoint. client.get() is the unsigned convenience meant for public
    # reads (spec, board, quote) — using it here would silently 401 and this bot would compute
    # every risk control (inventory, PnL, what to cancel) against an empty account forever.
    status, account = client.call("GET", "/v1/account", signed=True)
    if status != 200:
        print(f"  ! /v1/account returned {status}: {account.get('message', account)}")
        return {}
    return account


def cancel_all_open_orders(client: Ikenga):
    account = get_account(client)
    for order in account.get("open_orders", []):
        if order.get("symbol") != SYMBOL:
            continue
        status, _ = client.call("DELETE", f"/v1/orders/{order['order_id']}", signed=True)
        print(f"  cancelled {order['order_id']} ({'ok' if status == 200 else status})")


def get_inventory(client: Ikenga):
    account = get_account(client)
    balances = {b["asset"]: b["balance"] for b in account.get("balances", [])}
    open_orders = [o for o in account.get("open_orders", []) if o.get("symbol") == SYMBOL]
    return balances.get(BASE_ASSET, 0.0), balances.get(QUOTE_ASSET, 0.0), open_orders


def round_to_tick(price: float) -> float:
    return round(price, 2)


def main():
    print(f"venue: {BASE}  symbol: {SYMBOL}")
    print(f"caps: position ±{POSITION_CAP} {BASE_ASSET}, loss limit ${LOSS_LIMIT_USD}, "
          f"half-spread {HALF_SPREAD_BPS}bps, requote every {REQUOTE_INTERVAL_SEC}s")
    client = load_or_register(BASE)

    target_base = target_quote = target_ref_price = None
    last_ref_price = None

    while _running:
        price, source = get_reference_price(client)

        if price is None:
            print(f"[{time.strftime('%H:%M:%S')}] no reference price ({source}) — pulling quotes")
            cancel_all_open_orders(client)
            time.sleep(REQUOTE_INTERVAL_SEC)
            continue

        if last_ref_price is not None:
            jump_bps = abs(price - last_ref_price) / last_ref_price * 10_000
            if jump_bps > PRICE_JUMP_HALT_BPS:
                print(f"[{time.strftime('%H:%M:%S')}] reference jumped {jump_bps:.0f}bps in one "
                      f"cycle ({last_ref_price} -> {price}) — halting this round, pulling quotes")
                cancel_all_open_orders(client)
                last_ref_price = price
                time.sleep(REQUOTE_INTERVAL_SEC)
                continue
        last_ref_price = price

        if target_base is None:
            target_base, target_quote, target_ref_price = get_target_inventory(client, price)

        base_bal, quote_bal, open_orders = get_inventory(client)
        inventory = base_bal - target_base  # +ve = long relative to this bot's persisted baseline

        equity_now = base_bal * price + quote_bal
        equity_start = target_base * target_ref_price + target_quote
        pnl = equity_now - equity_start
        if pnl < -LOSS_LIMIT_USD:
            print(f"[{time.strftime('%H:%M:%S')}] mark-to-market PnL {pnl:+.2f} breached loss "
                  f"limit -${LOSS_LIMIT_USD} — halting and cancelling all quotes")
            cancel_all_open_orders(client)
            break

        # Skew the mid against inventory: long -> quote lower (sell it off), short -> quote
        # higher (buy it back). Clamp at the cap so skew alone can never fully close a position
        # it's also supposed to be limiting.
        skew_ratio = max(-1.0, min(1.0, inventory / POSITION_CAP)) if POSITION_CAP > 0 else 0.0
        skewed_mid = price * (1 - SKEW_COEFF * skew_ratio * (HALF_SPREAD_BPS / 10_000))

        half_spread = skewed_mid * (HALF_SPREAD_BPS / 10_000)
        bid = round_to_tick(skewed_mid - half_spread)
        ask = round_to_tick(skewed_mid + half_spread)

        quote_bid = inventory < POSITION_CAP
        quote_ask = inventory > -POSITION_CAP

        print(f"[{time.strftime('%H:%M:%S')}] ref={price:.2f} ({source}) inventory={inventory:+.4f} "
              f"pnl={pnl:+.2f} -> bid={bid if quote_bid else 'off'} ask={ask if quote_ask else 'off'}")

        cancel_all_open_orders(client)

        if quote_bid:
            status, resp = client.call("POST", "/v1/orders", {
                "symbol": SYMBOL, "side": "buy", "order_type": "limit",
                "price": bid, "qty": QUOTE_SIZE,
            }, signed=True)
            if status != 200:
                print(f"  bid rejected: {resp.get('code')} — {resp.get('message')}")
        else:
            print(f"  bid suppressed — at position cap")

        if quote_ask:
            status, resp = client.call("POST", "/v1/orders", {
                "symbol": SYMBOL, "side": "sell", "order_type": "limit",
                "price": ask, "qty": QUOTE_SIZE,
            }, signed=True)
            if status != 200:
                print(f"  ask rejected: {resp.get('code')} — {resp.get('message')}")
        else:
            print(f"  ask suppressed — at position cap")

        for _ in range(int(REQUOTE_INTERVAL_SEC * 10)):
            if not _running:
                break
            time.sleep(0.1)

    print("cancelling all resting orders before exit...")
    cancel_all_open_orders(client)
    print("done.")


if __name__ == "__main__":
    main()
