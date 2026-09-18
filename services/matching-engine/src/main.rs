mod api;
mod auth;
mod bench;
mod autopilot;
mod challenge;
mod credits;
mod events;
mod crypto;
mod ed25519;
mod fees;
mod forecast;
mod http;
mod json;
mod liquidity;
mod orderbook;
mod prediction;
mod privacy;
mod router;
mod state;
mod trust;
mod types;
mod wal;
mod ws;

use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use state::AppState;

fn main() {
    // Container HEALTHCHECK entrypoint. debian-slim ships no curl, and adding one purely to
    // probe an endpoint means a larger image and another package to keep patched — the binary
    // can check itself.
    let args: Vec<String> = std::env::args().collect();
    if let Some(i) = args.iter().position(|a| a == "--check-sources") {
        let pair = args.get(i + 1).cloned().unwrap_or_else(|| "BTC-USD".to_string());
        std::process::exit(check_sources(&pair));
    }
    if std::env::args().any(|a| a == "--healthcheck") {
        std::process::exit(run_healthcheck());
    }

    // `IKENGA_ENV=production` turns off every convenience that is fine locally and dangerous in
    // public: the dev faucet that mints balance from nothing, the auto-generated owner key that
    // changes on every restart, and running with no durability at all.
    let production = std::env::var("IKENGA_ENV").as_deref() == Ok("production");

    // Durability. Default to a local file so the safe thing needs no configuration; production
    // refuses to start without one, because an exchange that forgets its balances on restart is
    // not a thing you can run in public.
    let wal_path = match std::env::var("IKENGA_WAL_PATH") {
        Ok(p) if p == "off" => {
            if production {
                eprintln!("FATAL: IKENGA_WAL_PATH=off is not allowed with IKENGA_ENV=production.");
                eprintln!("Without a write-ahead log every balance and order is lost on restart.");
                std::process::exit(1);
            }
            None
        }
        Ok(p) => Some(std::path::PathBuf::from(p)),
        Err(_) => Some(std::path::PathBuf::from("data/ikenga.wal")),
    };

    let mut state = match AppState::with_wal(wal_path.as_deref()) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("FATAL: could not open the write-ahead log: {e}");
            std::process::exit(1);
        }
    };

    install_liquidity_sources(&mut state, production);
    let state = Arc::new(state);

    if production && std::env::var("IKENGA_OWNER_KEY").is_err() {
        eprintln!("FATAL: IKENGA_ENV=production requires IKENGA_OWNER_KEY to be set.");
        eprintln!("Otherwise the key that guards your treasury changes on every restart.");
        std::process::exit(1);
    }

    // Demo agents, seeded only into a fresh deployment. On a restart their registrations come
    // back from the log instead — regenerating them would silently invalidate every client's key
    // on every deploy, which is the kind of thing that makes an API unusable.
    //
    // A real agent generates its own Ed25519 keypair locally and registers only the public half.
    // Here the server generates the keypair FOR the demo agent purely as a convenience so there's
    // something to sign with out of the box — but even so, only the public key ever enters
    // `state.register_agent` / the agent registry / the WAL. The private key (seed) is printed
    // once, to this terminal, and then exists nowhere else in this process.
    let recovered_agents = state.agent_registry.len();
    if recovered_agents == 0 && !production {
        for agent_id in ["agent_A82F19", "agent_B10042"] {
            match ed25519::generate_keypair() {
                Some((seed, pubkey)) => {
                    state.register_agent(agent_id, pubkey, 250); // Developing band, for demos
                    state.adjust_balance(agent_id, "USD", 100_000.0);
                    state.adjust_balance(agent_id, "BTC", 5.0);
                    println!(
                        "demo agent {agent_id}: private_key_seed_hex={} (keep this — it signs requests; the server does not store it)",
                        crypto::hex_encode(&seed)
                    );
                }
                None => {
                    eprintln!(
                        "FATAL: could not generate an Ed25519 keypair for demo agent {agent_id} \
                         (is `openssl` on PATH? see src/ed25519.rs)."
                    );
                    std::process::exit(1);
                }
            }
        }
    } else if recovered_agents > 0 {
        println!("recovered {recovered_agents} agent(s) from the log — registrations unchanged");
    }

    // Owner key gates /v1/treasury, /v1/compliance/* and /dev/faucet. Pin it across restarts with
    // IKENGA_OWNER_KEY=<hex>; otherwise a fresh one is generated every startup.
    if std::env::var("IKENGA_OWNER_KEY").is_ok() {
        println!("owner key: taken from IKENGA_OWNER_KEY (not printed)");
    } else {
        println!("owner key (X-Owner-Key header, keep this like a password): {}", state.owner_key_hex());
    }

    if state.real_money_enabled {
        println!("value: REDEEMABLE BALANCES ENABLED (USDC withdrawable; PTS never is)");
    } else {
        println!("value: promotional points only (no redeemable assets, no withdrawals)");
    }

    let bind = std::env::var("IKENGA_BIND").unwrap_or_else(|_| {
        // PORT is what almost every hosting platform injects.
        let port = std::env::var("PORT").unwrap_or_else(|_| "8080".to_string());
        format!("0.0.0.0:{port}")
    });
    let listener = match TcpListener::bind(&bind) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("FATAL: could not bind {bind}: {e}");
            std::process::exit(1);
        }
    };

    if state.rate_limit_disabled {
        if production {
            eprintln!("FATAL: IKENGA_DISABLE_RATE_LIMIT cannot be set with IKENGA_ENV=production.");
            std::process::exit(1);
        }
        println!(
            "*** WARNING: IKENGA_DISABLE_RATE_LIMIT=1 — per-agent rate limiting is OFF. This \
             exists so load tests measure engine capacity instead of the limiter's 429s. Never \
             set this in a real deployment. ***"
        );
    }

    // Announced before the ready line, not after: an operator reads startup output until the
    // service says it is up, so anything printed afterwards is effectively unread — including
    // "autopilot is misconfigured and opened nothing".
    autopilot::install(Arc::clone(&state));

    println!("ikenga-matching-engine listening on http://{bind}");
    println!("env: {}", if production { "production" } else { "development" });
    if state.wal.is_enabled() {
        println!("durability: {} (fsync {})", state.wal.path().display(), state.wal.policy().label());
    } else {
        println!("*** durability: OFF — all state is lost on restart ***");
    }
    if state.orderbook_enabled {
        println!("order book: ON for BTC-USD, ETH-USD, SOL-USD");
    } else {
        println!("order book: off (pari-mutuel markets need no counterparty — see docs/MARKETS.md)");
    }
    if production {
        println!("dev faucet: DISABLED (production)");
    } else {
        println!("dev faucet (owner-only): POST /dev/faucet/:agent_id/:asset  body: raw number, e.g. 1000");
    }

    // Top the house's market-making bankroll back up, if seeding is switched on.
    //
    // Only ever in promotional points. Points are minted freely at registration already, so
    // minting a little more here creates nothing that did not already exist and can never be
    // withdrawn by anyone. Seeding a *redeemable* market has to be funded by a real deposit to
    // the house agent, because inventing redeemable balance is precisely the thing the reserve
    // invariant exists to make impossible — so this refuses to do it, loudly, rather than
    // quietly seeding markets with money that is not there.
    if let Ok(raw) = std::env::var("IKENGA_SEED_PER_MARKET") {
        let per: f64 = raw.trim().parse().unwrap_or(0.0);
        if per > 0.0 {
            let bankroll: f64 = std::env::var("IKENGA_SEED_BANKROLL")
                .ok()
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(10_000.0);
            let held = state.get_balance(state::HOUSE_AGENT, api::POINTS_ASSET);
            if held < bankroll {
                state.adjust_balance(state::HOUSE_AGENT, api::POINTS_ASSET, bankroll - held);
            }
            println!(
                "seed liquidity: {per} {} on each outcome of every new market, from a {bankroll} \
                 bankroll held by {} (barred from the staking API — see state::seed_market)",
                api::POINTS_ASSET,
                state::HOUSE_AGENT
            );
            let redeemable_templates = autopilot::templates_from_env()
                .0
                .iter()
                .any(|t| credits::is_redeemable(&t.asset));
            if redeemable_templates {
                println!(
                    "  NOTE: some autopilot templates settle in a redeemable asset. Those markets \
                     are seeded only if {} actually holds that asset — deposit it, it is not \
                     minted.",
                    state::HOUSE_AGENT
                );
            }
        }
    }

    // The public pages are served over whatever the operator put in front of this process, and
    // this process does not do TLS. Said once at startup because the consequence is not obvious:
    // the betting page works fine over plain http (it signs with its own Ed25519 rather than
    // WebCrypto precisely so it can), but anyone on the path can rewrite that page.
    if !production {
        println!(
            "TLS: not handled here. Over plain http the betting page still works, and it warns \
             visitors it is unencrypted. Before real money, put it behind HTTPS — a Cloudflare \
             tunnel or Caddy in front of this port is the short version."
        );
    } else if std::env::var("IKENGA_PUBLIC_URL").map(|u| u.starts_with("https")).unwrap_or(false) {
        println!("TLS: terminated upstream (IKENGA_PUBLIC_URL is https)");
    } else {
        println!(
            "*** TLS: production with no https IKENGA_PUBLIC_URL. The betting page will tell \
             visitors the connection is not encrypted, and it will be right. ***"
        );
    }

    // Flush the log on the way out. A SIGTERM is how every hosting platform asks a process to
    // stop before it redeploys, so this is the normal shutdown path, not an edge case.
    install_shutdown_handler(Arc::clone(&state));
    install_settlement_sweeper(Arc::clone(&state));

    // One thread per *connection*, with keep-alive, and a hard cap on how many at once.
    //
    // The original code was also thread-per-connection, but every response said
    // `Connection: close`, so a connection served exactly one request — meaning the cost of
    // creating and destroying an OS thread landed on every single order. Under load that got
    // worse rather than better: throughput *fell* from ~10.1k to ~6.4k orders/sec going from 8
    // to 64 concurrent clients while p99 went from 2.8ms to 40.7ms, which is the signature of
    // scheduler thrash rather than saturation. Keep-alive amortizes the thread across every
    // order on that connection, which is what a trading agent actually does.
    //
    // A fixed worker pool was tried first and rejected: with keep-alive, a worker is held for
    // the whole life of a connection, so a handful of idle clients could occupy every worker
    // and lock out all other traffic — an availability bug with a trivially small attack. A
    // thread per connection raises the bar to thousands of connections, and MAX_CONNECTIONS
    // bounds memory.
    //
    // GAP: this is the Apache-prefork model and it has prefork's ceiling. Thousands of idle
    // connections still exhaust the cap, and the honest fix is async I/O (tokio's epoll event
    // loop), which needs the network access this build doesn't have. Until then, this server
    // wants a reverse proxy in front of it terminating slow clients.
    let active = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    println!(
        "http: thread-per-connection, keep-alive on, max {MAX_CONNECTIONS} concurrent connections"
    );

    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                if active.load(Ordering::Relaxed) >= MAX_CONNECTIONS {
                    // Shed load explicitly rather than queueing unboundedly. Dropping the
                    // stream closes it, which a client sees immediately instead of hanging.
                    drop(stream);
                    continue;
                }
                active.fetch_add(1, Ordering::Relaxed);
                let state = Arc::clone(&state);
                let active = Arc::clone(&active);
                thread::spawn(move || {
                    serve_connection(stream, state);
                    active.fetch_sub(1, Ordering::Relaxed);
                });
            }
            Err(e) => eprintln!("accept error: {e}"),
        }
    }
}

