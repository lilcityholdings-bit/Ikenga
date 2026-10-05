"""Ikenga Clear: usage metering and billing for services that AI agents call.

A service owner (an MCP server or an API) reports each agent call to Clear. Clear counts the
calls, prices them with the owner's price list, and turns them into one bill per agent owner.
The bill is paid into the service owner's own Stripe account. Ikenga never holds or moves
money: Clear keeps the count and writes the bill, nothing else.

Test mode only. There is no live mode and no Stripe call in this code yet.

Standard library only, so it runs anywhere with Python 3.9+.
"""
import html
import json
import os
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Heavy-usage rate from the published pricing. Charged by Ikenga to the service owner, and only
# above what their platform plan includes (the allowance is not set yet).
SETTLEMENT_FEE_CENTS = 1


class ClearError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def require_test_mode():
    """Refuse to start unless we are plainly in test mode."""
    mode = os.environ.get("CLEAR_MODE", "test")
    if mode != "test":
        raise SystemExit(f"CLEAR_MODE={mode!r}: only test mode exists. Live mode is not built.")
    if os.environ.get("STRIPE_SECRET_KEY", "").startswith(("sk_live", "rk_live")):
        raise SystemExit("A live Stripe key is set. Clear runs in test mode only; remove it.")


def money(cents):
    return f"${cents / 100:,.2f}"


@dataclass
class Service:
    id: str
    name: str
    owner: str
    stripe_account: str  # the service owner's own Stripe account; bills are paid straight into it
    prices: dict  # tool name -> price per call, in cents


@dataclass
class Agent:
    id: str
    name: str
    owner: str  # the company or person who pays for this agent's usage


@dataclass
class Usage:
    ref: str
    service_id: str
    agent_id: str
    tool: str
    units: int


@dataclass
class Meter:
    services: dict = field(default_factory=dict)
    agents: dict = field(default_factory=dict)
    usage: list = field(default_factory=list)
    _seen: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_service(self, service):
        self.services[service.id] = service

    def add_agent(self, agent):
        self.agents[agent.id] = agent

    def record(self, service_id, agent_id, tool, ref, units=1):
        """Count one call. Sending the same ref twice counts it once, so retries are safe."""
        service = self.services.get(service_id)
        if service is None:
            raise ClearError("UNKNOWN_SERVICE", f"no service {service_id!r}", 404)
        if agent_id not in self.agents:
            raise ClearError("UNKNOWN_AGENT", f"no agent {agent_id!r}", 404)
        if tool not in service.prices:
            raise ClearError("UNKNOWN_TOOL", f"{service.name} has no price for {tool!r}")
        if not isinstance(units, int) or isinstance(units, bool) or units < 1:
            raise ClearError("BAD_UNITS", "units must be a whole number, 1 or more")
        if not ref or not isinstance(ref, str):
            raise ClearError("NO_REF", "each call needs a unique ref so retries are not double-counted")
        with self._lock:
            key = (service_id, ref)
            if key in self._seen:
                return self._seen[key], False
            u = Usage(ref, service_id, agent_id, tool, units)
            self.usage.append(u)
            self._seen[key] = u
            return u, True

    def bills(self, service_id):
        """One bill per agent owner for this service, with a line per agent and tool."""
        service = self.services[service_id]
        with self._lock:
            rows = [u for u in self.usage if u.service_id == service_id]
        by_owner = {}
        for u in rows:
            agent = self.agents[u.agent_id]
            lines = by_owner.setdefault(agent.owner, {})
            line = lines.setdefault((agent.id, u.tool), {"agent": agent.name, "agent_id": agent.id,
                                                         "tool": u.tool, "calls": 0,
                                                         "unit_cents": service.prices[u.tool]})
            line["calls"] += u.units
        out = []
        for owner in sorted(by_owner):
            lines = sorted(by_owner[owner].values(), key=lambda l: (l["agent"], l["tool"]))
            for line in lines:
                line["amount_cents"] = line["calls"] * line["unit_cents"]
            out.append({
                "bill_to": owner,
                "service": service.name,
                "pay_to": service.owner,
                "pay_to_stripe_account": service.stripe_account,
                "lines": lines,
                "calls": sum(l["calls"] for l in lines),
                "total_cents": sum(l["amount_cents"] for l in lines),
                "status": "draft (test mode, not sent to Stripe)",
            })
        return out

    def summary(self, service_id):
        bills = self.bills(service_id)
        return {
            "calls": sum(b["calls"] for b in bills),
            "agents": len({l["agent_id"] for b in bills for l in b["lines"]}),
            "bills": len(bills),
            "billed_cents": sum(b["total_cents"] for b in bills),
            "ikenga_fee_if_over_plan_cents": len(bills) * SETTLEMENT_FEE_CENTS,
        }


