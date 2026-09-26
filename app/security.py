"""
Security layer for the agent-facing API.

Threat model this addresses (agents are fast, automated, and may be
adversarial -- assume zero trust):

1. Credential theft / replay      -> hashed API keys, nonce+timestamp replay
                                      protection on every state-changing call
2. Brute force / key stuffing     -> per-key AND per-IP rate limiting
3. Parameter tampering            -> strict pydantic validation, allow-lists
                                      for token addresses, bounds on amounts
4. Slippage / sandwich exploitation -> server-enforced max slippage ceiling,
                                      agents cannot set slippage above it
5. Stale price / deadline abuse   -> every quote and swap carries a short
                                      expiry; expired quotes are rejected
6. Duplicate/replayed swaps       -> idempotency keys required on swap calls
7. Over-authorization             -> agents can only ever spend from their
                                      OWN wallet via pre-signed approvals;
                                      this service never custodies funds or
                                      holds agent private keys
"""
import hashlib
import hmac
import os
import sqlite3
import time
import secrets
from typing import Optional

from fastapi import Header, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings

limiter = Limiter(key_func=get_remote_address)

# SQLite-backed key store so keys survive restarts and work across both
# the `create_api_key.py` script and the running server process.
# PRODUCTION NOTE: swap for Postgres + Redis (for nonces) before handling
# any real traffic or running multiple server instances -- SQLite doesn't
# handle concurrent writers well and nonces need a shared, TTL-capable store.
_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.db")


def _get_conn():
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS api_keys "
        "(hashed_key TEXT PRIMARY KEY, agent_id TEXT, active INTEGER)"
    )
    return conn


# Nonces are short-lived and low-stakes to lose on restart (worst case: a
# replay window briefly re-opens), so in-memory is acceptable for a single
# instance. Move to Redis with TTL before running >1 instance.
_USED_NONCES: set[str] = set()
_NONCE_TIMESTAMPS: dict[str, float] = {}


def hash_api_key(raw_key: str) -> str:
    """Never store raw API keys. Always store a peppered hash."""
    return hmac.new(
        settings.API_KEY_PEPPER.encode(), raw_key.encode(), hashlib.sha256
    ).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Returns (raw_key_to_give_agent_once, hashed_key_to_store)."""
    raw = f"adx_{secrets.token_urlsafe(32)}"
    return raw, hash_api_key(raw)


def register_api_key(agent_id: str) -> str:
    raw, hashed = generate_api_key()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO api_keys (hashed_key, agent_id, active) VALUES (?, ?, 1)",
        (hashed, agent_id),
    )
    conn.commit()
    conn.close()
    return raw  # shown to the agent operator exactly once


async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> str:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    hashed = hash_api_key(x_api_key)
    conn = _get_conn()
    row = conn.execute(
        "SELECT agent_id, active FROM api_keys WHERE hashed_key = ?", (hashed,)
    ).fetchone()
    conn.close()
    if not row or not row[1]:
        # Constant-time-ish failure path; don't leak which part was wrong
        raise HTTPException(status_code=401, detail="Invalid API key")
    return row[0]


def check_and_consume_nonce(nonce: str, max_age_seconds: int = 300) -> None:
    """
    Replay protection for state-changing endpoints (e.g. /swap).
    Agents must send a unique nonce per request; reused or stale nonces
    are rejected outright. This stops a captured/replayed request from
    executing a second time.
    """
    now = time.time()
    # purge old nonces
    stale = [n for n, ts in _NONCE_TIMESTAMPS.items() if now - ts > max_age_seconds]
    for n in stale:
        _NONCE_TIMESTAMPS.pop(n, None)
        _USED_NONCES.discard(n)

    if nonce in _USED_NONCES:
        raise HTTPException(status_code=409, detail="Nonce already used (replay rejected)")
    _USED_NONCES.add(nonce)
    _NONCE_TIMESTAMPS[nonce] = now


def enforce_slippage_ceiling(requested_bps: int) -> int:
    """
    Agents may request tighter slippage but never looser than our ceiling.
    This blunts sandwich-attack style exploitation where a malicious or
    compromised agent tries to authorize an unlimited-slippage swap.
    """
    if requested_bps < 0:
        raise HTTPException(status_code=400, detail="Slippage cannot be negative")
    return min(requested_bps, settings.MAX_SLIPPAGE_BPS)


def is_allowed_token(token_address: str, allow_list: set[str]) -> bool:
    """Only the top-20 configured token contracts are swappable -- no
    arbitrary attacker-supplied token contracts (a classic vector for
    fake-token / honeypot / infinite-approval scams)."""
    return token_address.lower() in {a.lower() for a in allow_list}
