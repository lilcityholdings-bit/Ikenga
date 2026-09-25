# Auto Body Shop

Staging, approval and rollback for AI agents' system instructions: the API in
[`openapi.yaml`](openapi.yaml). Agents fetch their live instruction and report telemetry. Every
batch of telemetry queues an optimizer that may **stage** a revised instruction. A human (or
your own eval gate) **approves** it into production and can **roll it back** instantly.

It uses only the Python standard library (`http.server` + `sqlite3`), like the rest of this repo. The
exception is the optional Claude optimizer, which needs the `anthropic` package.

## Run it

```bash
cd services/auto-body-shop
ABS_ADMIN_KEY=$(openssl rand -hex 32) python3 server.py    # :8090, state in data/autobodyshop.db
python3 test_server.py                                    # 13 end-to-end tests, no setup
```

Or from the repo root: `docker compose up auto-body-shop`.

## Walkthrough

```bash
H='-H Content-Type:application/json -H X-Admin-Key:'$ABS_ADMIN_KEY
# An agent's first configuration: stage it, then approve it.
curl -s $H localhost:8090/v1/candidates -d '{"agent_id":"bot_alpha","system_instruction":"Extract entities as JSON."}'
#   {"status": "STAGED", "version": "v1726800000"}
curl -s $H localhost:8090/v1/approve -d '{"agent_id":"bot_alpha","version":"v1726800000"}'
curl -s localhost:8090/v1/config/bot_alpha            # what the agent loads at startup
curl -s -H Content-Type:application/json localhost:8090/v1/telemetry \
     -d '{"agent_id":"bot_alpha","latency_ms":245.5,"success":true}'
curl -s $H localhost:8090/v1/versions/bot_alpha       # history, per-version metrics, optimizer jobs
curl -s $H localhost:8090/v1/rollback -d '{"agent_id":"bot_alpha","version":"<the active version>"}'
```

## How versions move

```
STAGED ──approve──▶ ACTIVE ──(next approve)──▶ ARCHIVED ──rollback──▶ ACTIVE
                      └──rollback──▶ ROLLED_BACK   (never restored by a later rollback)
```

- Each agent has **exactly one ACTIVE version**. A unique index in SQLite enforces this as well as the code.
- **Rollback names the version it demotes.** If that version is no longer the active one, the
  call returns 409 and changes nothing. A late rollback can't knock out a fix someone just approved.
- **Every write is committed to SQLite (`synchronous=FULL`) before the response.** The tests
  `kill -9` the server and check that versions, telemetry and staged candidates all come back.
  Optimizer jobs cut off mid-run are re-queued at startup.

## The optimizer

Every `ABS_BATCH_SIZE` traces for an agent (default 100), a background job is queued. It collects
that batch's success rate and latency (mean/p50/p95), counting only traces that ran on the
ACTIVE version, then asks the configured optimizer for a revised instruction:

| `ABS_OPTIMIZER` | What happens |
|---|---|
| `none` (default) | Job recorded as `SKIPPED`. Nothing is staged automatically. |
| `command` | Runs `ABS_OPTIMIZER_CMD`. The job context goes to stdin as JSON; stdout becomes the candidate. Use this to plug in your own eval harness or model. |
| `claude` | Calls Claude through the `anthropic` SDK (`ABS_CLAUDE_MODEL`, default `claude-opus-5`, with server-side refusal fallback). Credentials come from `ANTHROPIC_API_KEY`. |

Every job ends as `STAGED`, `SKIPPED` (no active config, no change proposed, no optimizer) or
`FAILED` (with the error). You can see it under `GET /v1/versions/{agent_id}`.

**The optimizer never promotes anything, on purpose.** Telemetry holds only latency and a success
flag. There are no inputs, outputs or error messages, so any optimizer is working from thin evidence.
Treat its output as a proposal. To get real evidence before approving, run the candidate on shadow
traffic and report those traces with `"version": "<candidate>"` on `/v1/telemetry`.
`/v1/versions` then shows the candidate's own success rate and latency next to the live version's.
Shadow traces never count as evidence about the live version.

## Configuration

| Variable | Default | |
|---|---|---|
| `ABS_PORT` / `ABS_HOST` | `8090` / `0.0.0.0` | |
| `ABS_DB` | `data/autobodyshop.db` | SQLite file; mount a volume on its directory. |
| `ABS_ADMIN_KEY` | unset | Required as `X-Admin-Key` on approve, rollback, candidates and versions. Agents' config fetch and telemetry don't need it. |
| `ABS_ENV` | unset | `production` refuses to start without `ABS_ADMIN_KEY`. |
| `ABS_BATCH_SIZE` | `100` | Traces per optimization batch, per agent. |
| `ABS_OPTIMIZER`, `ABS_OPTIMIZER_CMD`, `ABS_OPTIMIZER_TIMEOUT`, `ABS_CLAUDE_MODEL` | | See above. |
| `ABS_ACCESS_LOG` | unset | `1` logs every request. |

## Additions to the original contract

These are marked "(extension)" in `openapi.yaml`:

- An admin key, since approve and rollback with no auth would let anyone change production prompts.
- 409 responses on rollback and 422 validation errors.
- An optional `version` field on telemetry, for shadow traces.
- `POST /v1/candidates`. The original contract had no way to create an agent's *first* configuration.
- `GET /v1/versions/{agent_id}` and `GET /health`.

## Not done

- **Scale:** there is one process, one SQLite file, and one optimizer job at a time. That's fine for
  thousands of agents at modest telemetry rates. It is not a multi-node deployment.
- **Retention:** telemetry is kept forever. Add pruning before it becomes a problem.
- **The Docker image hasn't been built yet.** It was written without a Docker daemon available.
  The Python it runs is what the tests cover.
