"""Talking to Keptvow: read an agent's trust level, and report how it paid.

Reading uses Keptvow's public endpoint, GET /v1/trust/{agent_id}, which needs no key. If Keptvow
can't be reached the agent is treated as "unknown", so an outage makes Clear stricter, not looser.

Reporting uses POST /v1/attestations. Keptvow weighs a new reporting source at zero until it has
standing, so these reports are recorded but move no scores until the Keptvow operator registers
Clear as a source.
"""
import json
import threading
import time
import urllib.parse
import urllib.request

LEVELS = ["caution", "unknown", "fair", "good", "excellent"]  # worst to best
CACHE_SECS = 600


class Keptvow:
    def __init__(self, base_url, source="clear", source_secret="", timeout=4):
        self.base, self.source, self.secret, self.timeout = base_url, source, source_secret, timeout
        self._cache, self._lock = {}, threading.Lock()

    def level(self, agent_id):
        with self._lock:
            hit = self._cache.get(agent_id)
            if hit and hit[1] > time.time():
                return hit[0]
        level = "unknown"
        try:
            url = f"{self.base}/v1/trust/{urllib.parse.quote(agent_id, safe='')}"
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                level = json.loads(r.read()).get("trust_level", "unknown")
        except Exception:
            level = "unknown"
        if level not in LEVELS:
            level = "unknown"
        with self._lock:
            self._cache[agent_id] = (level, time.time() + CACHE_SECS)
        return level

    def report(self, agent_id, event):
        """event: 'cleared_cleanly' (paid) or 'ghosted' (never paid). Returns True if sent."""
        if not self.secret:
            return False
        body = json.dumps({"source": self.source, "secret": self.secret, "subject": agent_id,
                           "event": event, "domain": "commerce"}).encode()
        req = urllib.request.Request(f"{self.base}/v1/attestations", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status == 200
        except Exception:
            return False


def at_least(level, floor):
    return LEVELS.index(level) >= LEVELS.index(floor)
