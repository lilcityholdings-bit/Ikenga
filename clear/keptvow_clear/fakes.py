"""Stand-ins for Stripe and Keptvow, for the demo and tests only.

They answer the handful of calls Clear makes, in the same shape the real services answer, so
Clear's own code runs unchanged against them. Passing against these proves Clear's logic, not that
real Stripe accepts every field: see the note in stripe_api.py.
"""
import json
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def fake_keptvow(levels):
    """levels: Keptvow id -> trust level. Anything else is 'unknown'. Records attestations."""
    reports = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            aid = urllib.parse.unquote(self.path.rsplit("/", 1)[-1])
            self._json(200, {"agent_id": aid, "trust_level": levels.get(aid, "unknown"), "score": 100})

        def do_POST(self):
            reports.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self._json(200, {"accepted": True})

    srv, url = _serve(H)
    srv.reports = reports
    return srv, url


def fake_stripe():
    """Customers and invoices, per connected account. srv.pay(id) marks an invoice paid."""
    state = {"customers": {}, "invoices": {}, "items": [], "idem": {}, "accounts": set()}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self):
            if not self.headers.get("Authorization", "").startswith("Bearer sk_test_"):
                self._json(401, {"error": {"message": "Invalid API Key provided"}})
                return None
            acct = self.headers.get("Stripe-Account")
            if not acct:
                self._json(400, {"error": {"message": "this stand-in expects a Stripe-Account header"}})
                return None
            state["accounts"].add(acct)
            return acct

        def do_GET(self):
            acct = self._guard()
            if not acct:
                return
            inv = state["invoices"].get(self.path.rsplit("/", 1)[-1])
            if not inv or inv["account"] != acct:
                return self._json(404, {"error": {"message": "No such invoice"}})
            self._json(200, inv)

        def do_POST(self):
            acct = self._guard()
            if not acct:
                return
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"] or 0)).decode()).items()}
            idem = self.headers.get("Idempotency-Key")
            if idem and idem in state["idem"]:
                return self._json(200, state["idem"][idem])
            p = self.path
            if p == "/v1/customers":
                out = {"id": "cus_" + secrets.token_hex(6), "account": acct, **form}
                state["customers"][out["id"]] = out
            elif p == "/v1/invoices":
                out = {"id": "in_" + secrets.token_hex(6), "account": acct, "status": "draft", "amount_due": 0,
                       "customer": form["customer"], "metadata": {"clear_bill_id": form.get("metadata[clear_bill_id]")}}
                state["invoices"][out["id"]] = out
            elif p == "/v1/invoiceitems":
                inv = state["invoices"][form["invoice"]]
                inv["amount_due"] += int(form["amount"])
                out = {"id": "ii_" + secrets.token_hex(6), **form}
                state["items"].append(out)
            elif p.endswith("/finalize"):
                out = state["invoices"][p.split("/")[3]]
                out["status"] = "open"
                out["hosted_invoice_url"] = f"https://invoice.stripe.com/i/test_{out['id']}"
            elif p.endswith("/send"):
                out = state["invoices"][p.split("/")[3]]
            else:
                return self._json(404, {"error": {"message": "unknown path"}})
            if idem:
                state["idem"][idem] = out
            self._json(200, out)

    srv, url = _serve(H)
    srv.state = state
    srv.pay = lambda inv_id: state["invoices"][inv_id].update(status="paid")
    return srv, url
