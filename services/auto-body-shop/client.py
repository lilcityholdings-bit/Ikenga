"""Agent-side client. Stdlib only; copy this file into your agent.

It is built to keep the shop *out* of your agent's critical path:

- get_config() caches the last good config on disk and revalidates with ETag. If the shop is
  unreachable, slow, or returns an error, you get the cached config back ("fail static"). The
  agent never fails to start or answer because this service is down, as long as it has fetched
  a config successfully once.
- report() sends telemetry from a background thread through a bounded queue. It never blocks and
  never raises; if the shop is down, reports are dropped (and counted), not retried forever.

    shop = Client("https://shop.example", key=AGENT_KEY, agent_id="bot_alpha")
    cfg = shop.get_config(session=conversation_id)      # sticky canary routing per session
    out = run_my_agent(cfg["config"], user_input)
    shop.report(cfg, input=user_input, output=out, latency_ms=elapsed, success=True, session=conversation_id)
"""
import json
import os
import queue
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request


class Client:
    def __init__(self, base_url, key, agent_id, cache_dir=None, timeout=2.0, max_queue=10_000):
        self.base, self.key, self.agent_id, self.timeout = base_url.rstrip("/"), key, agent_id, timeout
        self.cache_path = os.path.join(cache_dir or tempfile.gettempdir(), f"abs-config-{agent_id}.json")
        self.q = queue.Queue(max_queue)
        self.dropped = 0
        self.last_error = None
        threading.Thread(target=self._drain, daemon=True).start()

    def _request(self, method, path, body=None, headers=None):
        h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", **(headers or {})}
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.status, resp.headers, resp.read()

    def _cached(self):
        try:
            with open(self.cache_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def get_config(self, session=None):
        cached = self._cached()
        path = f"/v1/config/{urllib.parse.quote(self.agent_id)}"
        if session:
            path += "?session=" + urllib.parse.quote(session)
        headers = {}
        if cached and cached.get("_session") == session:
            headers["If-None-Match"] = '"%s"' % cached["fingerprint"]
        try:
            status, _, raw = self._request("GET", path, headers=headers)
            fresh = dict(json.loads(raw), _session=session)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(fresh, f)
            os.replace(tmp, self.cache_path)
            return fresh
        except urllib.error.HTTPError as e:
            if e.code == 304 and cached:
                return cached
            self.last_error = f"HTTP {e.code}"
        except (OSError, ValueError) as e:
            self.last_error = repr(e)
        if cached:
            return cached
        raise RuntimeError(f"No config available: shop unreachable ({self.last_error}) and nothing cached yet")

    def report(self, cfg, *, input=None, output=None, error=None, latency_ms, success, session=None):
        body = {"agent_id": self.agent_id, "version": cfg["version"], "latency_ms": float(latency_ms),
                "success": bool(success), "input": input, "output": output, "error": error, "session": session}
        try:
            self.q.put_nowait({k: v for k, v in body.items() if v is not None})
        except queue.Full:
            self.dropped += 1

    def report_sync(self, cfg, **kw):
        """Same as report() but waits and returns the response (with the trace's feedback_token)."""
        body = {"agent_id": self.agent_id, "version": cfg["version"], **{k: v for k, v in kw.items() if v is not None}}
        _, _, raw = self._request("POST", "/v1/telemetry", body)
        return json.loads(raw)

    def _drain(self):
        while True:
            body = self.q.get()
            try:
                self._request("POST", "/v1/telemetry", body)
            except (OSError, ValueError) as e:
                self.dropped += 1
                self.last_error = repr(e)
