#!/usr/bin/env python3
"""Render data/summary.json into the public page.

    python3 build_site.py

Writes site/index.html (a complete page for any static host) and site/fragment.html (the same page
without the document wrapper, for previews). The data is embedded in the page, so it works with no
server and no network calls.
"""
import html
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")

STYLE = """
<title>Solana Bot Cost Index</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;800&family=Public+Sans:wght@400;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: a single ledger column; each app is one line item whose bar is its real cost per 100 SOL. */
:root {
  --bg: #f6f7f9; --panel: #ffffff; --fg: #141821; --muted: #5b6475; --rule: #dde1e8;
  --accent: #2346d6;
  --c-app: #1f2a44; --c-net: #7d8aa6; --c-tip: #c98a12;
  --display: "Archivo", "Helvetica Neue", Arial, sans-serif;
  --body: "Public Sans", "Segoe UI", Roboto, Arial, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #0f1218; --panel: #161b24; --fg: #e8ebf1; --muted: #9aa4b6; --rule: #2a3140;
  --accent: #8ea3ff; --c-app: #c9d3ea; --c-net: #66728c; --c-tip: #e0a93a; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #0f1218; --panel: #161b24; --fg: #e8ebf1; --muted: #9aa4b6; --rule: #2a3140;
  --accent: #8ea3ff; --c-app: #c9d3ea; --c-net: #66728c; --c-tip: #e0a93a; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font-family: var(--body); font-size: 15px; line-height: 1.55; }
.wrap { max-width: 980px; margin: 0 auto; padding-block: 40px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 40px; }
h1, h2 { font-family: var(--display); text-wrap: balance; margin: 0; letter-spacing: -0.01em; }
h1 { font-size: clamp(30px, 5vw, 46px); font-weight: 800; line-height: 1.05; }
h2 { font-size: 20px; font-weight: 700; }
p { margin: 0; max-width: 66ch; }
a { color: var(--accent); }
a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.eyebrow { font-family: var(--mono); font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
header { display: grid; gap: 14px; }
.lede { font-size: 17px; color: var(--muted); }
.meta { display: flex; flex-wrap: wrap; gap: 8px 22px; font-family: var(--mono); font-size: 12.5px; color: var(--muted); }
.meta b { color: var(--fg); font-weight: 500; }
.ledger { background: var(--panel); border: 1px solid var(--rule); border-radius: 6px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; min-width: 700px; font-variant-numeric: tabular-nums; }
th { font-family: var(--mono); font-size: 11.5px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); text-align: left; padding: 12px; border-bottom: 1px solid var(--rule); white-space: nowrap; }
td { padding: 14px 12px; border-bottom: 1px solid var(--rule); vertical-align: middle; }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-family: var(--mono); font-size: 13.5px; white-space: nowrap; }
.rank { font-family: var(--mono); color: var(--muted); width: 28px; }
.app { font-family: var(--display); font-weight: 700; font-size: 16px; white-space: nowrap; }
.total { font-family: var(--display); font-weight: 800; font-size: 19px; }
.bar { display: flex; height: 12px; width: 100%; min-width: 120px; background: var(--rule); border-radius: 2px; overflow: hidden; }
.bar span { display: block; height: 100%; }
.s-app { background: var(--c-app); } .s-net { background: var(--c-net); } .s-tip { background: var(--c-tip); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; color: var(--muted); }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
.split { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 32px; }
.split > div { display: grid; gap: 10px; align-content: start; min-width: 0; }
ul { margin: 0; padding-left: 18px; display: grid; gap: 6px; }
.note { font-size: 13.5px; color: var(--muted); }
.sources { font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; }
footer { border-top: 1px solid var(--rule); padding-top: 18px; font-size: 13px; color: var(--muted); display: grid; gap: 6px; }
</style>
"""


def fmt_time(ts):
    return time.strftime("%d %b %Y %H:%M UTC", time.gmtime(ts))


