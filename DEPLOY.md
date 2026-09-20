# Deploy

The install package is `services/matching-engine/Dockerfile` + `docker-compose.yml` at the repo
root. Both are new this round — `README.md` referenced "this project's Dockerfile" for a long
time without one actually existing in the repo, same pattern as the missing docs fixed earlier.
Both are now built, run, health-checked, and crash/restart-tested for real, not just written —
see "Verified this round" below.

## Quickest path

```bash
docker compose up -d --build
curl http://127.0.0.1:8080/health
docker compose logs -f matching-engine   # your demo agents' seeds and owner key print here once
```

That's development mode: promotional points only, an owner key that changes every restart, and
the write-ahead log on by default (`fsync` every record) writing into the `ikenga-data` volume so
balances survive a container restart.

## Going to production

Two environment variables are load-bearing, and the binary refuses to start without both once
`IKENGA_ENV=production` is set — this isn't a suggestion, `main.rs` hard-exits otherwise:

```bash
# In docker-compose.yml, uncomment and set:
IKENGA_ENV=production
IKENGA_OWNER_KEY=<openssl rand -hex 32>
```

`IKENGA_ENV=production` also refuses `IKENGA_WAL_PATH=off` — durability can't be turned off in
production, only relocated (`IKENGA_WAL_PATH=/some/other/path`).

### What this still doesn't give you

Per `docs/ROADMAP.md` Phase 1, this is the durable, restartable, health-checked *server*. It is
not yet reachable by anyone outside whatever host it runs on:

- **A domain.** Point one at wherever you run this container.
- **TLS.** The server speaks plain HTTP only (`TLS: not handled here` in its own startup banner).
  Put a reverse proxy in front — Caddy auto-provisions Let's Encrypt certs with about five lines
  of config; a Cloudflare Tunnel needs no open inbound port at all. Either terminates TLS and
  forwards to `matching-engine:8080`.
- **A host that doesn't erase itself.** Any VPS, or a container platform (Fly.io, Railway,
  Render) that will run `docker compose` or the image directly and keep the `ikenga-data` volume
  around across deploys.

## Environment variables that matter

| Variable | Default | What it does |
|---|---|---|
| `PORT` / `IKENGA_BIND` | `0.0.0.0:8080` | Listen address. Most hosting platforms inject `PORT`; set `IKENGA_BIND` to override both host and port. |
| `IKENGA_ENV` | unset (development) | `production` enables the two FATAL checks above and disables the dev faucet. |
| `IKENGA_OWNER_KEY` | auto-generated per boot | Required in production. Guards `/v1/treasury`, `/v1/compliance/*`, `/dev/faucet/*`. |
| `IKENGA_WAL_PATH` | `data/ikenga.wal` | Where the write-ahead log lives. `off` only allowed outside production. |
| `IKENGA_WAL_SYNC` | `always` | `always` (fsync every record, safest), `interval` (batched, can lose ~200ms on a crash), `never` (load-testing only — see `src/wal.rs`). |
| `IKENGA_ENABLE_ORDERBOOK` | unset (off) | Set `1` to turn on the custodial BTC-USD/ETH-USD/SOL-USD order book. Off by default because pari-mutuel markets need no counterparty and are what a fresh deployment can run on day one. |

## Verified this round

Not asserted — actually run, against this exact Dockerfile and compose file:

- `docker build` succeeds cleanly from a fresh checkout.
- The container reports `healthy` in `docker ps` (proves `--healthcheck` — see below — actually
  reaches the server inside the container).
- `docker compose up -d --build` — the single documented install command — brings up a working,
  healthy stack.
- **Restart durability, for real:** funded a demo agent to $105,000 via the owner faucet,
  `docker restart`'d the container, and confirmed via a signed `GET /v1/account` call with that
  agent's original key that the balance came back exactly $105,000 / 5 BTC. The startup log
  showed `recovered 2 agent(s) from the log` rather than re-minting fresh demo credentials, which
  is the thing that would silently break every existing client's key on a routine redeploy.

## Two bugs this round's build-and-run pass actually found

Both were real, both are fixed and covered by a regression test, and neither would have surfaced
from reading the code alone — they needed the deploy path actually exercised:

1. **`--healthcheck` ignored `IKENGA_BIND`.** It only ever checked the `PORT` environment
   variable, so a deployment that set a custom `IKENGA_BIND` port would have its container marked
   permanently unhealthy by Docker/an orchestrator despite the server working correctly. Fixed by
   factoring both the listener setup and the healthcheck onto one shared `resolve_bind()`, so they
   can't drift apart again. Verified live: booted the server on a non-default port via
   `IKENGA_BIND=0.0.0.0:9191` and confirmed `--healthcheck` now exits 0 against it.
2. **Two unconditional `retain()` scans on the hottest paths in the server.** `NonceCache::check_and_record`
   (every signed request) and `IdempotencyCache::put` (every authenticated write) each ran a full
   O(n) scan of their map, under a single global lock, on *every call* — the shape of a bug that
   passes every existing test (small n) and degrades under real sustained load (n grows with
   traffic and never shrinks between calls). `trust::RateLimiter` elsewhere in this same codebase
   already solved this correctly (sweep only past a size threshold); these two didn't follow that
   pattern. Fixed to match it, with regression tests proving replay/idempotency correctness is
   unchanged and that the maps still eventually shed expired entries.
3. **The container failed to start at all on a real cloud platform.** Deployed to Railway with a
   persistent volume at `/app/data`, and it crash-looped on `FATAL: could not open the
   write-ahead log: Permission denied (os error 13)`. The Dockerfile `chown`'d `/app/data` to the
   `ikenga` user at *build* time, but any platform mounting a volume there at *container start*
   (Railway, a Kubernetes PVC, a plain `docker run -v`) silently replaces that ownership with
   whatever the fresh volume's default is — root, here. `docker compose` never caught this because
   its named volume happened to come back owned correctly; a platform's volume didn't. Fixed with
   `entrypoint.sh`: the container now starts as root, `chown`s `/app/data` *after* the volume is
   actually mounted, then drops to the unprivileged `ikenga` user via `setpriv` before ever
   executing the server binary. Verified live: reproduced the exact failure with a fresh Docker
   named volume mounted over `/app/data`, confirmed it broke the old Dockerfile the same way
   Railway did, then confirmed the fixed image starts clean, reports `healthy`, and that the
   actual server process (not just an unrelated `docker exec`) runs as UID 999, not root.

## Deployed

Live on Railway as of this round: build from `services/matching-engine/Dockerfile` via
`rootDirectory: services/matching-engine`, a persistent volume mounted at `/app/data`,
`IKENGA_ENV=production` with a real `IKENGA_OWNER_KEY`. See the project in the Railway dashboard
for the current URL and status.