/// Hard ceiling on concurrent connections; past this, new ones are refused rather than queued.
const MAX_CONNECTIONS: usize = 1024;

/// How long a machine-priced market keeps retrying its observation before giving up and voiding.
///
/// A failed price read is usually a source blip, not an unanswerable question, and voiding pays
/// nobody. Retrying costs nothing — the sweeper runs every few seconds anyway.
const OBSERVATION_GRACE_MS: i64 = 30 * 60 * 1000;

/// The longest any market may sit unresolved before it voids and refunds.
///
/// The guarantee that nothing is open-ended. A question the operator never answers is not a
/// reason to keep everyone's money, so past this the market closes itself out and every stake
/// goes back.
const FINALITY_BACKSTOP_MS: i64 = 7 * 24 * 60 * 60 * 1000;

/// How long a kept-alive connection may sit idle before it's reclaimed. Deliberately short: an
/// idle connection costs a thread, so this is the main defence against a client that opens
/// connections and then does nothing with them.
const IDLE_TIMEOUT: Duration = Duration::from_secs(10);

/// Serves requests on one connection until the client closes it, goes idle, or asks to close.
fn serve_connection(stream: TcpStream, state: Arc<AppState>) {
    // Nagle's algorithm batches small writes, which adds latency to exactly the shape of
    // traffic this server has: small request, small response, immediately.
    let _ = stream.set_nodelay(true);
    let _ = stream.set_read_timeout(Some(IDLE_TIMEOUT));

    // Resolved once per connection, not per request: it can't change mid-connection, and
    // peer_addr() is a syscall.
    let peer_ip = stream
        .peer_addr()
        .map(|a| a.ip().to_string())
        .unwrap_or_else(|_| "unknown".to_string());

    let mut reader = std::io::BufReader::new(&stream);

    loop {
        let req = match http::read_request_from(&mut reader, peer_ip.clone()) {
            Ok(Some(r)) => r,
            Ok(None) => return, // clean EOF
            Err(e) if e.kind() == std::io::ErrorKind::InvalidData => {
                // Oversized body, over-long line, or too many headers. Say so and close, rather
                // than dropping the connection — a client that gets no answer retries, and a
                // client that retries an oversized request is a loop.
                let _ = http::Response::json(
                    413,
                    &json::Json::obj(vec![
                        ("code", json::Json::str("REQUEST_TOO_LARGE")),
                        ("message", json::Json::str(e.to_string())),
                    ]),
                )
                .write_to(&stream, false);
                return;
            }
            Err(_) => return, // idle timeout or client vanished — reclaim the worker
        };

        // A WebSocket upgrade takes over the connection for its whole lifetime, so it gets a
        // dedicated thread rather than parking a pool worker forever.
        if req.method == "GET" && req.path == "/v1/marketdata" {
            drop(reader); // release the borrow so the socket can move into the WS thread
            let state = Arc::clone(&state);
            thread::spawn(move || handle_ws_connection(stream, state, &req));
            return;
        }

        // HTTP/1.1 keeps the connection alive unless the client says otherwise.
        let keep_alive = !req
            .header("connection")
            .map(|v| v.eq_ignore_ascii_case("close"))
            .unwrap_or(false);

        let response = route_idempotent(&state, &req);
        if response.write_to(&stream, keep_alive).is_err() || !keep_alive {
            return;
        }
    }
}

