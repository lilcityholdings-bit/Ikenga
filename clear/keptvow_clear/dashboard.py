"""Pages for people: a landing page, a sign-in, and the service owner's dashboard."""
import html
import time

from .billing import cents_str

e = html.escape

CSS = """
/* Layout: one column of statements under a summary strip, like a bank statement on a phone. */
:root {
  --paper: #f5f6f4; --sheet: #ffffff; --ink: #1c2321; --muted: #5b6763; --rule: #d8dedb;
  --accent: #0f6b5c; --flag-bg: #fff3d4; --flag-ink: #684800;
  --ok: #1f7a43; --warn: #9a5b00; --bad: #b42318;
  --display: "Fraunces", Georgia, serif; --body: "Public Sans", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --paper: #111614; --sheet: #192020; --ink: #e5ebe8; --muted: #9ba8a3; --rule: #2d3634;
  --accent: #5fc4b0; --flag-bg: #3a2e10; --flag-ink: #f2d58a; --ok: #6fd39a; --warn: #f0b660; --bad: #ff8a80;
  color-scheme: dark } }
:root[data-theme="dark"] {
  --paper: #111614; --sheet: #192020; --ink: #e5ebe8; --muted: #9ba8a3; --rule: #2d3634;
  --accent: #5fc4b0; --flag-bg: #3a2e10; --flag-ink: #f2d58a; --ok: #6fd39a; --warn: #f0b660; --bad: #ff8a80;
  color-scheme: dark }
body { background: var(--paper); color: var(--ink); font: 15px/1.5 var(--body); margin: 0; }
.wrap { max-width: 780px; margin: 0 auto; padding-inline: 16px; padding-block: 28px 48px; display: grid; gap: 20px; }
h1 { font: 600 2rem/1.15 var(--display); margin: 0; text-wrap: balance; }
h1 span { color: var(--accent); }
h2 { font: 600 1.15rem var(--display); margin: 0; }
.lede { color: var(--muted); margin: 6px 0 0; max-width: 62ch; }
.flag { background: var(--flag-bg); color: var(--flag-ink); padding: 10px 14px; border-radius: 6px; font-size: .9rem; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1px; background: var(--rule);
  border: 1px solid var(--rule); border-radius: 8px; overflow: hidden; }
.stat { background: var(--sheet); padding: 12px 14px; }
.stat b { display: block; font: 500 1.3rem var(--mono); font-variant-numeric: tabular-nums; }
.label, .stat small { color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; }
.card { background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; padding: 16px; display: grid; gap: 10px; min-width: 0; }
.card header { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 14px; }
.sub { font: .82rem var(--mono); color: var(--muted); overflow-wrap: anywhere; }
.pill { font-size: .75rem; border: 1px solid currentColor; border-radius: 99px; padding: 1px 10px; align-self: start; white-space: nowrap; }
.s-paid, .t-good, .t-excellent { color: var(--ok); }
.s-sent, .s-ready, .t-fair, .t-unknown { color: var(--warn); }
.s-overdue, .s-void, .t-caution { color: var(--bad); }
.s-carried_forward, .s-rolled_over { color: var(--muted); }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 10px 6px 0; border-bottom: 1px solid var(--rule); white-space: nowrap; }
th { font-weight: 500; color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .05em; }
.n { text-align: right; font-family: var(--mono); padding-right: 0; padding-left: 12px; }
tfoot td { border-bottom: 0; font-weight: 600; }
.note { color: var(--muted); font-size: .85rem; margin: 0; }
footer p, .muted { color: var(--muted); font-size: .88rem; margin: 0 0 6px; max-width: 65ch; }
form { display: grid; gap: 10px; max-width: 420px; }
input { font: 15px var(--mono); padding: 10px; border: 1px solid var(--rule); border-radius: 6px; background: var(--sheet); color: var(--ink); }
button { font: 600 15px var(--body); padding: 10px 14px; border: 0; border-radius: 6px; background: var(--accent); color: var(--sheet); cursor: pointer; }
button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
a { color: var(--accent); }
"""

FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600'
         '&family=IBM+Plex+Mono:wght@400;500&family=Public+Sans:wght@400;500;600&display=swap">')

TEST_FLAG = ('<div class="flag"><b>Test mode.</b> No real money moves. Invoices are Stripe <i>test</i> invoices. '
             'Clear counts usage and writes bills; agent owners pay service owners directly, into their own '
             'Stripe accounts. Keptvow never holds or moves money.</div>')


def page(title, body, full_document=True):
    head = f"<title>{e(title)}</title>\n{FONTS}\n<style>{CSS}</style>"
    if not full_document:
        return head + body
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">{head}</head><body>{body}</body></html>')


def landing():
    return page("Keptvow Clear", f"""<div class="wrap">
  <div><h1>Keptvow <span>Clear</span></h1>
  <p class="lede">Get paid when AI agents use your MCP server or API. Clear checks each agent's Keptvow
  record before you serve it, counts what it uses, and bills its owner every month. Payment goes
  straight into your own Stripe account.</p></div>
  {TEST_FLAG}
  <p><a href="/dashboard">Service owner sign-in</a></p></div>""")


