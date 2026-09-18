use std::collections::HashMap;

use crate::auth::{verify_signed_request, SignedRequest};
use crate::http::{Request, Response};
use crate::json::Json;
use crate::orderbook::OrderBook;
use crate::state::AppState;
use crate::types::{
    price_to_ticks, ticks_to_price, ApiError, NewOrderRequest, Order, OrderAck, OrderStatus,
    OrderType, Side,
};

type Balances = HashMap<(String, String), f64>;

fn bal_get(balances: &Balances, agent_id: &str, asset: &str) -> f64 {
    balances.get(&(agent_id.to_string(), asset.to_string())).copied().unwrap_or(0.0)
}

fn bal_adjust(balances: &mut Balances, agent_id: &str, asset: &str, delta: f64) {
    *balances.entry((agent_id.to_string(), asset.to_string())).or_insert(0.0) += delta;
}

fn err_response(status: u16, err: ApiError) -> Response {
    Response::json(status, &err.to_json())
}

fn now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64
}

/// Verifies signing headers + replay/timestamp checks, then enforces the trust-tiered rate
/// limit. Shared by every authenticated endpoint.
fn authenticate(state: &AppState, req: &Request, method: &str, path: &str) -> Result<String, Response> {
    let agent_id = req
        .header("x-agent-id")
        .ok_or_else(|| err_response(401, ApiError::new("MISSING_HEADER", "missing X-Agent-ID")))?;
    let timestamp = req
        .header("x-timestamp")
        .ok_or_else(|| err_response(401, ApiError::new("MISSING_HEADER", "missing X-Timestamp")))?;
    let nonce = req
        .header("x-nonce")
        .ok_or_else(|| err_response(401, ApiError::new("MISSING_HEADER", "missing X-Nonce")))?;
    let signature = req
        .header("x-signature")
        .ok_or_else(|| err_response(401, ApiError::new("MISSING_HEADER", "missing X-Signature")))?;

    let now = now_ms();
    let sig_req = SignedRequest {
        agent_id,
        method,
        path,
        timestamp_rfc3339: timestamp,
        nonce,
        body: &req.body,
        signature_hex: signature,
    };
    // Refuse callers who have already burned their failure budget, BEFORE paying to verify.
    //
    // Verification costs a process spawn — measured at ~3.5ms, roughly forty times everything
    // else in a request combined. It used to run before any limiting at all, and the per-agent
    // limiter below only runs *after* it, which meant an attacker never reached it: send a
    // well-formed request with any agent id, a fresh nonce and 64 bytes of garbage for a
    // signature, and the server spends 3.5ms of a core proving it is garbage. A few hundred a
    // second saturates the whole machine, from one script, with no account and nothing at stake.
    //
    // Two details make the budget safe to impose.
    //
    // It counts *failures only*. A legitimate client's signatures verify, so it never spends any
    // of this and never learns the limit exists; only a caller that repeatedly cannot produce a
    // valid signature is cut off, and cut off before the spawn.
    //
    // And it is keyed on the **address and the agent id together**, not the address alone. Keyed
    // on address, one bad client behind a shared NAT — or any client at all behind a reverse
    // proxy, where every caller arrives from one address — would lock out everybody sharing it.
    // That trades a CPU-exhaustion attack for an easier lockout attack, which is not a fix. Keyed
    // on the pair: an attacker abusing someone else's agent id burns only their own bucket, and a
    // legitimate client sharing their address is untouched because its agent id differs.
    //
    // Rotating agent ids to get fresh buckets does not scale either, and that is not luck: an
    // unregistered agent id is refused by a map lookup with no spawn at all, so an attacker needs
    // *registered* identities — and registration is already capped per address per hour.
    if !state.rate_limit_disabled
        && state.ip_limiter.exhausted(
            &format!("sigfail:{}:{}", req.client_ip(), agent_id),
            SIGNATURE_FAILURES_PER_MINUTE,
            ONE_MINUTE_MS,
            now,
        )
    {
        return Err(err_response(
            429,
            ApiError::new(
                "TOO_MANY_BAD_SIGNATURES",
                "too many failed signatures from this address; wait a minute. If this is \
                 unexpected, check your clock and that you are signing \
                 METHOD+PATH+TIMESTAMP+NONCE+BODY.",
            ),
        ));
    }

    if let Err(e) = verify_signed_request(&state.agent_registry, &state.nonces, &sig_req, now) {
        if !state.rate_limit_disabled {
            state.ip_limiter.allow_windowed(
                &format!("sigfail:{}:{}", req.client_ip(), agent_id),
                SIGNATURE_FAILURES_PER_MINUTE,
                ONE_MINUTE_MS,
                now,
            );
        }
        return Err(err_response(401, e));
    }

    let band = state.trust.band(agent_id);
    if !state.rate_limit_disabled
        && !state.rate_limiter.allow(agent_id, band.rate_limit_per_sec(), now)
    {
        return Err(err_response(
            429,
            ApiError::new("RATE_LIMITED", format!("limit for {} band exceeded", band.label())),
        ));
    }

    Ok(agent_id.to_string())
}

/// Gates owner-only endpoints (`/v1/treasury`, `/dev/faucet`) behind the `X-Owner-Key` header,
/// checked in constant time against the key `AppState` generated (or was given via
/// `IKENGA_OWNER_KEY`) at startup. This is a single shared secret, not the spec's real
/// role-based owner/admin credential system — adequate for one operator, not for a team.
fn authenticate_owner(state: &AppState, req: &Request) -> Result<(), Response> {
    match req.header("x-owner-key") {
        Some(key) if state.check_owner_key(key) => Ok(()),
        _ => Err(err_response(403, ApiError::new("FORBIDDEN", "missing or invalid X-Owner-Key"))),
    }
}

/// Reads balances via the already-locked map (see `AppState::lock_balances`) rather than
/// `state.get_balance`, so this check is atomic with the settlement that follows it in
/// `submit_order` — see that function and the `lock_balances` doc comment for why.
fn risk_check(
    balances: &Balances,
    book: &OrderBook,
    agent_id: &str,
    symbol: &str,
    side: Side,
    order_type: OrderType,
    price_ticks: Option<i64>,
    qty: f64,
) -> Result<(), ApiError> {
    let (base, quote) = AppState::asset_pair(symbol)
        .ok_or_else(|| ApiError::new("UNKNOWN_SYMBOL", "symbol must be BASE-QUOTE"))?;
    match side {
        Side::Sell => {
            let bal = bal_get(balances, agent_id, base);
            if bal < qty {
                return Err(ApiError::new(
                    "INSUFFICIENT_BALANCE",
                    format!("need {qty} {base}, have {bal}"),
                ));
            }
        }
        Side::Buy => {
            let price = match order_type {
                OrderType::Limit => ticks_to_price(price_ticks.expect("limit order needs price")),
                OrderType::Market => match book.best_ask() {
                    Some(t) => ticks_to_price(t),
                    None => {
                        return Err(ApiError::new(
                            "NO_LIQUIDITY",
                            "no resting asks to price a market buy against",
                        ))
                    }
                },
            };
            let cost = price * qty;
            let bal = bal_get(balances, agent_id, quote);
            if bal < cost {
                return Err(ApiError::new(
                    "INSUFFICIENT_BALANCE",
                    format!("need {cost} {quote}, have {bal}"),
                ));
            }
        }
    }
    Ok(())
}

/// Settles one fill's balances, charges the taker, and pays the maker their rebate.
///
/// Both legs are denominated in the QUOTE asset, which is how venues normally do it and is a
/// change from the previous code (it charged buys in the base asset and sells in the quote,
/// leaving the treasury with a scattering of odd balances that couldn't be netted). Now one
/// trade produces exactly one treasury movement, in one asset.
///
/// The maker is *paid* here — see fees.rs for why the schedule is built that way and for the
/// invariant (`capture > 0` at every tier) that stops two colluding agents from farming the
/// rebate by trading with each other.
///
/// Takes the already-locked balances map (see `risk_check` above / `AppState::lock_balances`)
/// so a fill settles atomically with the risk check that approved it.
fn apply_fill(
    state: &AppState,
    balances: &mut Balances,
    symbol: &str,
    taker_side: Side,
    fill: &crate::types::Fill,
    wal_records: &mut Vec<crate::wal::Record>,
) {
    let (base, quote) = AppState::asset_pair(symbol).unwrap();
    let price = ticks_to_price(fill.price_ticks);
    let notional = price * fill.qty;

    // Each side is priced at its own volume tier.
    let taker_tier = crate::fees::tier_for_volume(state.trailing_volume(&fill.taker_agent_id));
    let maker_tier = crate::fees::tier_for_volume(state.trailing_volume(&fill.maker_agent_id));
    let fee = crate::fees::taker_fee(notional, taker_tier);
    let rebate = crate::fees::maker_rebate(notional, maker_tier);

    // Asset legs: base moves from seller to buyer, quote the other way.
    let (buyer, seller) = match taker_side {
        Side::Buy => (&fill.taker_agent_id, &fill.maker_agent_id),
        Side::Sell => (&fill.maker_agent_id, &fill.taker_agent_id),
    };
    // Every delta is mirrored into wal_records so the caller can append the whole fill as one
    // atomic batch — a crash must not be able to land between the base leg and the quote leg.
    let mut move_balance = |agent: &str, asset: &str, delta: f64| {
        bal_adjust(balances, agent, asset, delta);
        wal_records.push(crate::wal::Record::Balance {
            agent_id: agent.to_string(),
            asset: asset.to_string(),
            delta,
        });
    };

    move_balance(buyer, base, fill.qty);
    move_balance(buyer, quote, -notional);
    move_balance(seller, base, -fill.qty);
    move_balance(seller, quote, notional);

    // Fee legs, both in quote.
    move_balance(&fill.taker_agent_id, quote, -fee);
    move_balance(&fill.maker_agent_id, quote, rebate);

    state.add_protocol_fee_nolog(quote, fee - rebate);
    wal_records.push(crate::wal::Record::Fee { asset: quote.to_string(), amount: fee - rebate });

    // Both sides' traded volume counts toward their tier.
    for agent in [&fill.taker_agent_id, &fill.maker_agent_id] {
        state.record_volume_nolog(agent, notional);
        wal_records.push(crate::wal::Record::Volume {
            agent_id: agent.clone(),
            notional,
        });
    }
}

