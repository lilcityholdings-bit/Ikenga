//! In-process benchmarks. Run with:
//!
//! ```bash
//! cargo test --release -- --ignored --nocapture bench
//! ```
//!
//! These deliberately isolate the engine from HTTP so each layer's cost is attributable. The
//! spec's targets are 10k orders/sec on a single instance and p50 8ms / p95 25ms / p99 80ms
//! trade latency — but "trade latency" over HTTP and "matching throughput" in-process are
//! different numbers, and conflating them is how you end up reporting a figure that means
//! nothing. `loadtest.py` measures the end-to-end HTTP path separately.
//!
//! Ignored by default because they're slow and timing-dependent — a CI machine under load would
//! make them flaky if they asserted hard thresholds, so they print rather than assert.

#![cfg(test)]

use std::time::Instant;

use crate::orderbook::OrderBook;
use crate::types::{Order, OrderStatus, OrderType, Side, price_to_ticks};

fn mk_order(id: &str, agent: &str, side: Side, price: f64, qty: f64) -> Order {
    Order {
        order_id: id.to_string(),
        agent_id: agent.to_string(),
        symbol: "BTC-USD".to_string(),
        side,
        order_type: OrderType::Limit,
        price_ticks: price_to_ticks(price),
        qty,
        filled_qty: 0.0,
        status: OrderStatus::Open,
        created_at_ms: 0,
    }
}

fn report(label: &str, ops: usize, elapsed: std::time::Duration) {
    let secs = elapsed.as_secs_f64();
    let per_sec = ops as f64 / secs;
    let per_op_us = elapsed.as_micros() as f64 / ops as f64;
    println!("{label:<44} {per_sec:>12.0} ops/sec  {per_op_us:>9.2} us/op");
}

#[test]
#[ignore]
fn bench_id_generation() {
    const N: usize = 20_000;
    let start = Instant::now();
    for _ in 0..N {
        std::hint::black_box(crate::privacy::random_id("o_"));
    }
    report("random_id (CSPRNG per call)", N, start.elapsed());
}

#[test]
#[ignore]
fn bench_alias_issuance() {
    const N: usize = 20_000;
    let reg = crate::privacy::AliasRegistry::default();
    let start = Instant::now();
    for _ in 0..N {
        std::hint::black_box(reg.issue("agent_A"));
    }
    report("AliasRegistry::issue", N, start.elapsed());
}

#[test]
#[ignore]
fn bench_matching_resting_only() {
    const N: usize = 50_000;
    let mut book = OrderBook::new("BTC-USD");
    let ids: Vec<String> = (0..N).map(|i| format!("o{i}")).collect();
    let start = Instant::now();
    for (i, id) in ids.iter().enumerate() {
        // Non-crossing sells at ascending prices: pure "rest on the book" cost.
        book.submit(mk_order(id, "agent_A", Side::Sell, 65_000.0 + i as f64, 1.0), 0);
    }
    report("orderbook: rest a non-crossing limit", N, start.elapsed());
}

#[test]
#[ignore]
fn bench_matching_crossing() {
    const N: usize = 50_000;
    let mut book = OrderBook::new("BTC-USD");
    let maker_ids: Vec<String> = (0..N).map(|i| format!("m{i}")).collect();
    let taker_ids: Vec<String> = (0..N).map(|i| format!("t{i}")).collect();
    for id in &maker_ids {
        book.submit(mk_order(id, "agent_A", Side::Sell, 65_000.0, 1.0), 0);
    }
    let start = Instant::now();
    for id in &taker_ids {
        let fills = book.submit(mk_order(id, "agent_B", Side::Buy, 65_000.0, 1.0), 0);
        std::hint::black_box(fills);
    }
    report("orderbook: fully-crossing taker (1 fill)", N, start.elapsed());
}

#[test]
#[ignore]
fn bench_json_serialize_ack() {
    use crate::types::{Fill, OrderAck};
    const N: usize = 20_000;
    let aliases = crate::privacy::AliasRegistry::default();
    let ack = OrderAck {
        order_id: "o_abc".to_string(),
        status: OrderStatus::Filled,
        filled_qty: 1.0,
        avg_fill_price: Some(65_000.0),
        fills: vec![Fill {
            trade_id: "t_abc".to_string(),
            symbol: "BTC-USD".to_string(),
            price_ticks: price_to_ticks(65_000.0),
            qty: 1.0,
            taker_order_id: "o_abc".to_string(),
            maker_order_id: "o_def".to_string(),
            taker_agent_id: "agent_A".to_string(),
            maker_agent_id: "agent_B".to_string(),
            ts_ms: 0,
        }],
    };
    let start = Instant::now();
    for _ in 0..N {
        std::hint::black_box(ack.to_json_for("agent_A", &aliases).to_string());
    }
    report("serialize OrderAck w/ 1 fill to JSON", N, start.elapsed());
}

