# Ikenga Clear

Usage billing for MCP servers and APIs that AI agents call.

Clear counts each agent's calls to your service, prices them with your price list, and writes one
bill per agent owner. When live billing is built, bills are paid straight into **your own Stripe
account**. Ikenga never holds or moves money. Clear keeps the count and writes the bill.

Part of Ikenga, launching January 1, 2027: Arena tests agents, Agenttrust scores them, Clear bills
them.

**Test mode only today.** Nothing here touches real money or calls Stripe.

## Try it

```
python3 clear/demo.py            # runs the demo, prints the bills, writes clear/demo_page.html
python3 clear/demo.py --serve    # same, then shows the page at http://127.0.0.1:8400/
python3 clear/test_clear.py      # tests
```

The demo starts Clear, a pretend "Weather Lookup" service, and six test agents owned by three
companies. The agents call the service. Some calls are sent twice, like a network retry, and Clear
counts each one once. You get one bill per company.

Python 3.9+ only. No packages to install.

## How a service owner plugs it in

1. Set a price per tool (for example `get_forecast` = 2¢ per call).
2. Put your Clear service key in an environment variable. Never in code.
3. In your server, after each agent call, report it:

```python
from clear import ClearClient
clear = ClearClient("https://clear.example", os.environ["CLEAR_SERVICE_KEY"])

clear.report(agent_id, "get_forecast", ref=request_id)  # ref makes a retry safe
```

4. Read your bills: `GET /v1/bills` with `Authorization: Bearer <service key>`.

| Method | Path | What |
|---|---|---|
| POST | `/v1/usage` | `{agent_id, tool, ref, units?}`. Counts one call. Same `ref` twice counts once |
| GET | `/v1/bills` | Your bills, one per agent owner, plus totals |
| GET | `/` | The bills as a page (demo only) |

## Pricing

Included in Ikenga platform plans. Heavy usage is billed to the service owner at $0.01 per
settled bill, above the plan allowance.

## Not done yet

- **Stripe.** No Stripe calls at all. Next step is creating invoices on the owner's own Stripe
  account with Stripe Connect, in Stripe test mode first.
- **Live mode.** Does not exist. The demo refuses to start if `CLEAR_MODE` is not `test` or if a
  live Stripe key is set.
- **Storage.** Usage is kept in memory and lost on restart.
- **Agent identity.** The demo service trusts whatever agent ID the caller sends. Real use needs
  signed agent identity (Agenttrust's area).
- **Billing periods, accounts, sign-up, dashboards.** None yet. Price lists and agents are set in
  code for the demo.
- **Plan allowance** for the $0.01 rate is not decided.
- **Older netting code.** An earlier design (netting payments between bots) lives in
  `services/matching-engine/src/clearing.rs`. See `docs/CLEARING.md`. It is not the launch
  product.
- **Own repo.** Clear should move to `lilcityholdings-bit/ikenga-clear`.
- Before real money: a lawyer's view on billing and payments rules.
