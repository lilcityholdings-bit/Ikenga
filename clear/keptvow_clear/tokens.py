"""Call passes: how a service knows which agent is calling, without seeing the agent's key.

An agent swaps its own key for a short-lived pass made out to one service. The service sends the
pass to Clear with each check and usage report. A pass made out to service A is refused for
service B, so a service can't reuse an agent's pass to run up charges elsewhere.

Format: v1.<base64url payload>.<base64url HMAC-SHA256>, signed with CLEAR_TOKEN_SECRET.
"""
import base64
import hashlib
import hmac
import json
import time

DEFAULT_TTL_SECS = 15 * 60


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(secret, agent_id, service_id, ttl=DEFAULT_TTL_SECS, now=None):
    exp = int((now or time.time()) + ttl)
    payload = _b64(json.dumps({"a": agent_id, "s": service_id, "e": exp}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    return f"v1.{payload}.{sig}", exp


def verify(secret, token, service_id, now=None):
    """Returns the agent id, or raises ValueError with a plain reason."""
    try:
        version, payload, sig = (token or "").split(".")
    except ValueError:
        raise ValueError("the call pass is malformed")
    want = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    if version != "v1" or not hmac.compare_digest(want, sig):
        raise ValueError("the call pass is not valid")
    body = json.loads(_unb64(payload))
    if body["s"] != service_id:
        raise ValueError("this call pass was made out to a different service")
    if body["e"] < (now or time.time()):
        raise ValueError("the call pass has expired; the agent should get a new one")
    return body["a"]
