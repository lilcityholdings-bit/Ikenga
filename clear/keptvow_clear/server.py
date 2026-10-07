"""Clear's HTTP API and the service owner's dashboard. Standard library only.

    Public          POST /v1/services   POST /v1/owners   GET /health
    Service key     GET /v1/service   PUT /v1/service/prices|policy|stripe
                    POST /v1/check   POST /v1/usage   GET /v1/bills
    Owner key       POST /v1/agents   PATCH /v1/agents/{id}   GET /v1/owner
    Agent key       POST /v1/passes
    Admin secret    POST /v1/admin/close|send|sync   POST /v1/admin/bills/{id}/paid
                    POST /v1/admin/agents/{id}/keptvow-verified
    People          GET /   GET /dashboard   POST /login   POST /logout

Keys go in `Authorization: Bearer <key>`; the admin secret in `X-Admin-Secret`.
"""
import hmac
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import dashboard
from .billing import Denied, prev_period, valid_policy, valid_prices
from .store import period_of, now_ms

MAX_BODY = 64 * 1024
STATUS = {"UNAUTHORIZED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404, "UNKNOWN_SERVICE": 404,
          "UNKNOWN_AGENT": 404, "BAD_STATE": 409, "PERIOD_OPEN": 409}


class Err(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def make_server(clear, host="127.0.0.1", port=8400):
    store, cfg = clear.store, clear.cfg

    class H(BaseHTTPRequestHandler):
        server_version = "KeptvowClear/1"

        def log_message(self, *a):
            pass

        # -- plumbing ---------------------------------------------------------------------------
        def send(self, status, body, ctype="application/json", headers=()):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def json(self, status, obj):
            self.send(status, json.dumps(obj, indent=2, default=str))

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise Err(413, "TOO_BIG", "request body is over 64 KB")
            raw = self.rfile.read(n) if n else b""
            if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
            try:
                b = json.loads(raw or b"{}")
            except ValueError:
                raise Err(400, "BAD_BODY", "body must be JSON")
            if not isinstance(b, dict):
                raise Err(400, "BAD_BODY", "body must be a JSON object")
            return b

        def bearer(self):
            a = self.headers.get("Authorization", "")
            return a[7:].strip() if a.startswith("Bearer ") else ""

        def need_service(self):
            s = store.service_by_key(self.bearer())
            if not s:
                raise Err(401, "UNAUTHORIZED", "send your service key as 'Authorization: Bearer svc_key_...'")
            return s

        def need_owner(self):
            o = store.owner_by_key(self.bearer())
            if not o:
                raise Err(401, "UNAUTHORIZED", "send your owner key as 'Authorization: Bearer own_key_...'")
            return o

        def need_admin(self):
            if not hmac.compare_digest(self.headers.get("X-Admin-Secret", "").encode(), cfg.admin_secret.encode()):
                raise Err(401, "UNAUTHORIZED", "admin only: send X-Admin-Secret")

        def cookie(self, name):
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
            return None

        def handle_any(self, method):
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            try:
                self.route(method, path, [p for p in path.split("/") if p])
            except Err as e:
                self.json(e.status, {"code": e.code, "message": e.message})
            except Denied as d:
                self.json(STATUS.get(d.code, 400), {"code": d.code, "message": d.message})
            except ValueError as e:
                self.json(400, {"code": "BAD_INPUT", "message": str(e)})

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def do_PUT(self):
            self.handle_any("PUT")

        def do_PATCH(self):
            self.handle_any("PATCH")

        # -- routes -----------------------------------------------------------------------------
        def route(self, m, path, parts):
            if (m, path) == ("GET", "/health"):
                return self.json(200, {"ok": True, "mode": cfg.mode})
            if (m, path) == ("GET", "/"):
                return self.send(200, dashboard.landing(), "text/html; charset=utf-8")
            if (m, path) == ("GET", "/dashboard"):
                sid = store.session_service(self.cookie("clear_session"))
                if not sid:
                    return self.send(200, dashboard.login(), "text/html; charset=utf-8")
                return self.send(200, dashboard.service_page(clear, sid), "text/html; charset=utf-8",
                                 [("Cache-Control", "no-store")])
            if (m, path) == ("POST", "/login"):
                s = store.service_by_key(self.body().get("key", "").strip())
                if not s:
                    return self.send(401, dashboard.login("That key didn't match a service."), "text/html; charset=utf-8")
                tok = store.new_session(s["id"])
                return self.send(303, "", "text/plain", [("Location", "/dashboard"), (
                    "Set-Cookie", f"clear_session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200")])
            if (m, path) == ("POST", "/logout"):
                return self.send(303, "", "text/plain", [("Location", "/dashboard"),
                                                         ("Set-Cookie", "clear_session=; Max-Age=0; Path=/")])

            if (m, path) == ("POST", "/v1/services"):
                b = self.body()
                if not b.get("name") or not b.get("owner_name"):
                    raise Err(400, "BAD_INPUT", "name and owner_name are required")
                sid, key = store.create_service(str(b["name"])[:100], str(b["owner_name"])[:100], b.get("email"))
                return self.json(201, {"service_id": sid, "service_key": key,
                                       "note": "Save the key now; Clear keeps only a hash of it."})
            if (m, path) == ("POST", "/v1/owners"):
                b = self.body()
                if not b.get("name"):
                    raise Err(400, "BAD_INPUT", "name is required")
                oid, key = store.create_owner(str(b["name"])[:100], b.get("email"))
                return self.json(201, {"owner_id": oid, "owner_key": key,
                                       "note": "Save the key now; Clear keeps only a hash of it."})

            # service owner
            if (m, path) == ("GET", "/v1/service"):
                s = self.need_service()
                return self.json(200, {"id": s["id"], "name": s["name"], "prices_microdollars": s["prices"],
                                       "policy": s["policy"], "stripe_account": s["stripe_account"],
                                       "summary": clear.service_summary(s["id"])})
            if m == "PUT" and path in ("/v1/service/prices", "/v1/service/policy", "/v1/service/stripe"):
                s, b = self.need_service(), self.body()
                if path.endswith("prices"):
                    store.update_service(s["id"], prices=valid_prices(b))
                elif path.endswith("policy"):
                    store.update_service(s["id"], policy=valid_policy(b))
                else:
                    acct = str(b.get("stripe_account", ""))
                    if not acct.startswith("acct_"):
                        raise Err(400, "BAD_INPUT", "stripe_account must look like acct_...")
                    store.update_service(s["id"], stripe_account=acct)
                return self.json(200, {"ok": True})
            if (m, path) == ("POST", "/v1/check"):
                s, b = self.need_service(), self.body()
                return self.json(200, clear.check(s, b.get("pass"), b.get("tool"), b.get("units", 1), b.get("ref")))
            if (m, path) == ("POST", "/v1/usage"):
                s, b = self.need_service(), self.body()
                u, new = clear.record(s, b.get("pass"), b.get("tool"), b.get("ref"), b.get("units", 1))
                return self.json(201 if new else 200, {"counted": new, "ref": u["ref"], "period": u["period"],
                                                       "note": "Recorded. No money moved."})
            if (m, path) == ("GET", "/v1/bills"):
                return self.json(200, {"bills": clear.bills(service_id=self.need_service()["id"])})

            # agent owner
            if (m, path) == ("POST", "/v1/agents"):
                o, b = self.need_owner(), self.body()
                if not b.get("name"):
                    raise Err(400, "BAD_INPUT", "name is required")
                cap = b.get("monthly_cap")
                if cap is not None and (not isinstance(cap, int) or cap < 0):
                    raise Err(400, "BAD_INPUT", "monthly_cap is whole micro-dollars, or null")
                aid, key = store.create_agent(o["id"], str(b["name"])[:100], cap, b.get("keptvow_id"))
                return self.json(201, {"agent_id": aid, "agent_key": key,
                                       "note": "Give the key to the agent. Save it now; Clear keeps only a hash."})
            if m == "PATCH" and len(parts) == 3 and parts[:2] == ["v1", "agents"]:
                o, b = self.need_owner(), self.body()
                a = store.agent(parts[2])
                if not a or a["owner_id"] != o["id"]:
                    raise Err(404, "NOT_FOUND", "no such agent of yours")
                if "monthly_cap" in b:
                    cap = b["monthly_cap"]
                    if cap is not None and (not isinstance(cap, int) or cap < 0):
                        raise Err(400, "BAD_INPUT", "monthly_cap is whole micro-dollars, or null")
                    store.run("UPDATE agents SET monthly_cap=? WHERE id=?", cap, a["id"])
                if "active" in b:
                    store.run("UPDATE agents SET active=? WHERE id=?", 1 if b["active"] else 0, a["id"])
                return self.json(200, {"ok": True})
            if (m, path) == ("GET", "/v1/owner"):
                return self.json(200, clear.owner_summary(self.need_owner()["id"]))

            # agent
            if (m, path) == ("POST", "/v1/passes"):
                return self.json(201, clear.issue_pass(self.bearer(), str(self.body().get("service_id", ""))))

            # operator
            if m == "POST" and parts[:2] == ["v1", "admin"]:
                self.need_admin()
                b = self.body()
                if parts[2:] == ["close"]:
                    p = b.get("period") or prev_period(period_of(now_ms()))
                    return self.json(200, {"period": p, "bills": clear.close_period(p, force=bool(b.get("force")))})
                if parts[2:] == ["send"]:
                    return self.json(200, clear.send_bills())
                if parts[2:] == ["sync"]:
                    return self.json(200, clear.sync())
                if len(parts) == 5 and parts[2] == "bills" and parts[4] == "paid":
                    clear.mark_paid(parts[3], b.get("note") or "marked paid by the operator")
                    return self.json(200, {"ok": True})
                if len(parts) == 5 and parts[2] == "agents" and parts[4] == "keptvow-verified":
                    if not store.agent(parts[3]):
                        raise Err(404, "NOT_FOUND", "no such agent")
                    store.run("UPDATE agents SET keptvow_verified=1 WHERE id=?", parts[3])
                    return self.json(200, {"ok": True})
            raise Err(404, "NOT_FOUND", "no such path")

    return ThreadingHTTPServer((host, port), H)


def background(clear, every_secs):
    """Once a month close last month; every few minutes send ready bills and check payments."""
    def loop():
        while True:
            try:
                clear.close_period(prev_period(period_of(now_ms())))
                clear.send_bills()
                clear.sync()
            except Exception as e:  # keep going; the next pass retries
                print("background pass failed:", e)
            time.sleep(every_secs)
    threading.Thread(target=loop, daemon=True).start()