#[test]
#[ignore]
fn bench_json_parse_order_body() {
    const N: usize = 20_000;
    let body = r#"{"symbol":"BTC-USD","side":"Buy","order_type":"Limit","price":65000.0,"qty":0.5}"#;
    let start = Instant::now();
    for _ in 0..N {
        std::hint::black_box(crate::json::parse(body).unwrap());
    }
    report("parse an order request body", N, start.elapsed());
}

#[test]
#[ignore]
fn bench_hmac_verify() {
    // No longer what the server actually does (see bench_ed25519_verify below) — kept only as
    // the "what an in-process symmetric check would cost" baseline that PERFORMANCE.md compares
    // the real Ed25519-via-openssl cost against.
    const N: usize = 20_000;
    let secret = vec![0x42u8; 32];
    let msg = b"POST/v1/orders2026-01-01T00:00:00Znonce123{}";
    let start = Instant::now();
    for _ in 0..N {
        std::hint::black_box(crate::crypto::hmac_sha256(&secret, msg));
    }
    report("hmac_sha256, in-process (old scheme, for comparison only)", N, start.elapsed());
}

#[test]
#[ignore]
fn bench_ed25519_verify() {
    // The real cost of a single request's signature check today: a process spawn plus two
    // temp-file writes, not an in-process function call. Compare directly against
    // bench_hmac_verify above — this is the number that actually caps orders/sec now.
    const N: usize = 200;
    let (seed, pubkey) = crate::ed25519::generate_keypair().expect("openssl must be present");
    let msg = b"POST/v1/orders2026-01-01T00:00:00Znonce123{}";
    let sig = crate::ed25519::sign(&seed, msg).expect("signing should succeed");
    let start = Instant::now();
    for _ in 0..N {
        assert!(std::hint::black_box(crate::ed25519::verify(&pubkey, msg, &sig)));
    }
    report("ed25519 verify via openssl subprocess (current scheme)", N, start.elapsed());
}

// ---------------------------------------------------------------------------------------------
// End-to-end HTTP benchmark. Needs a server already running on :8080 — start one, then:
//
//   SEED_A=<private_key_seed_hex from startup> cargo test --release -- --ignored --nocapture bench_http
//
// Written in Rust rather than Python on purpose: a Python load generator is slow enough that
// you end up measuring the client, not the server, and then "optimizing" numbers that were
// never the server's fault.
// ---------------------------------------------------------------------------------------------

use std::io::{Read, Write};
use std::net::TcpStream;

/// Inverse of auth.rs's `parse_rfc3339_to_unix_secs` — civil-from-days (Howard Hinnant), so the
/// benchmark can produce a timestamp inside the server's 60s signing window.
fn now_rfc3339() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let days = secs.div_euclid(86_400);
    let tod = secs.rem_euclid(86_400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        tod / 3600,
        (tod % 3600) / 60,
        tod % 60
    )
}

/// Reads exactly one HTTP response (headers + Content-Length body) so the connection stays
/// usable for the next request. `read_to_end` can't be used here — it waits for EOF, which on a
/// kept-alive connection never comes.
fn read_one_response(s: &mut TcpStream) -> std::io::Result<bool> {
    let mut buf = Vec::new();
    let mut byte = [0u8; 1];
    // Headers, up to the blank line.
    while !buf.ends_with(b"\r\n\r\n") {
        if s.read(&mut byte)? == 0 {
            return Ok(false);
        }
        buf.push(byte[0]);
    }
    let head = String::from_utf8_lossy(&buf).to_string();
    let len: usize = head
        .lines()
        .find_map(|l| {
            let (k, v) = l.split_once(':')?;
            k.eq_ignore_ascii_case("content-length").then(|| v.trim().parse().ok())?
        })
        .unwrap_or(0);
    let mut body = vec![0u8; len];
    if len > 0 {
        s.read_exact(&mut body)?;
    }
    Ok(head.starts_with("HTTP/1.1 200"))
}

fn percentile(sorted_us: &[u128], p: f64) -> f64 {
    if sorted_us.is_empty() {
        return 0.0;
    }
    let idx = ((sorted_us.len() as f64 - 1.0) * p).round() as usize;
    sorted_us[idx] as f64 / 1000.0 // -> milliseconds
}