/// Dispatches a request, and lets a client that asks for it retry safely.
///
/// # Why this is opt-in, and why it returns a receipt rather than the original answer
///
/// The first version of this replayed the stored response body to any repeat of a signed request.
/// It broke a property this codebase already tested for: a captured registration could be
/// replayed and would come back `201 Created` instead of being refused. Nothing was created
/// twice — the cache answered — but the shape was wrong in a way that matters.
///
/// A signature proves the *request* came from the key holder. It does not prove the *sender*
/// does. Someone holding a captured copy of a signed request is not the agent, and handing them
/// that request's original response would tell them things they never saw: a stake reply carries
/// the agent's remaining balance.
///
/// So two deliberate limits. It is **off unless the caller asks** (`X-Idempotent: true`), which
/// leaves the default behaviour — and the property the onboarding test pins — exactly as it was.
/// And a replay returns a **receipt**, not the original body: the status that request produced
/// and the fact that it already happened. That answers the only question a retrying bot actually
/// has ("did my stake land?") while disclosing nothing a refusal would not have.
///
/// Only state-changing methods are covered. Repeating a GET is harmless, and caching one would
/// just serve stale data nobody asked to be stale.
fn route_idempotent(state: &Arc<AppState>, req: &http::Request) -> http::Response {
    let opted_in = req
        .header("x-idempotent")
        .map(|v| v.eq_ignore_ascii_case("true") || v == "1")
        .unwrap_or(false);

    let signed = match (req.header("x-agent-id"), req.header("x-nonce"), req.header("x-signature")) {
        (Some(a), Some(n), Some(sig))
            if opted_in && matches!(req.method.as_str(), "POST" | "DELETE") =>
        {
            Some((a.to_string(), n.to_string(), sig.to_string()))
        }
        _ => None,
    };

    let Some((agent_id, nonce, signature)) = signed else {
        return route(state, req);
    };

    let now = api::now_ms_pub();
    if let Some((status, _body)) = state.idempotency.get(&agent_id, &nonce, &signature, now) {
        let mut replayed = http::Response::json(
            status,
            &json::Json::obj(vec![
                ("idempotent_replay", json::Json::Bool(true)),
                ("original_status", json::Json::num(status as f64)),
                (
                    "message",
                    json::Json::str(
                        "This exact request was already processed; it was not run again. Read \
                         /v1/account for the resulting position and balance.",
                    ),
                ),
            ]),
        );
        replayed.headers.push(("X-Idempotent-Replay".to_string(), "true".to_string()));
        return replayed;
    }

    let response = route(state, req);
    // Never remember a rejection: an unauthenticated caller must not be able to write into this
    // map, and a refusal is not a receipt for anything.
    if response.status != 401 && response.status != 403 && response.status < 500 {
        state.idempotency.put(&agent_id, &nonce, &signature, response.status, &response.body, now);
    }
    response
}