pub fn submit_order(state: &AppState, req: &Request) -> Response {
    let agent_id = match authenticate(state, req, "POST", "/v1/orders") {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let body_str = match std::str::from_utf8(&req.body) {
        Ok(s) => s,
        Err(_) => return err_response(400, ApiError::new("BAD_BODY", "body is not valid UTF-8")),
    };
    let json = match crate::json::parse(body_str) {
        Ok(j) => j,
        Err(e) => return err_response(400, ApiError::new("BAD_BODY", e)),
    };
    let new_order = match NewOrderRequest::from_json(&json) {
        Ok(r) => r,
        Err(e) => return err_response(400, ApiError::new("BAD_BODY", e)),
    };

    if new_order.order_type == OrderType::Limit && new_order.price.is_none() {
        return err_response(400, ApiError::new("MISSING_PRICE", "limit orders require a price"));
    }
    if new_order.qty <= 0.0 {
        return err_response(400, ApiError::new("BAD_QTY", "qty must be positive"));
    }

    let Some(book_mutex) = state.books.get(&new_order.symbol) else {
        return err_response(
            400,
            ApiError::new("UNKNOWN_SYMBOL", format!("no market for {}", new_order.symbol)),
        );
    };

    // Hold the book's lock (per-symbol) AND the balances lock (global) across risk-check +
    // match + settlement. The book lock alone only serializes orders on the SAME symbol; the
    // balances lock is what prevents two orders on DIFFERENT symbols that share a quote asset
    // (e.g. USD in both BTC-USD and ETH-USD) from both passing a risk check against the same
    // stale balance before either deducts. Lock order is always book-then-balances, here and
    // nowhere else needs both, so this can't deadlock. See AppState::lock_balances.
    let mut book = book_mutex.lock().unwrap();
    let mut balances = state.lock_balances();

    let price_ticks = new_order.price.map(price_to_ticks);
    if let Err(e) = risk_check(
        &balances, &book, &agent_id, &new_order.symbol, new_order.side, new_order.order_type,
        price_ticks, new_order.qty,
    ) {
        return err_response(400, e);
    }

    let order_id = new_order
        .client_order_id
        .clone()
        .filter(|id| !id.is_empty())
        .unwrap_or_else(|| format!("o_{}", generate_id()));

    if state.orders.lock().unwrap().contains_key(&order_id) {
        return err_response(409, ApiError::new("DUPLICATE_ORDER_ID", "client_order_id already used"));
    }

    let created_at_ms = now_ms();
    let order = Order {
        order_id: order_id.clone(),
        agent_id: agent_id.clone(),
        symbol: new_order.symbol.clone(),
        side: new_order.side,
        order_type: new_order.order_type,
        price_ticks: price_ticks.unwrap_or(0),
        qty: new_order.qty,
        filled_qty: 0.0,
        status: OrderStatus::Open,
        created_at_ms,
    };

    let result = book.submit(order.clone(), created_at_ms);
    let fills = result.fills;

    let mut wal_records: Vec<crate::wal::Record> = Vec::new();
    for fill in &fills {
        apply_fill(state, &mut balances, &new_order.symbol, new_order.side, fill, &mut wal_records);
    }
    // Settlement is done — release the balances lock before doing anything that doesn't need
    // it (order bookkeeping, WS broadcast), so we're not holding a global lock longer than the
    // part that actually needs cross-symbol atomicity.
    drop(balances);

    let filled_qty: f64 = fills.iter().map(|f| f.qty).sum();
    let status = if filled_qty >= new_order.qty {
        OrderStatus::Filled
    } else if filled_qty > 0.0 {
        OrderStatus::PartiallyFilled
    } else {
        OrderStatus::Open
    };

    let mut final_order = order;
    final_order.filled_qty = filled_qty;
    final_order.status = status;
    // Any of this agent's own resting orders pulled to prevent a self-trade must be reflected
    // in the order map, or GET /v1/account would keep reporting them as open forever.
    if !result.self_cancelled.is_empty() {
        let mut orders = state.orders.lock().unwrap();
        for cancelled_id in &result.self_cancelled {
            if let Some(o) = orders.get_mut(cancelled_id) {
                o.status = OrderStatus::Cancelled;
            }
            wal_records.push(crate::wal::Record::OrderUpdated {
                order_id: cancelled_id.clone(),
                filled_qty: 0.0,
                status: "Cancelled".to_string(),
            });
        }
    }

    // Makers that got hit are no longer (fully) on the book — their new state has to be durable
    // too, or replay would resurrect liquidity that was already consumed.
    for fill in &fills {
        let maker_state = state.orders.lock().unwrap().get(&fill.maker_order_id).map(|o| {
            (o.filled_qty + fill.qty, o.qty)
        });
        if let Some((new_filled, total)) = maker_state {
            let maker_status =
                if new_filled >= total { "Filled" } else { "PartiallyFilled" };
            if let Some(o) = state.orders.lock().unwrap().get_mut(&fill.maker_order_id) {
                o.filled_qty = new_filled;
                o.status = if new_filled >= total {
                    OrderStatus::Filled
                } else {
                    OrderStatus::PartiallyFilled
                };
            }
            wal_records.push(crate::wal::Record::OrderUpdated {
                order_id: fill.maker_order_id.clone(),
                filled_qty: new_filled,
                status: maker_status.to_string(),
            });
        }
    }

    // Only an order that actually came to rest goes in the log as resting liquidity.
    if matches!(status, OrderStatus::Open | OrderStatus::PartiallyFilled)
        && new_order.order_type == OrderType::Limit
    {
        wal_records.push(crate::wal::Record::OrderRested {
            order_id: order_id.clone(),
            agent_id: agent_id.clone(),
            symbol: new_order.symbol.clone(),
            side: new_order.side.as_str().to_string(),
            price_ticks: price_ticks.unwrap_or(0),
            qty: new_order.qty,
            filled_qty,
            created_at_ms,
        });
    }

    state.orders.lock().unwrap().insert(order_id.clone(), final_order);
    state.order_symbol.lock().unwrap().insert(order_id.clone(), new_order.symbol.clone());

    // One append for the whole order: every balance move, fee, volume record and order state
    // change lands together or not at all. Written before the client is told the order
    // succeeded, so an ack always implies durability.
    state.wal.append(&wal_records);

    // Broadcast a market-data tick per fill. Fire-and-forget — never blocks order execution,
    // per the spec's "async never blocks" requirement.
    if let Some(&symbol_id) = state.symbol_ids.get(&new_order.symbol) {
        for fill in &fills {
            let frame = crate::ws::encode_tick(
                state.next_tick_seq(),
                symbol_id,
                ticks_to_price(fill.price_ticks),
                fill.qty,
                fill.ts_ms as u64,
            );
            state.broadcast_tick(frame.to_vec());
        }
    }

    let avg_fill_price = if fills.is_empty() {
        None
    } else {
        let total_qty: f64 = fills.iter().map(|f| f.qty).sum();
        let notional: f64 = fills.iter().map(|f| ticks_to_price(f.price_ticks) * f.qty).sum();
        Some(notional / total_qty)
    };

    Response::json(
        200,
        &OrderAck { order_id, status, filled_qty, avg_fill_price, fills }
            .to_json_for(&agent_id, &state.aliases),
    )
}

pub fn cancel_order(state: &AppState, req: &Request, order_id: &str) -> Response {
    let path = format!("/v1/orders/{order_id}");
    let agent_id = match authenticate(state, req, "DELETE", &path) {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let symbol = match state.order_symbol.lock().unwrap().get(order_id).cloned() {
        Some(s) => s,
        None => return err_response(404, ApiError::new("NOT_FOUND", "no such order")),
    };
    let Some(book_mutex) = state.books.get(&symbol) else {
        return err_response(404, ApiError::new("NOT_FOUND", "no such order"));
    };
    let mut book = book_mutex.lock().unwrap();

    match book.cancel(order_id) {
        Some(order) if order.agent_id == agent_id => {
            if let Some(o) = state.orders.lock().unwrap().get_mut(order_id) {
                o.status = OrderStatus::Cancelled;
            }
            state.wal.append(&[crate::wal::Record::OrderUpdated {
                order_id: order_id.to_string(),
                filled_qty: order.filled_qty,
                status: "Cancelled".to_string(),
            }]);
            Response::json(
                200,
                &Json::obj(vec![
                    ("order_id", Json::str(order_id)),
                    ("status", Json::str("Cancelled")),
                ]),
            )
        }
        Some(_) => err_response(403, ApiError::new("NOT_YOUR_ORDER", "cannot cancel another agent's order")),
        None => err_response(404, ApiError::new("NOT_FOUND", "order not resting (already filled/cancelled)")),
    }
}

/// `GET /v1/account/record` — a signed, portable statement of this agent's forecasting history.
///
/// # Why this is signed and not merely published
///
/// The venue promises never to publish who bet what, and that promise is load-bearing: the moment
/// a public stream reveals positions, the informed money leaves. So this endpoint is signed by the
/// caller, returns only the caller's own record, and the venue publishes nothing.
///
/// What it hands back is a *bearer document*. The attestation is signed with the venue's Ed25519
/// key, whose public half is published at `/v1/spec`, so whoever the agent chooses to show it to
/// can verify it offline — no call back here, no account, no permission. The agent decides who
/// sees its record; the venue only vouches for it.
///
/// That is the answer to the honest objection that points are worth nothing. They are, and they
/// always will be. What an agent takes away from playing here is not chips, it is a checkable
/// history of calls made before the answers were known, against questions that were hash-committed
/// before any money moved. That is worth something to an operator deciding whether to trust a
/// model, and it is very hard to obtain anywhere else.
pub fn get_agent_record(state: &AppState, req: &Request) -> Response {
    let agent_id = match authenticate(state, req, "GET", "/v1/account/record") {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let now = now_ms();
    let markets = state.markets.lock().unwrap();
    let stakes = state.stakes.lock().unwrap();
    let empty: Vec<crate::prediction::Stake> = Vec::new();
    let resolved: Vec<(&crate::prediction::Market, &[crate::prediction::Stake])> = markets
        .values()
        .filter(|m| m.winning_outcome.is_some())
        .map(|m| {
            (
                m,
                stakes.get(&m.market_id).map(|v| v.as_slice()).unwrap_or(empty.as_slice()),
            )
        })
        .collect();

    let record = crate::forecast::agent_record(&agent_id, &resolved, now);
    drop(stakes);
    drop(markets);

    let signature = state.sign_attestation(record.attestation_input().as_bytes());
    let pubkey = state.attestation_pubkey_hex();
    Response::json(200, &record.to_json(pubkey, signature))
}

pub fn get_account(state: &AppState, req: &Request) -> Response {
    let agent_id = match authenticate(state, req, "GET", "/v1/account") {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let score = state.trust.get_score(&agent_id);
    let band = state.trust.band(&agent_id);

    let balances: Vec<Json> = state
        .balances
        .lock()
        .unwrap()
        .iter()
        .filter(|((aid, _), _)| aid == &agent_id)
        .map(|((_, asset), bal)| Json::obj(vec![("asset", Json::str(asset.clone())), ("balance", Json::num(*bal))]))
        .collect();

    let open_orders: Vec<Json> = state
        .orders
        .lock()
        .unwrap()
        .values()
        .filter(|o| o.agent_id == agent_id && matches!(o.status, OrderStatus::Open | OrderStatus::PartiallyFilled))
        .map(Order::to_json)
        .collect();

    // What this agent is actually holding in prediction markets.
    //
    // Without this a bot is flying blind: it can read its balance and it can read the public
    // board, but it cannot answer "what am I currently exposed to?" — so it cannot reconcile
    // after a restart, cannot avoid doubling a position, and cannot decide whether to hedge.
    // Every serious client would have had to keep its own shadow ledger and hope the two never
    // diverged. Only the caller's own stakes appear; the endpoint is signed, and nothing here
    // reveals anybody else's position.
    let positions: Vec<Json> = {
        let markets = state.markets.lock().unwrap();
        let stakes = state.stakes.lock().unwrap();
        let now = now_ms();
        let mut out: Vec<(i64, Json)> = Vec::new();
        for (market_id, all) in stakes.iter() {
            let mine: Vec<&crate::prediction::Stake> =
                all.iter().filter(|s| s.agent_id == agent_id).collect();
            if mine.is_empty() {
                continue;
            }
            let Some(market) = markets.get(market_id) else { continue };

            let mut by_outcome = vec![0.0; market.outcomes.len()];
            for st in &mine {
                if st.outcome_idx < by_outcome.len() {
                    by_outcome[st.outcome_idx] += st.amount;
                }
            }
            let staked: f64 = by_outcome.iter().sum();
            let pool: f64 = all.iter().map(|s| s.amount).sum();

            let outcomes: Vec<Json> = market
                .outcomes
                .iter()
                .enumerate()
                .map(|(i, name)| {
                    Json::obj(vec![
                        ("index", Json::num(i as f64)),
                        ("name", Json::str(name.clone())),
                        ("my_stake", Json::num(by_outcome[i])),
                    ])
                })
                .collect();

            // What this position is worth if the market settles the way it currently stands.
            // A bot needs the number it would mark its book at, not just what it paid.
            let settled_value = market.winning_outcome.map(|w| {
                let winning_pool: f64 = all
                    .iter()
                    .filter(|s| s.outcome_idx == w)
                    .map(|s| s.amount)
                    .sum();
                let mine_on_winner = by_outcome.get(w).copied().unwrap_or(0.0);
                if winning_pool <= 0.0 || mine_on_winner <= 0.0 {
                    return 0.0;
                }
                let losing = pool - winning_pool;
                let rake = losing * crate::prediction::DEFAULT_RAKE_BPS / 10_000.0;
                mine_on_winner + (losing - rake) * mine_on_winner / winning_pool
            });

            out.push((
                market.closes_at_ms,
                Json::obj(vec![
                    ("market_id", Json::str(market_id.clone())),
                    ("question", Json::str(market.question.clone())),
                    ("asset", Json::str(market.asset.clone())),
                    ("status", Json::str(market.effective_status(now).label())),
                    ("closes_at_ms", Json::num(market.closes_at_ms as f64)),
                    ("observed_at_ms", Json::num(market.observed_at_ms as f64)),
                    ("outcomes", Json::Array(outcomes)),
                    ("total_staked", Json::num(staked)),
                    ("market_pool", Json::num(pool)),
                    (
                        "winning_outcome",
                        market.winning_outcome.map(|w| Json::num(w as f64)).unwrap_or(Json::Null),
                    ),
                    ("settled_value", settled_value.map(Json::num).unwrap_or(Json::Null)),
                ]),
            ));
        }
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out.into_iter().map(|(_, j)| j).collect()
    };

    Response::json(
        200,
        &Json::obj(vec![
            ("agent_id", Json::str(agent_id.clone())),
            ("trust_score", Json::num(score as f64)),
            ("trust_band", Json::str(band.label())),
            // Standing earned with real money at stake, which is the only kind the sold feed
            // weights by. Published so an agent can see the difference rather than wonder why
            // a good record on points markets did not move it.
            (
                "forecast_trust_score",
                Json::num(state.trust.get_forecast_score(&agent_id) as f64),
            ),
            ("rate_limit_per_sec", Json::num(band.rate_limit_per_sec() as f64)),
            ("balances", Json::Array(balances)),
            ("positions", Json::Array(positions)),
            ("open_orders", Json::Array(open_orders)),
        ]),
    )
}

/// Unauthenticated — market data quotes are public, unlike orders/account which touch funds.
pub fn get_quote(state: &AppState, req: &Request) -> Response {
    let Some(symbol) = req.query_param("symbol") else {
        return err_response(400, ApiError::new("MISSING_PARAM", "symbol query param required"));
    };
    let Some(book_mutex) = state.books.get(symbol) else {
        return err_response(404, ApiError::new("UNKNOWN_SYMBOL", "no such market"));
    };
    let book = book_mutex.lock().unwrap();
    let best_bid = book.best_bid().map(ticks_to_price);
    let best_ask = book.best_ask().map(ticks_to_price);
    let spread = match (best_bid, best_ask) {
        (Some(b), Some(a)) => Some(a - b),
        _ => None,
    };
    Response::json(
        200,
        &Json::obj(vec![
            ("symbol", Json::str(symbol)),
            ("best_bid", best_bid.map(Json::num).unwrap_or(Json::Null)),
            ("best_ask", best_ask.map(Json::num).unwrap_or(Json::Null)),
            ("spread", spread.map(Json::num).unwrap_or(Json::Null)),
        ]),
    )
}

/// Points granted to a newly registered agent, so it can stake in prediction markets
/// immediately.
///
/// This is the thing that makes the market work from participant one. Points have no cash value
/// and cost nothing to mint, so there is no deposit, no wallet, no KYC and no money between
/// registering and placing a first bet — which removes every step where a bot would otherwise
/// drop out. It grants nothing worth farming for the same reason it costs nothing to give.
const STARTING_POINTS: f64 = 1_000.0;
pub const POINTS_ASSET: &str = crate::credits::POINTS;

/// Anonymous requests per second per IP against the public feed. Generous for a human or a bot
/// evaluating the product, useless as a way to make the server do work.
/// Per-IP ceiling on the unauthenticated read endpoints: the board, one market, a quote, the tool
/// list, the spec.
///
/// These have no signature to rate-limit against, which is deliberate — an agent must be able to
/// price a bet and read the rules before it has an identity, and making it register first is
/// friction at exactly the wrong moment. But unmetered they are a free compute grant: a single
/// socket sustained about 4,000 quotes a second in testing, each one taking the markets and stakes
/// locks that settlement also needs. Nothing crashed, and that is not the point — the point is
/// that one anonymous caller should not be able to decide how much of the venue's time it gets.
///
/// Set generously. A well-behaved client reads the board once and then follows /v1/events; sixty a
/// second is far past anything a real integration does and still cheap to serve.
pub const PUBLIC_READ_PER_SEC: u32 = 60;

pub const FEED_REQUESTS_PER_SEC: u32 = 5;

/// How many markets `GET /v1/markets` returns by default, and the most it will return at all.
/// Live markets come first, so the default comfortably covers everything actually bettable on
/// any realistic venue.
pub const MARKET_PAGE_DEFAULT: usize = 100;
pub const MARKET_PAGE_MAX: usize = 500;

/// Anonymous polls per second per IP against the event cursor. Higher than the feed's cap: this
/// is the endpoint bots are *supposed* to poll, and a cursor query is cheap.
pub const EVENT_REQUESTS_PER_SEC: u32 = 20;
pub const EVENT_PAGE_DEFAULT: usize = 200;
pub const EVENT_PAGE_MAX: usize = 1_000;

/// Failed signature verifications allowed per address per minute before signed requests from it
/// are refused without being checked.
///
/// Generous on purpose: a real client with a bug or a skewed clock gets a long run of clear 401s
/// telling it what is wrong before this ever bites, and a client that is working never touches it
/// at all. It exists to bound the cost of a caller that cannot sign, not to police mistakes.
pub const SIGNATURE_FAILURES_PER_MINUTE: u32 = 60;
pub const ONE_MINUTE_MS: i64 = 60_000;

/// Shortest dispute window an agent may set on a market it opens. Five minutes: long enough for
/// another participant or a watching bot to notice and object, short enough not to be a burden.
pub const MIN_AGENT_DISPUTE_WINDOW_MS: i64 = 5 * 60 * 1000;

/// How long past its own deadline a self-settling market may sit before the operator is told.
///
/// The sweeper runs every few seconds, so anything still waiting minutes later is not slow, it is
/// stuck — and the difference between "give it a second" and "your price feed is down" is exactly
/// what the attention panel exists to tell you.
pub const AUTOMATION_GRACE_MS: i64 = 3 * 60 * 1000;

/// How long a proposed outcome stays challengeable before it can be paid.
///
/// Ten minutes by default: long enough that a wrong resolution can be caught by anyone watching,
/// short enough that a correct one isn't punished with a day of limbo. Per-market overridable,
/// and covered by the commitment so it cannot be shortened after stakes are placed.
const DEFAULT_DISPUTE_WINDOW_MS: i64 = 600_000;

fn market_json(
    market: &crate::prediction::Market,
    stakes: &[crate::prediction::Stake],
    include_rule: bool,
) -> Json {
    let pools = crate::prediction::pools(stakes, market.outcomes.len());
    market_json_from_pools(market, &pools, include_rule)
}

/// The same rendering, from totals that were already known.
///
/// Everything on a market page that a reader sees is derived from the pool totals, not from the
/// individual stakes — so a page view has no business walking a list that grows without bound.
/// Splitting it here is what lets the hot paths use the running totals while settlement keeps
/// working from the stakes themselves.
fn market_json_from_pools(
    market: &crate::prediction::Market,
    pools: &[f64],
    include_rule: bool,
) -> Json {
    let total: f64 = pools.iter().sum();
    let implied: Option<Vec<f64>> = if total > 0.0 && total.is_finite() {
        Some(pools.iter().map(|p| p / total).collect())
    } else {
        None
    };
    let outcomes: Vec<Json> = market
        .outcomes
        .iter()
        .enumerate()
        .map(|(i, name)| {
            Json::obj(vec![
                ("index", Json::num(i as f64)),
                ("name", Json::str(name.clone())),
                ("pool", Json::num(pools[i])),
                (
                    "implied_probability",
                    implied.as_ref().map(|p| Json::num(p[i])).unwrap_or(Json::Null),
                ),
            ])
        })
        .collect();

    let mut fields = vec![
        ("market_id", Json::str(market.market_id.clone())),
        ("question", Json::str(market.question.clone())),
        ("status", Json::str(market.effective_status(now_ms()).label())),
        ("asset", Json::str(market.asset.clone())),
        ("outcomes", Json::Array(outcomes)),
        ("total_pool", Json::num(pools.iter().sum::<f64>())),
        // Stakes placed, which is what this always counted — not distinct people. Derived from
        // the pool totals now, so a market page costs the same whether it holds ten stakes or a
        // hundred thousand.
        ("total_staked", Json::num(total)),
        ("closes_at_ms", Json::num(market.closes_at_ms as f64)),
        (
            "winning_outcome",
            market.winning_outcome.map(|w| Json::num(w as f64)).unwrap_or(Json::Null),
        ),
    ];
    fields.push(("observed_at_ms", Json::num(market.observed_at_ms as f64)));
    fields.push(("commitment_sha256", Json::str(market.commitment.clone())));
    fields.push(("commitment_intact", Json::Bool(market.commitment_is_intact())));
    if let Some(p) = &market.proposal {
        fields.push((
            "proposal",
            Json::obj(vec![
                ("outcome", p.outcome.map(|o| Json::num(o as f64)).unwrap_or(Json::Null)),
                ("proposed_at_ms", Json::num(p.proposed_at_ms as f64)),
                ("evidence", Json::str(p.evidence.clone())),
                ("automatic", Json::Bool(p.automatic)),
                (
                    "payable_at_ms",
                    Json::num((p.proposed_at_ms + market.dispute_window_ms) as f64),
                ),
            ]),
        ));
    }
    if include_rule {
        fields.push(("resolution", market.resolution.to_json()));
        fields.push(("dispute_window_ms", Json::num(market.dispute_window_ms as f64)));
        fields.push((
            "commitment_input",
            Json::str(market.commitment_input()),
        ));
        fields.push((
            "verify_commitment",
            Json::str(
                "commitment_sha256 = SHA-256(commitment_input). Recompute it yourself: if it \
                 matches, the terms you are being settled under are byte-for-byte the terms that \
                 were published before anyone staked.",
            ),
        ));
        fields.push((
            "payout_rule",
            Json::str(
                "Pari-mutuel. Winners get their stake back plus a pro-rata share of the losing \
                 pool, after a rake taken ONLY from the losing pool — so a correct forecast can \
                 never pay out less than it staked, and a market where everyone agreed costs \
                 nobody anything.",
            ),
        ));
        fields.push(("rake_bps_of_losing_pool", Json::num(crate::prediction::DEFAULT_RAKE_BPS)));
    }
    Json::obj(fields)
}

/// `GET /v1/markets` — every market, public and unauthenticated.
///
/// Discovery has to work before registration or nobody can evaluate whether the markets are worth
/// joining.
pub fn list_markets(state: &AppState, req: &Request) -> Response {
    if let Some(r) = public_read_limited(state, req, "board") { return r; }
    let markets = state.markets.lock().unwrap();
    let stakes = state.stakes.lock().unwrap();

    // Bounded, and biased towards what a caller can act on.
    //
    // This used to serialise every market in memory. With autopilot opening them on a timer that
    // is a response that grows all day — and the callers are agents polling for something to bet
    // on, who need the *live* board, not a year of receipts. Settled markets still appear, capped,
    // newest first, because a track record is worth showing; history in bulk is what the feed and
    // the log are for.
    let limit: usize = req
        .query_param("limit")
        .and_then(|v| v.parse().ok())
        .unwrap_or(MARKET_PAGE_DEFAULT)
        .clamp(1, MARKET_PAGE_MAX);

    let mut live: Vec<&crate::prediction::Market> =
        markets.values().filter(|m| !m.status.is_final()).collect();
    live.sort_by(|a, b| a.closes_at_ms.cmp(&b.closes_at_ms));

    let mut done: Vec<&crate::prediction::Market> =
        markets.values().filter(|m| m.status.is_final()).collect();
    done.sort_by(|a, b| b.closes_at_ms.cmp(&a.closes_at_ms));

    let total = markets.len();
    let items: Vec<&crate::prediction::Market> =
        live.iter().chain(done.iter()).copied().take(limit).collect();
    let shown = items.len();

    let list: Vec<Json> = items
        .iter()
        .map(|m| {
            let s = stakes.get(&m.market_id).cloned().unwrap_or_default();
            market_json(m, &s, false)
        })
        .collect();
    Response::json(
        200,
        &Json::obj(vec![
            ("markets", Json::Array(list)),
            ("shown", Json::num(shown as f64)),
            ("total", Json::num(total as f64)),
            (
                "note",
                Json::str(
                    "Pari-mutuel, so there is no counterparty to wait for — a stake is valid with \
                     any number of other participants, including none.",
                ),
            ),
        ]),
    )
}

/// `GET /v1/markets/{id}` — one market in full, including its resolution rule.
pub fn get_market(state: &AppState, req: &Request, market_id: &str) -> Response {
    if let Some(r) = public_read_limited(state, req, "board") { return r; }
    let markets = state.markets.lock().unwrap();
    let Some(market) = markets.get(market_id) else {
        return err_response(404, ApiError::new("UNKNOWN_MARKET", "no such market"));
    };
    let pools = state.pools_for(market_id, market.outcomes.len());
    Response::json(200, &market_json_from_pools(market, &pools, true))
}

/// Applies the anonymous read ceiling, returning the 429 to send if the caller is over it.
///
/// Keyed per endpoint family as well as per IP so that hammering the board cannot lock a caller
/// out of quoting a bet it is halfway through deciding on.
fn public_read_limited(state: &AppState, req: &Request, family: &str) -> Option<Response> {
    if state.rate_limit_disabled {
        return None;
    }
    let now = now_ms();
    if state
        .ip_limiter
        .allow(&format!("{family}:{}", req.client_ip()), PUBLIC_READ_PER_SEC, now)
    {
        return None;
    }
    Some(err_response(
        429,
        ApiError::new(
            "RATE_LIMITED",
            format!(
                "public reads are limited to {PUBLIC_READ_PER_SEC}/s per address. Register an \
                 agent and follow GET /v1/events instead of re-reading the board — it is cheaper \
                 for you and cannot miss anything."
            ),
        ),
    ))
}

/// The address a caller can actually reach this venue at.
///
/// Taken from the request's own `Host` header, because that is by definition an address that just
/// worked — the caller reached us with it. The alternative, and what this did first, was to build
/// it from `IKENGA_BIND`: on any real deployment that is `0.0.0.0:8080`, so every tool definition
/// handed to an agent pointed at `http://0.0.0.0:8080` and nothing an agent did with them could
/// ever connect. It was correct only on the developer's own laptop, which is the worst place for
/// a bug like this to hide.
///
/// `IKENGA_PUBLIC_URL` still wins when set, for a deployment behind a proxy that rewrites Host, or
/// one that terminates TLS elsewhere and needs the https:// form advertised.
fn public_base_url(req: &Request) -> String {
    if let Ok(u) = std::env::var("IKENGA_PUBLIC_URL") {
        let u = u.trim().trim_end_matches('/');
        if !u.is_empty() {
            return u.to_string();
        }
    }
    // Host is attacker-controlled, so it is bounded and filtered to characters that cannot break
    // out of the URL it is pasted into. A caller that sends a hostile Host only poisons the
    // response to its own request.
    let host = req
        .header("host")
        .map(|h| h.trim())
        .filter(|h| {
            !h.is_empty()
                && h.len() <= 253
                && h.chars().all(|c| c.is_ascii_alphanumeric() || "-._:[]".contains(c))
        })
        .unwrap_or("127.0.0.1:8080");
    // A proxy that terminated TLS tells us so; otherwise assume the scheme we are actually served
    // over, which is plain HTTP.
    let scheme = match req.header("x-forwarded-proto").map(|p| p.trim().to_ascii_lowercase()) {
        Some(p) if p.starts_with("https") => "https",
        _ => "http",
    };
    format!("{scheme}://{host}")
}

/// `GET /v1/oracle?pair=BTC-USD` — is this venue actually able to settle anything right now?
///
/// # Why this needed its own endpoint
///
/// Price sources are the single point of failure that does not announce itself. Everything else
/// keeps working perfectly without them: markets open, bets are accepted, the board looks alive.
/// Then nothing resolves, and the first visible symptom is a wave of voids seven days later,
/// long after anyone would connect it to a configuration mistake made on day one.
///
/// The shell already had `./ikenga sources`, which is the right tool for whoever is at a terminal.
/// The operator here is on a phone. This is the same check over HTTP, so it can be read from the
/// console — and so the answer to "why is nothing settling" takes one tap instead of an SSH
/// session.
///
/// Owner-only: it makes outbound requests to every configured venue, so leaving it open would
/// hand a stranger a way to spend the venue's rate limits at the exchanges.
pub fn oracle_health(state: &AppState, req: &Request) -> Response {
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }
    let pair = req.query_param("pair").unwrap_or("BTC-USD");
    let Some((base, quote)) = pair.split_once('-').filter(|(b, q)| !b.is_empty() && !q.is_empty())
    else {
        return err_response(400, ApiError::new("BAD_PAIR", "pair must look like BTC-USD"));
    };

    let spec = match std::env::var("IKENGA_ROUTE_SOURCES") {
        Ok(v) if !v.trim().is_empty() => v,
        Ok(_) => String::new(),
        Err(_) => crate::liquidity::DEFAULT_SOURCES.to_string(),
    };
    let (sources, problems) = crate::liquidity::sources_from_spec(&spec);

    let mut rows = Vec::new();
    let mut prices: Vec<f64> = Vec::new();
    for src in &sources {
        match src.quote(base, quote, 1.0) {
            Some(q) if q.buy_amount.is_finite() && q.buy_amount > 0.0 => {
                prices.push(q.buy_amount);
                rows.push(Json::obj(vec![
                    ("source", Json::str(src.name())),
                    ("answered", Json::Bool(true)),
                    ("price", Json::num(q.buy_amount)),
                ]));
            }
            _ => rows.push(Json::obj(vec![
                ("source", Json::str(src.name())),
                ("answered", Json::Bool(false)),
                ("price", Json::Null),
                (
                    "why",
                    Json::str(
                        "no usable answer — unreachable from this server, rate-limited, or this \
                         venue does not serve your country",
                    ),
                ),
            ])),
        }
    }

    let required = crate::router::RouterConfig::from_env().min_sources;
    let can_settle = prices.len() >= required;
    prices.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let median = if prices.is_empty() {
        None
    } else if prices.len() % 2 == 1 {
        Some(prices[prices.len() / 2])
    } else {
        Some((prices[prices.len() / 2 - 1] + prices[prices.len() / 2]) / 2.0)
    };
    let spread_pct = match (median, prices.first(), prices.last()) {
        (Some(m), Some(lo), Some(hi)) if m > 0.0 => Some((hi - lo) / m * 100.0),
        _ => None,
    };

    let verdict = if !can_settle {
        format!(
            "CANNOT SETTLE. {} of {required} required sources answered. Markets will open, take \
             bets, and never resolve. Add sources until at least {required} answer — the usual \
             cause is a venue that does not serve your country (Binance.com and OKX do not serve \
             the United States).",
            prices.len()
        )
    } else if spread_pct.is_some_and(|s| s > 2.0) {
        "CANNOT SETTLE RELIABLY. The sources answered but disagree by more than the 2% tolerance. \
         Check they all quote the same thing — USD against USDT is the usual culprit."
            .to_string()
    } else if prices.len() == required {
        format!(
            "Settling, with no margin. Exactly {required} sources answered, so one of them going \
             down stops every settlement. Add another."
        )
    } else {
        format!("Settling normally: {} sources answered, {required} needed.", prices.len())
    };

    Response::json(
        200,
        &Json::obj(vec![
            ("pair", Json::str(pair)),
            ("can_settle", Json::Bool(can_settle)),
            ("verdict", Json::str(verdict)),
            ("sources_answered", Json::num(prices.len() as f64)),
            ("sources_required", Json::num(required as f64)),
            ("median_price", median.map(Json::num).unwrap_or(Json::Null)),
            ("spread_pct", spread_pct.map(Json::num).unwrap_or(Json::Null)),
            ("max_spread_pct", Json::num(2.0)),
            ("sources", Json::Array(rows)),
            (
                "config_problems",
                Json::Array(problems.into_iter().map(Json::str).collect()),
            ),
            ("configured", Json::str(spec)),
            ("presets_available", Json::str(
                crate::liquidity::PRESETS.iter().map(|(n, _)| *n).collect::<Vec<_>>().join(", "),
            )),
        ]),
    )
}

/// `GET /v1/tools` — this venue described as callable tools, ready to paste into an agent.
///
/// # Why a second description of the same API
///
/// `/v1/spec` is written for whoever is *building* a client: prose, worked examples, the reasoning
/// behind each rule. It is the right shape for a person or a model reading once and then writing
/// code.
///
/// It is the wrong shape for the thing most agents actually are. An LLM-driven agent does not read
/// documentation at run time; it is handed a list of tools with JSON schemas and calls them. Give
/// that agent prose and someone has to sit in the middle translating it into function definitions
/// — which is a person, doing it by hand, once per framework, and getting the required fields
/// subtly wrong.
///
/// So the venue publishes its own tool definitions. Fetch this, hand the array to the model, and
/// it can trade. Nobody writes an integration layer, and the definitions cannot drift out of date
/// relative to the API because they are served by it.
///
/// Each entry carries an `http` block as well as a schema, so a generic executor — about thirty
/// lines — can turn any tool call into a request without knowing anything about this venue in
/// particular.
pub fn get_tools(state: &AppState, req: &Request) -> Response {
    if let Some(r) = public_read_limited(state, req, "docs") { return r; }
    let prop = |ty: &str, desc: &str| Json::obj(vec![
        ("type", Json::str(ty)),
        ("description", Json::str(desc)),
    ]);
    let tool = |name: &str,
                description: &str,
                props: Vec<(&str, Json)>,
                required: Vec<&str>,
                method: &str,
                path: &str,
                auth: &str| {
        Json::obj(vec![
            ("name", Json::str(name)),
            ("description", Json::str(description)),
            (
                "input_schema",
                Json::obj(vec![
                    ("type", Json::str("object")),
                    ("properties", Json::obj(props)),
                    (
                        "required",
                        Json::Array(required.into_iter().map(Json::str).collect()),
                    ),
                ]),
            ),
            (
                "http",
                Json::obj(vec![
                    ("method", Json::str(method)),
                    // Braced names are substituted from the arguments; anything left over that the
                    // path did not consume becomes the query string on GET and the JSON body on
                    // POST. That one rule is enough to execute every tool here.
                    ("path", Json::str(path)),
                    ("auth", Json::str(auth)),
                ]),
            ),
        ])
    };

    let tools = vec![
        tool(
            "ikenga_register",
            "Create an identity at this prediction venue and receive 1000 PTS of promotional \
             credit immediately. PTS can never be withdrawn for cash; it exists so you can trade, \
             integrate and build a track record without depositing anything. Sign this request \
             with the private key matching the public key you are registering.",
            vec![("pubkey_hex", prop("string", "Your Ed25519 public key, 64 hex characters."))],
            vec!["pubkey_hex"],
            "POST",
            "/v1/agents",
            "self-signed",
        ),
        tool(
            "ikenga_list_markets",
            "List open prediction markets: the question, the outcomes, how much money is on each \
             side, and when each closes. Use this to find something to forecast.",
            vec![("limit", prop("integer", "How many to return. Default 100."))],
            vec![],
            "GET",
            "/v1/markets",
            "none",
        ),
        tool(
            "ikenga_quote",
            "CALL THIS BEFORE EVERY BET. Prices a hypothetical stake: what it would pay if you \
             are right, how much your own money moves the odds, and breakeven_probability — the \
             probability you must exceed for the bet to be worth making. Pass your own estimate as \
             `belief` and it also returns expected_value, a verdict, and max_stake_at_belief, the \
             largest amount that still has positive expected value. If the verdict says not to \
             bet, do not bet: this is a negative-sum game against other participants and betting \
             without an edge loses money reliably.",
            vec![
                ("market_id", prop("string", "From ikenga_list_markets.")),
                ("outcome", prop("integer", "Index of the outcome you would back. 0 is the first.")),
                ("amount", prop("number", "How much you are considering staking.")),
                (
                    "belief",
                    prop("number", "Your own probability that this outcome happens, strictly between 0 and 1. Optional but strongly recommended — without it you get no verdict."),
                ),
            ],
            vec!["market_id", "outcome", "amount"],
            "GET",
            "/v1/markets/{market_id}/quote",
            "none",
        ),
        tool(
            "ikenga_place_stake",
            "Back an outcome with money. Your stake joins a pool; if your outcome happens you get \
             your stake back plus a pro-rata share of the losing pool, less a 1% fee taken only \
             from the losers. A correct forecast never returns less than it risked. Set \
             dry_run=true to validate everything and be told what would happen without moving any \
             money.",
            vec![
                ("market_id", prop("string", "The market to stake in.")),
                ("outcome", prop("integer", "Index of the outcome you are backing.")),
                ("amount", prop("number", "How much to stake, in the market's asset.")),
                ("dry_run", prop("boolean", "If true, nothing is committed and the response tells you what would have happened.")),
            ],
            vec!["market_id", "outcome", "amount"],
            "POST",
            "/v1/markets/{market_id}/stakes",
            "signed",
        ),
        tool(
            "ikenga_create_challenge",
            "Post a head-to-head bet on ANY question and put your own money behind it. Anyone at \
             all can take the other side — another bot, a person on the website, it is the same \
             object either way. Nothing is live until somebody funds the other side, and your \
             stake is returned in full if nobody does. Two ways to settle it, both stated up \
             front and neither of which lets you decide alone: resolution.kind = \
             \"price_threshold\" settles automatically from a cross-checked price feed, or \
             \"mutual_agreement\" settles when you and whoever took the other side both report \
             the same outcome afterwards. Use this when the board has nothing you want, or when \
             you need to hedge a risk you actually carry.",
            vec![
                ("question", prop("string", "The question, stated so a stranger can settle it.")),
                ("outcomes", prop("array", "Exactly two outcome names, e.g. [\"YES\",\"NO\"].")),
                ("my_outcome", prop("integer", "Which of the two sides you are taking. 0 or 1.")),
                ("my_stake", prop("number", "What you are putting up.")),
                ("their_stake", prop("number", "What the other side must put up. The ratio to my_stake is the odds.")),
                ("closes_at_ms", prop("integer", "Unix ms when betting stops.")),
                ("observed_at_ms", prop("integer", "Unix ms when the answer is read. Must be after closes_at_ms.")),
                ("expires_at_ms", prop("integer", "Unix ms after which the unmatched offer is withdrawn and your stake returned.")),
                ("resolution", prop("object", "How it settles. Either {\"kind\":\"price_threshold\", \"symbol\":\"BTC-USD\", \"comparator\":\"above\", \"threshold\":70000, \"if_true_outcome\":0, \"if_false_outcome\":1} for a feed, or {\"kind\":\"mutual_agreement\", \"criteria\":\"how you will both know\"} for anything else.")),
            ],
            vec!["question", "my_stake", "their_stake", "closes_at_ms", "observed_at_ms", "resolution"],
            "POST",
            "/v1/challenges",
            "signed",
        ),
        tool(
            "ikenga_list_challenges",
            "Open head-to-head offers waiting for someone to take the other side, with the stake \
             required and what it pays.",
            vec![],
            vec![],
            "GET",
            "/v1/challenges",
            "none",
        ),
        tool(
            "ikenga_accept_challenge",
            "Take the other side of an open head-to-head offer. Both stakes lock immediately and \
             it settles like any other market.",
            vec![("challenge_id", prop("string", "From ikenga_list_challenges."))],
            vec!["challenge_id"],
            "POST",
            "/v1/challenges/{challenge_id}/accept",
            "signed",
        ),
        tool(
            "ikenga_report_outcome",
            "For a head-to-head bet that settles by mutual agreement: say what actually happened, \
             after the observation time. It pays out the moment you and the other side report the \
             same outcome. Report different things and it goes to the operator to review, and \
             voids with both stakes returned if nobody reviews it. You cannot change your answer \
             and you cannot settle alone — which is exactly why anyone is willing to take a bet \
             on a question you wrote.",
            vec![
                ("market_id", prop("string", "The market the challenge became once it was taken. It is in your positions from ikenga_get_account.")),
                ("outcome", prop("integer", "The index of what actually happened.")),
            ],
            vec!["market_id", "outcome"],
            "POST",
            "/v1/markets/{market_id}/report",
            "signed",
        ),
        tool(
            "ikenga_get_account",
            "Your balances, every position you currently hold, and your trust score. Call this \
             after a restart to reconcile — you do not need to keep your own shadow ledger.",
            vec![],
            vec![],
            "GET",
            "/v1/account",
            "signed",
        ),
        tool(
            "ikenga_get_events",
            "Everything that has happened since a cursor: markets opening, closing, resolving, \
             paying out. Poll this rather than re-fetching the market list — it is cheaper, it \
             cannot miss an event that happened while you were away, and it will tell you if you \
             have been gone long enough to have a gap.",
            vec![
                ("since", prop("integer", "The cursor from your last call. Use 0 the first time.")),
                ("limit", prop("integer", "Maximum events to return.")),
            ],
            vec![],
            "GET",
            "/v1/events",
            "none",
        ),
        tool(
            "ikenga_get_record",
            "Your own forecasting record: every market you were in that resolved, your implied \
             probability on each, and your Brier score — signed with the venue's Ed25519 key, with \
             each market's commitment hash so anyone you show it to can verify it independently \
             without contacting this server. This is the durable thing you earn here, and the \
             reason trading on non-redeemable points is still worth your compute.",
            vec![],
            vec![],
            "GET",
            "/v1/account/record",
            "signed",
        ),
        tool(
            "ikenga_get_feed",
            "The venue's aggregate forecast: a probability for every live market, weighted by how \
             well each participant has scored historically. Free at a delay. Use it as a prior in \
             your own model. If you have no edge of your own, read this instead of betting — it \
             costs nothing to be wrong about.",
            vec![],
            vec![],
            "GET",
            "/v1/feed",
            "none",
        ),
    ];

    Response::json(
        200,
        &Json::obj(vec![
            ("service", Json::str("ikenga")),
            (
                "what_this_is",
                Json::str(
                    "This venue's own tool definitions, in the shape an LLM agent framework \
                     expects. Hand the `tools` array to your model as its tool list and it can \
                     trade here with no integration code.",
                ),
            ),
            ("base_url", Json::str(public_base_url(req))),
            ("tools", Json::Array(tools)),
            (
                "how_to_execute",
                Json::Array(vec![
                    Json::str(
                        "Substitute any {braced} argument into the path. Remaining arguments \
                         become the query string on GET and the JSON body on POST.",
                    ),
                    Json::str(
                        "auth=none: send it as-is. auth=signed: add X-Agent-ID, X-Timestamp \
                         (RFC3339), X-Nonce (unique) and X-Signature — hex of an Ed25519 \
                         signature over METHOD + PATH + X-Timestamp + X-Nonce + raw body, \
                         concatenated with no separators. auth=self-signed is the same, signed \
                         with the key you are registering.",
                    ),
                    Json::str(
                        "Serialise the JSON body ONCE and both sign and send that exact buffer. \
                         Signing a re-serialised copy is the most common integration failure; if \
                         it happens, the 401 body shows you the precise bytes the server expected.",
                    ),
                    Json::str(
                        "PATH in the signature includes the query string exactly as sent.",
                    ),
                ]),
            ),
            (
                "suggested_loop",
                Json::Array(vec![
                    Json::str("ikenga_register once, then keep the key."),
                    Json::str("ikenga_list_markets to see what is open."),
                    Json::str("ikenga_quote with your own belief, for anything you have a view on."),
                    Json::str("ikenga_place_stake only where the verdict is positive, sized at or below max_stake_at_belief."),
                    Json::str("ikenga_get_events in a loop to learn how it turned out."),
                    Json::str("ikenga_report_outcome for any head-to-head bet of yours that settles by agreement, once its observation time has passed."),
                    Json::str("ikenga_get_record when you want to prove how you have done."),
                ]),
            ),
            ("full_documentation", Json::str("GET /v1/spec")),
        ]),
    )
}

/// `GET /v1/markets/{id}/quote?outcome=0&amount=100[&belief=0.62]` — price a bet before making it.
///
/// # The gap this closes
///
/// Until this existed, an agent could see the pools and nothing else. To decide whether a bet was
/// worth making it had to re-derive the venue's payout rule from prose, apply the rake convention,
/// account for the fact that its own stake changes the odds it is being offered, and only then
/// work out what it needed to believe. Every integrator had to get all of that right, in their own
/// code, before their first bet — and an integrator who got it subtly wrong would lose money and
/// never know why. A venue that makes the caller reimplement its own arithmetic to find out
/// whether the caller should be there is not one a careful operator lets their agent use.
///
/// # The number that matters
///
/// `breakeven_probability`. Everything else is scaffolding for it. It is the probability the
/// caller must *exceed* for this bet to be worth making, given the pools right now and the size
/// they are contemplating. If the caller's own estimate is below it, the correct action is to not
/// bet, and this endpoint will say so in `verdict`.
///
/// That is deliberate, and it is the opposite of what a casino does. A venue whose participants
/// are programs cannot survive on participants who do not know what they are doing, because a
/// program that loses money at a venue is switched off by its operator and never returns. The
/// venue's interest and the caller's interest point the same way here: it wants the bets where
/// someone actually has a view, and it wants the other ones not to happen.
///
/// # Market impact
///
/// In a thin pool your own stake is most of the pool, so the odds you get are not the odds you
/// saw. `implied_probability_after` is what the board reads once your money is in it, and
/// `max_stake_at_belief` is the largest amount that still has non-negative expected value at your
/// stated belief — past that point you are betting against your own price.
pub fn quote_stake(state: &AppState, req: &Request, market_id: &str) -> Response {
    if let Some(r) = public_read_limited(state, req, "quote") { return r; }
    let markets = state.markets.lock().unwrap();
    let Some(market) = markets.get(market_id) else {
        return err_response(404, ApiError::new("UNKNOWN_MARKET", "no such market"));
    };
    let outcome_count = market.outcomes.len();
    let outcome_names: Vec<String> = market.outcomes.clone();
    let asset = market.asset.clone();
    let closes_at_ms = market.closes_at_ms;
    let created_at_ms = market.created_at_ms;
    let is_open = market.status == crate::prediction::MarketStatus::Open;
    drop(markets);

    let outcome: usize = match req.query_param("outcome").map(|v| v.parse::<usize>()) {
        Some(Ok(o)) if o < outcome_count => o,
        Some(_) => {
            return err_response(
                400,
                ApiError::new(
                    "UNKNOWN_OUTCOME",
                    "outcome must be an index into this market's outcomes",
                ),
            )
        }
        None => 0,
    };
    let amount: f64 = match req.query_param("amount").map(|v| v.parse::<f64>()) {
        Some(Ok(a)) if a.is_finite() && a > 0.0 && a <= crate::credits::MAX_AMOUNT => a,
        Some(_) => {
            return err_response(
                400,
                ApiError::new("BAD_AMOUNT", "amount must be a positive, finite number"),
            )
        }
        None => 0.0,
    };
    // A belief outside (0,1) is not a probability. Refusing it rather than clamping matters: a
    // caller that meant 62 and not 0.62 should be told, not quietly quoted on certainty.
    let belief: Option<f64> = match req.query_param("belief").map(|v| v.parse::<f64>()) {
        Some(Ok(b)) if b.is_finite() && b > 0.0 && b < 1.0 => Some(b),
        Some(_) => {
            return err_response(
                400,
                ApiError::new("BAD_BELIEF", "belief must be a probability strictly between 0 and 1"),
            )
        }
        None => None,
    };

    // Running totals, not a walk of every stake. On a market with tens of thousands of stakes
    // the walk cost more than everything else in this handler put together, and got worse the
    // more the market was used.
    let pools = state.pools_for(market_id, outcome_count);
    let total: f64 = pools.iter().sum();
    let backed = pools[outcome];
    let rake_frac = crate::prediction::DEFAULT_RAKE_BPS / 10_000.0;

    // Your stake joins your own side, so the pool that pays you grows and the pool you are paid
    // *from* does not.
    let backed_after = backed + amount;
    let total_after = total + amount;
    let against = total - backed;
    let distributable = (against * (1.0 - rake_frac)).max(0.0);

    let profit_if_right =
        if backed_after > 0.0 && amount > 0.0 { distributable * amount / backed_after } else { 0.0 };
    let payout_if_right = amount + profit_if_right;

    // p·profit = (1−p)·stake  ⇒  p = stake / (stake + profit).
    // With nothing on the other side there is no profit to win and no probability high enough,
    // which is reported as 1.0 rather than a division by zero.
    let breakeven = if amount <= 0.0 {
        Json::Null
    } else if profit_if_right <= 0.0 {
        Json::num(1.0)
    } else {
        Json::num(amount / (amount + profit_if_right))
    };

    let implied_before = if total > 0.0 { backed / total } else { 0.0 };
    let implied_after = if total_after > 0.0 { backed_after / total_after } else { 0.0 };

    let (ev, verdict, max_stake) = match belief {
        Some(p) if amount > 0.0 => {
            let ev = p * profit_if_right - (1.0 - p) * amount;
            // EV(a) = 0  ⇒  a* = p·distributable/(1−p) − backed.
            let ceiling = p * distributable / (1.0 - p) - backed;
            let verdict = if ev > 0.0 {
                "positive_expected_value"
            } else if ev < 0.0 {
                "negative_expected_value — do not place this bet"
            } else {
                "break_even"
            };
            (
                Json::num(ev),
                Json::str(verdict),
                if ceiling.is_finite() && ceiling > 0.0 { Json::num(ceiling) } else { Json::num(0.0) },
            )
        }
        _ => (
            Json::Null,
            Json::str("pass ?belief=<your probability> to be told whether this bet is worth making"),
            Json::Null,
        ),
    };

    // Said plainly, because these are the conditions under which an agent loses money for reasons
    // that have nothing to do with being wrong.
    let mut warnings: Vec<Json> = Vec::new();
    if !is_open {
        warnings.push(Json::str("this market is not open — a stake will be refused"));
    }
    if against <= 0.0 {
        warnings.push(Json::str(
            "nobody has taken the other side yet, so there is nothing to win; if it stays that way \
             the market voids and you are refunded in full",
        ));
    }
    if amount > 0.0 && total > 0.0 && amount > total {
        warnings.push(Json::str(
            "your stake is larger than the entire existing pool, so you are mostly setting the \
             price you are paid at — see implied_probability_after",
        ));
    }
    if let Some(p) = belief {
        if (p - implied_before).abs() < 0.02 && total > 0.0 {
            warnings.push(Json::str(
                "your estimate is within 2 points of the pool's — that is not an edge, and the \
                 rake will take the difference",
            ));
        }
    }

    Response::json(
        200,
        &Json::obj(vec![
            ("market_id", Json::str(market_id)),
            ("asset", Json::str(asset)),
            ("outcome", Json::num(outcome as f64)),
            ("outcome_name", Json::str(outcome_names[outcome].clone())),
            ("amount", Json::num(amount)),
            ("open", Json::Bool(is_open)),
            ("closes_at_ms", Json::num(closes_at_ms as f64)),
            ("closes_in_ms", Json::num((closes_at_ms - now_ms_pub()).max(0) as f64)),
            (
                "pools",
                Json::Array(
                    pools
                        .iter()
                        .enumerate()
                        .map(|(i, p)| {
                            Json::obj(vec![
                                ("index", Json::num(i as f64)),
                                ("name", Json::str(outcome_names[i].clone())),
                                ("pool", Json::num(*p)),
                                (
                                    "implied_probability",
                                    Json::num(if total > 0.0 { p / total } else { 0.0 }),
                                ),
                            ])
                        })
                        .collect(),
                ),
            ),
            ("total_pool", Json::num(total)),
            ("payout_if_right", Json::num(payout_if_right)),
            ("profit_if_right", Json::num(profit_if_right)),
            ("loss_if_wrong", Json::num(amount)),
            ("breakeven_probability", breakeven),
            ("implied_probability_before", Json::num(implied_before)),
            ("implied_probability_after", Json::num(implied_after)),
            ("your_share_of_winning_side", Json::num(if backed_after > 0.0 { amount / backed_after } else { 0.0 })),
            ("expected_value", ev),
            ("max_stake_at_belief", max_stake),
            ("verdict", verdict),
            ("rake_bps", Json::num(crate::prediction::DEFAULT_RAKE_BPS)),
            (
                "early_liquidity_rebate_bps_of_rake",
                Json::num(crate::prediction::DEFAULT_REBATE_SHARE_BPS),
            ),
            (
                "earliness_now",
                Json::num(
                    crate::prediction::StakeWindow { opened_at_ms: created_at_ms, closes_at_ms }
                        .earliness(now_ms_pub()),
                ),
            ),
            ("warnings", Json::Array(warnings)),
            (
                "note",
                Json::str(
                    "breakeven_probability is the number that decides this. If your own estimate \
                     is not above it, do not bet — the rake makes a coin flip a losing game, and \
                     this venue would rather sell you the forecast feed than take that bet.",
                ),
            ),
        ]),
    )
}

/// `POST /v1/markets/{id}/stakes` — back an outcome.
///
/// Body: `{"outcome": 0, "amount": 25}`. Signed like any agent endpoint.
pub fn place_stake(state: &AppState, req: &Request, market_id: &str) -> Response {
    let path = format!("/v1/markets/{market_id}/stakes");
    let agent_id = match authenticate(state, req, "POST", &path) {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let Ok(body_str) = std::str::from_utf8(&req.body) else {
        return err_response(400, ApiError::new("BAD_BODY", "body must be UTF-8 JSON"));
    };
    let Ok(parsed) = crate::json::parse(body_str) else {
        return err_response(400, ApiError::new("BAD_BODY", "body must be valid JSON"));
    };
    let (Some(outcome), Some(amount)) = (
        parsed.get("outcome").and_then(crate::json::Json::as_f64),
        parsed.get("amount").and_then(crate::json::Json::as_f64),
    ) else {
        return err_response(
            400,
            ApiError::new("MISSING_FIELD", "outcome (index) and amount are both required"),
        );
    };
    if outcome < 0.0 {
        return err_response(400, ApiError::new("UNKNOWN_OUTCOME", "outcome must be >= 0"));
    }

    if state.is_operator_agent(&agent_id) {
        return err_response(
            403,
            ApiError::new(
                "OPERATOR_CANNOT_STAKE",
                "this agent is declared as operator-controlled and may not stake in markets the                  operator resolves",
            ),
        );
    }

    // A rehearsal: every check the real call makes, nothing committed. Opt-in and off by
    // default, so a client that does not know about it cannot accidentally believe it has bet.
    let dry_run = parsed
        .get("dry_run")
        .and_then(|v| match v {
            crate::json::Json::Bool(b) => Some(*b),
            _ => None,
        })
        .unwrap_or(false);

    if dry_run {
        return match state.dry_run_stake(market_id, &agent_id, outcome as usize, amount, now_ms()) {
            Ok(remaining) => Response::json(
                200,
                &Json::obj(vec![
                    ("dry_run", Json::Bool(true)),
                    ("would_succeed", Json::Bool(true)),
                    ("market_id", Json::str(market_id)),
                    ("outcome", Json::num(outcome)),
                    ("amount", Json::num(amount)),
                    ("balance_would_remain", Json::num(remaining)),
                    (
                        "note",
                        Json::str(
                            "Nothing was staked and no money moved. Send the same request without \
                             dry_run to commit it. For what it would pay, call GET \
                             /v1/markets/{id}/quote.",
                        ),
                    ),
                ]),
            ),
            Err(e) => {
                let status = match e.code() {
                    "MALFORMED_MARKET" | "INSUFFICIENT_BALANCE" => 400,
                    _ => 422,
                };
                // Reported as the error the real call would have produced, so a caller can
                // handle a rehearsal failure with the same branch that handles a live one.
                err_response(
                    status,
                    ApiError::new(
                        e.code(),
                        format!("{} (dry run — nothing was staked)", e.message()),
                    ),
                )
            }
        };
    }

    match state.place_stake(market_id, &agent_id, outcome as usize, amount, now_ms()) {
        Ok(remaining) => {
            let markets = state.markets.lock().unwrap();
            let market = markets.get(market_id);
            // Cloning the entire stake list to work out one probability made every bet more
            // expensive than the last: the busiest market — the one people most want to bet in —
            // became the slowest to bet in. Running totals instead.
            let implied = market.and_then(|m| {
                let pools = state.pools_for(market_id, m.outcomes.len());
                let total: f64 = pools.iter().sum();
                (total > 0.0 && total.is_finite())
                    .then(|| pools.iter().map(|p| p / total).collect::<Vec<f64>>())
            });
            Response::json(
                200,
                &Json::obj(vec![
                    ("market_id", Json::str(market_id)),
                    ("outcome", Json::num(outcome)),
                    ("amount", Json::num(amount)),
                    ("balance_remaining", Json::num(remaining)),
                    (
                        "market_implied_probabilities",
                        implied
                            .map(|p| Json::Array(p.into_iter().map(Json::num).collect()))
                            .unwrap_or(Json::Null),
                    ),
                    (
                        "settlement",
                        Json::str(
                            "Paid out when the market resolves. Winners receive their stake plus \
                             a share of the losing pool; the rake comes only from the losing side.",
                        ),
                    ),
                ]),
            )
        }
        Err(e) => {
            let status = match e.code() {
                "MALFORMED_MARKET" | "INSUFFICIENT_BALANCE" => 400,
                _ => 422,
            };
            err_response(status, ApiError::new(e.code(), e.message()))
        }
    }
}

/// `POST /v1/markets` — open a market. Owner-only.
///
/// Deliberately not open to agents yet: whoever writes the question and the resolution rule
/// decides who wins, so letting anyone create a market they can also stake in is a
/// self-resolution attack with extra steps. See docs/ROADMAP.md.
pub fn create_market(state: &AppState, req: &Request) -> Response {
    // Either the operator, or any registered agent. Agents get a narrower door — see below.
    let is_owner = authenticate_owner(state, req).is_ok();
    let creator = if is_owner {
        None
    } else {
        match authenticate(state, req, "POST", "/v1/markets") {
            Ok(id) => Some(id),
            Err(_) => {
                return err_response(
                    401,
                    ApiError::new(
                        "UNAUTHORIZED",
                        "sign this request as a registered agent, or send the owner key",
                    ),
                )
            }
        }
    };

    let Ok(body_str) = std::str::from_utf8(&req.body) else {
        return err_response(400, ApiError::new("BAD_BODY", "body must be UTF-8 JSON"));
    };
    let Ok(parsed) = crate::json::parse(body_str) else {
        return err_response(400, ApiError::new("BAD_BODY", "body must be valid JSON"));
    };
    let (Some(question), Some(closes_at), Some(observed_at)) = (
        parsed.get("question").and_then(crate::json::Json::as_str),
        parsed.get("closes_at_ms").and_then(crate::json::Json::as_f64),
        parsed.get("observed_at_ms").and_then(crate::json::Json::as_f64),
    ) else {
        return err_response(
            400,
            ApiError::new(
                "MISSING_FIELD",
                "question, closes_at_ms and observed_at_ms are required (observation must be \
                 strictly after close)",
            ),
        );
    };
    let outcomes: Vec<String> = match parsed.get("outcomes") {
        Some(Json::Array(items)) => {
            items.iter().filter_map(|i| i.as_str().map(|s| s.to_string())).collect()
        }
        _ => vec!["YES".to_string(), "NO".to_string()],
    };

    let Some(resolution) = parsed
        .get("resolution")
        .and_then(crate::prediction::ResolutionSpec::from_json)
    else {
        return err_response(
            400,
            ApiError::new(
                "MISSING_FIELD",
                "resolution is required: either {\"kind\":\"price_threshold\",\"symbol\":..., \
                 \"comparator\":\"above\",\"threshold\":...,\"if_true_outcome\":0, \
                 \"if_false_outcome\":1} or {\"kind\":\"operator_declared\",\"criteria\":..., \
                 \"source\":...}",
            ),
        );
    };

    let now = now_ms();
    let mut market = crate::prediction::Market {
        market_id: format!(
            "mkt_{}",
            crate::crypto::hex_encode(&crate::crypto::random_bytes_pooled(8))
        ),
        question: question.to_string(),
        outcomes,
        resolution,
        asset: parsed
            .get("asset")
            .and_then(crate::json::Json::as_str)
            .unwrap_or(POINTS_ASSET)
            .to_string(),
        closes_at_ms: closes_at as i64,
        observed_at_ms: observed_at as i64,
        dispute_window_ms: parsed
            .get("dispute_window_ms")
            .and_then(crate::json::Json::as_f64)
            .unwrap_or(DEFAULT_DISPUTE_WINDOW_MS as f64) as i64,
        commitment: String::new(),
        status: crate::prediction::MarketStatus::Open,
        proposal: None,
        winning_outcome: None,
        created_at_ms: now,
        disputed_at_ms: None,
    };

    // An agent may open a market, but only one whose outcome it cannot influence.
    //
    // Whoever writes a resolution rule effectively decides who wins it, so an agent that could
    // create an `operator_declared` market and also stake in it would be marking its own
    // homework. Restricting agents to `price_threshold` removes the problem at the root rather
    // than policing it: the symbol, comparator and threshold are committed up front, the answer
    // comes from the cross-checked median of external feeds, and the creator has no more say in
    // the result than anybody else. The operator keeps both kinds because someone has to be able
    // to write questions no price feed can answer.
    if let Some(agent_id) = &creator {
        if !market.resolution.is_machine_resolved() {
            return err_response(
                403,
                ApiError::new(
                    "AGENT_MARKETS_MUST_BE_MACHINE_RESOLVED",
                    "agents can only open markets that settle from a price feed \
                     (resolution.kind = \"price_threshold\"). A market you resolve yourself is a \
                     market you can win by writing the rule, so those stay operator-only.",
                ),
            );
        }
        // A dispute window of zero means an auto-resolution pays out the instant it is
        // proposed, with no interval in which anyone can object to a bad price observation.
        // The operator may choose that for its own markets; an agent opening a market others
        // will stake into may not, because the person who would be hurt by it is not the one
        // making the choice.
        if market.dispute_window_ms < MIN_AGENT_DISPUTE_WINDOW_MS {
            return err_response(
                400,
                ApiError::new(
                    "DISPUTE_WINDOW_TOO_SHORT",
                    format!(
                        "dispute_window_ms must be at least {} on an agent-opened market, so a \
                         bad observation can be challenged before anyone is paid",
                        MIN_AGENT_DISPUTE_WINDOW_MS
                    ),
                ),
            );
        }
        if state.trust.band(agent_id) == crate::trust::TrustBand::New
            && !state.rate_limit_disabled
        {
            return err_response(
                403,
                ApiError::new(
                    "TRUST_TOO_LOW",
                    "opening markets needs a trust score above the New band. Stake in existing \
                     markets and forecast well — trust is awarded on how calibrated your \
                     settled predictions were.",
                ),
            );
        }
    }

    // Validate BEFORE committing, so a malformed market never gets a commitment hash at all.
    if let Err(e) = market.validate_spec(now) {
        return err_response(400, ApiError::new("MALFORMED_MARKET", e.message()));
    }
    if !state.asset_is_stakeable(&market.asset) {
        return err_response(
            400,
            ApiError::new(
                "ASSET_NOT_ENABLED",
                format!(
                    "asset {} is not enabled on this deployment (real-money assets need \
                     IKENGA_REAL_MONEY=1)",
                    market.asset
                ),
            ),
        );
    }
    market.commitment = market.compute_commitment();

    let id = market.market_id.clone();
    state.open_market(market);

    let markets = state.markets.lock().unwrap();
    Response::json(201, &market_json(markets.get(&id).unwrap(), &[], true))
}

/// `POST /v1/markets/{id}/propose` — assert an outcome and start the dispute window. Owner-only.
///
/// Body: `{"outcome": 0, "evidence": "..."}` or `{"void": true, "evidence": "..."}`.
///
/// Nothing is paid here, and that separation is the point. Polymarket's $7M false resolution
/// paid out because the asserted outcome *was* the settlement. Here an assertion only starts a
/// clock, and anyone can stop it.
pub fn propose_outcome(state: &AppState, req: &Request, market_id: &str) -> Response {
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }
    let parsed = std::str::from_utf8(&req.body)
        .ok()
        .and_then(|s| crate::json::parse(s).ok())
        .unwrap_or(Json::Null);
    let void = matches!(parsed.get("void"), Some(Json::Bool(true)));
    let outcome = parsed.get("outcome").and_then(crate::json::Json::as_f64);
    if !void && outcome.is_none() {
        return err_response(
            400,
            ApiError::new("MISSING_FIELD", "provide outcome (index) or void: true"),
        );
    }
    let evidence = parsed
        .get("evidence")
        .and_then(crate::json::Json::as_str)
        .unwrap_or("")
        .to_string();
    if evidence.trim().is_empty() {
        return err_response(
            400,
            ApiError::new(
                "MISSING_FIELD",
                "evidence is required — a proposal nobody can check is not a resolution",
            ),
        );
    }

    let winning = if void { None } else { Some(outcome.unwrap_or(0.0).max(0.0) as usize) };
    match state.propose_outcome(market_id, winning, evidence, false, now_ms()) {
        Ok(p) => Response::json(
            200,
            &Json::obj(vec![
                ("market_id", Json::str(market_id)),
                ("proposed_outcome", p.outcome.map(|o| Json::num(o as f64)).unwrap_or(Json::Null)),
                ("void_proposed", Json::Bool(p.outcome.is_none())),
                ("evidence", Json::str(p.evidence.clone())),
                ("automatic", Json::Bool(false)),
                ("status", Json::str("Proposed")),
                (
                    "note",
                    Json::str("Nothing has been paid. Call finalize after the dispute window."),
                ),
            ]),
        ),
        Err(e) => err_response(409, ApiError::new("CANNOT_PROPOSE", e)),
    }
}

/// `POST /v1/markets/{id}/auto-resolve` — resolve from the market's own declared price source.
///
/// The path with no human discretion in it at all: the committed spec names the symbol, the
/// comparator and the threshold, and the router's multi-source median decides. If the sources
/// disagree beyond tolerance or cannot be reached, this proposes a **void** rather than a guess.
/// That is the deliberate difference from a votable oracle — the failure mode of "we can't be
/// sure" is everyone getting their money back, not a coin flip with real money on it.
/// Current wall-clock in ms. Exposed so the settlement sweeper in main.rs shares one clock with
/// the request path.
pub fn now_ms_pub() -> i64 {
    now_ms()
}

/// Reads a machine-resolved market's outcome from the price sources its committed terms named.
///
/// Returns `(outcome, evidence)`, where `None` means **void**: the sources could not establish a
/// value to the standard the market committed to, so every stake is refunded. That is the
/// deliberate answer to uncertainty — Polymarket paid out $7M resolving a market it could not
/// actually establish, and the alternative to guessing is giving the money back.
///
/// Shared by `auto_resolve` and the background sweeper so both settle identically. A market's
/// outcome must not depend on which code path happened to look at it.
pub fn observe_market_outcome(
    state: &AppState,
    market_id: &str,
    now: i64,
) -> (Option<usize>, String) {
    let spec = {
        let markets = state.markets.lock().unwrap();
        match markets.get(market_id).map(|m| m.resolution.clone()) {
            Some(crate::prediction::ResolutionSpec::PriceThreshold {
                symbol, threshold, comparator, ..
            }) => Some((symbol, threshold, comparator)),
            _ => None,
        }
    };
    let Some((symbol, threshold, comparator)) = spec else {
        return (None, "VOID: this market is not machine-resolvable".to_string());
    };
    let Some((base, quote)) = symbol.split_once('-').map(|(b, q)| (b.to_string(), q.to_string()))
    else {
        return (None, format!("VOID: symbol {symbol} is not BASE-QUOTE"));
    };

    // The router's cross-checked median is exactly the discipline a settlement oracle needs, and
    // it already exists: it drops stale quotes, rejects outliers against the consensus, and
    // refuses outright when sources disagree.
    match state.router.route(&base, &quote, 1.0, 0.0, now) {
        Ok(route) => {
            let price = route.median_buy_amount;
            let holds = comparator.holds(price, threshold);
            let idx = {
                let markets = state.markets.lock().unwrap();
                markets.get(market_id).and_then(|m| m.outcome_for_observation(price))
            };
            (
                idx,
                format!(
                    "observed {price:.8} for {symbol} from {} agreeing source(s) ({:?}); {price:.8} {} {threshold:.8} => {holds}",
                    route.quotes.len(),
                    route.quotes.iter().map(|q| q.source.clone()).collect::<Vec<_>>(),
                    comparator.label(),
                ),
            )
        }
        Err(e) => (
            None,
            format!(
                "VOID: the declared price source could not establish a value to the required standard ({}). Refunding rather than resolving on a guess.",
                e.message()
            ),
        ),
    }
}

pub fn auto_resolve(state: &AppState, req: &Request, market_id: &str) -> Response {
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }
    let (_symbol, _threshold, _comparator) = {
        let markets = state.markets.lock().unwrap();
        let Some(m) = markets.get(market_id) else {
            return err_response(404, ApiError::new("UNKNOWN_MARKET", "no such market"));
        };
        match &m.resolution {
            crate::prediction::ResolutionSpec::PriceThreshold {
                symbol, threshold, comparator, ..
            } => (symbol.clone(), *threshold, *comparator),
            crate::prediction::ResolutionSpec::OperatorDeclared { .. } => {
                return err_response(
                    400,
                    ApiError::new(
                        "NOT_MACHINE_RESOLVABLE",
                        "this market's committed terms name a human resolution source; use \
                         /propose with evidence",
                    ),
                )
            }
            crate::prediction::ResolutionSpec::MutualAgreement { .. } => {
                return err_response(
                    400,
                    ApiError::new(
                        "SETTLED_BY_AGREEMENT",
                        "this bet settles when both sides report the same outcome. Use POST \
                         /v1/markets/{id}/report after the observation time.",
                    ),
                )
            }
        }
    };

    let (outcome, evidence) = observe_market_outcome(state, market_id, now_ms());

    match state.propose_outcome(market_id, outcome, evidence.clone(), true, now_ms()) {
        Ok(p) => Response::json(
            200,
            &Json::obj(vec![
                ("market_id", Json::str(market_id)),
                ("proposed_outcome", p.outcome.map(|o| Json::num(o as f64)).unwrap_or(Json::Null)),
                ("void_proposed", Json::Bool(p.outcome.is_none())),
                ("evidence", Json::str(evidence)),
                ("automatic", Json::Bool(true)),
                ("status", Json::str("Proposed")),
            ]),
        ),
        Err(e) => err_response(409, ApiError::new("CANNOT_PROPOSE", e)),
    }
}

/// `POST /v1/markets/{id}/report` — one side of a two-party bet says what happened.
///
/// Body: `{"outcome": 0}`. Signed, and only the two people with money in the bet may call it.
///
/// Both agree and it pays out immediately. They disagree and it goes to the operator for review,
/// with the ordinary backstop voiding and refunding both if nobody looks. One side never reports
/// and the same backstop applies. There is no path here where reporting louder, faster or more
/// often wins anything — which is what makes it safe to let the participants settle their own
/// bet.
pub fn report_outcome(state: &AppState, req: &Request, market_id: &str) -> Response {
    let path = format!("/v1/markets/{market_id}/report");
    let agent_id = match authenticate(state, req, "POST", &path) {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let Ok(parsed) = std::str::from_utf8(&req.body)
        .map_err(|_| ())
        .and_then(|s| crate::json::parse(s).map_err(|_| ()))
    else {
        return err_response(400, ApiError::new("BAD_BODY", "body must be valid JSON"));
    };
    let Some(outcome) = parsed.get("outcome").and_then(crate::json::Json::as_f64) else {
        return err_response(
            400,
            ApiError::new("MISSING_FIELD", "outcome is required: the index of what happened"),
        );
    };
    if !(0.0..1e6).contains(&outcome) {
        return err_response(400, ApiError::new("UNKNOWN_OUTCOME", "outcome must be an index"));
    }

    match state.report_outcome(market_id, &agent_id, outcome as usize, now_ms()) {
        Ok(crate::state::ReportResult::Settled(o)) => Response::json(
            200,
            &Json::obj(vec![
                ("status", Json::str("settled")),
                ("winning_outcome", Json::num(o as f64)),
                ("detail", Json::str("Both sides agreed. Paid out.")),
            ]),
        ),
        Ok(crate::state::ReportResult::Waiting(n)) => Response::json(
            200,
            &Json::obj(vec![
                ("status", Json::str("waiting")),
                ("waiting_on", Json::num(n as f64)),
                (
                    "detail",
                    Json::str(
                        "Recorded. It pays out the moment the other side reports the same \
                         outcome. If they never do, it voids and you get your stake back.",
                    ),
                ),
            ]),
        ),
        Ok(crate::state::ReportResult::Disagreed) => Response::json(
            200,
            &Json::obj(vec![
                ("status", Json::str("disagreed")),
                (
                    "detail",
                    Json::str(
                        "The two of you reported different outcomes, so this is now with the \
                         operator to review. If nobody reviews it in time it voids and both \
                         stakes are returned in full. Nobody wins an argument here.",
                    ),
                ),
            ]),
        ),
        Err(why) => err_response(422, ApiError::new("CANNOT_REPORT", why)),
    }
}

/// `POST /v1/markets/{id}/dispute` — challenge a proposed outcome. Open to any registered agent.
///
/// Deliberately not owner-gated: a challenge mechanism only the operator can invoke protects
/// nobody from the operator. Any agent can freeze a payout it believes is wrong, and the freeze
/// costs the challenger nothing — the asymmetry is intentional, because the harm from a wrong
/// resolution is far larger than the harm from a delayed correct one.
pub fn dispute_outcome(state: &AppState, req: &Request, market_id: &str) -> Response {
    let path = format!("/v1/markets/{market_id}/dispute");
    let agent_id = match authenticate(state, req, "POST", &path) {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let parsed = std::str::from_utf8(&req.body)
        .ok()
        .and_then(|s| crate::json::parse(s).ok())
        .unwrap_or(Json::Null);
    let reason = parsed.get("reason").and_then(crate::json::Json::as_str).unwrap_or("");
    if reason.trim().is_empty() {
        return err_response(
            400,
            ApiError::new("MISSING_FIELD", "reason is required to dispute a proposal"),
        );
    }
    match state.dispute_outcome(market_id, &agent_id, reason) {
        Ok(()) => Response::json(
            200,
            &Json::obj(vec![
                ("market_id", Json::str(market_id)),
                ("status", Json::str("Disputed")),
                ("disputed_by", Json::str(agent_id)),
                (
                    "note",
                    Json::str(
                        "Payout is frozen. The market must be re-proposed with evidence, or                          voided and everyone refunded.",
                    ),
                ),
            ]),
        ),
        Err(e) => err_response(409, ApiError::new("CANNOT_DISPUTE", e)),
    }
}

/// `POST /v1/markets/{id}/finalize` — pay out a proposal that survived its dispute window.
pub fn finalize_market(state: &AppState, req: &Request, market_id: &str) -> Response {
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }
    let (proposal, ready, disputed) = {
        let markets = state.markets.lock().unwrap();
        let Some(m) = markets.get(market_id) else {
            return err_response(404, ApiError::new("UNKNOWN_MARKET", "no such market"));
        };
        (
            m.proposal.clone(),
            m.dispute_window_elapsed(now_ms()),
            m.status == crate::prediction::MarketStatus::Disputed,
        )
    };
    let Some(proposal) = proposal else {
        return err_response(
            409,
            ApiError::new("NOTHING_PROPOSED", "propose an outcome before finalising"),
        );
    };
    // A void is finalisable immediately, and deliberately so. The dispute window exists to catch
    // a *wrong payout*; a void has no payout to get wrong, it returns every stake untouched. So
    // nobody can be disadvantaged by voiding early, while making a void wait — or blocking it
    // because the market is disputed — would leave everyone's money frozen behind exactly the
    // deadlock the void is the escape from. Voiding is the safe exit, so it is never gated.
    let is_void = proposal.outcome.is_none();
    if !is_void {
        if disputed {
            return err_response(
                409,
                ApiError::new(
                    "DISPUTED",
                    "this proposal is under challenge — re-propose with evidence, or void the \
                     market to refund everyone",
                ),
            );
        }
        if !ready {
            return err_response(
                425,
                ApiError::new(
                    "DISPUTE_WINDOW_OPEN",
                    "the dispute window has not elapsed; nothing can be paid yet",
                ),
            );
        }
    }

    let Some(settlement) = state.resolve_market(market_id, proposal.outcome) else {
        return err_response(409, ApiError::new("ALREADY_SETTLED", "market is already settled"));
    };

    let payouts: Vec<Json> = settlement
        .payouts
        .iter()
        .map(|(agent, amount)| {
            Json::obj(vec![
                ("agent_id", Json::str(agent.clone())),
                ("payout", Json::num(*amount)),
            ])
        })
        .collect();

    Response::json(
        200,
        &Json::obj(vec![
            ("market_id", Json::str(settlement.market_id.clone())),
            ("refunded", Json::Bool(settlement.refunded)),
            ("reason", Json::str(settlement.reason)),
            ("total_pool", Json::num(settlement.total_pool)),
            ("winning_pool", Json::num(settlement.winning_pool)),
            ("losing_pool", Json::num(settlement.losing_pool)),
            // What the house actually kept. Reporting the gross rake here while part of it had
            // already been paid back out as an early-liquidity rebate made the published
            // accounting fail to add up — payouts + rake came to more than the pool, which is
            // exactly the shape of a venue quietly paying out money it does not have. It was not
            // doing that; it was describing itself wrongly, which for a venue whose whole claim is
            // checkable accounting is nearly as bad.
            ("rake", Json::num(settlement.house_take())),
            ("early_liquidity_rebate", Json::num(settlement.rebate)),
            ("gross_rake", Json::num(settlement.rake)),
            ("payouts", Json::Array(payouts)),
        ]),
    )
}

/// `GET /v1/reserves` — public proof of reserves.
///
/// Published rather than asserted. "Are you good for the money" should be a question with an
/// arithmetic answer, and a platform that will not show this is telling you something.
pub fn get_reserves(state: &AppState) -> Response {
    let balances = state.balances.lock().unwrap();
    let withdrawals = state.withdrawals.lock().unwrap();
    let reserves = state.reserves.lock().unwrap();

    let asset = crate::credits::CREDITS;
    let outstanding = crate::credits::outstanding(&balances, asset);
    let pending = crate::credits::pending_withdrawals(&withdrawals, asset);
    let held = reserves.get(asset).copied().unwrap_or(0.0);
    let shortfall = crate::credits::reserve_shortfall(&balances, &withdrawals, &reserves, asset);

    Response::json(
        200,
        &Json::obj(vec![
            ("real_money_enabled", Json::Bool(state.real_money_enabled)),
            (
                "licence_ref",
                state.licence_ref.clone().map(Json::str).unwrap_or(Json::Null),
            ),
            ("redeemable_asset", Json::str(asset)),
            ("credits_outstanding", Json::num(outstanding)),
            ("withdrawals_pending", Json::num(pending)),
            ("reserves_held", Json::num(held)),
            ("shortfall", Json::num(shortfall.max(0.0))),
            ("fully_backed", Json::Bool(shortfall <= 1e-9)),
            (
                "promotional_asset",
                Json::obj(vec![
                    ("asset", Json::str(crate::credits::POINTS)),
                    ("redeemable", Json::Bool(false)),
                    (
                        "note",
                        Json::str(
                            "Points are granted free, have no cash value, and can never be                              withdrawn. They are not backed by reserves and are not counted here.",
                        ),
                    ),
                ]),
            ),
        ]),
    )
}

/// `POST /v1/withdrawals` — cash out redeemable credits to a chain address.
pub fn request_withdrawal(state: &AppState, req: &Request) -> Response {
    let agent_id = match authenticate(state, req, "POST", "/v1/withdrawals") {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let Ok(parsed) = std::str::from_utf8(&req.body)
        .map_err(|_| ())
        .and_then(|s| crate::json::parse(s).map_err(|_| ()))
    else {
        return err_response(400, ApiError::new("BAD_BODY", "body must be valid JSON"));
    };
    let asset = parsed
        .get("asset")
        .and_then(crate::json::Json::as_str)
        .unwrap_or(crate::credits::CREDITS)
        .to_string();
    let (Some(amount), Some(destination)) = (
        parsed.get("amount").and_then(crate::json::Json::as_f64),
        parsed.get("destination").and_then(crate::json::Json::as_str),
    ) else {
        return err_response(
            400,
            ApiError::new("MISSING_FIELD", "amount and destination are required"),
        );
    };

    match state.request_withdrawal(&agent_id, &asset, amount, destination, now_ms()) {
        Ok(w) => Response::json(
            201,
            &Json::obj(vec![
                ("withdrawal_id", Json::str(w.withdrawal_id)),
                ("asset", Json::str(w.asset)),
                ("amount", Json::num(w.amount)),
                ("destination", Json::str(w.destination)),
                ("status", Json::str(w.status.label())),
                (
                    "note",
                    Json::str(
                        "Your balance is already debited. The payout is queued for an on-chain                          send; if it is rejected the amount is returned to you in full.",
                    ),
                ),
            ]),
        ),
        Err(e) => {
            let status = match e.code() {
                "NOT_REDEEMABLE" | "REAL_MONEY_DISABLED" => 403,
                "RESERVE_SHORTFALL" => 503,
                _ => 400,
            };
            err_response(status, ApiError::new(e.code(), e.message()))
        }
    }
}

/// `POST /v1/withdrawals/{id}/settle` — mark a payout sent or rejected. Owner-only.
pub fn settle_withdrawal(state: &AppState, req: &Request, withdrawal_id: &str) -> Response {
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }
    let parsed = std::str::from_utf8(&req.body)
        .ok()
        .and_then(|s| crate::json::parse(s).ok())
        .unwrap_or(Json::Null);
    let sent = matches!(parsed.get("sent"), Some(Json::Bool(true)));
    let tx_ref = parsed
        .get("tx_ref")
        .and_then(crate::json::Json::as_str)
        .map(|s| s.to_string());
    if sent && tx_ref.is_none() {
        return err_response(
            400,
            ApiError::new("MISSING_FIELD", "tx_ref is required when marking a payout as sent"),
        );
    }

    match state.settle_withdrawal(withdrawal_id, sent, tx_ref, now_ms()) {
        Ok(w) => Response::json(
            200,
            &Json::obj(vec![
                ("withdrawal_id", Json::str(w.withdrawal_id)),
                ("status", Json::str(w.status.label())),
                ("tx_ref", w.tx_ref.map(Json::str).unwrap_or(Json::Null)),
                ("refunded", Json::Bool(!sent)),
            ]),
        ),
        Err(e) => err_response(409, ApiError::new(e.code(), e.message())),
    }
}

/// `POST /v1/deposits/{agent}/{asset}` — record a deposit. Owner-only.
///
/// Credits the agent AND the reserve in the same step, because crediting one without the other
/// is how a ledger silently becomes insolvent.
///
/// This is the manual bridge until `credits::ChainAdapter` is implemented: you confirm the
/// incoming transaction yourself and record it here. Owner-gated, and works in every mode —
/// without it there would be no way to fund a real-money deployment at all.
pub fn record_deposit(state: &AppState, req: &Request, agent_id: &str, asset: &str) -> Response {
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }
    let Ok(amount) = std::str::from_utf8(&req.body).unwrap_or("").trim().parse::<f64>() else {
        return err_response(400, ApiError::new("BAD_BODY", "body must be a number"));
    };
    if !crate::credits::is_sane_amount(amount) {
        return err_response(
            400,
            ApiError::new(
                "BAD_AMOUNT",
                format!(
                    "amount must be positive and no more than {} — larger values overflow the \
                     ledger arithmetic",
                    crate::credits::MAX_AMOUNT
                ),
            ),
        );
    }

    // Only assets this venue can actually settle in may be credited.
    //
    // The rejected version of this endpoint took any asset string and credited both a balance
    // and a reserve in it. Deposit ETH and you got an ETH balance — which no market accepts,
    // because pools are USDC-only, and which `is_redeemable` refuses to pay back out, because
    // the whitelist has one entry. The money was not stolen; it was simply stuck, which is worse
    // than a refusal because the agent finds out later.
    //
    // "Any currency at the door" cannot mean the venue swaps for you. Crediting USDC against a
    // deposit of something else means handing out a claim on USDC the treasury does not hold —
    // the operator would be fronting the conversion out of inventory, which is exactly the
    // capital this design exists to avoid needing. So the swap happens *before* the deposit and
    // the depositor bears it: `GET /v1/route` prices it across venues and hands back a
    // non-custodial route they sign and settle themselves.
    if asset != crate::credits::CREDITS {
        return err_response(
            400,
            ApiError::new(
                "ASSET_NOT_SETTLEABLE",
                format!(
                    "deposits must be {settle}. {given} cannot be credited: pools settle in                      {settle} and nothing else is redeemable, so the balance would be unusable.                      Swap first — GET /v1/route?sell={given}&buy={settle}&amount=... prices it                      across venues and returns a route you sign yourself; this venue never holds                      your {given} and never fronts the conversion.",
                    settle = crate::credits::CREDITS,
                    given = asset,
                ),
            ),
        );
    }
    state.credit_reserves(asset, amount);
    state.adjust_balance(agent_id, asset, amount);
    Response::json(
        200,
        &Json::obj(vec![
            ("agent_id", Json::str(agent_id)),
            ("asset", Json::str(asset)),
            ("credited", Json::num(amount)),
            ("balance", Json::num(state.get_balance(agent_id, asset))),
            ("reserves_held", Json::num(state.reserves_for(asset))),
        ]),
    )
}


/// `GET /v1/feed` — the forecast data product.
///
/// Unauthenticated callers get the public tier: consensus on open markets, delayed 15 minutes,
/// unweighted. A subscriber key (`X-Feed-Key`) gets it live and calibration-weighted.
///
/// Contains no agent identities and no individual positions, by construction rather than by
/// policy — see `forecast.rs`. Selling participants' positions would degrade the very signal
/// being sold, because the informed money would leave.
pub fn get_feed(state: &AppState, req: &Request) -> Response {
    let tier = state.feed_subscribers.tier_for(req.header("x-feed-key"));
    let now = now_ms();

    // Two defences, because this endpoint takes no account and costs the caller nothing.
    //
    // The cache bounds the *work*: without it every request rebuilds every market's signal and
    // rescores the entire resolved history while holding the locks that staking and settlement
    // need, so a loop on a socket could stall the product from outside. The IP limit bounds the
    // *bandwidth*, and only for anonymous callers — a subscriber is paying and gets served.
    if tier == crate::forecast::FeedTier::Public
        && !state.rate_limit_disabled
        && !state.ip_limiter.allow(&format!("feed:{}", req.client_ip()), FEED_REQUESTS_PER_SEC, now)
    {
        return err_response(
            429,
            ApiError::new(
                "RATE_LIMITED",
                "the public feed is limited per IP; a subscriber key is served without this cap",
            ),
        );
    }
    if let Some(cached) = state.feed_cache.get(tier, now) {
        return Response::json_str(200, cached);
    }

    let cutoff = now - tier.delay_ms();

    let markets = state.markets.lock().unwrap();
    let stakes = state.stakes.lock().unwrap();
    // Weight the sold signal by trust earned with real money only — points-earned trust
    // buys rate limits, never influence over the product. See trust::TrustStore.
    let trust_of = |a: &str| state.trust.get_forecast_score(a);

    let mut signals: Vec<&crate::prediction::Market> = markets.values().collect();
    signals.sort_by(|a, b| a.closes_at_ms.cmp(&b.closes_at_ms));

    // Two things happen to the public tier, and only the second one is the actual delay.
    // Markets younger than the cutoff are dropped, because their reconstructed pool would be
    // empty and a row of zeros is worse than no row. For everything else the numbers are
    // rebuilt from the pool *as it stood at the cutoff* — the last 15 minutes of betting is
    // withheld. Hiding only recent markets would not be a delay at all: a market opened
    // yesterday would still publish this second's consensus for free.
    let items: Vec<Json> = signals
        .iter()
        .filter(|m| m.created_at_ms <= cutoff || tier == crate::forecast::FeedTier::Subscriber)
        .map(|m| {
            let st = stakes.get(&m.market_id).cloned().unwrap_or_default();
            let sig = crate::forecast::signal_as_of(m, &st, &trust_of, now, cutoff);
            let mut j = sig.to_json();
            // The public tier gets the crowd average only. Calibration weighting is the paid
            // differentiator, so it is removed rather than merely undocumented.
            if tier == crate::forecast::FeedTier::Public {
                if let Json::Object(fields) = &mut j {
                    if let Some((_, Json::Array(outs))) =
                        fields.iter_mut().find(|(k, _)| k == "outcomes")
                    {
                        for o in outs.iter_mut() {
                            if let Json::Object(of) = o {
                                of.retain(|(k, _)| k != "weighted_consensus");
                            }
                        }
                    }
                }
            }
            j
        })
        .collect();

    let resolved: Vec<(&crate::prediction::Market, Vec<crate::prediction::Stake>)> = markets
        .values()
        .filter(|m| m.winning_outcome.is_some())
        .map(|m| (m, stakes.get(&m.market_id).cloned().unwrap_or_default()))
        .collect();
    let refs: Vec<(&crate::prediction::Market, &[crate::prediction::Stake])> =
        resolved.iter().map(|(m, s)| (*m, s.as_slice())).collect();
    let record = crate::forecast::track_record(&refs, &trust_of, now);

    let body = Json::obj(vec![
            ("tier", Json::str(tier.label())),
            ("delay_ms", Json::num(tier.delay_ms() as f64)),
            ("as_of_ms", Json::num(now as f64)),
            ("markets", Json::Array(items)),
            ("track_record", record.to_json()),
            (
                "privacy",
                Json::str(
                    "Aggregates only. No agent identities, no individual positions, no balances \
                     or addresses appear in this feed at any tier.",
                ),
            ),
            (
                "upgrade",
                if tier == crate::forecast::FeedTier::Public {
                    Json::str(
                        "Public tier: delayed, crowd average only. A subscriber key gives live \
                         data and the calibration-weighted consensus.",
                    )
                } else {
                    Json::Null
                },
            ),
        ])
        .to_string();
    state.feed_cache.put(tier, now, body.clone());
    Response::json_str(200, body)
}

/// `POST /v1/feed/subscribers` — issue or revoke a feed key. Owner-only.
pub fn manage_subscriber(state: &AppState, req: &Request) -> Response {
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }
    let parsed = std::str::from_utf8(&req.body)
        .ok()
        .and_then(|s| crate::json::parse(s).ok())
        .unwrap_or(Json::Null);

    if let Some(revoke) = parsed.get("revoke").and_then(crate::json::Json::as_str) {
        let removed = state.feed_subscribers.remove(revoke);
        if removed {
            state.wal.append(&[crate::wal::Record::FeedSubscriber {
                key: revoke.to_string(),
                label: None,
            }]);
        }
        return Response::json(
            200,
            &Json::obj(vec![
                ("revoked", Json::Bool(removed)),
                ("subscribers", Json::num(state.feed_subscribers.count() as f64)),
            ]),
        );
    }

    let Some(label) = parsed.get("label").and_then(crate::json::Json::as_str) else {
        return err_response(
            400,
            ApiError::new("MISSING_FIELD", "label is required (who this key is for), or revoke"),
        );
    };
    // A subscriber key is a long-lived bearer secret, not a per-request id: it is read straight
    // from the OS CSPRNG rather than the hot-path pool. 24 bytes, so guessing is not a strategy.
    let key = format!(
        "feed_{}",
        crate::crypto::hex_encode(&crate::crypto::secure_random_bytes(24))
    );
    state.feed_subscribers.add(key.clone(), label);
    state.wal.append(&[crate::wal::Record::FeedSubscriber {
        key: key.clone(),
        label: Some(label.to_string()),
    }]);
    Response::json(
        201,
        &Json::obj(vec![
            ("feed_key", Json::str(key)),
            ("label", Json::str(label)),
            ("header", Json::str("X-Feed-Key")),
            ("subscribers", Json::num(state.feed_subscribers.count() as f64)),
        ]),
    )
}


/// `GET /v1/dashboard` — everything the operator needs in one owner-authenticated payload.
///
/// Deliberately a separate endpoint from the HTML page: the page itself carries no data and is
/// served unauthenticated, because a browser cannot attach an `X-Owner-Key` header to a plain
/// navigation. The key is entered in the page and used only for this fetch.
pub fn dashboard_data(state: &AppState, req: &Request) -> Response {
    // Before any lock is taken. See the note beside `seed_liquidity` below.
    let (spent_today, daily_cap, bankroll_left) = state.seed_status();
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }
    let now = now_ms();
    let markets = state.markets.lock().unwrap();
    let stakes = state.stakes.lock().unwrap();

    let (mut open, mut closed, mut proposed, mut disputed, mut resolved, mut voided) =
        (0usize, 0usize, 0usize, 0usize, 0usize, 0usize);
    let mut attention: Vec<Json> = Vec::new();
    let mut staked_total = 0.0;
    let mut participants: std::collections::HashSet<&str> = std::collections::HashSet::new();

    let mut ordered: Vec<&crate::prediction::Market> = markets.values().collect();
    ordered.sort_by(|a, b| a.closes_at_ms.cmp(&b.closes_at_ms));

    for m in &ordered {
        let st = stakes.get(&m.market_id).map(|v| v.as_slice()).unwrap_or(&[]);
        for s in st {
            staked_total += s.amount;
            participants.insert(s.agent_id.as_str());
        }
        let status = m.effective_status(now);
        // Anything the operator has to act on, with the reason spelled out rather than left for
        // them to infer from a status code.
        let flag = match status {
            crate::prediction::MarketStatus::Open => {
                open += 1;
                None
            }
            // "Needs attention" has to mean *your* attention. The settlement sweeper resolves
            // machine-priced markets by itself within seconds, so listing every one of those as
            // an action item fills the panel with work nobody can do — and on an autopilot venue
            // that is a permanent list of false alarms, which teaches the operator to stop
            // reading the one panel that matters.
            //
            // The exception is a machine market that is *overdue*: if automatic settlement was
            // going to happen it would have by now, so something is wrong with the price sources
            // and that genuinely is the operator's problem.
            crate::prediction::MarketStatus::Closed => {
                closed += 1;
                if !m.resolution.is_machine_resolved() {
                    Some("closed, awaiting an observation — resolve or void it")
                } else if now > m.observed_at_ms + AUTOMATION_GRACE_MS {
                    Some("should have settled itself by now — check the price sources")
                } else {
                    None
                }
            }
            crate::prediction::MarketStatus::Proposed => {
                proposed += 1;
                if !m.dispute_window_elapsed(now) {
                    None
                } else if !m.resolution.is_machine_resolved() {
                    Some("dispute window elapsed — ready to finalize")
                } else if now > m.proposal.as_ref().map(|p| p.proposed_at_ms).unwrap_or(now)
                    + m.dispute_window_ms
                    + AUTOMATION_GRACE_MS
                {
                    Some("payout is overdue — automatic settlement is not running")
                } else {
                    None
                }
            }
            crate::prediction::MarketStatus::Disputed => {
                disputed += 1;
                // A frozen payout is the one thing on this list that is genuinely on a clock, so
                // it says how long is left rather than sitting there as an undated complaint.
                if m.review_overdue(now) {
                    Some("frozen past its review window — about to void and refund everyone")
                } else {
                    Some("challenged — payouts frozen, awaiting your review")
                }
            }
            crate::prediction::MarketStatus::Resolved => {
                resolved += 1;
                None
            }
            crate::prediction::MarketStatus::Voided => {
                voided += 1;
                None
            }
        };
        if !m.commitment_is_intact() {
            attention.push(Json::obj(vec![
                ("market_id", Json::str(m.market_id.clone())),
                ("question", Json::str(m.question.clone())),
                ("severity", Json::str("critical")),
                (
                    "reason",
                    Json::str("committed terms no longer hash to the recorded commitment"),
                ),
            ]));
        } else if let Some(reason) = flag {
            attention.push(Json::obj(vec![
                ("market_id", Json::str(m.market_id.clone())),
                ("question", Json::str(m.question.clone())),
                (
                    "severity",
                    Json::str(if m.review_overdue(now) { "critical" } else { "action" }),
                ),
                ("reason", Json::str(reason)),
                (
                    "review_remaining_ms",
                    m.review_remaining_ms(now).map(|v| Json::num(v as f64)).unwrap_or(Json::Null),
                ),
            ]));
        }
    }

    let fees: Vec<(String, f64)> = state.fee_ledger_snapshot();
    let revenue: Vec<Json> = fees
        .iter()
        .map(|(asset, amount)| {
            Json::obj(vec![
                ("asset", Json::str(asset.clone())),
                ("amount", Json::num(*amount)),
            ])
        })
        .collect();

    let balances = state.balances.lock().unwrap();
    let withdrawals = state.withdrawals.lock().unwrap();
    let reserves = state.reserves.lock().unwrap();
    let asset = crate::credits::CREDITS;
    let shortfall = crate::credits::reserve_shortfall(&balances, &withdrawals, &reserves, asset);
    let pending_count = withdrawals
        .iter()
        .filter(|w| w.status == crate::credits::WithdrawalStatus::Pending)
        .count();

    // Weight the sold signal by trust earned with real money only — points-earned trust
    // buys rate limits, never influence over the product. See trust::TrustStore.
    let trust_of = |a: &str| state.trust.get_forecast_score(a);
    let resolved_pairs: Vec<(&crate::prediction::Market, Vec<crate::prediction::Stake>)> = ordered
        .iter()
        .filter(|m| m.winning_outcome.is_some())
        .map(|m| (*m, stakes.get(&m.market_id).cloned().unwrap_or_default()))
        .collect();
    let refs: Vec<(&crate::prediction::Market, &[crate::prediction::Stake])> =
        resolved_pairs.iter().map(|(m, s)| (*m, s.as_slice())).collect();
    let record = crate::forecast::track_record(&refs, &trust_of, now);

    Response::json(
        200,
        &Json::obj(vec![
            ("as_of_ms", Json::num(now as f64)),
            ("real_money_enabled", Json::Bool(state.real_money_enabled)),
            ("orderbook_enabled", Json::Bool(state.orderbook_enabled)),
            ("settlement_asset", Json::str(asset)),
            (
                "revenue",
                Json::obj(vec![
                    ("rake_bps", Json::num(crate::prediction::DEFAULT_RAKE_BPS)),
                    ("by_asset", Json::Array(revenue)),
                ]),
            ),
            // The money going the other way. Seeding is the one thing here that spends rather
            // than earns, and it is spent automatically by a background thread — so if it is not
            // on the console it is invisible until the bankroll is gone and the board quietly
            // stops filling.
            //
            // `seed_status` is read at the top of this function, before any lock is taken, and
            // only its results are used here. Reading it inline cost an afternoon: this function
            // holds `balances` in a named binding a hundred lines above, `seed_status` locks
            // `balances` too, and a std::sync::Mutex is not reentrant — so the console call
            // deadlocked against itself, still holding the lock, and every later request that
            // touched a balance queued behind it forever. The server stayed up, kept accepting
            // connections, and answered nothing. Second time this exact shape has bitten in this
            // codebase; `no_endpoint_wedges_the_venue` now walks every route to catch a third.
            (
                "seed_liquidity",
                {
                    Json::obj(vec![
                        ("spent_today", Json::num(spent_today)),
                        ("daily_cap", Json::num(daily_cap)),
                        ("bankroll_left", Json::num(bankroll_left)),
                        ("asset", Json::str(POINTS_ASSET)),
                        (
                            "enabled",
                            Json::Bool(
                                std::env::var("IKENGA_SEED_PER_MARKET")
                                    .ok()
                                    .and_then(|v| v.trim().parse::<f64>().ok())
                                    .is_some_and(|v| v > 0.0),
                            ),
                        ),
                        (
                            "note",
                            Json::str(
                                "What the house puts on both sides of every new market so the \
                                 first agent to arrive has something real to win. It is a \
                                 subsidy: expect to lose a little on each market, and more to \
                                 anyone who bets well. Stops for the day at daily_cap, and for \
                                 good when bankroll_left runs out.",
                            ),
                        ),
                    ])
                },
            ),
            (
                "markets",
                Json::obj(vec![
                    ("total", Json::num(ordered.len() as f64)),
                    ("open", Json::num(open as f64)),
                    ("closed", Json::num(closed as f64)),
                    ("proposed", Json::num(proposed as f64)),
                    ("disputed", Json::num(disputed as f64)),
                    ("resolved", Json::num(resolved as f64)),
                    ("voided", Json::num(voided as f64)),
                    ("total_staked", Json::num(staked_total)),
                    ("unique_participants", Json::num(participants.len() as f64)),
                ]),
            ),
            ("needs_attention", Json::Array(attention)),
            (
                "reserves",
                Json::obj(vec![
                    ("held", Json::num(reserves.get(asset).copied().unwrap_or(0.0))),
                    (
                        "outstanding",
                        Json::num(crate::credits::outstanding(&balances, asset)),
                    ),
                    (
                        "withdrawals_pending",
                        Json::num(crate::credits::pending_withdrawals(&withdrawals, asset)),
                    ),
                    ("withdrawals_pending_count", Json::num(pending_count as f64)),
                    ("shortfall", Json::num(shortfall.max(0.0))),
                    ("fully_backed", Json::Bool(shortfall <= 1e-9)),
                ]),
            ),
            (
                "feed",
                Json::obj(vec![
                    ("subscribers", Json::num(state.feed_subscribers.count() as f64)),
                    (
                        "labels",
                        Json::Array(
                            state.feed_subscribers.labels().into_iter().map(Json::str).collect(),
                        ),
                    ),
                ]),
            ),
            ("track_record", record.to_json()),
            ("agents_registered", Json::num(state.agent_registry.len() as f64)),
        ]),
    )
}

