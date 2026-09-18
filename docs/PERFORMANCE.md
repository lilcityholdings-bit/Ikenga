# Performance

Every number on this page was measured this round, against the code actually in this repo, on a
4-core sandbox. It replaces an earlier, unverified claim of "32.5k orders/sec, p99 0.55ms" that
had no benchmark run behind it in this codebase — see "The number that didn't survive contact"
below for what that claim actually was.

## In-process (no HTTP, no signing) — `cargo test --release -- --ignored --nocapture bench`

The matching engine itself, isolated from the network and cryptography:

| Operation | Throughput | Per-op |
|---|---|---|
| Rest a non-crossing limit order | 1,512,269/sec | 0.66 µs |
| Fully-crossing taker (1 fill) | 670,180/sec | 1.49 µs |
| Parse an order request body (JSON) | 1,236,964/sec | 0.81 µs |
| Serialize a fill ack to JSON | 272,708/sec | 3.67 µs |
| Issue a counterparty alias | 468,076/sec | 2.14 µs |
| Generate a random ID (CSPRNG) | 828,507/sec | 1.21 µs |
| HMAC-SHA256 (legacy scheme, comparison only) | 662,911/sec | 1.51 µs |
| **Ed25519 verify via `openssl` subprocess** | **4,823/sec** | **207.32 µs** |

Read that last row carefully — it is the real ceiling on this deployment. Every other operation
in the table is 100-300x faster. The matching engine was never the bottleneck; shelling out to
`openssl` for every signature is, and `docs/ARCHITECTURE.md` explains why that trade was made
anyway (real audited crypto vs. a hand-rolled elliptic-curve implementation).

## End-to-end HTTP (`bench_http_end_to_end`, 8 threads × 250 requests, keep-alive)

```
requests          2000  (1850 non-200)
throughput        873 requests/sec (wall-clock; see caveat)
latency p50       0.33 ms
latency p95       4.33 ms
latency p99       7.75 ms
```

The 1,850 non-200 responses are not a bug. The demo agent used for this benchmark has a fresh
trust score (0, band `New`), and `New` is rate-limited to **10 requests/sec** by design (see
`src/trust.rs` — bands run 10 → 50 → 100 → 500 → 2000 req/sec as trust score rises). Eight threads
firing as fast as possible into a 10/sec cap will get almost all of their requests refused with
`429`, and that is the rate limiter doing its job, not the server failing. The p50/p95/p99 above
are latencies of whichever requests got a response at all (accepted or rate-limited), not a clean
"accepted-only" number — rerun with a higher-trust agent (or several agents split across the
load) if you need the throughput ceiling above the rate limiter rather than through it.

## The number that didn't survive contact

The original README claimed "32.5k orders/sec, p99 0.55ms on a 2-core box." Nothing in this repo
— no benchmark, no log, no script — reproduces that number, and the actual measured HTTP path is
gated by the trust-tier rate limiter before it ever gets close. Two honest possibilities: it was
measured against a build with rate limiting disabled or a pre-registered high-trust agent, or it
was asserted rather than measured. Either way, don't cite it — cite the table above, or re-run
`bench_http_end_to_end` yourself with `BENCH_THREADS`/`BENCH_PER_THREAD` against a high-trust
agent and update this file with what you get.

## Reproducing this

```bash
cd services/matching-engine
cargo test --release -- --ignored --nocapture bench       # in-process numbers
cargo run --release &                                       # then, for the HTTP number:
SEED_A=<agent_A's seed from startup> \
  cargo test --release -- --ignored --nocapture bench_http_end_to_end
```

`BENCH_THREADS`, `BENCH_PER_THREAD`, and `BENCH_KEEPALIVE=0` (to force a fresh TCP connection per
request instead of reusing one) are all read from the environment — see `src/bench.rs`.