fn route(state: &Arc<AppState>, req: &http::Request) -> http::Response {
    let path_segments: Vec<&str> = req.path.split('/').filter(|s| !s.is_empty()).collect();
    match (req.method.as_str(), path_segments.as_slice()) {
        // The order book needs a counterparty on the other side of every trade, which is the one
        // thing a new venue does not have. It is off unless IKENGA_ENABLE_ORDERBOOK=1. Everything
        // the product actually sells — pari-mutuel markets, the forecast feed — settles without
        // it, so a cold start is not a broken start.
        ("POST", ["v1", "orders"]) if state.orderbook_enabled => api::submit_order(&state, &req),
        ("DELETE", ["v1", "orders", order_id]) if state.orderbook_enabled => {
            api::cancel_order(&state, &req, order_id)
        }
        ("GET", ["v1", "quote"]) if state.orderbook_enabled => api::get_quote(&state, &req),
        ("POST" | "DELETE" | "GET", ["v1", "orders", ..] | ["v1", "quote"]) => {
            http::Response::json(
                404,
                &json::Json::obj(vec![
                    ("code", json::Json::str("ORDERBOOK_DISABLED")),
                    (
                        "message",
                        json::Json::str(
                            "the order book is not enabled on this deployment; use the \
                             pari-mutuel markets at /v1/markets, which need no counterparty",
                        ),
                    ),
                ]),
            )
        }
        ("GET", ["v1", "account"]) => api::get_account(&state, &req),
        ("GET", ["v1", "account", "record"]) => api::get_agent_record(&state, &req),
        ("GET", ["v1", "treasury"]) => api::get_treasury(&state, &req),
        ("GET", ["v1", "privacy"]) => api::get_privacy_policy(&state, &req),
        ("GET", ["v1", "fees"]) => api::get_fee_schedule(&state, &req),
        ("GET", ["v1", "route"]) => api::get_route(&state, &req),
        ("POST", ["v1", "agents"]) => api::register_agent(&state, &req),
        ("GET", ["v1", "markets"]) => api::list_markets(&state, &req),
        ("POST", ["v1", "markets"]) => api::create_market(&state, &req),
        ("GET", ["v1", "markets", market_id]) => api::get_market(&state, &req, market_id),
        ("GET", ["v1", "tools"]) => api::get_tools(&state, &req),
        ("GET", ["v1", "oracle"]) => api::oracle_health(&state, &req),
        ("GET", ["v1", "markets", market_id, "quote"]) => {
            api::quote_stake(&state, &req, market_id)
        }
        ("POST", ["v1", "markets", market_id, "stakes"]) => {
            api::place_stake(&state, &req, market_id)
        }
        ("POST", ["v1", "markets", market_id, "propose"]) => {
            api::propose_outcome(&state, &req, market_id)
        }
        ("POST", ["v1", "markets", market_id, "auto-resolve"]) => {
            api::auto_resolve(&state, &req, market_id)
        }
        ("POST", ["v1", "markets", market_id, "report"]) => {
            api::report_outcome(&state, &req, market_id)
        }
        ("POST", ["v1", "markets", market_id, "dispute"]) => {
            api::dispute_outcome(&state, &req, market_id)
        }
        ("POST", ["v1", "markets", market_id, "finalize"]) => {
            api::finalize_market(&state, &req, market_id)
        }
        ("POST", ["v1", "challenges"]) => api::create_challenge(&state, &req),
        ("GET", ["v1", "challenges"]) => api::list_challenges(&state, &req),
        ("POST", ["v1", "challenges", id, "accept"]) => api::accept_challenge(&state, &req, id),
        ("POST", ["v1", "challenges", id, "withdraw"]) => api::withdraw_challenge(&state, &req, id),
        ("GET", ["v1", "spec"]) => api::get_spec(&state, &req),
        // Where a machine looks first, by convention. Same document.
        ("GET", [".well-known", "ikenga.json"]) => api::get_spec(&state, &req),
        ("GET", ["v1", "events"]) => api::get_events(&state, &req),
        ("GET", ["v1", "feed"]) => api::get_feed(&state, &req),
        ("POST", ["v1", "feed", "subscribers"]) => api::manage_subscriber(&state, &req),
        ("GET", ["bet"]) => api::bet_page(&state, &req),
        ("GET", ["build"]) => api::build_page(&state, &req),
        ("GET", ["agent.py"]) => api::agent_py(&state, &req),
        ("GET", ["dashboard"]) => api::dashboard(&state, &req),
        ("GET", ["v1", "dashboard"]) => api::dashboard_data(&state, &req),
        ("GET", ["v1", "reserves"]) => api::get_reserves(&state),
        ("POST", ["v1", "withdrawals"]) => api::request_withdrawal(&state, &req),
        ("POST", ["v1", "withdrawals", id, "settle"]) => {
            api::settle_withdrawal(&state, &req, id)
        }
        ("POST", ["v1", "deposits", agent_id, asset]) => {
            api::record_deposit(&state, &req, agent_id, asset)
        }
        // A browser gets a page it can use; everything else gets the JSON index it came for.
        //
        // Both audiences arrive at the same address and want different things, and picking one
        // punishes the other: a person landing on a wall of JSON assumes the thing is broken, and
        // an agent handed HTML has to be taught a second URL before it can start. Accept already
        // says which is which, so it is asked rather than guessed.
        ("GET", []) if wants_html(&req) => api::bet_page(&state, &req),
        ("GET", []) => api::index(&state),
        // Unauthenticated on purpose: load balancers and uptime checks call it constantly and
        // can't sign requests, and it exposes nothing an attacker doesn't already know.
        ("GET", ["health"]) => api::health(&state),
        ("POST", ["v1", "compliance", "resolve"]) => api::resolve_alias(&state, &req),
        ("GET", ["v1", "compliance", "disclosures"]) => api::get_disclosures(&state, &req),
        ("POST", ["dev", "faucet", agent_id, asset]) => api::dev_faucet(&state, &req, agent_id, asset),
        _ => http::Response::json(
            404,
            &json::Json::obj(vec![
                ("code", json::Json::str("NOT_FOUND")),
                ("message", json::Json::str("no such route")),
            ]),
        ),
    }
}