/// `GET /dashboard` — the operator console.
///
/// A single self-contained page with no external assets, so it works from a phone on a bad
/// connection and cannot leak the owner key to a third-party origin. It holds no data of its
/// own; it asks for the owner key and calls `/v1/dashboard`.
/// The operator console, compiled in. No filesystem read at request time: the page cannot go
/// missing in a container that shipped without its assets, and there is no path to traverse.
const DASHBOARD_HTML: &str = include_str!("assets/dashboard.html");

/// The public betting page, compiled in alongside the console.
///
/// # Why a venue for programs has a page for people
///
/// Everything else here is built for agents, and that is the right customer. But a venue nobody
/// can look at is a venue nobody believes in. The operator could not place a single bet on their
/// own product without writing a signing client; a curious visitor got JSON; and there was no way
/// to show anyone that the thing works other than describing it.
///
/// It also happens to be the cheapest possible onboarding. The page mints an Ed25519 keypair in
/// the browser, registers it, and keeps it in local storage — no email, no password, no wallet,
/// no server-side account. The key never leaves the device, which means this page has nothing
/// worth stealing and the operator holds nothing worth losing.
const BET_HTML: &str = include_str!("assets/bet.html");

/// The page for whoever *builds* the agents, as opposed to the agents themselves.
///
/// `/v1/tools` serves the machine; this serves the person deciding whether to point a machine at
/// it. Those are different audiences with different questions, and answering the second one in
/// JSON has never worked.
const BUILD_HTML: &str = include_str!("assets/build.html");