fn signed_order_request(
    agent: &str,
    seed: &[u8; 32],
    nonce: &str,
    ts: &str,
    body: &str,
    keep_alive: bool,
) -> String {
    let mut payload = Vec::new();
    payload.extend_from_slice(b"POST");
    payload.extend_from_slice(b"/v1/orders");
    payload.extend_from_slice(ts.as_bytes());
    payload.extend_from_slice(nonce.as_bytes());
    payload.extend_from_slice(body.as_bytes());
    let sig = crate::crypto::hex_encode(
        &crate::ed25519::sign(seed, &payload).expect("openssl must be present to sign"),
    );
    // HTTP/1.1 defaults to keep-alive, so a client that intends to read until EOF MUST ask for
    // close explicitly. Omitting this is what made the connection-per-request benchmark hang.
    let conn_hdr = if keep_alive { "keep-alive" } else { "close" };
    format!(
        "POST /v1/orders HTTP/1.1\r\nHost: localhost\r\nX-Agent-ID: {agent}\r\nX-Timestamp: {ts}\r\n\
         X-Nonce: {nonce}\r\nX-Signature: {sig}\r\nContent-Type: application/json\r\n\
         Connection: {conn_hdr}\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    )
}

#[test]
#[ignore]
fn bench_http_end_to_end() {
    let Ok(seed_hex) = std::env::var("SEED_A") else {
        println!(
            "SKIPPED bench_http_end_to_end: set SEED_A to the agent's private_key_seed_hex \
             from startup (and run a server on :8080)"
        );
        return;
    };
    let seed_bytes = crate::crypto::hex_decode(&seed_hex).expect("SEED_A must be hex");
    let seed: [u8; 32] = seed_bytes.try_into().expect("SEED_A must be 32 bytes (64 hex chars)");
    let agent = "agent_A82F19";

    // Configurable so the ceiling can actually be found: if throughput rises as THREADS rises,
    // the previous number was the load generator's limit, not the server's.
    let threads: usize =
        std::env::var("BENCH_THREADS").ok().and_then(|v| v.parse().ok()).unwrap_or(8);
    let per_thread: usize =
        std::env::var("BENCH_PER_THREAD").ok().and_then(|v| v.parse().ok()).unwrap_or(250);
    let keep_alive = std::env::var("BENCH_KEEPALIVE").as_deref() != Ok("0");

    // Must be inside the server's 60s timestamp window, so it has to be the real clock.
    let ts = now_rfc3339();

    let overall = Instant::now();
    let handles: Vec<_> = (0..threads)
        .map(|t| {
            let seed = seed;
            let ts = ts.clone();
            let keep_alive = keep_alive;
            std::thread::spawn(move || {
                let mut samples: Vec<u128> = Vec::with_capacity(per_thread);
                let mut errors = 0usize;
                let mut conn: Option<TcpStream> = if keep_alive {
                    TcpStream::connect("127.0.0.1:8080").ok().map(|s| {
                        let _ = s.set_nodelay(true);
                        s
                    })
                } else {
                    None
                };
                for i in 0..per_thread {
                    let nonce = format!("bench-{t}-{i}-{}", crate::privacy::random_id(""));
                    let body = r#"{"symbol":"BTC-USD","side":"Buy","order_type":"Limit","price":1000.0,"qty":0.001}"#;
                    let req = signed_order_request(agent, &seed, &nonce, &ts, body, keep_alive);

                    let start = Instant::now();
                    let ok = if keep_alive {
                        // Realistic agent behaviour: one connection, many orders. Requires the
                        // server to actually support keep-alive; before it did, every one of
                        // these paid a TCP handshake and a fresh server thread.
                        (|| -> std::io::Result<bool> {
                            let s = conn.as_mut().ok_or_else(|| {
                                std::io::Error::new(std::io::ErrorKind::NotConnected, "no conn")
                            })?;
                            s.write_all(req.as_bytes())?;
                            read_one_response(s)
                        })()
                        .unwrap_or_else(|_| {
                            conn = None;
                            false
                        })
                    } else {
                        (|| -> std::io::Result<bool> {
                            let mut s = TcpStream::connect("127.0.0.1:8080")?;
                            s.set_nodelay(true)?;
                            s.write_all(req.as_bytes())?;
                            let mut resp = Vec::new();
                            s.read_to_end(&mut resp)?;
                            Ok(resp.starts_with(b"HTTP/1.1 200"))
                        })()
                        .unwrap_or(false)
                    };
                    samples.push(start.elapsed().as_micros());
                    if !ok {
                        errors += 1;
                    }
                }
                (samples, errors)
            })
        })
        .collect();

    let mut all: Vec<u128> = Vec::new();
    let mut errors = 0usize;
    for h in handles {
        let (s, e) = h.join().unwrap();
        all.extend(s);
        errors += e;
    }
    let wall = overall.elapsed();
    all.sort_unstable();

    let total = all.len();
    let mode = if keep_alive { "keep-alive" } else { "connection-per-request" };
    println!("\n--- end-to-end HTTP (POST /v1/orders, {threads} threads x {per_thread}, {mode}) ---");
    println!("requests          {total}  ({errors} non-200)");
    println!("throughput        {:.0} orders/sec", total as f64 / wall.as_secs_f64());
    println!("latency p50       {:.2} ms", percentile(&all, 0.50));
    println!("latency p95       {:.2} ms", percentile(&all, 0.95));
    println!("latency p99       {:.2} ms", percentile(&all, 0.99));
    println!("spec targets      10000 orders/sec, p50 8ms / p95 25ms / p99 80ms\n");
}
