"""Small clients for the two sides. Standard library only.

Service owner (an MCP server or API):

    clear = ClearService("https://clear.keptvow.com", os.environ["CLEAR_SERVICE_KEY"])

    def handle(request):
        with clear.gate(request.headers["X-Clear-Pass"], "get_forecast", ref=request.id) as call:
            if not call.allowed:
                return 402, call.reason          # don't serve
            return 200, do_the_work()            # counted only if this doesn't raise

Agent:

    agent = ClearAgent("https://clear.keptvow.com", os.environ["CLEAR_AGENT_KEY"])
    headers = {"X-Clear-Pass": agent.pass_for("svc_...")}   # cached until shortly before it expires
"""
import json
import threading
import time
import urllib.error
import urllib.request


class ClearError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


def _call(base, method, path, key, body=None, timeout=5):
    req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read())
        except Exception:
            j = {}
        raise ClearError(e.code, j.get("code", "HTTP_ERROR"), j.get("message", str(e)))


class _Call:
    def __init__(self, svc, pass_, tool, ref, units):
        self.svc, self.pass_, self.tool, self.ref, self.units = svc, pass_, tool, ref, units
        self.allowed, self.reason, self.code, self.counted = False, "", "", False

    def __enter__(self):
        try:
            r = self.svc.check(self.pass_, self.tool, self.units, self.ref)
        except ClearError as e:
            r = {"allow": False, "code": e.code, "reason": e.message}
        self.allowed, self.code, self.reason = r["allow"], r.get("code", ""), r.get("reason", "")
        return self

    def __exit__(self, exc_type, *_):
        if self.allowed and exc_type is None:
            self.counted = self.svc.record(self.pass_, self.tool, self.ref, self.units)["counted"]
        return False


class ClearService:
    def __init__(self, url, service_key):
        self.url, self.key = url, service_key

    def check(self, pass_, tool, units=1, ref=None):
        return _call(self.url, "POST", "/v1/check", self.key, {"pass": pass_, "tool": tool, "units": units, "ref": ref})

    def record(self, pass_, tool, ref, units=1):
        return _call(self.url, "POST", "/v1/usage", self.key, {"pass": pass_, "tool": tool, "ref": ref, "units": units})

    def gate(self, pass_, tool, ref, units=1):
        """Check before serving; count after serving, only if serving succeeded."""
        return _Call(self, pass_, tool, ref, units)


class ClearAgent:
    def __init__(self, url, agent_key):
        self.url, self.key = url, agent_key
        self._passes, self._lock = {}, threading.Lock()

    def pass_for(self, service_id):
        with self._lock:
            hit = self._passes.get(service_id)
            if hit and hit[1] - 60 > time.time():
                return hit[0]
        r = _call(self.url, "POST", "/v1/passes", self.key, {"service_id": service_id})
        with self._lock:
            self._passes[service_id] = (r["pass"], r["expires_at"])
        return r["pass"]