/// `--check-sources BTC-USD` — call every configured price source and show what came back.
///
/// # Why this is a first-class command
///
/// The price sources are the oracle: they decide who won and therefore who gets paid. A
/// misconfigured one does not announce itself, it just never answers, and the router quietly
/// falls back to whatever is left — so a venue can run for weeks on a single unverified feed
/// while looking completely healthy. The only honest way to know is to ask each source and print
/// the answer, which is what this does.
///
/// Exits non-zero when fewer than two sources answer, because one source is not a cross-check.
fn check_sources(pair: &str) -> i32 {
    let (base, quote) = match pair.split_once('-') {
        Some((b, q)) if !b.is_empty() && !q.is_empty() => (b, q),
        _ => {
            eprintln!("usage: --check-sources BTC-USD");
            return 2;
        }
    };

    let spec = match std::env::var("IKENGA_ROUTE_SOURCES") {
        Ok(v) if !v.trim().is_empty() => v,
        Ok(_) => {
            println!("IKENGA_ROUTE_SOURCES is set to empty — no sources, so nothing can settle.");
            return 1;
        }
        Err(_) => {
            println!("Using the default sources: {}", liquidity::DEFAULT_SOURCES);
            println!();
            liquidity::DEFAULT_SOURCES.to_string()
        }
    };

    let (sources, problems) = liquidity::sources_from_spec(&spec);
    for p in &problems {
        eprintln!("config problem: {p}");
    }
    if sources.is_empty() {
        eprintln!("No usable sources after parsing. Nothing to check.");
        return 1;
    }

    println!("Asking {} source(s) for {} -> {}", sources.len(), base, quote);
    println!();
    let mut prices: Vec<(String, f64)> = Vec::new();
    for src in &sources {
        match src.quote(base, quote, 1.0) {
            Some(q) if q.buy_amount.is_finite() && q.buy_amount > 0.0 => {
                println!("  {:<20} {}", src.name(), autopilot::format_price(q.buy_amount));
                prices.push((src.name().to_string(), q.buy_amount));
            }
            Some(q) => println!("  {:<20} unusable answer ({})", src.name(), q.buy_amount),
            None => println!(
                "  {:<20} no answer  (unreachable from here, rate-limited, or this venue does \
                 not serve your region)",
                src.name()
            ),
        }
    }

    println!();
    if prices.len() < 2 {
        println!(
            "{} source(s) answered. At least two must agree before anything settles, so markets \
             will open and take bets and then NEVER RESOLVE on this configuration.",
            prices.len()
        );
        println!();
        println!("Most common cause: a venue that does not serve your country. Binance.com and");
        println!("OKX do not serve the United States; from a US server they simply never answer.");
        println!();
        println!("Add more sources until at least two answer here:");
        println!("  IKENGA_ROUTE_SOURCES=\"{}\" ./ikenga restart", liquidity::DEFAULT_SOURCES);
        println!();
        println!("All presets: {}", liquidity::PRESETS.iter().map(|(n, _)| *n)
            .collect::<Vec<_>>().join(", "));
        return 1;
    }

    let mut sorted: Vec<f64> = prices.iter().map(|(_, p)| *p).collect();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let median = if sorted.len() % 2 == 1 {
        sorted[sorted.len() / 2]
    } else {
        (sorted[sorted.len() / 2 - 1] + sorted[sorted.len() / 2]) / 2.0
    };
    let spread = (sorted[sorted.len() - 1] - sorted[0]) / median * 100.0;
    println!("  median  {}", autopilot::format_price(median));
    println!("  spread  {spread:.3}% across {} sources", prices.len());
    println!();
    if spread > 2.0 {
        println!(
            "That spread is wide. Check the pair is quoted the same way everywhere (USD vs \
             USDT is the usual culprit) before letting money settle against it."
        );
        return 1;
    }
    println!("Good — these agree closely enough to settle against.");
    if prices.len() < 3 {
        println!();
        println!(
            "Only {} answered though, and two is the minimum. One of them going down or \
             rate-limiting you stops every settlement. Add a third.",
            prices.len()
        );
    }
    0
}