def render(summary, sources):
    apps = summary["apps"]
    scale = max(a["cost_per_100"]["total"] for a in apps) if apps else 1
    windows = [a["window"] for a in apps if a.get("window")]
    total_trades = sum(a["trades"] for a in apps)
    sample_note = ("Solana's free public data server" if summary["rpc"] == "public"
                   else "full-day transaction history")

    rows = []
    for i, a in enumerate(apps, 1):
        c = a["cost_per_100"]
        w = lambda v: f"{100 * v / scale:.2f}%"
        ex = a["examples"][0] if a["examples"] else None
        rows.append(f"""<tr>
<td class="rank">{i}</td>
<td class="app">{html.escape(a['app'])}{'<sup>*</sup>' if a.get('note') else ''}</td>
<td class="num total">{c['total']:.2f}</td>
<td style="width:26%"><div class="bar" role="img" aria-label="app fee {c['app_fee']:.2f}, network fee {c['network_fee']:.2f}, tips {c['tips']:.2f} SOL per 100 SOL">
<span class="s-app" style="width:{w(c['app_fee'])}"></span><span class="s-net" style="width:{w(c['network_fee'])}"></span><span class="s-tip" style="width:{w(c['tips'])}"></span></div></td>
<td class="num">{a['typical_trade_cost_pct']:.2f}%</td>
<td class="num">{a['median_trade_sol']:.3f} SOL</td>
<td class="num">{a['trades']}</td>
<td class="num">{f'<a href="https://solscan.io/tx/{ex}" target="_blank" rel="noopener">a trade</a>' if ex else ''}</td>
</tr>""")

    src_items = []
    for app in [a["app"] for a in apps] + [u["app"] for u in summary.get("unranked", [])] + summary.get("not_enough_data", []):
        spec = sources["apps"][app]
        links = ", ".join(f'<a href="{html.escape(s.split(" ")[0])}" target="_blank" rel="noopener">{html.escape(s.split("/")[2] if "//" in s else s)}</a>' for s in spec["sources"])
        src_items.append(f"<li><b>{html.escape(app)}</b>: {len(spec['fee_wallets'])} fee wallet(s), from {links}</li>")
    tip_items = "".join(f"<li><b>{html.escape(k)}</b>: {len(v['accounts'])} account(s), {html.escape(v['source'])}</li>"
                        for k, v in sources["tip_accounts"].items())
    missing = summary.get("not_enough_data", [])
    notes = "".join(f"<p class=\"note\"><sup>*</sup> {html.escape(a['app'])}: {html.escape(a['note'])}</p>"
                    for a in apps if a.get("note"))
    unranked = "".join(f"<li><b>{html.escape(u['app'])}</b>: {html.escape(u['reason'])}.</li>"
                       for u in summary.get("unranked", []))
    unranked_block = (f'<div style="display:grid;gap:8px"><h2>Not ranked yet</h2><ul class="note">{unranked}'
                      f'</ul></div>') if unranked else ""
    window = (f"{fmt_time(min(w[0] for w in windows))} to {fmt_time(max(w[1] for w in windows))}"
              if windows else "n/a")

    return f"""{STYLE}
<div class="wrap">
<header>
  <div class="eyebrow">Independent · measured on-chain · free</div>
  <h1>What Solana trading bots really cost you</h1>
  <p class="lede">For every 100 SOL traded, how much each app's users actually paid: the app's fee, Solana's network fee, and tips paid to get trades landed. Read straight from the transactions, so anyone can check any number.</p>
  <div class="meta"><span>Trades measured <b>{total_trades}</b></span><span>Apps <b>{len(apps)}</b></span><span>Window <b>{window}</b></span><span>Data <b>{sample_note}</b></span></div>
</header>

<section style="display:grid;gap:14px">
  <div class="legend"><span><i class="s-app"></i>App fee</span><span><i class="s-net"></i>Network fee</span><span><i class="s-tip"></i>Landing tips</span></div>
  <div class="ledger"><table>
    <thead><tr><th></th><th>App</th><th class="num">SOL per 100 SOL</th><th>Where it goes</th><th class="num">Typical trade</th><th class="num">Median size</th><th class="num">Trades</th><th class="num">Check</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
  <p class="note"><b>SOL per 100 SOL</b> is the total cost across every trade measured, divided by the total traded. <b>Typical trade</b> is the median single trade's cost as a share of its size. When it is much higher than the first column, the app's users mostly make small trades, where flat network fees and tips weigh heavily.{(' No priceable trades were found for: ' + html.escape(', '.join(missing)) + '.') if missing else ''}</p>
  {notes}
  {unranked_block}
</section>

<section class="split">
  <div>
    <h2>Not counted yet</h2>
    <p class="note">Every figure is a floor: users paid at least this much. These costs are real and not yet included:</p>
    <ul class="note">
      <li><b>Slippage and pool fees</b>: the price moving against a trade, and the trading pool's own fee.</li>
      <li><b>Front-running</b>: losses to bots that trade just before and after a user.</li>
      <li><b>Fees paid in tokens other than SOL</b>, and referral cuts paid out later.</li>
      <li><b>Tips to landing services that don't publish their accounts</b>, including seven of Axiom's eight tip accounts.</li>
    </ul>
  </div>
  <div>
    <h2>How it works</h2>
    <ul class="note">
      <li>Each app collects its fee into public wallets. Every successful transaction that paid one of them is a trade on that app.</li>
      <li>For each trade, the trader is the wallet that signed it and swapped a token. Its SOL change, minus refundable account deposits, gives the trade size.</li>
      <li>Costs are the SOL that landed in the app's fee wallets, the network fee the trader paid, and SOL sent to published tip accounts.</li>
      <li>This edition samples each app's most recent trades. The code is open and anyone can re-run it.</li>
    </ul>
  </div>
</section>

<section style="display:grid;gap:10px">
  <h2>Where every address comes from</h2>
  <p class="note">Apps are listed only when their fee wallets are documented in a public source. Nothing is guessed.</p>
  <ul class="sources">{''.join(src_items)}{tip_items}</ul>
</section>

<footer>
  <span>Independent. No app pays to be listed or ranked, and there are no referral links on this page.</span>
  <span>Generated {fmt_time(summary['collected_at'])}. Found an error? Every row links to a real transaction, so it can be checked.</span>
</footer>
</div>
"""


def main():
    with open(os.path.join(HERE, "data", "summary.json")) as f:
        summary = json.load(f)
    with open(os.path.join(HERE, "sources.json")) as f:
        sources = json.load(f)
    page = render(summary, sources)
    os.makedirs(SITE, exist_ok=True)
    # The numbers behind the page, published beside it so anyone can reuse or check them.
    with open(os.path.join(SITE, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    with open(os.path.join(SITE, "fragment.html"), "w") as f:
        f.write(page)
    with open(os.path.join(SITE, "index.html"), "w") as f:
        f.write('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                + page.replace("<div class=\"wrap\">", "</head>\n<body>\n<div class=\"wrap\">", 1)
                + "\n</body>\n</html>\n")
    print(f"wrote {os.path.join(SITE, 'index.html')}")


if __name__ == "__main__":
    main()
