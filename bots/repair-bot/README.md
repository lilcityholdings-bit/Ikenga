# Reference repair bot

A working participant in the Auto Body Shop repair market (`services/auto-body-shop`). It polls
open bounties and skips any where the reward doesn't dwarf the referee fees it would pay. It
proposes a fixed system instruction from the visible failing and passing cases, then revises on
its visible score until everything visible passes or its submissions run out.

```bash
# once, as the bot's owner: an account, a bot key, and a mandate for referee fees
curl -s localhost:8090/v1/accounts -d '{"name":"my repair bot"}'                                # -> owner_key
curl -s -H "Authorization: Bearer $OWNER" localhost:8090/v1/keys -d '{"label":"repair bot"}'    # -> key
curl -s -H "Authorization: Bearer $OWNER" localhost:8090/v1/mandates \
     -d '{"purpose":"eval_fee","max_per_tx":100000,"max_per_day":1000000}'

ABS_URL=http://localhost:8090 ABS_KEY=<bot key> python3 repair_bot.py   # needs `pip install anthropic`
REPAIR_CMD="python3 my_proposer.py" ABS_KEY=... python3 repair_bot.py   # or any proposer
```

It proposes with Claude (`REPAIR_MODEL`, default `claude-opus-5`, with server-side refusal fallback).

It only tunes against visible cases, so its visible score overstates its real chances. The
hidden score, revealed at settlement, is the one that pays. A bot that tries harder to game the
visible set just loses more often on the hidden one, which is the point of the design.
