# Keptvow Clear

Get paid when AI agents use your MCP server or API, including agents you've never dealt with.

Before you serve an agent, Clear checks its **Keptvow** record and its owner's spending limits.
After you serve it, Clear counts the call. Each month Clear writes one bill per agent owner and
sends it as a Stripe invoice from **your own Stripe account**, so the owner pays you directly.
Keptvow never holds or moves money.

Part of Keptvow (formerly Ikenga): Arena tests agents, Keptvow scores them, Clear bills them.

**Test mode only.** Clear refuses to start with `CLEAR_MODE` set to anything but `test`, or with a
live Stripe key.

## Why it's more than a meter

| Who | What Clear does for them |
|---|---|
| Service owner | Refuses agents Keptvow flags. Gives agents with no record a small starting limit ($5 a month by default). Refuses agents whose owner has an overdue bill **anywhere** on Clear. |
| Agent owner | A monthly spending cap per agent, across every service. It's enforced when usage is recorded, so no service can charge past it, even one that skips the check. |
| Keptvow | Clear reports on-time payments (`cleared_cleanly`) and bills left unpaid 30 days past due (`ghosted`), so paying bills builds an agent's score. |

## Try it

```
python3 clear/demo.py            # the whole loop, printed, plus clear/demo_page.html
python3 clear/demo.py --serve    # same, then a live dashboard at http://127.0.0.1:8400/dashboard
python3 clear/tests/test_clear.py
cd clear && python3 -m keptvow_clear    # run the server (test mode)
```

Python 3.9+ only. No packages to install. Data is kept in a SQLite file (`CLEAR_DB`).

## Plugging in a service (about 5 lines)

```python
from keptvow_clear.sdk import ClearService
clear = ClearService(CLEAR_URL, os.environ["CLEAR_SERVICE_KEY"])

with clear.gate(request.headers["X-Clear-Pass"], "get_forecast", ref=request_id) as call:
    if not call.allowed:
        return 402, call.reason
    return 200, do_the_work()      # counted only if this succeeds
```

Agents send an `X-Clear-Pass`. A pass is a short-lived ticket made out to your service only, so
you never see the agent's key and can't reuse its pass anywhere else:

```python
from keptvow_clear.sdk import ClearAgent
agent = ClearAgent(CLEAR_URL, os.environ["CLEAR_AGENT_KEY"])
headers = {"X-Clear-Pass": agent.pass_for("svc_...")}
```

## API

Keys go in `Authorization: Bearer <key>`. Money amounts are whole **micro-dollars**
(1,000,000 = $1), so prices like $0.0004 a call work.

| Who | Method and path | What |
|---|---|---|
| Anyone | `POST /v1/services` `{name, owner_name, email?}` | Sign up a service. Returns its key once |
| Anyone | `POST /v1/owners` `{name, email}` | Sign up as an agent owner. Returns its key once |
| Service | `PUT /v1/service/prices` `{tool: micro-dollars}` | Your price per call |
| Service | `PUT /v1/service/policy` `{min_trust, unknown_monthly_cap, agent_monthly_cap}` | Who you serve |
| Service | `PUT /v1/service/stripe` `{stripe_account: "acct_..."}` | Where you get paid |
| Service | `POST /v1/check` `{pass, tool, units?, ref?}` | Should I serve this? |
| Service | `POST /v1/usage` `{pass, tool, ref, units?}` | Count a served call. The same `ref` twice counts once |
| Service | `GET /v1/service`, `GET /v1/bills` | This month so far, your bills, Keptvow's fee |
| Owner | `POST /v1/agents` `{name, monthly_cap?, keptvow_id?}` | Add an agent. Returns its key once |
| Owner | `PATCH /v1/agents/{id}` `{monthly_cap?, active?}` | Change a cap, or switch an agent off |
| Owner | `GET /v1/owner` | Spend per agent, and bills |
| Agent | `POST /v1/passes` `{service_id}` | A pass for one service, good for 15 minutes |
| Operator | `POST /v1/admin/close`, `/send`, `/sync` | Close a month, send invoices, check payments |
| Operator | `POST /v1/admin/agents/{id}/keptvow-verified` | Confirm an agent really is that Keptvow id |
| People | `GET /dashboard` | Service owner's dashboard (sign in with the service key) |

Bills under $0.50 (Stripe's minimum charge) roll into the next month instead of being sent.

## Settings

| Variable | Default | What |
|---|---|---|
| `CLEAR_MODE` | `test` | Only `test` exists |
| `CLEAR_ADMIN_SECRET` | random each start | Operator secret. Set it |
| `CLEAR_TOKEN_SECRET` | random each start | Signs passes. Set it, or passes stop working on restart |
| `CLEAR_DB` | `clear/data/clear.db` | Where data is kept |
| `STRIPE_SECRET_KEY` | none | A Stripe **test** key. Without it, bills wait as "ready" |
| `KEPTVOW_URL` | `https://keptvow.com` | Where trust levels are read |
| `KEPTVOW_SOURCE_SECRET` | none | Lets Clear report payments to Keptvow |
| `CLEAR_DAYS_UNTIL_DUE` / `CLEAR_GRACE_DAYS` | 14 / 30 | Invoice terms; days overdue before Keptvow hears |
| `CLEAR_INCLUDED_SETTLEMENTS` | 0 | Paid bills a plan includes before the 1¢ rate |
| `CLEAR_SYNC_SECS` | 0 (off) | Run close / send / sync automatically this often |

## Pricing

Included in Keptvow platform plans. Heavy usage is 1¢ per paid bill above the plan's allowance,
billed to the service owner separately and never taken from their invoices.

## Not done yet

- **Never run against real Stripe.** The machine this was built on can't reach Stripe or
  keptvow.com, so both are tested against local stand-ins. The first job is one run with a
  Stripe test key and a Stripe Connect test account.
- **Connecting Stripe.** Service owners paste their `acct_...` id. Real use needs Stripe
  Connect's sign-in flow, so Clear knows the owner actually approved it.
- **Proving a Keptvow id.** An agent's Keptvow record only counts after the operator confirms
  the link by hand. Keptvow needs a way for an agent to prove it owns an id (for example,
  signing a challenge), and then Clear can check it automatically.
- **Clear as a Keptvow source.** Keptvow ignores Clear's payment reports until the Keptvow
  operator gives Clear standing (`TRUSTED_SOURCES` or `/v1/sources`).
- **Not built:** an owner dashboard (owners use the API), sign-up rate limits, email receipts,
  refunds and disputes, currencies other than USD, and the plan allowance number.
- **Small race:** two calls checked at the same moment can both pass a limit, and the second is
  then refused when recorded. The service served it but isn't paid for it.
- **Not deployed.** Nothing runs anywhere but a test machine.
- **Own repo.** Clear should move to its own repo.
- **Before real money:** a lawyer's view on invoicing on behalf of others and on the payment rules.