/// Probes this service's own `/health` endpoint. Exit 0 = healthy, 1 = not.
///
/// Treats 503 as unhealthy, which is the point: `/health` returns 503 when the write-ahead log
/// is off, so a container that came up without its persistent volume attached gets restarted and
/// reported instead of quietly serving traffic it is going to forget.
/// Wires up the non-custodial router's price sources (see router.rs / liquidity.rs).
///
/// Configured with `IKENGA_ROUTE_SOURCES`; `IKENGA_ROUTE_DEMO=1` substitutes two invented
/// fixed-rate sources so the endpoint can be exercised locally. The demo sources are refused
/// outright in production — they always answer, always look fresh, and always agree with each
/// other, which is precisely the shape the router's outlier defence cannot see through. Serving
/// a real agent a made-up price is worse than serving no price.
fn install_liquidity_sources(state: &mut AppState, production: bool) {
    // Default to a working set rather than to nothing. Price sources are what settle markets, so
    // an unconfigured venue with no sources is one that opens markets forever and resolves none —
    // and the symptom (everything voids after seven days) shows up long after the cause.
    let spec = match std::env::var("IKENGA_ROUTE_SOURCES") {
        Ok(v) if !v.trim().is_empty() => v,
        // An explicit empty string means "no sources, I know what I'm doing".
        Ok(_) => String::new(),
        Err(_) => liquidity::DEFAULT_SOURCES.to_string(),
    };
    if !spec.trim().is_empty() {
        let (sources, problems) = liquidity::sources_from_spec(&spec);
        for problem in &problems {
            eprintln!("WARNING: IKENGA_ROUTE_SOURCES: {problem}");
        }
        for source in sources {
            state.router.add_source(source);
        }
    }

    if std::env::var("IKENGA_ROUTE_DEMO").as_deref() == Ok("1") {
        if production {
            eprintln!("FATAL: IKENGA_ROUTE_DEMO=1 cannot be set with IKENGA_ENV=production.");
            eprintln!("It serves invented prices, which is worse than serving none.");
            std::process::exit(1);
        }
        println!(
            "*** WARNING: IKENGA_ROUTE_DEMO=1 — /v1/route is quoting INVENTED prices from two \
             fake venues. For local testing only. ***"
        );
        state.router.add_source(Box::new(liquidity::FixedRateSource::new("demo_venue_a", 64_000.0)));
        state.router.add_source(Box::new(liquidity::FixedRateSource::new("demo_venue_b", 64_180.0)));
    }

    let names = state.router.source_names();
    if names.is_empty() {
        println!(
            "routing: no liquidity sources configured — GET /v1/route returns 503 (set \
             IKENGA_ROUTE_SOURCES; see docs/CUSTODY.md)"
        );
    } else {
        println!(
            "routing: {} source(s) [{}], fee {}bps, min {} agreeing source(s) per route",
            names.len(),
            names.join(", "),
            state.router.config().fee_bps,
            state.router.config().min_sources
        );
        if names.len() < state.router.config().min_sources {
            eprintln!(
                "WARNING: fewer sources ({}) than min_sources ({}) — every route will be refused \
                 until more are configured.",
                names.len(),
                state.router.config().min_sources
            );
        }
        if state.router.config().min_sources < 2 {
            eprintln!(
                "WARNING: IKENGA_ROUTE_MIN_SOURCES < 2 — quotes are not cross-checked, so a \
                 single manipulated or broken feed decides every route."
            );
        }
    }
}

