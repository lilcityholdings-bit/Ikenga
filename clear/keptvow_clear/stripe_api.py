"""Stripe invoices on the service owner's own Stripe account.

Every call carries a `Stripe-Account` header naming the service owner's connected account, so the
invoice is that owner's invoice and the agent owner pays them directly. Clear's own Stripe account
never receives the money. Test keys only: config.py refuses anything but sk_test_/rk_test_.

Every write carries an Idempotency-Key derived from the bill, so a retry after a timeout can't
create a second invoice.

NOT YET CHECKED AGAINST REAL STRIPE: the network this was written on blocks api.stripe.com, so it
is tested against the stand-in in fakes.py. Run it once against a Stripe test account before
trusting it.
"""
import json
import urllib.error
import urllib.parse
import urllib.request


class StripeError(Exception):
    pass


class Stripe:
    def __init__(self, key, api_base="https://api.stripe.com", timeout=15):
        self.key, self.base, self.timeout = key, api_base, timeout

    def _call(self, method, path, account, form=None, idem=None):
        data = urllib.parse.urlencode(form or {}).encode() if method == "POST" else None
        headers = {"Authorization": f"Bearer {self.key}", "Stripe-Account": account}
        if idem:
            headers["Idempotency-Key"] = idem
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", {}).get("message", "")
            except Exception:
                msg = ""
            raise StripeError(f"Stripe said {e.code}: {msg or e.reason}")
        except urllib.error.URLError as e:
            raise StripeError(f"couldn't reach Stripe: {e.reason}")

    def create_customer(self, account, owner_id, name, email):
        form = {"name": name, "metadata[clear_owner_id]": owner_id}
        if email:
            form["email"] = email
        return self._call("POST", "/v1/customers", account, form, idem=f"clear-cus-{account}-{owner_id}")["id"]

    def create_invoice(self, account, customer, bill, days_until_due):
        """Draft invoice, its line items, then finalize and send. Returns (invoice id, hosted URL)."""
        bid = bill["id"]
        inv = self._call("POST", "/v1/invoices", account, {
            "customer": customer, "collection_method": "send_invoice", "days_until_due": days_until_due,
            "auto_advance": "false", "currency": "usd", "pending_invoice_items_behavior": "exclude",
            "metadata[clear_bill_id]": bid, "description": f"Usage for {bill['period']} (Keptvow Clear)",
        }, idem=f"clear-inv-{bid}")
        for i, line in enumerate(bill["lines"]):
            self._call("POST", "/v1/invoiceitems", account, {
                "customer": customer, "invoice": inv["id"], "currency": "usd", "amount": line["cents"],
                "description": line["description"],
            }, idem=f"clear-ii-{bid}-{i}")
        self._call("POST", f"/v1/invoices/{inv['id']}/finalize", account, {}, idem=f"clear-fin-{bid}")
        sent = self._call("POST", f"/v1/invoices/{inv['id']}/send", account, {}, idem=f"clear-send-{bid}")
        return inv["id"], sent.get("hosted_invoice_url")

    def invoice_status(self, account, invoice_id):
        return self._call("GET", f"/v1/invoices/{urllib.parse.quote(invoice_id)}", account)["status"]