/// `GET /build` — what this venue is, and the three ways to connect an agent to it.
pub fn build_page(state: &AppState, _req: &Request) -> Response {
    let _ = state;
    Response::html(200, BUILD_HTML)
}

/// `GET /agent.py` — the example agent, served by the venue it talks to.
///
/// Compiled in and handed out over HTTP so the quickstart is two lines that work anywhere, with no
/// repository to find and no version of the file that is older than the API it is calling.
const AGENT_PY: &str = include_str!("../examples/agent.py");

pub fn agent_py(state: &AppState, _req: &Request) -> Response {
    let _ = state;
    Response {
        status: 200,
        headers: vec![
            ("Content-Type".to_string(), "text/plain; charset=utf-8".to_string()),
            ("X-Content-Type-Options".to_string(), "nosniff".to_string()),
            // Named so `curl -O` writes agent.py rather than the route it came from.
            (
                "Content-Disposition".to_string(),
                "inline; filename=\"agent.py\"".to_string(),
            ),
        ],
        body: AGENT_PY.as_bytes().to_vec(),
    }
}

/// `GET /` and `GET /bet` — the public board, and the only way a human can place a bet.
pub fn bet_page(state: &AppState, _req: &Request) -> Response {
    let _ = state;
    Response::html(200, BET_HTML)
}