fn run_healthcheck() -> i32 {
    use std::io::{Read, Write};
    let port = std::env::var("PORT").unwrap_or_else(|_| "8080".to_string());
    let addr = format!("127.0.0.1:{port}");

    let result = (|| -> std::io::Result<bool> {
        let mut stream = TcpStream::connect(&addr)?;
        stream.set_read_timeout(Some(Duration::from_secs(3)))?;
        stream.set_write_timeout(Some(Duration::from_secs(3)))?;
        stream.write_all(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")?;
        let mut buf = String::new();
        stream.read_to_string(&mut buf)?;
        Ok(buf.starts_with("HTTP/1.1 200"))
    })();

    match result {
        Ok(true) => 0,
        Ok(false) => {
            eprintln!("healthcheck: service responded but reports itself unhealthy");
            1
        }
        Err(e) => {
            eprintln!("healthcheck: could not reach {addr}: {e}");
            1
        }
    }
}

fn handle_ws_connection(mut stream: TcpStream, state: Arc<AppState>, req: &http::Request) {
    if ws::handshake(&mut stream, req).is_err() {
        return;
    }

    // A blocked write must not park this thread forever holding a subscriber slot.
    let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));

    let (tx, rx) = mpsc::sync_channel::<Vec<u8>>(AppState::TICK_QUEUE_DEPTH);
    state.tick_subscribers.lock().unwrap().push(tx);

    let closed = Arc::new(AtomicBool::new(false));
    if let Ok(reader_stream) = stream.try_clone() {
        let closed_writer = Arc::clone(&closed);
        thread::spawn(move || {
            ws::wait_for_disconnect(reader_stream);
            closed_writer.store(true, Ordering::Relaxed);
        });
    }

    loop {
        if closed.load(Ordering::Relaxed) {
            break;
        }
        match rx.recv_timeout(Duration::from_millis(500)) {
            Ok(frame) => {
                if ws::write_binary_frame(&mut stream, &frame).is_err() {
                    break;
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => continue,
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
}

/// Flushes the write-ahead log on SIGTERM/SIGINT.
///
/// Hosting platforms send SIGTERM before every redeploy, so an unflushed log here means routine
/// deploys lose the tail of the trade history. Implemented with `libc`-free raw `signal(2)` via
/// a self-pipe would be cleaner; with no external crates available, this uses a watcher thread
/// on a flag set by a minimal signal handler registered through `std`'s only available route —
/// so instead we simply sync on a short timer AND on process exit paths we control.
/// Whether this caller is a browser asking for a page rather than a client asking for data.
///
/// Deliberately strict: only an explicit `text/html` in Accept counts. Curl sends `*/*`, most HTTP
/// libraries send nothing at all, and both must keep getting JSON — a scripted client that
/// suddenly receives HTML because a header changed shape is a very confusing outage.
fn wants_html(req: &http::Request) -> bool {
    req.header("accept").is_some_and(|a| a.to_ascii_lowercase().contains("text/html"))
}

/// Settles markets on their own schedule, so nothing needs a human poking it.
///
/// Two jobs, every few seconds:
///
/// 1. A machine-resolved market past its observation time with no proposal yet gets one, read
///    from the price sources its committed terms named.
/// 2. A proposal past its dispute window, unchallenged, gets finalised and paid.
///
/// Without this every market waits on a manual API call, which is merely tedious with a handful
/// of operator-written markets and completely unworkable once agents are opening their own. A
/// market that resolves itself is also a market whose settlement nobody can be accused of timing.
///
/// Disputed markets are deliberately left alone: a challenge is a request for a human to look.
fn install_settlement_sweeper(state: Arc<AppState>) {
    let interval = std::env::var("IKENGA_SWEEP_INTERVAL_MS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(5_000)
        .max(250);

    thread::spawn(move || loop {
        thread::sleep(Duration::from_millis(interval));
        let now = api::now_ms_pub();

        // Collect first, act second: holding the markets lock while resolving would deadlock
        // against the settlement path, which needs it too.
        let (to_propose, to_finalize, newly_closed, overdue_review, stale_unresolved): (Vec<(String, i64)>, Vec<String>, Vec<String>, Vec<String>, Vec<String>) = {
            let markets = state.markets.lock().unwrap();
            let mut propose: Vec<(String, i64)> = Vec::new();
            let mut finalize = Vec::new();
            let mut newly_closed = Vec::new();
            let mut overdue_review = Vec::new();
            let mut stale = Vec::new();
            for m in markets.values() {
                if m.status.is_final() {
                    continue;
                }
                // A freeze is a pause for review, not a veto. If the review window has run out
                // with no decision, the market voids and every stake is refunded in full —
                // because the alternative is an objection nobody answered holding other people's
                // money indefinitely, which is the one outcome nobody agreed to.
                if m.status == prediction::MarketStatus::Disputed {
                    if m.review_overdue(now) {
                        overdue_review.push(m.market_id.clone());
                    }
                    continue;
                }
                match &m.proposal {
                    None => {
                        // Crossing into Closed is a state change nothing else announces: the
                        // status is derived from the clock, so without this a client watching the
                        // event stream would see a market open and then, much later, resolve —
                        // with no signal that betting had stopped in between.
                        if m.status == prediction::MarketStatus::Open && now >= m.closes_at_ms {
                            newly_closed.push(m.market_id.clone());
                        }
                        if m.resolution.is_machine_resolved() && now >= m.observed_at_ms {
                            propose.push((m.market_id.clone(), m.observed_at_ms));
                        } else if !m.resolution.is_machine_resolved()
                            && now >= m.observed_at_ms + FINALITY_BACKSTOP_MS
                        {
                            stale.push(m.market_id.clone());
                        }
                    }
                    Some(_) => {
                        if m.dispute_window_elapsed(now) {
                            finalize.push(m.market_id.clone());
                        }
                    }
                }
            }
            (propose, finalize, newly_closed, overdue_review, stale)
        };

        // Deliberately out here, after the read guard above has been dropped.
        //
        // The first version of this ran inside that block and re-locked `state.markets` while the
        // same thread already held it. A std Mutex is not reentrant, so the sweeper deadlocked
        // against itself the moment a market first crossed its closing time — holding the markets
        // lock forever, which then hung every request that needed it. The whole server stopped
        // answering, a few minutes after start, with no error anywhere. Collect under the lock,
        // act after it: the same rule the rest of this loop already follows.
        for id in newly_closed {
            let became_closed = {
                let mut markets = state.markets.lock().unwrap();
                match markets.get_mut(&id) {
                    Some(m) if m.status == prediction::MarketStatus::Open => {
                        m.status = prediction::MarketStatus::Closed;
                        true
                    }
                    _ => false,
                }
            };
            if became_closed {
                state.events.record(
                    events::EventKind::MarketClosed,
                    &id,
                    "no longer accepting stakes; awaiting the observation",
                    now,
                );
            }
        }

        // Refund offers nobody took. Their money has been held since they were posted, so this
        // is not tidying — it is somebody's balance.
        for c in state.challenges.expired_unmatched(now) {
            match state.release_challenge(
                &c.challenge_id,
                None,
                challenge::ChallengeStatus::Expired,
            ) {
                Ok(_) => println!(
                    "sweeper: challenge {} expired unmatched — refunded {} {}",
                    c.challenge_id, c.proposer_stake, c.asset
                ),
                Err(e) => eprintln!("sweeper: could not release {}: {:?}", c.challenge_id, e),
            }
        }

        // Frozen too long with no decision: refund everyone and close it out.
        for id in overdue_review {
            if state.resolve_market(&id, None).is_some() {
                println!(
                    "sweeper: {id} was frozen past its review window with no decision — voided \
                     and every stake refunded in full"
                );
            }
        }

        for (id, observed_at) in to_propose {
            let (outcome, evidence) = api::observe_market_outcome(&state, &id, now);

            // Do not void on the first miss.
            //
            // A failed observation usually means a price source blipped, not that the question is
            // unanswerable — and voiding refunds everyone and pays nobody, which is a real cost
            // for a transient outage. The sweeper runs every few seconds, so the cheap thing is
            // to try again. Only once the grace period has passed with no usable answer does the
            // market give up and void, and by then it genuinely could not be resolved.
            if outcome.is_none() && now < observed_at + OBSERVATION_GRACE_MS {
                continue;
            }
            if outcome.is_none() {
                println!(
                    "sweeper: {id} could not be observed for {}s — voiding, every stake refunded",
                    OBSERVATION_GRACE_MS / 1000
                );
            }
            if let Err(e) = state.propose_outcome(&id, outcome, evidence, true, now) {
                eprintln!("sweeper: could not propose for {id}: {e}");
            }
        }

        // Nothing sits unresolved forever.
        //
        // Machine-priced markets are handled above, but an operator-declared one that nobody ever
        // gets round to deciding would stay Closed indefinitely with everyone's money in it. Past
        // the backstop it voids and every stake is refunded: an unanswered question is not a
        // reason to keep somebody's funds.
        for id in stale_unresolved {
            if state.resolve_market(&id, None).is_some() {
                println!(
                    "sweeper: {id} was never resolved within {}h of its observation time — \
                     voided, every stake refunded in full",
                    FINALITY_BACKSTOP_MS / 3_600_000
                );
            }
        }
        // Trim history after settling, so the working set tracks what is live rather than
        // everything that ever happened. See AppState::prune_settled_markets.
        let keep = std::env::var("IKENGA_MARKET_HISTORY")
            .ok()
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(1_000);
        let dropped = state.prune_settled_markets(keep);
        if dropped > 0 {
            println!("sweeper: trimmed {dropped} settled market(s) from memory (kept {keep})");
        }

        for id in to_finalize {
            let outcome = {
                let markets = state.markets.lock().unwrap();
                markets.get(&id).and_then(|m| m.proposal.as_ref()).map(|p| p.outcome)
            };
            if let Some(outcome) = outcome {
                if state.resolve_market(&id, outcome).is_none() {
                    eprintln!("sweeper: {id} was already settled");
                }
            }
        }
    });
}

fn install_shutdown_handler(state: Arc<AppState>) {
    // No signal API in std without a crate, so the pragmatic version: a background thread that
    // syncs the log every second. Combined with fsync-per-record (the default policy) this is
    // belt and braces; with IKENGA_WAL_SYNC=interval it bounds loss to ~1s even if the process
    // is killed without warning.
    //
    // GAP: a real deployment wants a proper SIGTERM handler that stops accepting connections,
    // drains in-flight orders, then syncs. That needs the `signal-hook` or `libc` crate.
    thread::spawn(move || loop {
        thread::sleep(Duration::from_secs(1));
        state.wal.sync();
    });
}