def login(error=""):
    err = f'<p class="note" style="color:var(--bad)">{e(error)}</p>' if error else ""
    return page("Keptvow Clear sign-in", f"""<div class="wrap">
  <h1>Keptvow <span>Clear</span></h1>
  <form method="post" action="/login">
    <label class="label" for="key">Your service key</label>
    <input id="key" name="key" type="password" autocomplete="off" placeholder="svc_key_..." required>
    <button type="submit">Sign in</button>{err}
  </form></div>""")


def _when(ms):
    return time.strftime("%b %d, %Y", time.gmtime(ms / 1000)) if ms else ""


def service_page(clear, service_id, full_document=True, title="Keptvow Clear"):
    s = clear.store.service(service_id)
    summ = clear.service_summary(service_id)
    bills = clear.bills(service_id=service_id)
    owners = {o["id"]: o for o in clear.store.all("SELECT id, name FROM owners")}
    policy = {"min_trust": "unknown", "unknown_monthly_cap": 5_000_000, **s["policy"]}

    month_rows = "".join(
        f'<tr><td>{e(r["owner"])}</td><td>{e(r["agent"])}</td>'
        f'<td><span class="pill t-{e(r["trust_level"])}">{e(r["trust_level"])}</span></td>'
        f'<td class="n">{r["units"]:,}</td><td class="n">{e(r["amount"])}</td></tr>' for r in summ["this_month"])
    open_owed = sum(b["total_cents"] for b in bills if b["status"] in ("sent", "overdue", "ready"))
    paid = sum(b["total_cents"] for b in bills if b["status"] == "paid")

    cards = []
    for b in bills:
        if b["status"] == "rolled_over":
            continue
        lines = "".join(f'<tr><td>{e(l["description"])}</td><td class="n">{cents_str(l["cents"])}</td></tr>' for l in b["lines"])
        due = f" · due {_when(b['due_ms'])}" if b["due_ms"] and b["status"] in ("sent", "overdue") else ""
        link = (f' · <a href="{e(b["invoice_url"])}" rel="noopener">Stripe invoice</a>'
                if b["invoice_url"] and b["invoice_url"].startswith("https://") else "")
        note = f'<p class="note">{e(b["note"])}</p>' if b["note"] else ""
        label = b["status"].replace("_", " ")
        cards.append(f"""<article class="card">
  <header><div><div class="label">{e(b["period"])} · bill to</div><h2>{e(owners.get(b["owner_id"], {}).get("name", b["owner_id"]))}</h2>
    <div class="sub">{e(b["id"])}{due}{link}</div></div>
    <span class="pill s-{e(b["status"])}">{e(label)}</span></header>
  <div class="scroll"><table><tbody>{lines}</tbody>
    <tfoot><tr><td>Total</td><td class="n">{cents_str(b["total_cents"])}</td></tr></tfoot></table></div>{note}
</article>""")

    fee = summ["keptvow_fee"]
    prices = ", ".join(f"<code>{e(t)}</code> {e(clear_dollars(v))}" for t, v in s["prices"].items())
    cap = policy["unknown_monthly_cap"]
    signout = '<form method="post" action="/logout"><button type="submit">Sign out</button></form>' if full_document else ""
    body = f"""<div class="wrap">
  <div><h1>Keptvow <span>Clear</span></h1>
  <p class="lede"><b>{e(s["name"])}</b>, run by {e(s["owner_name"])}. Paid into Stripe account
  <code>{e(s["stripe_account"] or "not connected yet")}</code>.</p></div>
  {TEST_FLAG}
  <div class="stats">
    <div class="stat"><small>{e(summ["period"])} so far</small><b>{e(summ["this_month_total"])}</b></div>
    <div class="stat"><small>Agents this month</small><b>{len(summ["this_month"])}</b></div>
    <div class="stat"><small>Billed, unpaid</small><b>{cents_str(open_owed)}</b></div>
    <div class="stat"><small>Paid to you</small><b>{cents_str(paid)}</b></div>
  </div>
  <section class="card"><h2>This month, by agent</h2>
    <div class="scroll"><table><thead><tr><th>Owner</th><th>Agent</th><th>Keptvow</th><th class="n">Calls</th><th class="n">Owed</th></tr></thead>
    <tbody>{month_rows or '<tr><td colspan="5">No usage yet this month.</td></tr>'}</tbody></table></div></section>
  <h2>Bills</h2>
  {''.join(cards) or '<p class="muted">No bills yet. Bills are written when a month closes.</p>'}
  <footer>
    <p><b>Who you serve.</b> Agents Keptvow rates <b>{e(policy["min_trust"])}</b> or better. Agents with no
    Keptvow record can spend up to <b>{e(clear_dollars(cap) if cap is not None else "any amount")}</b> a month here.
    An agent whose owner has an overdue bill anywhere on Clear is refused until it's paid.</p>
    <p><b>Prices per call:</b> {prices or "none set"}.</p>
    <p><b>Keptvow's fee:</b> {fee["paid_bills"]} paid bills, {fee["included_in_plan"]} included in your plan,
    {fee["billable"]} × 1¢ = {e(fee["amount"])}. Billed to you separately, never taken from your invoices.</p>
    {signout}
  </footer></div>"""
    return page(title, body, full_document)


def clear_dollars(micros):
    from .billing import dollars
    return dollars(micros)