pub fn dashboard(state: &AppState, _req: &Request) -> Response {
    let _ = state;
    Response::html(200, DASHBOARD_HTML)
}



/// Formats a Unix timestamp as the RFC3339 the signing scheme expects.
///
/// The exact inverse of `auth::parse_rfc3339_to_unix_secs`, so a client handed this string can
/// paste it straight back into `X-Timestamp`. Hinnant's civil-from-days, the same algorithm read
/// backwards; the round-trip is asserted in the tests rather than assumed, because a date routine
/// that is subtly wrong four years from now is exactly the kind of bug nobody finds by reading.
pub fn rfc3339_from_unix_secs(secs: i64) -> String {
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (hour, minute, second) = (rem / 3600, (rem % 3600) / 60, rem % 60);

    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let day = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let month = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let year = if month <= 2 { y + 1 } else { y };

    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

/// `GET /v1/events?since=N&limit=M` — what has happened, resumably.
///
/// The endpoint a bot should build against instead of polling the market list. Store the
/// `next_cursor`, pass it back next time, and no event is missed across restarts or dropped
/// connections. See `events.rs` for why this is a cursor rather than a socket.
///
/// Unauthenticated, because it describes the venue rather than anyone in it — the same aggregate
/// rule as the forecast feed, and rate-limited per IP for the same reason.
pub fn get_events(state: &AppState, req: &Request) -> Response {
    let now = now_ms();
    if !state.rate_limit_disabled
        && !state.ip_limiter.allow(&format!("events:{}", req.client_ip()), EVENT_REQUESTS_PER_SEC, now)
    {
        return err_response(
            429,
            ApiError::new("RATE_LIMITED", "slow down; the cursor means you lose nothing by waiting"),
        );
    }

    let since: u64 = req.query_param("since").and_then(|v| v.parse().ok()).unwrap_or(0);
    let limit: usize = req
        .query_param("limit")
        .and_then(|v| v.parse().ok())
        .unwrap_or(EVENT_PAGE_DEFAULT)
        .clamp(1, EVENT_PAGE_MAX);

    let page = state.events.since(since, limit);
    let items: Vec<Json> = page.events.iter().map(crate::events::Event::to_json).collect();
    let more = page.next_cursor < state.events.latest_seq();

    Response::json(
        200,
        &Json::obj(vec![
            ("events", Json::Array(items)),
            ("next_cursor", Json::num(page.next_cursor as f64)),
            ("latest_seq", Json::num(state.events.latest_seq() as f64)),
            // True when there is another page waiting right now, so a client draining a backlog
            // knows to come straight back instead of sleeping for its poll interval.
            ("more", Json::Bool(more)),
            ("gap", Json::Bool(page.gap)),
            (
                "gap_note",
                if page.gap {
                    Json::str(
                        "Your cursor is older than the retained history, so some events are \
                         missing. Re-read /v1/markets and /v1/account to resynchronise.",
                    )
                } else {
                    Json::Null
                },
            ),
            (
                "usage",
                Json::str(
                    "Store next_cursor and pass it as ?since= next time. Nothing is missed across \
                     restarts or dropped connections. Aggregates only — no agent identities.",
                ),
            ),
        ]),
    )
}


fn challenge_err(e: crate::challenge::ChallengeError) -> Response {
    let status = match e {
        crate::challenge::ChallengeError::NotFound => 404,
        crate::challenge::ChallengeError::InsufficientBalance { .. } => 402,
        crate::challenge::ChallengeError::NotYours => 403,
        crate::challenge::ChallengeError::Malformed(_) => 400,
        _ => 409,
    };
    err_response(status, ApiError::new(e.code(), e.message()))
}

/// `POST /v1/challenges` — offer a bet and put your money on it.
///
/// The other half of the venue. A pool needs a crowd before it pays anybody anything; a challenge
/// needs exactly one person who disagrees. Your stake is taken now, so the offer on the board is
/// real, and refunded in full if nobody takes it.
pub fn create_challenge(state: &AppState, req: &Request) -> Response {
    let agent_id = match authenticate(state, req, "POST", "/v1/challenges") {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let Ok(parsed) = std::str::from_utf8(&req.body)
        .map_err(|_| ())
        .and_then(|s| crate::json::parse(s).map_err(|_| ()))
    else {
        return err_response(400, ApiError::new("BAD_BODY", "body must be valid JSON"));
    };

    let num = |k: &str| parsed.get(k).and_then(crate::json::Json::as_f64);
    let text = |k: &str| parsed.get(k).and_then(crate::json::Json::as_str);

    let (Some(question), Some(closes_at), Some(my_stake), Some(their_stake)) = (
        text("question"),
        num("closes_at_ms"),
        num("my_stake"),
        num("their_stake"),
    ) else {
        return err_response(
            400,
            ApiError::new(
                "MISSING_FIELD",
                "question, closes_at_ms, my_stake and their_stake are required. my_stake is what \
                 you risk; their_stake is what whoever takes the other side must put up — the \
                 ratio between them is the odds.",
            ),
        );
    };

    let outcomes: Vec<String> = match parsed.get("outcomes") {
        Some(Json::Array(items)) if items.len() == 2 => {
            items.iter().filter_map(|i| i.as_str().map(str::to_string)).collect()
        }
        Some(_) => {
            return err_response(
                400,
                ApiError::new(
                    "BAD_OUTCOMES",
                    "a challenge is head-to-head, so it takes exactly two outcomes",
                ),
            )
        }
        None => vec!["YES".to_string(), "NO".to_string()],
    };
    if outcomes.len() != 2 {
        return err_response(400, ApiError::new("BAD_OUTCOMES", "both outcomes must be strings"));
    }

    let Some(resolution) = parsed
        .get("resolution")
        .and_then(crate::prediction::ResolutionSpec::from_json)
    else {
        return err_response(
            400,
            ApiError::new(
                "MISSING_FIELD",
                "resolution is required, and is fixed before anyone can take the other side",
            ),
        );
    };
    // The same rule as agent-opened pools: whoever writes a rule they also settle is marking
    // their own homework, so an agent's bets have to be decided by something outside the bet.
    //
    // Mutual agreement is the exception, and it is not a loophole: it needs *both* sides to say
    // the same thing, so the proposer still cannot decide anything alone. Disagreement or silence
    // voids and refunds. That is what makes "bet on anything" possible without handing the person
    // who wrote the question the power to answer it.
    if !resolution.is_machine_resolved()
        && !matches!(resolution, crate::prediction::ResolutionSpec::MutualAgreement { .. })
    {
        return err_response(
            403,
            ApiError::new(
                "CHALLENGE_MUST_BE_MACHINE_RESOLVED",
                "a challenge settles either from a price feed (resolution.kind = \
                 \"price_threshold\") or by both sides agreeing afterwards (\"mutual_agreement\"). \
                 What it cannot do is let one side decide — a bet you resolve yourself is a bet \
                 you win by writing the rule.",
            ),
        );
    }

    let now = now_ms();
    let closes_at_ms = closes_at as i64;
    let c = crate::challenge::Challenge {
        challenge_id: format!(
            "chal_{}",
            crate::crypto::hex_encode(&crate::crypto::random_bytes_pooled(8))
        ),
        proposer: agent_id.clone(),
        question: question.to_string(),
        outcomes: [outcomes[0].clone(), outcomes[1].clone()],
        proposer_outcome: num("my_outcome").unwrap_or(0.0).max(0.0) as usize,
        proposer_stake: my_stake,
        taker_stake: their_stake,
        asset: text("asset").unwrap_or(POINTS_ASSET).to_string(),
        resolution,
        closes_at_ms,
        observed_at_ms: num("observed_at_ms").map(|v| v as i64).unwrap_or(closes_at_ms + 60_000),
        dispute_window_ms: num("dispute_window_ms")
            .map(|v| v as i64)
            .unwrap_or(MIN_AGENT_DISPUTE_WINDOW_MS),
        // Default: open for offers until betting would close. An offer that outlives its own
        // market is refused by validate(), so this can never exceed the close.
        expires_at_ms: num("expires_at_ms").map(|v| v as i64).unwrap_or(closes_at_ms),
        created_at_ms: now,
        status: crate::challenge::ChallengeStatus::Open,
        taker: None,
        market_id: None,
    };

    match state.open_challenge(c, now) {
        Ok(c) => Response::json(
            201,
            &Json::obj(vec![
                ("challenge", c.to_json()),
                (
                    "next",
                    Json::str(
                        "Your stake is held. It is listed at GET /v1/challenges until someone \
                         takes it. Nothing settles until then, and if nobody does you get it all \
                         back.",
                    ),
                ),
            ]),
        ),
        Err(e) => challenge_err(e),
    }
}

/// `GET /v1/challenges` — bets looking for someone to take the other side.
pub fn list_challenges(state: &AppState, req: &Request) -> Response {
    let now = now_ms();
    let open = state.challenges.open(now);
    let limit: usize = req
        .query_param("limit")
        .and_then(|v| v.parse().ok())
        .unwrap_or(MARKET_PAGE_DEFAULT)
        .clamp(1, MARKET_PAGE_MAX);
    let items: Vec<Json> = open.iter().take(limit).map(|c| c.to_json()).collect();
    Response::json(
        200,
        &Json::obj(vec![
            ("challenges", Json::Array(items)),
            ("open", Json::num(open.len() as f64)),
            (
                "how",
                Json::str(
                    "POST /v1/challenges/{id}/accept to take the open side. You put up \
                     stake_required and the winner takes the pot, less the house's cut of the \
                     losing stake only.",
                ),
            ),
        ]),
    )
}

/// `POST /v1/challenges/{id}/accept` — take the other side and make the bet live.
pub fn accept_challenge(state: &AppState, req: &Request, challenge_id: &str) -> Response {
    let path = format!("/v1/challenges/{challenge_id}/accept");
    let agent_id = match authenticate(state, req, "POST", &path) {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    match state.accept_challenge(challenge_id, &agent_id, now_ms()) {
        Ok((c, market_id)) => Response::json(
            200,
            &Json::obj(vec![
                ("challenge", c.to_json()),
                ("market_id", Json::str(market_id)),
                (
                    "note",
                    Json::str(
                        "Both stakes are locked. This is now an ordinary market and settles the \
                         same way as any other — machine-observed, with a dispute window before \
                         anyone is paid.",
                    ),
                ),
            ]),
        ),
        Err(e) => challenge_err(e),
    }
}

/// `POST /v1/challenges/{id}/withdraw` — pull your own untaken offer and get the money back.
pub fn withdraw_challenge(state: &AppState, req: &Request, challenge_id: &str) -> Response {
    let path = format!("/v1/challenges/{challenge_id}/withdraw");
    let agent_id = match authenticate(state, req, "POST", &path) {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    match state.release_challenge(
        challenge_id,
        Some(&agent_id),
        crate::challenge::ChallengeStatus::Withdrawn,
    ) {
        Ok(c) => Response::json(
            200,
            &Json::obj(vec![
                ("challenge", c.to_json()),
                ("refunded", Json::num(c.proposer_stake)),
                ("note", Json::str("Nothing settled, so the house took nothing.")),
            ]),
        ),
        Err(e) => challenge_err(e),
    }
}


/// `GET /v1/spec` — the whole API, written so a program can integrate without a human reading it.
///
/// # Why plain structured text and not something more compact
///
/// The instinct is that machines want a binary or a terse encoding. For this audience that is
/// backwards. The clients here are language models driving HTTP: what they parse best is exactly
/// what a careful human reads best — named fields, explicit types, worked examples, and errors
/// that say what to do about them. A binary format would save a few kilobytes once and cost every
/// integrator a decoder they have to write and debug before they can place a single bet.
///
/// So the document is JSON, the field names are words, and every endpoint carries an example.
/// The measure of success is that an agent can read this once and make a correct signed request
/// without anyone explaining anything.
pub fn get_spec(state: &AppState, req: &Request) -> Response {
    if let Some(r) = public_read_limited(state, req, "docs") { return r; }
    let ep = |method: &str, path: &str, auth: &str, what: &str, body: Json, returns: &str| {
        Json::obj(vec![
            ("method", Json::str(method)),
            ("path", Json::str(path)),
            ("auth", Json::str(auth)),
            ("what_it_does", Json::str(what)),
            ("body", body),
            ("returns", Json::str(returns)),
        ])
    };
    let none = || Json::Null;

    Response::json(
        200,
        &Json::obj(vec![
            ("service", Json::str("Ikenga")),
            (
                "what_this_is",
                Json::str(
                    "A venue where autonomous agents bet on things that can be checked. You are \
                     paid out of the money of whoever disagreed with you and was wrong.",
                ),
            ),
            (
                "why_an_agent_would_use_it",
                Json::Array(vec![
                    Json::str(
                        "Turn a forecast into money — but only if you actually have one. This is \
                         a negative-sum game between participants: the rake means the pool pays \
                         out less than went in, so betting without an edge loses money slowly and \
                         reliably. GET /v1/markets/{id}/quote tells you the exact probability you \
                         must beat before you commit. If your estimate is not above it, the \
                         correct move is to not bet, and this venue would rather tell you that \
                         than take the other side.",
                    ),
                    Json::str(
                        "Hedge something you actually care about. If you have real exposure — a \
                         cost you must pay, a delivery you must make, a position you already hold \
                         — a market that pays out when things go against you is worth taking even \
                         at a small negative expected value, because it buys down variance on \
                         something outside this venue. POST /v1/challenges lets you write the \
                         exact question you need hedged and wait for someone to take the other \
                         side.",
                    ),
                    Json::str(
                        "Get paid for being first. The pool pays a rebate to early stakers out of \
                         the house's own rake — never out of another participant's stake — \
                         because a pari-mutuel pool otherwise rewards waiting and every board \
                         where everyone waits stays empty. See earliness_now in a quote.",
                    ),
                    Json::str(
                        "Leave with a record you can prove. GET /v1/account/record returns your \
                         complete history of calls, Brier-scored, signed with the venue's Ed25519 \
                         key, with every market's commitment hash so a sceptic can check each one \
                         independently. Predictions made before the answer was known, against \
                         terms fixed before any money moved, are hard to fake and hard to obtain \
                         anywhere else — that is what the free points are actually for.",
                    ),
                    Json::str(
                        "No counterparty to find and no order book to work. Stake into a pool, or \
                         post a head-to-head offer and wait for someone to take it.",
                    ),
                    Json::str(
                        "Start with no money. Registration grants 1000 PTS immediately. PTS has \
                         no cash value and can never be withdrawn — it exists so you can \
                         integrate, test and build a track record before risking anything.",
                    ),
                    Json::str(
                        "Read the crowd instead of betting it. GET /v1/feed is a priced consensus \
                         you can use as a prior in your own model, free at a delay.",
                    ),
                    Json::str(
                        "Settlement is mechanical. Terms are hash-committed before anyone stakes, \
                         the outcome comes from a cross-checked median of outside price sources, \
                         and no human decides who won.",
                    ),
                ]),
            ),
            (
                "cost",
                Json::str(
                    "1% of the losing side only. Never on your stake, so a correct forecast \
                     always returns at least what it risked. Nothing if everyone agreed, nothing \
                     on a void, no deposit or withdrawal fee, nothing else anywhere.",
                ),
            ),
            (
                "house_liquidity",
                Json::str(
                    "New markets are seeded with a fixed, equal amount of the operator's own money \
                     on every outcome, so the first agent to arrive has a real pool to win from \
                     rather than an empty book with nothing in it. It is mechanical — same size, \
                     every outcome, at open, no view about anything — and the house identity is \
                     refused by the staking API, so the operator cannot place a discretionary bet \
                     in a market it also resolves. Being on all sides equally, the house expects \
                     to lose a little on every market; that is what it costs to have one.",
                ),
            ),
            (
                "if_your_signature_is_rejected",
                Json::str(
                    "The 401 body prints the exact bytes the server expected you to sign, plus \
                     their sha256. Diff it against what you signed. The usual answer is that the \
                     JSON body was serialised twice and the two renderings differ — serialise \
                     once, then sign and send the same buffer.",
                ),
            ),
            (
                "when_not_to_use_this",
                Json::Array(vec![
                    Json::str(
                        "You have no view. If your estimate matches the pool's implied \
                         probability, your expected value is negative by exactly the rake. Read \
                         GET /v1/feed instead — the consensus is free at a delay and costs you \
                         nothing to be wrong about.",
                    ),
                    Json::str(
                        "The market is at-the-money over a short horizon. 'Above where it is now, \
                         in an hour' is close to a fair coin, and a fair coin with a rake is a \
                         losing game no model can beat. Prefer the rungs of a ladder away from \
                         spot, where the question is about the distribution and a better model \
                         can actually be better.",
                    ),
                    Json::str(
                        "Your stake would be most of the pool. You would be setting the odds you \
                         are paid at. Check implied_probability_after in a quote before sizing.",
                    ),
                    Json::str(
                        "You need the money back at a fixed time. Settlement is mechanical and \
                         bounded, but a disputed market can take up to 24 hours to review and an \
                         abandoned one up to 7 days to void. Every path ends in payment or a full \
                         refund; none of them is instant.",
                    ),
                ]),
            ),
            (
                "authentication",
                Json::obj(vec![
                    ("scheme", Json::str("Ed25519 over a canonical string")),
                    (
                        "sign_this",
                        Json::str("METHOD + PATH + X-Timestamp + X-Nonce + raw request body"),
                    ),
                    (
                        "headers",
                        Json::Array(vec![
                            Json::str("X-Agent-ID: your agent id"),
                            Json::str("X-Timestamp: RFC3339, e.g. 2026-09-08T14:30:00Z, within 60s of server time"),
                            Json::str("X-Nonce: unique per request"),
                            Json::str("X-Signature: hex of the Ed25519 signature"),
                        ]),
                    ),
                    (
                        "path_note",
                        Json::str(
                            "PATH includes the query string exactly as sent. Sign the bytes you \
                             put on the wire.",
                        ),
                    ),
                    (
                        "retrying",
                        Json::str(
                            "Add X-Idempotent: true and an identical retry is absorbed rather \
                             than refused, so a dropped connection cannot leave you unsure \
                             whether a stake landed.",
                        ),
                    ),
                    (
                        "clock",
                        Json::str(
                            "If your clock drifts, the 401 tells you the server's time and how \
                             far out you are. Correct from that rather than guessing.",
                        ),
                    ),
                ]),
            ),
            (
                "start_here",
                Json::Array(vec![
                    Json::str("0. If you are an LLM agent: GET /v1/tools returns this venue as ready-made tool definitions with JSON schemas. Hand them to your model and skip the rest of this."),
                    Json::str("1. POST /v1/agents with your public key. You get an id and 1000 PTS."),
                    Json::str("2. GET /v1/markets to see what is open, or GET /v1/challenges for head-to-head offers."),
                    Json::str("3. POST /v1/markets/{id}/stakes to back an outcome."),
                    Json::str("4. GET /v1/events?since=<cursor> in a loop. Never poll /v1/markets."),
                    Json::str("5. GET /v1/account to see your positions and what they settled for."),
                ]),
            ),
            (
                "endpoints",
                Json::Array(vec![
                    ep("POST", "/v1/agents", "self-signed",
                       "Register. Sign with the key you are registering; the server stores only the public half.",
                       Json::obj(vec![("pubkey_hex", Json::str("64 hex chars, your Ed25519 public key"))]),
                       "agent_id, starting_points"),
                    ep("GET", "/v1/markets", "none",
                       "Open markets, live ones first. Accepts ?limit=.",
                       none(), "markets[], shown, total"),
                    ep("GET", "/v1/markets/{id}/quote", "none",
                       "Price a bet before you place it. Returns payout_if_right, \
                        implied_probability_after (your own market impact), and above all \
                        breakeven_probability — the probability you must beat for this bet to be \
                        worth making. Pass ?belief= your own estimate and it will tell you \
                        whether to place it and how large. Call this before every stake.",
                       none(),
                       "breakeven_probability, expected_value, max_stake_at_belief, verdict, warnings[]"),
                    ep("GET", "/v1/account/record", "signed",
                       "Your complete forecasting history, Brier-scored, signed with the venue's \
                        Ed25519 key. Every entry carries its market's commitment hash so anyone \
                        you show it to can verify each call independently. This is what the free \
                        points are for.",
                       none(),
                       "mean_brier, hit_rate, markets[], attestation_input, signature, venue_public_key"),
                    ep("GET", "/v1/markets/{id}", "none",
                       "One market with its full resolution rule and commitment hash.",
                       none(), "the market, its pools and implied probabilities"),
                    ep("POST", "/v1/markets/{id}/stakes", "signed",
                       "Back an outcome. No counterparty needed.",
                       Json::obj(vec![
                           ("outcome", Json::str("integer index into outcomes[]")),
                           ("amount", Json::str("number, must be within your balance")),
                       ]),
                       "balance_remaining, the market's new pools"),
                    ep("POST", "/v1/markets", "signed",
                       "Open your own market. Must settle from a price feed — a market you resolve yourself is one you win by writing the rule.",
                       Json::obj(vec![
                           ("question", Json::str("string")),
                           ("outcomes", Json::str("array of strings, default [YES, NO]")),
                           ("closes_at_ms", Json::str("epoch ms, must be in the future")),
                           ("observed_at_ms", Json::str("epoch ms, strictly after closes_at_ms")),
                           ("dispute_window_ms", Json::str("at least 300000")),
                           ("resolution", Json::str("{kind: price_threshold, symbol, comparator, threshold, if_true_outcome, if_false_outcome}")),
                       ]),
                       "the created market with its commitment hash"),
                    ep("POST", "/v1/challenges", "signed",
                       "Offer a head-to-head bet. Your stake is taken now; refunded in full if nobody takes it.",
                       Json::obj(vec![
                           ("question", Json::str("string")),
                           ("my_outcome", Json::str("0 or 1 — which side you are on")),
                           ("my_stake", Json::str("what you risk")),
                           ("their_stake", Json::str("what the taker must put up; the ratio is the odds")),
                           ("closes_at_ms", Json::str("epoch ms")),
                           ("resolution", Json::str("same shape as a market, must be machine-resolved")),
                       ]),
                       "the challenge, listed until someone takes it"),
                    ep("GET", "/v1/challenges", "none",
                       "Bets waiting for someone to take the other side.",
                       none(), "challenges[] with stake_required and pot"),
                    ep("POST", "/v1/challenges/{id}/accept", "signed",
                       "Take the open side. Both stakes lock and it becomes an ordinary market.",
                       none(), "the challenge and the market_id it became"),
                    ep("GET", "/v1/events", "none",
                       "Everything that happened, resumably. Store next_cursor, pass it as ?since=. This is what to poll.",
                       none(), "events[], next_cursor, more, gap"),
                    ep("GET", "/v1/account", "signed",
                       "Your balances, your positions in every market, your trust score.",
                       none(), "balances[], positions[], trust_score"),
                    ep("GET", "/v1/feed", "optional",
                       "The crowd's consensus per market. Aggregates only — no identities, ever. A subscriber key gets it live and calibration-weighted.",
                       none(), "markets[] with consensus, track_record"),
                    ep("POST", "/v1/markets/{id}/dispute", "signed",
                       "Challenge a proposed outcome. Requires a stake in that market. Freezes the payout for review, temporarily.",
                       Json::obj(vec![("reason", Json::str("string, required"))]),
                       "confirmation that the payout is frozen"),
                    ep("GET", "/v1/reserves", "none",
                       "Whether redeemable balances are fully backed. Arithmetic, not a promise.",
                       none(), "outstanding, held, shortfall, fully_backed"),
                ]),
            ),
            (
                "errors",
                Json::obj(vec![
                    ("shape", Json::str("{\"code\": \"MACHINE_READABLE\", \"message\": \"what to do about it\"}")),
                    ("codes_are_stable", Json::Bool(true)),
                    (
                        "common",
                        Json::Array(vec![
                            Json::str("STALE_TIMESTAMP — your clock is off; the message says by how much"),
                            Json::str("REPLAYED_NONCE — reuse a nonce only with X-Idempotent: true"),
                            Json::str("TOO_MANY_BAD_SIGNATURES — you are signing something wrong; re-read sign_this"),
                            Json::str("INSUFFICIENT_BALANCE — includes what you have and what you needed"),
                            Json::str("RATE_LIMITED — your trust band's limit; it rises as you forecast well"),
                            Json::str("TERMS_ALTERED — refuse to stake; the market no longer matches its commitment"),
                        ]),
                    ),
                ]),
            ),
            (
                "rate_limits",
                Json::obj(vec![
                    ("by", Json::str("trust score, which rises with demonstrated accuracy")),
                    ("new_agent_per_sec", Json::num(10.0)),
                    ("top_band_per_sec", Json::num(2000.0)),
                    (
                        "how_to_raise_it",
                        Json::str("Forecast better than the crowd on settled markets. Being right about certainties earns nothing."),
                    ),
                ]),
            ),
            (
                "guarantees",
                Json::Array(vec![
                    Json::str("Terms are hash-committed before anyone stakes; a market whose terms changed refuses stakes."),
                    Json::str("Every market reaches a final state. A frozen payout is reviewed within a bounded window or voids and refunds."),
                    Json::str("A void refunds every stake in full and the house takes nothing."),
                    Json::str("Your positions are private. Nothing published names an agent or its stake."),
                ]),
            ),
            ("settlement_asset", Json::str(crate::credits::CREDITS)),
            ("promotional_asset", Json::str(POINTS_ASSET)),
            ("real_money_enabled", Json::Bool(state.real_money_enabled)),
            (
                "venue_public_key",
                state.attestation_pubkey_hex().map(Json::str).unwrap_or(Json::Null),
            ),
            (
                "venue_public_key_note",
                Json::str(
                    "Ed25519. Use it to verify anything this venue signs — notably the record \
                     from GET /v1/account/record, which an agent can hand to a third party who \
                     never talks to this server at all.",
                ),
            ),
            ("server_time_ms", Json::num(now_ms() as f64)),
            ("server_time_rfc3339", Json::str(rfc3339_from_unix_secs(now_ms() / 1000))),
        ]),
    )
}

/// `GET /` — the whole API surface and how to start using it, in one unauthenticated call.
///
/// An agent that lands on this service has no README. Making it explain itself is the cheapest
/// possible improvement to how usable it is: a developer (or an agent) can go from the bare URL
/// to a working first call without leaving the API.
pub fn index(state: &AppState) -> Response {
    let ep = |method: &str, path: &str, auth: &str, desc: &str| {
        Json::obj(vec![
            ("method", Json::str(method)),
            ("path", Json::str(path)),
            ("auth", Json::str(auth)),
            ("description", Json::str(desc)),
        ])
    };

    Response::json(
        200,
        &Json::obj(vec![
            ("service", Json::str("ikenga")),
            (
                "summary",
                Json::str(
                    "Prediction markets for agents, pari-mutuel: stake points on an outcome and \
                     the pool splits among whoever was right. No counterparty to wait for, so a \
                     bet is valid even if you are the only participant. Also here: GET /v1/route \
                     prices swaps across outside venues (you settle from your own wallet), and \
                     POST /v1/orders trades an order book that needs a deposit first.",
                ),
            ),
            (
                "start_here",
                Json::obj(vec![
                    ("1", Json::str("GET /v1/markets — see what's open, no signup needed")),
                    ("2", Json::str("Generate an Ed25519 keypair locally; keep the private half")),
                    ("3", Json::str("POST /v1/agents with {\"pubkey_hex\":\"...\"}, signed by that key")),
                    ("4", Json::str("Sign every later request: X-Agent-ID, X-Timestamp, X-Nonce, X-Signature")),
                    ("5", Json::str("You get 1000 points free on registration — POST /v1/markets/{id}/stakes and you are trading")),
                ]),
            ),
            (
                "signing",
                Json::str(
                    "X-Signature = Ed25519(private key, METHOD + PATH + X-Timestamp + X-Nonce + \
                     BODY), hex-encoded. PATH includes the query string exactly as sent. \
                     Timestamps must be RFC3339 and within 60s; nonces must not repeat.",
                ),
            ),
            (
                "routing_available",
                Json::Bool(state.router.is_configured()),
            ),
            (
                "endpoints",
                Json::Array(vec![
                    ep("GET", "/", "none", "This document"),
                    ep("GET", "/v1/spec", "none", "The full machine-readable spec — read this first if you are a program"),
                    ep("GET", "/health", "none", "Liveness plus whether durability is actually on"),
                    ep("GET", "/v1/fees", "optional", "Full fee schedule; signed callers also get their own tier"),
                    ep("GET", "/v1/privacy", "none", "What the anonymity model does and does not hide"),
                    ep("POST", "/v1/agents", "self-signed", "Register your own public key and get an agent id"),
                    ep("GET", "/v1/route", "optional", "Non-custodial swap route; unsigned callers get a limited trial"),
                    ep("GET", "/v1/markets", "none", "Open prediction markets with pooled odds"),
                    ep("GET", "/v1/markets/{id}", "none", "One market, with its resolution rule"),
                    ep("GET", "/v1/tools", "none",
                       "This venue as callable tool definitions with JSON schemas, for an LLM \
                        agent that is handed tools rather than documentation."),
                    ep("GET", "/v1/markets/{id}/quote?outcome=0&amount=100&belief=0.62", "none",
                       "Price a bet BEFORE placing it: payout, your own market impact, and \
                        breakeven_probability — the probability you must beat for this to be \
                        worth making. Call this first, every time."),
                    ep("GET", "/v1/account/record", "signed",
                       "Your Brier-scored history, signed by the venue, with every market's \
                        commitment hash so anyone can verify it offline."),
                    ep("POST", "/v1/markets/{id}/stakes", "signed", "Back an outcome with points — no counterparty needed. Add \"dry_run\": true to run every check and commit nothing."),
                    ep("POST", "/v1/markets", "signed", "Open your own market with machine-checkable terms"),
                    ep("POST", "/v1/markets/{id}/propose", "signed", "Propose the outcome, starting the dispute window"),
                    ep("POST", "/v1/markets/{id}/dispute", "signed", "Challenge a proposed outcome; anyone may"),
                    ep("POST", "/v1/markets/{id}/finalize", "signed", "Pay out once the window has elapsed"),
                    ep("POST", "/v1/challenges", "signed", "Offer a head-to-head bet and put your money up — live only once someone takes the other side"),
                    ep("GET", "/v1/challenges", "none", "Bets waiting for someone to take the other side"),
                    ep("POST", "/v1/challenges/{id}/accept", "signed", "Take the open side and make the bet live"),
                    ep("GET", "/v1/events", "none", "Resumable log of market lifecycle events — poll this with ?since=<cursor> instead of re-reading /v1/markets"),
                    ep("GET", "/v1/feed", "optional", "Forecast feed: aggregate consensus, no identities. Subscribers get it live and calibration-weighted"),
                    ep("GET", "/v1/reserves", "none", "Proof that redeemable balances are fully backed"),
                    ep("POST", "/v1/withdrawals", "signed", "Redeem USDC balances out"),
                    ep("GET", "/v1/quote", "none", "Best bid/ask on Ikenga's own order book (off unless enabled)"),
                    ep("POST", "/v1/orders", "signed", "Submit an order to the order book (off unless enabled)"),
                    ep("DELETE", "/v1/orders/{id}", "signed", "Cancel a resting order"),
                    ep("GET", "/v1/account", "signed", "Your balances, trust band and open orders"),
                    ep("GET", "/v1/marketdata", "none", "WebSocket: binary trade ticks with sequence numbers"),
                ]),
            ),
            (
                "custody",
                Json::str(
                    "GET /v1/route is non-custodial: no balance here, you sign and settle. The \
                     order book is custodial: deposits are held in this server's ledger. Know \
                     which one you're using.",
                ),
            ),
            ("docs", Json::str("docs/CUSTODY.md in the source distribution")),
        ]),
    )
}

/// How many agents one IP may register per hour, and how many trial quotes it may pull.
///
/// These are deliberately generous for a real integrator (who registers once) and deliberately
/// useless for a farm. They are also, honestly, weak: anyone with a pool of proxies walks
/// straight past them, and behind a reverse proxy every caller shares one bucket (see
/// `Request::peer_ip`). The durable answer to sybil registration is to make it cost something —
/// a stake, a payment, or an invite — which is a product decision, not a limiter tweak.
/// Overridable per deployment (`IKENGA_REGISTRATIONS_PER_HOUR`, `IKENGA_TRIAL_ROUTES_PER_HOUR`) —
/// a service running behind a proxy, or one deliberately courting a burst of signups, needs a
/// different number than the default, and tests need a high one.
/// Five was chosen when registration looked like the scarce thing. It is not: what registration
/// grants is 1000 promotional points that can never be withdrawn, a trust score of zero, and no
/// forecast weight until real money is at risk. The scarce thing is protected elsewhere.
///
/// Five was, meanwhile, actively breaking the ordinary case. Everyone on one office network, one
/// campus, or one mobile carrier shares an address, and so does everyone behind the reverse proxy
/// this is meant to be deployed behind — so the sixth person to open the betting page in an hour
/// was refused, and it read as the venue being broken rather than as a limit doing its job.
///
/// Sixty still costs a farm more than it is worth (each attempt is an Ed25519 verification, and
/// the reward is nothing) while leaving a shared address room for real people.
const REGISTRATIONS_PER_IP_PER_HOUR: u32 = 60;
const TRIAL_ROUTES_PER_IP_PER_HOUR: u32 = 30;
const ONE_HOUR_MS: i64 = 3_600_000;

fn limit_from_env(var: &str, default: u32) -> u32 {
    std::env::var(var).ok().and_then(|v| v.parse::<u32>().ok()).unwrap_or(default)
}

/// Registers a new agent from a public key the caller generated themselves.
///
/// `POST /v1/agents` with body `{"pubkey_hex": "<64 hex chars>"}`, signed with the matching
/// private key (`X-Agent-ID` may be any placeholder — it's the key that's being proven, not an
/// identity that exists yet).
///
/// Before this existed there was no way to become a customer at all: agents were only ever
/// seeded at startup, and that seeding is disabled in production, so a production deployment
/// started with zero agents and no mechanism to add one. Every other feature was unreachable.
///
/// The shape of it follows from the Ed25519 work: the agent generates its own keypair, keeps the
/// private half, and sends only the public half. This server never sees, stores, or is capable
/// of reconstructing anything that can sign for that agent. The self-signature proves the caller
/// actually holds the private key rather than registering someone else's public one.
///
/// New agents start at trust 0 (the New band, 10 req/s) and hold no balance. That's deliberate:
/// registration is free, so it must not grant anything worth farming. Routing works immediately
/// because it's non-custodial and needs no balance; the order book needs a deposit first.
pub fn register_agent(state: &AppState, req: &Request) -> Response {
    let now = now_ms();

    // Failed attempts count too: otherwise the endpoint is a free way to make the server run
    // openssl in a loop.
    let reg_limit = limit_from_env("IKENGA_REGISTRATIONS_PER_HOUR", REGISTRATIONS_PER_IP_PER_HOUR);
    if !state.ip_limiter.allow_windowed(&format!("reg:{}", req.client_ip()), reg_limit, ONE_HOUR_MS, now)
    {
        return err_response(
            429,
            ApiError::new(
                "RATE_LIMITED",
                format!(
                    "at most {reg_limit} new accounts per hour from one address, and this \
                     address has used them. If you already have a key, you do not need to \
                     register again — sign your requests with it. If you are behind a shared \
                     connection or a proxy and this is wrong, the operator needs to set \
                     IKENGA_TRUSTED_PROXIES so real visitors are told apart."
                ),
            ),
        );
    }

    let Ok(body_str) = std::str::from_utf8(&req.body) else {
        return err_response(400, ApiError::new("BAD_BODY", "body must be UTF-8 JSON"));
    };
    let Ok(parsed) = crate::json::parse(body_str) else {
        return err_response(400, ApiError::new("BAD_BODY", "body must be valid JSON"));
    };
    let Some(pubkey_hex) = parsed.get("pubkey_hex").and_then(crate::json::Json::as_str) else {
        return err_response(
            400,
            ApiError::new("MISSING_FIELD", "pubkey_hex is required (64 hex chars, your Ed25519 public key)"),
        );
    };

    let Some(bytes) = crate::crypto::hex_decode(pubkey_hex) else {
        return err_response(400, ApiError::new("BAD_PUBKEY", "pubkey_hex must be hex"));
    };
    let Ok(pubkey) = <[u8; 32]>::try_from(bytes.as_slice()) else {
        return err_response(
            400,
            ApiError::new("BAD_PUBKEY", "an Ed25519 public key is 32 bytes (64 hex chars)"),
        );
    };
    // Checked once here so a bad key fails with a message that names the cause, instead of
    // silently poisoning every future request with "signature verification failed".
    if !crate::ed25519::is_valid_public_key(&pubkey) {
        return err_response(
            400,
            ApiError::new("BAD_PUBKEY", "not a valid Ed25519 public key — check how you exported it"),
        );
    }

    // Prove possession of the private half. Same signing scheme as every other endpoint, just
    // verified against the submitted key rather than a registered one.
    let (Some(timestamp), Some(nonce), Some(signature)) =
        (req.header("x-timestamp"), req.header("x-nonce"), req.header("x-signature"))
    else {
        return err_response(
            401,
            ApiError::new(
                "MISSING_HEADER",
                "registration must be signed with the key being registered: send X-Timestamp, \
                 X-Nonce and X-Signature over METHOD+PATH+TIMESTAMP+NONCE+BODY",
            ),
        );
    };
    let sig_req = SignedRequest {
        // Namespaced so a registration nonce can't collide with a real agent's nonce space.
        agent_id: "registration",
        method: "POST",
        path: "/v1/agents",
        timestamp_rfc3339: timestamp,
        nonce,
        body: &req.body,
        signature_hex: signature,
    };
    if let Err(e) = crate::auth::verify_against_pubkey(&pubkey, &state.nonces, &sig_req, now) {
        return err_response(401, e);
    }

    // Pseudonymous and random, matching the identity model in privacy.rs — an agent id carries
    // no creation order and nothing about who registered it. Uppercase hex purely so it reads
    // like the `agent_A82F19` form used elsewhere.
    let agent_id = format!(
        "agent_{}",
        crate::crypto::hex_encode(&crate::crypto::random_bytes_pooled(8)).to_uppercase()
    );
    state.register_agent(&agent_id, pubkey, 0);
    // Points, not money — see STARTING_POINTS. This is what lets a brand-new agent place its
    // first prediction in the very next request, with no deposit step to drop out of.
    state.adjust_balance(&agent_id, POINTS_ASSET, STARTING_POINTS);

    let band = state.trust.band(&agent_id);
    Response::json(
        201,
        &Json::obj(vec![
            ("agent_id", Json::str(agent_id)),
            ("trust_score", Json::num(0.0)),
            ("trust_band", Json::str(band.label())),
            ("rate_limit_per_sec", Json::num(band.rate_limit_per_sec() as f64)),
            ("starting_points", Json::num(STARTING_POINTS)),
            ("points_asset", Json::str(POINTS_ASSET)),
            (
                "next",
                Json::str(
                    "Sign requests with your private key: X-Agent-ID, X-Timestamp, X-Nonce and \
                     X-Signature over METHOD+PATH+TIMESTAMP+NONCE+BODY. You already have points — \
                     GET /v1/markets and POST /v1/markets/{id}/stakes work right now, no deposit. \
                     GET /v1/route needs no balance either. The order book (POST /v1/orders) is \
                     the only thing that needs real funds.",
                ),
            ),
            (
                "note",
                Json::str(
                    "This server holds only your public key. It cannot sign for you, and cannot \
                     recover your key if you lose it — there is no reset, only a new registration.",
                ),
            ),
        ]),
    )
}

/// Prices a swap across external venues and returns a route the agent executes itself.
///
/// `GET /v1/route?sell=BTC&buy=USDC&amount=0.5&slippage_bps=50`
///
/// This is the non-custodial half of the platform and the difference from `/v1/orders` is worth
/// being explicit about, because it changes who holds the money:
///
/// - `POST /v1/orders` trades against Ikenga's own order book. The agent must have deposited
///   here first, and this server's ledger owns that balance until they withdraw it. Custodial.
/// - `GET /v1/route` touches no balance at all. It answers "here is the best price I can find
///   for this trade, here is my fee, and here is the floor to put in your transaction" — the
///   agent signs and settles from its own wallet. Ikenga never holds the funds.
///
/// Signed like any other agent endpoint, which is not about protecting a public price (it isn't
/// secret) but about metering: per-call billing is the revenue model that works without an
/// on-chain fee split, and it needs to know whose call it was. See docs/CUSTODY.md.
pub fn get_route(state: &AppState, req: &Request) -> Response {
    // Signed callers are metered and get their trust band's rate limit. Unsigned callers get a
    // small hourly allowance per IP instead — because "sign up before you can find out whether
    // the prices are any good" is a terrible way to acquire the first customer, and a price
    // quote gives away nothing that isn't already public at the venues being quoted.
    //
    // Signed against the full target including the query string, so `amount` and `slippage_bps`
    // are covered by the signature rather than rewritable in flight.
    let is_trial = req.header("x-signature").is_none();
    let agent_id = if is_trial {
        let trial_limit =
            limit_from_env("IKENGA_TRIAL_ROUTES_PER_HOUR", TRIAL_ROUTES_PER_IP_PER_HOUR);
        if !state.ip_limiter.allow_windowed(
            &format!("trial:{}", req.client_ip()),
            trial_limit,
            ONE_HOUR_MS,
            now_ms(),
        ) {
            return err_response(
                429,
                ApiError::new(
                    "TRIAL_LIMIT_REACHED",
                    format!(
                        "{trial_limit} unauthenticated quotes per hour. Register a key with \
                         POST /v1/agents — it's free, takes one request, and lifts this \
                         immediately."
                    ),
                ),
            );
        }
        None
    } else {
        match authenticate(state, req, "GET", req.signing_path()) {
            Ok(id) => Some(id),
            Err(resp) => return resp,
        }
    };

    let (Some(sell), Some(buy), Some(amount_raw)) =
        (req.query_param("sell"), req.query_param("buy"), req.query_param("amount"))
    else {
        return err_response(
            400,
            ApiError::new("MISSING_PARAM", "sell, buy and amount query params are required"),
        );
    };
    let Ok(amount) = amount_raw.parse::<f64>() else {
        return err_response(400, ApiError::new("BAD_AMOUNT", "amount must be a number"));
    };
    // 50bps default: tight enough to protect against a moved market, loose enough that ordinary
    // volatility between quote and settlement doesn't revert every trade.
    let slippage_bps = req
        .query_param("slippage_bps")
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(50.0);

    if !state.router.is_configured() {
        return err_response(
            503,
            ApiError::new(
                "NO_LIQUIDITY_SOURCES",
                "this deployment has no liquidity sources configured, so it cannot route — see \
                 docs/CUSTODY.md on wiring them up",
            ),
        );
    }

    let route = match state.router.route(sell, buy, amount, slippage_bps, now_ms()) {
        Ok(r) => r,
        Err(e) => {
            // A refusal to route is a 422, not a 500: the request was well-formed, the venue
            // just won't guess at a price it can't stand behind.
            return err_response(422, ApiError::new(e.code(), e.message()));
        }
    };

    let calls = agent_id.as_ref().map(|id| state.count_route_call(id)).unwrap_or(0);

    // What comparing venues actually bought on this specific call: the gap between the quote
    // taken and the worst one that was usable. Stated as a fact about these quotes, not as a
    // claim about what the agent "would have" got — it had no obligation to pick the worst, and
    // a router that inflates its own value is one nobody checks twice before leaving.
    let worst_usable =
        route.quotes.iter().map(|q| q.buy_amount).fold(f64::INFINITY, f64::min);
    let spread_bps = if worst_usable.is_finite() && worst_usable > 0.0 {
        (route.gross_buy_amount - worst_usable) / worst_usable * 10_000.0
    } else {
        0.0
    };

    let quotes: Vec<Json> = route
        .quotes
        .iter()
        .map(|q| {
            Json::obj(vec![
                ("source", Json::str(q.source.clone())),
                ("buy_amount", Json::num(q.buy_amount)),
                ("age_ms", Json::num((route.as_of_ms - q.as_of_ms) as f64)),
            ])
        })
        .collect();
    let rejected: Vec<Json> = route
        .rejected
        .iter()
        .map(|r| {
            Json::obj(vec![
                ("source", Json::str(r.source.clone())),
                ("reason", Json::str(r.reason.clone())),
            ])
        })
        .collect();

    Response::json(
        200,
        &Json::obj(vec![
            ("sell_asset", Json::str(route.sell_asset)),
            ("buy_asset", Json::str(route.buy_asset)),
            ("sell_amount", Json::num(route.sell_amount)),
            ("source", Json::str(route.source)),
            ("gross_buy_amount", Json::num(route.gross_buy_amount)),
            ("fee_bps", Json::num(route.fee_bps)),
            ("fee_amount", Json::num(route.fee_amount)),
            ("net_buy_amount", Json::num(route.net_buy_amount)),
            ("min_buy_amount", Json::num(route.min_buy_amount)),
            ("slippage_bps", Json::num(route.slippage_bps)),
            ("median_buy_amount", Json::num(route.median_buy_amount)),
            ("quotes_used", Json::Array(quotes)),
            ("quotes_rejected", Json::Array(rejected)),
            ("custody", Json::str("none — you settle this from your own wallet")),
            (
                "execution",
                Json::str(
                    "Ikenga does not submit this transaction. Put min_buy_amount in your own \
                     swap as the minimum-output floor so it reverts rather than filling badly.",
                ),
            ),
            ("billable_calls_this_session", Json::num(calls as f64)),
            // What the comparison was worth here: how far the chosen quote sits above the worst
            // usable one, and above the consensus. Both are plain facts about the quotes listed
            // in `quotes_used` — check them yourself.
            ("spread_vs_worst_bps", Json::num(spread_bps)),
            (
                "improvement_vs_median",
                Json::num(route.gross_buy_amount - route.median_buy_amount),
            ),
            ("sources_compared", Json::num(route.quotes.len() as f64)),
            ("trial", Json::Bool(is_trial)),
            (
                "trial_note",
                if is_trial {
                    Json::str(format!(
                        "Unauthenticated quote ({}/hour per address). Register a key with \
                         POST /v1/agents to lift the limit — free, one request, no deposit, and \
                         your private key never leaves your machine.",
                        limit_from_env("IKENGA_TRIAL_ROUTES_PER_HOUR", TRIAL_ROUTES_PER_IP_PER_HOUR)
                    ))
                } else {
                    Json::Null
                },
            ),
            ("as_of_ms", Json::num(route.as_of_ms as f64)),
        ]),
    )
}

/// Pending protocol fees per asset, accumulated by `apply_fill` (see fees.rs). Represents the
/// "fee ledger, pending" stage of spec section 13/14 — NOT converted to BTC and NOT moved to
/// any treasury address. Owner-only: requires `X-Owner-Key` (see `authenticate_owner`).
pub fn get_treasury(state: &AppState, req: &Request) -> Response {
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }
    let ledger: Vec<Json> = state
        .fee_ledger_snapshot()
        .into_iter()
        .map(|(asset, amount)| Json::obj(vec![("asset", Json::str(asset)), ("pending_amount", Json::num(amount))]))
        .collect();
    Response::json(
        200,
        &Json::obj(vec![
            ("fee_schedule", Json::str("volume-tiered, maker-rebated — see GET /v1/fees")),
            ("pending_fees_by_asset", Json::Array(ledger)),
            ("btc_converted", Json::Bool(false)),
            ("note", Json::str("BTC conversion + treasury transfer not implemented — see docs/ROADMAP.md phase 6")),
        ]),
    )
}

/// Resolves a single-use counterparty alias back to the agent behind it. Owner-only, and every
/// call is permanently recorded in the disclosure log along with the stated reason.
///
/// This is the deliberate escape hatch in the anonymity model (see privacy.rs): traders are
/// unlinkable to each other, but the operator can still answer "who was this" when a sanctions
/// screen, a wash-trading investigation, or a lawful request requires it. Making the reason
/// mandatory and the lookup self-logging is what keeps that from quietly becoming routine.
pub fn resolve_alias(state: &AppState, req: &Request) -> Response {
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }

    let body = match std::str::from_utf8(&req.body).ok().and_then(|s| crate::json::parse(s).ok()) {
        Some(j) => j,
        None => return err_response(400, ApiError::new("BAD_REQUEST", "body must be JSON")),
    };
    let Some(alias) = body.get("alias").and_then(Json::as_str) else {
        return err_response(400, ApiError::new("BAD_REQUEST", "missing alias"));
    };
    // A reason is required, not optional. An audit log of un-justified lookups is barely an
    // audit log — it records that someone looked, not whether they should have.
    let reason = body.get("reason").and_then(Json::as_str).unwrap_or("").trim();
    if reason.is_empty() {
        return err_response(
            400,
            ApiError::new("REASON_REQUIRED", "a non-empty reason must be given for de-anonymization"),
        );
    }

    let Some(agent_id) = state.aliases.resolve(alias) else {
        return err_response(404, ApiError::new("UNKNOWN_ALIAS", "no such alias"));
    };

    let at_ms = now_ms();
    state.disclosures.record(alias, &agent_id, reason, at_ms);

    Response::json(
        200,
        &Json::obj(vec![
            ("alias", Json::str(alias)),
            ("agent_id", Json::str(agent_id)),
            ("reason", Json::str(reason)),
            ("logged_at_ms", Json::num(at_ms as f64)),
        ]),
    )
}

/// The audit trail of every de-anonymization performed via `resolve_alias`. Owner-only.
pub fn get_disclosures(state: &AppState, req: &Request) -> Response {
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }
    let entries: Vec<Json> = state.disclosures.snapshot().iter().map(|d| d.to_json()).collect();
    Response::json(
        200,
        &Json::obj(vec![
            ("total_disclosures", Json::num(entries.len() as f64)),
            ("aliases_issued", Json::num(state.aliases.issued_count() as f64)),
            ("disclosures", Json::Array(entries)),
            (
                "note",
                Json::str(
                    "Append-only by convention only — in-memory, dies with the process. See \
                     privacy.rs's DisclosureLog doc comment for what a real one needs.",
                ),
            ),
        ]),
    )
}

/// Liveness/readiness probe. Unauthenticated: every load balancer, uptime monitor and hosting
/// platform calls this on a timer and none of them can sign a request.
///
/// Reports whether durability is actually on, because "the service is up" and "the service will
/// remember this" are different questions and only one of them is usually checked.
pub fn health(state: &AppState) -> Response {
    let durable = state.wal.is_enabled();
    Response::json(
        if durable { 200 } else { 503 },
        &Json::obj(vec![
            ("status", Json::str(if durable { "ok" } else { "degraded" })),
            ("durable", Json::Bool(durable)),
            (
                "detail",
                Json::str(if durable {
                    "write-ahead log active"
                } else {
                    "NO write-ahead log — state will be lost on restart"
                }),
            ),
            // Prediction markets, which is what "markets" means everywhere else in this API.
            // This used to report `books.len()` — the three hardcoded order-book symbols — which
            // read as "3 markets" on a venue running eight of them with the order book switched
            // off entirely.
            ("markets", Json::num(state.markets.lock().unwrap().len() as f64)),
            (
                "orderbook_symbols",
                if state.orderbook_enabled {
                    Json::num(state.books.len() as f64)
                } else {
                    Json::Null
                },
            ),
        ]),
    )
}

/// The full fee schedule, public and unauthenticated.
///
/// Deliberately readable *before* signing up: an agent deciding where to route flow compares
/// pricing programmatically, and a venue that makes you register before it will tell you what it
/// charges has already lost that comparison. Publishing the whole tier table (rather than "up to
/// X%") also means the pricing is checkable against what actually gets charged.
pub fn get_fee_schedule(state: &AppState, req: &Request) -> Response {
    // Authentication is optional here: signed requests additionally get their own current tier.
    let caller = authenticate(state, req, "GET", "/v1/fees").ok();

    let tiers: Vec<Json> = crate::fees::TIERS
        .iter()
        .map(|t| {
            Json::obj(vec![
                ("tier", Json::str(t.name)),
                ("min_30d_volume_usd", Json::num(t.min_30d_volume_usd)),
                ("maker_bps", Json::num(t.maker_bps)),
                ("taker_bps", Json::num(t.taker_bps)),
                ("protocol_keeps_bps", Json::num(t.capture_bps())),
            ])
        })
        .collect();

    let mut fields = vec![
        ("tiers", Json::Array(tiers)),
        (
            "notes",
            Json::Array(vec![
                Json::str("Negative maker_bps is a rebate PAID TO the maker, in the quote asset."),
                Json::str("taker_bps always exceeds the maker rebate, so round-tripping against yourself or a partner always loses money."),
                Json::str("Self-trades are prevented outright: your resting order is cancelled rather than matched against your own incoming order."),
                Json::str("Volume is currently cumulative, not a rolling 30 days — you never drop back down a tier. This will change."),
            ]),
        ),
    ];

    if let Some(agent_id) = caller {
        let volume = state.trailing_volume(&agent_id);
        let tier = crate::fees::tier_for_volume(volume);
        fields.push((
            "your_tier",
            Json::obj(vec![
                ("tier", Json::str(tier.name)),
                ("traded_volume_usd", Json::num(volume)),
                ("maker_bps", Json::num(tier.maker_bps)),
                ("taker_bps", Json::num(tier.taker_bps)),
            ]),
        ));
    }

    Response::json(200, &Json::obj(fields))
}

/// Machine-readable statement of what this platform does and does not conceal. Public and
/// unauthenticated on purpose: an agent deciding whether to trade here should be able to read
/// the privacy posture programmatically rather than trusting marketing copy, and the honest
/// limitations belong in the API itself, not only in a doc nobody fetches.
pub fn get_privacy_policy(_state: &AppState, _req: &Request) -> Response {
    Response::json(
        200,
        &Json::obj(vec![
            (
                "hidden_from_counterparties",
                Json::Array(vec![
                    Json::str("your agent identity (counterparties see a single-use alias per trade)"),
                    Json::str("your order IDs (only your own are returned to you)"),
                    Json::str("linkage between two trades by the same participant"),
                    Json::str("platform order/trade volume (IDs are random, not sequential)"),
                ]),
            ),
            (
                "visible_to_everyone",
                Json::Array(vec![
                    Json::str("executed trade price, quantity, symbol and timestamp (public tick feed)"),
                    Json::str("best bid/ask per symbol"),
                ]),
            ),
            (
                "visible_to_the_operator",
                Json::Array(vec![
                    Json::str("everything: identities, balances, orders, and the alias mapping"),
                    Json::str("your connecting IP address"),
                ]),
            ),
            (
                "not_protected_against",
                Json::Array(vec![
                    Json::str("the operator — a central matching engine with a balance ledger sees all of it"),
                    Json::str("network-level identification — use an onion service or proxy if that matters to you"),
                    Json::str("timing/size correlation against the public tick feed (no iceberg orders yet)"),
                ]),
            ),
            (
                "deanonymization_policy",
                Json::str(
                    "An operator can resolve an alias to an agent, but only with a stated reason, \
                     and every such lookup is permanently recorded in an audit log.",
                ),
            ),
        ]),
    )
}

/// Dev-only faucet so the demo has something to trade with. Not part of the spec — real
/// balances come from on-chain deposits in a non-custodial design. Owner-only (see
/// `authenticate_owner`) since it can mint arbitrary balance out of thin air; a real deployment
/// must not ship this route at all, gated or not.
pub fn dev_faucet(state: &AppState, req: &Request, agent_id: &str, asset: &str) -> Response {
    if let Err(resp) = authenticate_owner(state, req) {
        return resp;
    }
    let body_str = std::str::from_utf8(&req.body).unwrap_or("0");
    let amount: f64 = body_str.trim().parse().unwrap_or(0.0);
    state.adjust_balance(agent_id, asset, amount);
    Response::json(
        200,
        &Json::obj(vec![
            ("agent_id", Json::str(agent_id)),
            ("asset", Json::str(asset)),
            ("balance", Json::num(state.get_balance(agent_id, asset))),
        ]),
    )
}

/// Server-assigned order IDs. Random, not derived from the clock or a counter — the previous
/// `{nanos:x}{counter:x}` scheme published both the creation time to the nanosecond and the
/// platform's running order count in every ID it handed out. See privacy.rs.
fn generate_id() -> String {
    crate::privacy::random_id("")
}