# ---------------------------------------------------------------------------------------------
# HTTP: how a service plugs in.
#
#   POST /v1/usage   Authorization: Bearer <service key>   {"agent_id", "tool", "ref", "units"?}
#   GET  /v1/bills   Authorization: Bearer <service key>
#   GET  /           the bills as a page (test mode, for the demo)
# ---------------------------------------------------------------------------------------------

def make_server(meter, service_keys, host="127.0.0.1", port=8400):
    """service_keys maps a secret key to a service id. Keys come from the environment, never code."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, indent=2))

        def _service(self):
            auth = self.headers.get("Authorization", "")
            key = auth[7:] if auth.startswith("Bearer ") else ""
            sid = service_keys.get(key)
            if sid is None:
                raise ClearError("UNAUTHORIZED", "send your service key as 'Authorization: Bearer <key>'", 401)
            return sid

        def _guard(self, fn):
            try:
                fn()
            except ClearError as e:
                self._json(e.status, {"code": e.code, "message": e.message})

        def do_POST(self):
            def go():
                if self.path != "/v1/usage":
                    raise ClearError("NOT_FOUND", "no such path", 404)
                sid = self._service()
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except ValueError:
                    raise ClearError("BAD_BODY", "body must be JSON")
                u, new = meter.record(sid, body.get("agent_id"), body.get("tool"), body.get("ref"),
                                      body.get("units", 1))
                self._json(201 if new else 200, {"counted": new, "ref": u.ref,
                                                 "note": "Recorded. No money moved."})
            self._guard(go)

        def do_GET(self):
            def go():
                if self.path == "/v1/bills":
                    sid = self._service()
                    self._json(200, {"mode": "test", "summary": meter.summary(sid),
                                     "bills": meter.bills(sid)})
                elif self.path == "/":
                    sid = next(iter(meter.services))
                    self._send(200, render_page(meter, sid, full_document=True), "text/html; charset=utf-8")
                else:
                    raise ClearError("NOT_FOUND", "no such path", 404)
            self._guard(go)

    return ThreadingHTTPServer((host, port), Handler)


class ClearClient:
    """What a service owner adds to their server: one call per agent request."""

    def __init__(self, url, service_key):
        self.url, self.key = url.rstrip("/"), service_key

    def report(self, agent_id, tool, ref, units=1):
        import urllib.request
        req = urllib.request.Request(
            self.url + "/v1/usage", method="POST",
            data=json.dumps({"agent_id": agent_id, "tool": tool, "ref": ref, "units": units}).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())


# ---------------------------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------------------------

PAGE_CSS = """
/* Layout: a stack of printed-style statements, one per agent owner, under a summary strip. */
:root {
  --paper: #f6f7f5; --sheet: #ffffff; --ink: #1d2422; --muted: #5d6965;
  --rule: #d9dfdc; --accent: #0f6b5c; --flag-bg: #fff4d6; --flag-ink: #6b4a00;
  --display: "Fraunces", Georgia, serif;
  --body: "Public Sans", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --paper: #121715; --sheet: #1a201e; --ink: #e6ebe9; --muted: #9aa7a2;
  --rule: #2e3734; --accent: #5fc4b0; --flag-bg: #3a2f12; --flag-ink: #f3d68a; color-scheme: dark } }
:root[data-theme="dark"] {
  --paper: #121715; --sheet: #1a201e; --ink: #e6ebe9; --muted: #9aa7a2;
  --rule: #2e3734; --accent: #5fc4b0; --flag-bg: #3a2f12; --flag-ink: #f3d68a; color-scheme: dark }
body { background: var(--paper); color: var(--ink); font: 15px/1.5 var(--body); margin: 0; }
.wrap { max-width: 760px; margin: 0 auto; padding-inline: 16px; padding-block: 28px 48px;
  display: grid; gap: 20px; }
h1 { font: 600 2rem/1.15 var(--display); margin: 0; text-wrap: balance; }
h1 span { color: var(--accent); }
.lede { color: var(--muted); margin: 6px 0 0; max-width: 62ch; }
.flag { background: var(--flag-bg); color: var(--flag-ink); padding: 10px 14px; border-radius: 6px;
  font-size: .9rem; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1px;
  background: var(--rule); border: 1px solid var(--rule); border-radius: 8px; overflow: hidden; }
.stat { background: var(--sheet); padding: 12px 14px; }
.stat b { display: block; font: 500 1.35rem var(--mono); font-variant-numeric: tabular-nums; }
.stat small, .label { color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; }
.bill { background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; padding: 18px;
  display: grid; gap: 12px; min-width: 0; }
.bill header { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 16px; }
.bill h2 { font: 600 1.2rem var(--display); margin: 2px 0 0; }
.payto { font: .82rem var(--mono); color: var(--muted); overflow-wrap: anywhere; }
.status { font-size: .75rem; color: var(--muted); border: 1px solid var(--rule); border-radius: 99px;
  padding: 2px 10px; align-self: start; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 8px 6px 0; border-bottom: 1px solid var(--rule); white-space: nowrap; }
th { font-weight: 500; color: var(--muted); font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; }
td.n, th.n { text-align: right; font-family: var(--mono); padding-right: 0; padding-left: 12px; }
tfoot td { border-bottom: 0; font-weight: 600; padding-top: 10px; }
code { font-family: var(--mono); font-size: .9em; }
footer p { color: var(--muted); font-size: .88rem; margin: 0 0 6px; max-width: 65ch; }
"""


def render_page(meter, service_id, full_document=False):
    e = html.escape
    s = meter.services[service_id]
    summ = meter.summary(service_id)
    bills_html = []
    for b in meter.bills(service_id):
        rows = "".join(
            f"<tr><td>{e(l['agent'])}</td><td><code>{e(l['tool'])}</code></td>"
            f"<td class=\"n\">{l['calls']:,}</td><td class=\"n\">{l['unit_cents']}¢</td>"
            f"<td class=\"n\">{money(l['amount_cents'])}</td></tr>" for l in b["lines"])
        bills_html.append(f"""
<article class="bill">
  <header>
    <div><div class="label">Bill to</div><h2>{e(b['bill_to'])}</h2>
      <div class="payto">Paid to {e(b['pay_to'])} · Stripe {e(b['pay_to_stripe_account'])}</div></div>
    <span class="status">Draft · test mode</span>
  </header>
  <div class="scroll"><table>
    <thead><tr><th>Agent</th><th>Tool</th><th class="n">Calls</th><th class="n">Price</th><th class="n">Amount</th></tr></thead>
    <tbody>{rows}</tbody>
    <tfoot><tr><td colspan="4">Total</td><td class="n">{money(b['total_cents'])}</td></tr></tfoot>
  </table></div>
</article>""")
    body = f"""
<div class="wrap">
  <div>
    <h1>Ikenga <span>Clear</span></h1>
    <p class="lede">Usage bills for <b>{e(s.name)}</b>, a test service run by {e(s.owner)}.
    Test agents called it; Clear counted every call and wrote one bill per agent owner.</p>
  </div>
  <div class="flag"><b>Test mode.</b> These are made-up agents and made-up prices. No money moved
  and nothing was sent to Stripe. When it is built, each bill will be paid straight into the
  service owner's own Stripe account. Ikenga never holds or moves money.</div>
  <div class="stats">
    <div class="stat"><small>Calls counted</small><b>{summ['calls']:,}</b></div>
    <div class="stat"><small>Agents</small><b>{summ['agents']}</b></div>
    <div class="stat"><small>Bills</small><b>{summ['bills']}</b></div>
    <div class="stat"><small>Total billed</small><b>{money(summ['billed_cents'])}</b></div>
  </div>
  {''.join(bills_html) or '<p>No usage yet.</p>'}
  <footer>
    <p><b>Ikenga's fee.</b> Clear is included in platform plans. Heavy usage is billed to the service
    owner at 1¢ per settled bill: here that would be {summ['bills']} × 1¢ =
    {money(summ['ikenga_fee_if_over_plan_cents'])}, and only above the plan allowance (not set yet).</p>
    <p>Prices: {', '.join(f'<code>{e(t)}</code> {c}¢' for t, c in s.prices.items())} per call.</p>
  </footer>
</div>"""
    fonts = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600'
             '&family=IBM+Plex+Mono:wght@400;500&family=Public+Sans:wght@400;500;600&display=swap">')
    head = f"<title>Ikenga Clear Demo</title>\n{fonts}\n<style>{PAGE_CSS}</style>"
    if full_document:
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">{head}</head>'
                f'<body>{body}</body></html>')
    return head + body
