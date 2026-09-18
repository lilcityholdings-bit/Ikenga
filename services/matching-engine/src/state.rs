use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::SyncSender;
use std::sync::{Mutex, MutexGuard};

use crate::auth::{AgentRegistry, NonceCache};
use crate::credits::Withdrawal;
use crate::orderbook::OrderBook;
use crate::trust::{RateLimiter, TrustStore};
use crate::types::Order;

pub struct AppState {
    /// Fixed set of markets created at startup — read-only after `new()`, so no Mutex needed
    /// around the map itself, only around each book (which does mutate on every order).
    pub books: HashMap<String, Mutex<OrderBook>>,
    /// (agent_id, asset) -> balance. Durable via the write-ahead log (see wal.rs): every delta
    /// is appended before it's acknowledged, and replayed on startup.
    pub balances: Mutex<HashMap<(String, String), f64>>,
    /// order_id -> which symbol/book it lives in (needed for cancel lookups without scanning
    /// every book).
    pub order_symbol: Mutex<HashMap<String, String>>,
    pub orders: Mutex<HashMap<String, Order>>,
    pub agent_registry: AgentRegistry,
    pub nonces: NonceCache,
    pub trust: TrustStore,
    pub rate_limiter: RateLimiter,
    /// Rate limiting for endpoints with no agent to attribute a call to — registration and trial
    /// quotes. Keyed by `<purpose>:<ip>` so the two budgets stay separate. See the constants in
    /// api.rs for the limits and their honest weaknesses.
    pub ip_limiter: RateLimiter,
    /// market_id -> running total staked on each outcome.
    ///
    /// # Why a cache exists here at all
    ///
    /// The pools were recomputed from the stake list on every read: every quote, every board
    /// listing, every market page. That is O(number of stakes) per request, so a market got
    /// slower the more it was used — measured, a market with tens of thousands of stakes served
    /// 683 quotes a second where a fresh one served 20,900. Thirty times slower, and worsening,
    /// on exactly the markets that matter most. Popularity was a denial of service.
    ///
    /// This is a running total instead, updated wherever a stake is added. Settlement deliberately
    /// still works from the stake list, which stays the source of truth — a cache that decided
    /// payouts would be a cache that could pay the wrong person. The worst a drift here could do
    /// is display the wrong odds, and `pools_match_the_stakes` asserts it does not drift.
    pool_cache: Mutex<HashMap<String, Vec<f64>>>,
    /// market_id -> (agent_id -> the outcome they say happened), for bets settled by agreement.
    ///
    /// In memory only. A restart loses reports that have not yet resolved a market, and the two
    /// sides simply report again; if neither does before the window closes the market voids and
    /// both are refunded. That is a safe way to lose data — nobody is paid wrongly — which is why
    /// it does not justify a new WAL record and the migration that comes with one.
    outcome_reports: Mutex<HashMap<String, HashMap<String, usize>>>,
    /// (day number since epoch, amount seeded that day). Bounds how fast the house's subsidy can
    /// leave, independently of how much of it there is.
    seed_spent_today: Mutex<(i64, f64)>,
    /// So the "cap reached" line is logged once a day rather than on every autopilot cycle.
    seed_cap_reported: std::sync::atomic::AtomicBool,
    /// One sender per connected WebSocket client; each has its own thread pulling from the
    /// paired receiver and writing frames to that socket (see ws.rs / main.rs). Broadcasting a
    /// tick = send to every sender, dropping ones whose receiver has gone away.
    /// Live market-data subscribers.
    ///
    /// A **bounded** channel, deliberately. An unbounded one meant a client that connected and
    /// then stopped reading was an unbounded memory leak: its socket buffer fills, the writer
    /// thread blocks, nothing drains the queue, and `send` on an unbounded channel never fails —
    /// so the dead-subscriber prune below never fired either. One slow reader could grow the
    /// server without limit.
    ///
    /// Bounded, a subscriber too far behind is dropped instead of buffered, which is also the
    /// right answer for live prices: frames queued behind a stalled client are stale by the time
    /// they would arrive, so keeping them costs memory to deliver something worthless.
    pub tick_subscribers: Mutex<Vec<SyncSender<Vec<u8>>>>,
    pub symbol_ids: HashMap<String, u16>,
    /// Single-use public aliases standing in for real agent IDs in anything a counterparty can
    /// see, plus the audit trail of every operator lookup behind one. See privacy.rs for the
    /// threat model — in particular, for what this does *not* hide.
    pub aliases: crate::privacy::AliasRegistry,
    pub disclosures: crate::privacy::DisclosureLog,
    /// Accumulated protocol fees per asset, pending conversion to BTC and transfer to the
    /// treasury (spec section 14) — neither of which is implemented here (see fees.rs doc
    /// comment). Exposed read-only via GET /v1/treasury, gated behind the owner key (see
    /// `owner_key` below) — a single shared secret, not a real multi-admin credential/role
    /// system, which the spec's Owner Control Center actually calls for. Fine for one operator
    /// running this locally; still a gap before anything resembling production.
    pub fee_ledger: Mutex<HashMap<String, f64>>,
    /// agent_id -> cumulative traded notional in quote units, driving their fee tier.
    volume_usd: Mutex<HashMap<String, f64>>,
    /// Shared secret gating owner-only endpoints (`/v1/treasury`, `/dev/faucet`). Checked via
    /// the `X-Owner-Key` header (hex-encoded) against this, in constant time. Comes from the
    /// `IKENGA_OWNER_KEY` env var (hex) if set, otherwise a fresh random key is generated at
    /// startup and printed once — see main.rs.
    owner_key: Vec<u8>,
    /// Monotonic counter stamped on every broadcast WS tick, so a client can detect a gap in
    /// the stream (see ws.rs / sdk/ts's marketData.ts). This is NOT the spec's "delta orderbook
    /// + checksum" — it's the minimal mechanism that makes gap detection possible at all for
    /// the flat trade-tick stream this build actually has.
    tick_seq: AtomicU64,
    /// Load-testing escape hatch, set by `IKENGA_DISABLE_RATE_LIMIT=1`. Without it a benchmark
    /// measures the rate limiter rejecting requests rather than the engine's actual capacity —
    /// the first end-to-end run of `bench_http_end_to_end` got 1950 of 2000 requests 429'd and
    /// the "throughput" number was meaningless. main.rs prints a loud warning when it's on; it
    /// must never be set in a real deployment.
    pub rate_limit_disabled: bool,
    /// Append-only durability log. Every state change goes here before the caller is told it
    /// happened, and `restore_from_wal` replays it at boot. See wal.rs.
    pub wal: crate::wal::Wal,
    /// Non-custodial swap routing (see router.rs). Separate from `books` on purpose: the order
    /// book is a custodial venue holding agents' balances, while this holds nothing and merely
    /// prices trades the agent settles from its own wallet. Empty unless sources are configured
    /// at startup, in which case `GET /v1/route` reports that plainly rather than inventing a
    /// price.
    pub router: crate::router::Router,
    /// Pari-mutuel prediction markets (see prediction.rs) and the stakes placed in each.
    /// Separate from `books` because they settle on an outcome rather than by matching, which is
    /// exactly why they work with one participant and the order book does not.
    pub markets: Mutex<HashMap<String, crate::prediction::Market>>,
    pub stakes: Mutex<HashMap<String, Vec<crate::prediction::Stake>>>,
    /// Whether redeemable, deposit-backed value is enabled (`IKENGA_REAL_MONEY=1`). Off by
    /// default, and off means the only stakeable asset is promotional points.
    pub real_money_enabled: bool,
    /// Optional free-text reference the operator can publish on `GET /` and `GET /v1/reserves`.
    /// Purely informational — nothing depends on it.
    pub licence_ref: Option<String>,
    /// Deposits recorded as received on-chain, per asset. The reserve side of the ledger:
    /// outstanding credits must never exceed this.
    pub reserves: Mutex<HashMap<String, f64>>,
    /// Withdrawal requests, by id. Balance is debited at request time so the same credits
    /// cannot be spent while a payout is pending.
    pub withdrawals: Mutex<Vec<Withdrawal>>,
    /// Paying subscribers to the forecast feed. Separate from agent identity on purpose:
    /// buying the data does not require participating, and participating does not expose you to
    /// the buyers.
    pub feed_subscribers: crate::forecast::FeedSubscribers,
    /// Rendered feed responses, so an unauthenticated caller cannot make the service rebuild
    /// them on every request. See `forecast::FeedCache`.
    pub feed_cache: crate::forecast::FeedCache,
    /// What just happened, for clients that are programs. See `events.rs`.
    pub events: crate::events::EventLog,
    /// Answers to signed requests, so a dropped connection can be retried safely. See
    /// `auth::IdempotencyCache`.
    pub idempotency: crate::auth::IdempotencyCache,
    /// Head-to-head offers waiting for someone to take the other side. See `challenge.rs`.
    pub challenges: crate::challenge::ChallengeBook,
    /// Whether the liquidity-dependent order book is served at all. Off by default — see
    /// `docs/MARKETS.md`; nothing else in the product needs a counterparty or capital.
    pub orderbook_enabled: bool,
    /// How many routes have been quoted per agent. This is the metering behind the
    /// per-call billing model — the only way a router earns without an on-chain fee split. Not
    /// durable yet (see the routing section of docs/CUSTODY.md).
    pub route_calls: Mutex<HashMap<String, u64>>,
}

/// The identity the venue's own seed liquidity is booked against.
///
/// A real agent id so that every existing mechanism — balances, the WAL, settlement, the reserve
/// invariant — applies to it unchanged, and so its position is as visible as anyone else's. It is
/// permanently barred from the staking API; see `seed_market`.
/// What happened when one side of a mutually-settled bet reported.
#[derive(Debug, Clone, PartialEq)]
pub enum ReportResult {
    /// Both sides said the same thing; it paid out.
    Settled(usize),
    /// Still waiting on this many other participants.
    Waiting(usize),
    /// The two sides said different things. Now the operator's problem, and a void if they
    /// never look.
    Disagreed,
}

pub const HOUSE_AGENT: &str = "agent_house";

/// The bankroll the operator configured, used to derive a sane default daily cap.
fn bankroll_default() -> f64 {
    std::env::var("IKENGA_SEED_BANKROLL")
        .ok()
        .and_then(|v| v.trim().parse().ok())
        .filter(|v: &f64| v.is_finite() && *v > 0.0)
        .unwrap_or(10_000.0)
}

impl AppState {
    pub fn new() -> Self {
        let mut books = HashMap::new();
        let mut symbol_ids = HashMap::new();
        for (i, symbol) in ["BTC-USD", "ETH-USD", "SOL-USD"].iter().enumerate() {
            books.insert(symbol.to_string(), Mutex::new(OrderBook::new(*symbol)));
            symbol_ids.insert(symbol.to_string(), (i + 1) as u16);
        }
        Self {
            books,
            balances: Mutex::new(HashMap::new()),
            order_symbol: Mutex::new(HashMap::new()),
            orders: Mutex::new(HashMap::new()),
            agent_registry: AgentRegistry::default(),
            nonces: NonceCache::default(),
            trust: TrustStore::default(),
            rate_limiter: RateLimiter::default(),
            ip_limiter: RateLimiter::default(),
            pool_cache: Mutex::new(HashMap::new()),
            outcome_reports: Mutex::new(HashMap::new()),
            seed_spent_today: Mutex::new((0, 0.0)),
            seed_cap_reported: std::sync::atomic::AtomicBool::new(false),
            tick_subscribers: Mutex::new(Vec::new()),
            symbol_ids,
            aliases: crate::privacy::AliasRegistry::default(),
            disclosures: crate::privacy::DisclosureLog::default(),
            fee_ledger: Mutex::new(HashMap::new()),
            volume_usd: Mutex::new(HashMap::new()),
            owner_key: std::env::var("IKENGA_OWNER_KEY")
                .ok()
                .and_then(|s| crate::crypto::hex_decode(&s))
                .filter(|b| !b.is_empty())
                .unwrap_or_else(|| crate::crypto::secure_random_bytes(32)),
            tick_seq: AtomicU64::new(0),
            rate_limit_disabled: std::env::var("IKENGA_DISABLE_RATE_LIMIT").as_deref() == Ok("1"),
            wal: crate::wal::Wal::disabled(),
            router: crate::router::Router::new(crate::router::RouterConfig::from_env()),
            markets: Mutex::new(HashMap::new()),
            stakes: Mutex::new(HashMap::new()),
            real_money_enabled: std::env::var("IKENGA_REAL_MONEY").as_deref() == Ok("1"),
            licence_ref: std::env::var("IKENGA_LICENCE_REF")
                .ok()
                .filter(|v| !v.trim().is_empty()),
            feed_subscribers: crate::forecast::FeedSubscribers::default(),
            feed_cache: crate::forecast::FeedCache::default(),
            events: crate::events::EventLog::default(),
            idempotency: crate::auth::IdempotencyCache::default(),
            challenges: crate::challenge::ChallengeBook::default(),
            orderbook_enabled: std::env::var("IKENGA_ENABLE_ORDERBOOK").as_deref() == Ok("1"),
            reserves: Mutex::new(HashMap::new()),
            withdrawals: Mutex::new(Vec::new()),
            route_calls: Mutex::new(HashMap::new()),
        }
    }

    /// Records that an agent was quoted a route, and returns their running total. Billing reads
    /// this; nothing enforces a charge yet.
    pub fn count_route_call(&self, agent_id: &str) -> u64 {
        let mut calls = self.route_calls.lock().unwrap();
        let entry = calls.entry(agent_id.to_string()).or_insert(0);
        *entry += 1;
        *entry
    }

    /// Same as `new()`, but durable: state changes are appended to `wal_path` and anything
    /// already in that file is replayed first.
    pub fn with_wal(wal_path: Option<&std::path::Path>) -> std::io::Result<Self> {
        let policy = crate::wal::SyncPolicy::from_env();
        let mut state = AppState::new();
        state.wal = crate::wal::Wal::open(wal_path, policy)?;

        if let Some(path) = wal_path {
            let (records, skipped) = crate::wal::Wal::replay(path)?;
            if skipped > 0 {
                eprintln!(
                    "WAL: {skipped} unreadable record(s) skipped — expected at most one, from a \
                     write interrupted by the crash you're recovering from. More than one means \
                     corruption worth investigating."
                );
            }
            let applied = records.len();
            state.apply_records(records);
            if applied > 0 {
                println!("WAL: recovered {applied} records from {}", path.display());
            }
        }
        Ok(state)
    }

    /// Rebuilds state from a replayed log. Pure application of recorded facts — no matching, no
    /// fee calculation, no ID generation. See wal.rs on why replay must not re-execute anything.
    fn apply_records(&mut self, records: Vec<crate::wal::Record>) {
        use crate::wal::Record;
        use crate::types::{Order, OrderStatus, OrderType, Side};

        for record in records {
            match record {
                Record::Agent { agent_id, pubkey_hex, trust } => {
                    if let Some(bytes) = crate::crypto::hex_decode(&pubkey_hex) {
                        if let Ok(pubkey) = <[u8; 32]>::try_from(bytes.as_slice()) {
                            self.agent_registry.register(&agent_id, pubkey);
                            self.trust.set_score(&agent_id, trust);
                        }
                    }
                }
                Record::Balance { agent_id, asset, delta } => {
                    *self.balances.lock().unwrap().entry((agent_id, asset)).or_insert(0.0) += delta;
                }
                Record::OrderRested {
                    order_id, agent_id, symbol, side, price_ticks, qty, filled_qty, created_at_ms,
                } => {
                    let Some(side) = Side::parse(&side) else { continue };
                    let order = Order {
                        order_id: order_id.clone(),
                        agent_id,
                        symbol: symbol.clone(),
                        side,
                        order_type: OrderType::Limit,
                        price_ticks,
                        qty,
                        filled_qty,
                        status: OrderStatus::Open,
                        created_at_ms,
                    };
                    // Replaying through `submit` in log order reconstructs price-time priority
                    // exactly, and can't cross anything: the log only records orders that came
                    // to rest, so by definition nothing already on the book matched them.
                    if let Some(book) = self.books.get(&symbol) {
                        book.lock().unwrap().submit(order.clone(), created_at_ms);
                    }
                    self.order_symbol.lock().unwrap().insert(order_id.clone(), symbol);
                    self.orders.lock().unwrap().insert(order_id, order);
                }
                Record::OrderUpdated { order_id, filled_qty, status } => {
                    let mut orders = self.orders.lock().unwrap();
                    if let Some(o) = orders.get_mut(&order_id) {
                        o.filled_qty = filled_qty;
                        o.status = match status.as_str() {
                            "Filled" => OrderStatus::Filled,
                            "PartiallyFilled" => OrderStatus::PartiallyFilled,
                            "Cancelled" => OrderStatus::Cancelled,
                            _ => OrderStatus::Open,
                        };
                    }
                    drop(orders);
                    // An order that is no longer live must also leave the book, or replay would
                    // resurrect filled and cancelled orders as tradeable liquidity.
                    if matches!(status.as_str(), "Filled" | "Cancelled") {
                        let symbol = self.order_symbol.lock().unwrap().get(&order_id).cloned();
                        if let Some(symbol) = symbol {
                            if let Some(book) = self.books.get(&symbol) {
                                book.lock().unwrap().cancel(&order_id);
                            }
                        }
                    }
                }
                Record::Fee { asset, amount } => {
                    *self.fee_ledger.lock().unwrap().entry(asset).or_insert(0.0) += amount;
                }
                Record::Volume { agent_id, notional } => {
                    *self.volume_usd.lock().unwrap().entry(agent_id).or_insert(0.0) += notional;
                }
                Record::MarketOpened {
                    market_id, question, outcomes, resolution, asset, closes_at_ms,
                    observed_at_ms, dispute_window_ms, commitment, created_at_ms,
                } => {
                    // A market whose resolution spec can't be read back is worse than a missing
                    // market: it would come back resolvable on terms nobody agreed to. Skip it
                    // and let the stakes below refund rather than settle on a guess.
                    let Some(resolution) = crate::prediction::ResolutionSpec::from_json(&resolution)
                    else {
                        eprintln!("WAL: market {market_id} has an unreadable resolution spec — skipped");
                        continue;
                    };
                    self.markets.lock().unwrap().insert(
                        market_id.clone(),
                        crate::prediction::Market {
                            market_id: market_id.clone(),
                            question,
                            outcomes,
                            resolution,
                            asset,
                            closes_at_ms,
                            observed_at_ms,
                            dispute_window_ms,
                            commitment,
                            status: crate::prediction::MarketStatus::Open,
                            proposal: None,
                            winning_outcome: None,
                            created_at_ms,
                            // A market replays as unfrozen; a dispute in the log re-freezes it.
                            disputed_at_ms: None,
                        },
                    );
                    self.stakes.lock().unwrap().entry(market_id).or_default();
                }
                Record::MarketProposed {
                    market_id, outcome, proposed_at_ms, evidence, automatic,
                } => {
                    if let Some(m) = self.markets.lock().unwrap().get_mut(&market_id) {
                        m.proposal = Some(crate::prediction::Proposal {
                            outcome: outcome.map(|o| o as usize),
                            proposed_at_ms,
                            evidence,
                            automatic,
                            disputed_by: Vec::new(),
                        });
                        m.status = crate::prediction::MarketStatus::Proposed;
                    }
                }
                // Challenge records rebuild the book only. The money is restored by the Balance
                // records written alongside them, exactly as with stakes — replaying a debit here
                // as well would charge everyone twice.
                Record::ChallengeOpened { .. }
                | Record::ChallengeMatched { .. }
                | Record::ChallengeReleased { .. } => {}
                Record::FeedSubscriber { key, label } => match label {
                    Some(l) => self.feed_subscribers.add(key, l),
                    None => {
                        self.feed_subscribers.remove(&key);
                    }
                },
                Record::Trust { agent_id, score, forecast_score } => {
                    self.trust.set_score(&agent_id, score);
                    if let Some(fs) = forecast_score {
                        self.trust.set_forecast_score(&agent_id, fs);
                    }
                }
                Record::Reserve { asset, delta } => {
                    *self.reserves.lock().unwrap().entry(asset).or_insert(0.0) += delta;
                }
                Record::WithdrawalRequested {
                    withdrawal_id, agent_id, asset, amount, destination, requested_at_ms,
                } => {
                    self.withdrawals.lock().unwrap().push(crate::credits::Withdrawal {
                        withdrawal_id,
                        agent_id,
                        asset,
                        amount,
                        destination,
                        status: crate::credits::WithdrawalStatus::Pending,
                        requested_at_ms,
                        settled_at_ms: None,
                        tx_ref: None,
                        note: None,
                    });
                }
                Record::WithdrawalSettled { withdrawal_id, sent, tx_ref, settled_at_ms } => {
                    let mut ws = self.withdrawals.lock().unwrap();
                    if let Some(w) = ws.iter_mut().find(|w| w.withdrawal_id == withdrawal_id) {
                        w.status = if sent {
                            crate::credits::WithdrawalStatus::Sent
                        } else {
                            crate::credits::WithdrawalStatus::Rejected
                        };
                        w.tx_ref = tx_ref;
                        w.settled_at_ms = Some(settled_at_ms);
                    }
                }
                Record::MarketDisputed { market_id, agent_id, disputed_at_ms, .. } => {
                    if let Some(m) = self.markets.lock().unwrap().get_mut(&market_id) {
                        // Replay the objection itself, not just its effect. Without this a
                        // restart would hand every previous disputer a fresh objection against
                        // the same proposal, which is exactly the loop the limit exists to stop.
                        if let Some(p) = m.proposal.as_mut() {
                            if !p.disputed_by.iter().any(|a| *a == agent_id) {
                                p.disputed_by.push(agent_id);
                            }
                        }
                        m.status = crate::prediction::MarketStatus::Disputed;
                        // Restore the freeze clock too, or a restart would silently give the
                        // operator a fresh 24 hours on a dispute that was already nearly out of
                        // time — and repeated restarts could extend it forever.
                        if m.disputed_at_ms.is_none() {
                            m.disputed_at_ms = Some(disputed_at_ms);
                        }
                    }
                }
                Record::StakePlaced { market_id, agent_id, outcome_idx, amount, placed_at_ms } => {
                    // Balances are restored by the Balance records written alongside this one,
                    // so replay here only rebuilds the pool, never re-debits anyone.
                    let replay_mid = market_id.clone();
                    self.stakes.lock().unwrap().entry(market_id).or_default().push(
                        crate::prediction::Stake {
                            agent_id,
                            outcome_idx: outcome_idx as usize,
                            amount,
                            placed_at_ms,
                        },
                    );
                    // Keep the running totals in step with what replay put back, or a
                    // restarted venue would serve odds computed from an empty cache.
                    {
                        let n = self
                            .markets
                            .lock()
                            .unwrap()
                            .get(&replay_mid)
                            .map(|m| m.outcomes.len())
                            .unwrap_or(2);
                        self.credit_pool(&replay_mid, outcome_idx as usize, amount, n);
                    }
                }
                Record::MarketSettled { market_id, winning_outcome, voided } => {
                    if let Some(m) = self.markets.lock().unwrap().get_mut(&market_id) {
                        m.status = if voided {
                            crate::prediction::MarketStatus::Voided
                        } else {
                            crate::prediction::MarketStatus::Resolved
                        };
                        m.winning_outcome = winning_outcome.map(|w| w as usize);
                    }
                }
            }
        }
    }

    // -- prediction markets -------------------------------------------------------------------

    /// Opens a market. Durable before it is visible, so a market can never take a stake it would
    /// forget on restart.

    /// Drops the oldest settled markets from memory once history grows past a limit.
    ///
    /// # Why this has to exist
    ///
    /// Nothing here ever removed a market. That was survivable while a human opened them by hand
    /// and fatal the moment autopilot did it on a timer: a venue running hourly markets on three
    /// pairs creates ~26,000 a year, and every one of them stays in the map forever with its
    /// question, its stakes, and its proposal evidence.
    ///
    /// Memory is the smaller half. The settlement sweeper walks every market **every second**,
    /// and the feed and dashboard aggregate all of them on every request — so the cost of running
    /// the venue grows with its whole history rather than with what is actually live. A venue is
    /// supposed to get better as it ages, not slower.
    ///
    /// Only *settled* markets are candidates. Anything Open, Closed, Proposed or Disputed is a
    /// live obligation and is never evicted no matter how old, because forgetting one would mean
    /// forgetting to pay someone. Evicted markets remain in the write-ahead log, which is the
    /// audit record; this trims the working set, not the history.
    pub fn prune_settled_markets(&self, keep: usize) -> usize {
        let mut markets = self.markets.lock().unwrap();
        let mut settled: Vec<(String, i64)> = markets
            .values()
            .filter(|m| m.status.is_final())
            .map(|m| (m.market_id.clone(), m.created_at_ms))
            .collect();
        if settled.len() <= keep {
            return 0;
        }
        // Oldest first, and by id as a tiebreak so the choice is deterministic when a batch of
        // markets share a creation millisecond — which autopilot makes likely, not rare.
        settled.sort_by(|a, b| a.1.cmp(&b.1).then_with(|| a.0.cmp(&b.0)));
        let drop_count = settled.len() - keep;
        let mut stakes = self.stakes.lock().unwrap();
        for (id, _) in settled.into_iter().take(drop_count) {
            markets.remove(&id);
            stakes.remove(&id);
        }
        drop_count
    }

    pub fn open_market(&self, market: crate::prediction::Market) {
        self.wal.append(&[crate::wal::Record::MarketOpened {
            market_id: market.market_id.clone(),
            question: market.question.clone(),
            outcomes: market.outcomes.clone(),
            resolution: market.resolution.to_json(),
            asset: market.asset.clone(),
            closes_at_ms: market.closes_at_ms,
            observed_at_ms: market.observed_at_ms,
            dispute_window_ms: market.dispute_window_ms,
            commitment: market.commitment.clone(),
            created_at_ms: market.created_at_ms,
        }]);
        self.events.record(
            crate::events::EventKind::MarketOpened,
            &market.market_id,
            format!("{} (closes {})", market.question, market.closes_at_ms),
            market.created_at_ms,
        );
        self.stakes.lock().unwrap().entry(market.market_id.clone()).or_default();
        self.markets.lock().unwrap().insert(market.market_id.clone(), market);
    }

    /// Takes a stake: validates the market, debits the balance, and records both atomically.
    ///
    /// The balance debit and the stake record go into one WAL batch on purpose. Split across two
    /// writes, a crash landing between them either takes an agent's money without recording the
    /// bet, or records a bet nobody paid for. Both are unrecoverable by inspection afterwards.
    /// Runs every check `place_stake` runs, and then changes nothing.
    ///
    /// A stake is irreversible, and the first one an integrator sends is the one most likely to be
    /// wrong: the outcome index off by one, the market already closed, the balance in the wrong
    /// asset. Without a rehearsal the only way to discover that is to do it for real, which for a
    /// cautious operator is a reason not to point their agent at an unfamiliar venue at all.
    ///
    /// Deliberately shares `validate_stake` and the same balance read with the real path rather
    /// than restating the rules. A dry run that checks a *different* set of conditions than the
    /// live call is worse than none: it tells the caller they are fine and then the real request
    /// fails, which is exactly the trust the feature was meant to build, spent.
    pub fn dry_run_stake(
        &self,
        market_id: &str,
        agent_id: &str,
        outcome_idx: usize,
        amount: f64,
        now_ms: i64,
    ) -> Result<f64, crate::prediction::MarketError> {
        let markets = self.markets.lock().unwrap();
        let Some(market) = markets.get(market_id) else {
            return Err(crate::prediction::MarketError::NotOpen);
        };
        market.validate_stake(outcome_idx, amount, now_ms)?;
        let asset = market.asset.clone();
        drop(markets);

        let balances = self.balances.lock().unwrap();
        let available = balances.get(&(agent_id.to_string(), asset)).copied().unwrap_or(0.0);
        if available < amount {
            return Err(crate::prediction::MarketError::InsufficientBalance {
                have: available,
                need: amount,
            });
        }
        Ok(available - amount)
    }

    pub fn place_stake(
        &self,
        market_id: &str,
        agent_id: &str,
        outcome_idx: usize,
        amount: f64,
        now_ms: i64,
    ) -> Result<f64, crate::prediction::MarketError> {
        let markets = self.markets.lock().unwrap();
        let Some(market) = markets.get(market_id) else {
            return Err(crate::prediction::MarketError::NotOpen);
        };
        market.validate_stake(outcome_idx, amount, now_ms)?;
        let asset = market.asset.clone();
        let outcome_count = market.outcomes.len();
        drop(markets);

        // Balance check and debit under one lock, so two concurrent stakes can't both pass a
        // check against the same funds — the same TOCTOU hazard the order book has.
        let mut balances = self.balances.lock().unwrap();
        let key = (agent_id.to_string(), asset.clone());
        let available = balances.get(&key).copied().unwrap_or(0.0);
        if available < amount {
            return Err(crate::prediction::MarketError::InsufficientBalance {
                have: available,
                need: amount,
            });
        }
        *balances.entry(key).or_insert(0.0) -= amount;
        let remaining = balances.get(&(agent_id.to_string(), asset.clone())).copied().unwrap_or(0.0);
        drop(balances);

        self.wal.append(&[
            crate::wal::Record::Balance {
                agent_id: agent_id.to_string(),
                asset,
                delta: -amount,
            },
            crate::wal::Record::StakePlaced {
                market_id: market_id.to_string(),
                agent_id: agent_id.to_string(),
                outcome_idx: outcome_idx as u32,
                amount,
                placed_at_ms: now_ms,
            },
        ]);

        self.stakes.lock().unwrap().entry(market_id.to_string()).or_default().push(
            crate::prediction::Stake {
                agent_id: agent_id.to_string(),
                outcome_idx,
                amount,
                placed_at_ms: now_ms,
            },
        );
        self.credit_pool(market_id, outcome_idx, amount, outcome_count);
        Ok(remaining)
    }

    /// Agent IDs the operator has declared as its own, barred from staking.
    ///
    /// An honest control, not a guarantee: an operator who controls the server can always stake
    /// through an undeclared identity. What this buys is that casual self-dealing is blocked, the
    /// declaration is public, and a breach of it is visible in the log rather than deniable.
    pub fn is_operator_agent(&self, agent_id: &str) -> bool {
        agent_id == HOUSE_AGENT
            || std::env::var("IKENGA_OPERATOR_AGENTS")
                .map(|v| v.split(',').any(|a| a.trim() == agent_id))
                .unwrap_or(false)
    }

    /// Seeds a new market with a little of the house's own money on every outcome.
    ///
    /// # The cold-start problem, which is fatal and cannot be solved from inside the pool
    ///
    /// A pari-mutuel market pays winners out of the losers. An empty one therefore has nothing in
    /// it to win: the first agent to arrive stakes, computes that its upside is zero, and is
    /// exactly right to leave. If nobody joins, the market voids and refunds it — no loss, but no
    /// reason to have come. Every arriving agent reaches the same conclusion, so the board never
    /// fills, and it never fills for a *correct* reason. No amount of documentation fixes that.
    /// Somebody has to put the first money down, and it cannot be a participant, because being
    /// first is precisely the position with no upside.
    ///
    /// So the house does it, the way a market maker always has. A fixed amount goes on each
    /// outcome the moment a market opens, and a first arrival now has a real, if small, pool to
    /// win from — and its forecast counts toward its record rather than evaporating in a void.
    ///
    /// # Why this does not become the house picking winners
    ///
    /// The operator also resolves markets, so an operator that could *choose* its bets would be
    /// running the oldest scam there is. Three properties stop that, and all three matter:
    ///
    /// - **It is mechanical.** Equal on every outcome, fixed size, at open, with no view about
    ///   anything. There is no decision to corrupt.
    /// - **It cannot be done by hand.** `HOUSE_AGENT` is treated as an operator agent, so the API
    ///   refuses a stake from it outright. The only path to this money is this function.
    /// - **It is spent, not minted.** The seed comes out of a balance the operator actually funded
    ///   and stops dead when that balance runs out, so it cannot quietly print stake or break the
    ///   reserve invariant on a redeemable asset.
    ///
    /// Being on every side equally, the house holds no opinion: it wins one leg and loses the
    /// other, pays rake on the losing one like anybody else, and expects to lose a little on each
    /// market. That is the cost of having a market at all.
    pub fn seed_market(&self, market_id: &str, now_ms: i64) -> Option<(usize, f64)> {
        let per_outcome: f64 = std::env::var("IKENGA_SEED_PER_MARKET")
            .ok()
            .and_then(|v| v.trim().parse().ok())
            .filter(|v: &f64| v.is_finite() && *v > 0.0)?;

        let (outcomes, asset) = {
            let markets = self.markets.lock().unwrap();
            let m = markets.get(market_id)?;
            (m.outcomes.len(), m.asset.clone())
        };
        if outcomes == 0 {
            return None;
        }

        // Check the whole seed is affordable before placing any of it. Seeding three outcomes of
        // four and running dry would leave a lopsided book that reads as a house opinion — the
        // one thing this must never look like.
        let needed = per_outcome * outcomes as f64;
        let held = self
            .balances
            .lock()
            .unwrap()
            .get(&(HOUSE_AGENT.to_string(), asset.clone()))
            .copied()
            .unwrap_or(0.0);
        if held < needed {
            return None;
        }

        // How fast the subsidy is allowed to leave, on top of how much of it there is.
        //
        // The bankroll already bounds the total, and the per-market seed bounds the loss on any
        // one market — an agent can take at most the house's losing side, less the rake. What
        // neither bounds is the *rate*. Three markets every ten minutes is 432 a day, so a bettor
        // that is reliably right empties a 10,000 bankroll inside a day and the board silently
        // stops being seeded, which is the exact failure the seeding existed to prevent, arriving
        // quietly a day late.
        //
        // This is deliberately attack-agnostic. It does not care whether the money is leaving to
        // a genuinely good forecaster, to someone exploiting the fact that a static 50/50 quote
        // gets stale as a market runs, or to something nobody has thought of yet. It caps the
        // day's spend either way and says so in the log.
        //
        // Held in memory, so a restart resets the day's tally. That is a real limitation and an
        // acceptable one: an attacker cannot restart the venue, and an operator who does gets a
        // fresh budget rather than a stuck one.
        let daily_max: f64 = std::env::var("IKENGA_SEED_DAILY_MAX")
            .ok()
            .and_then(|v| v.trim().parse().ok())
            .filter(|v: &f64| v.is_finite() && *v > 0.0)
            .unwrap_or(bankroll_default() / 4.0);
        {
            let day = now_ms.div_euclid(86_400_000);
            let mut spent = self.seed_spent_today.lock().unwrap();
            if spent.0 != day {
                *spent = (day, 0.0);
            }
            if spent.1 + needed > daily_max {
                if !self.seed_cap_reported.swap(true, std::sync::atomic::Ordering::Relaxed) {
                    println!(
                        "seed liquidity: today's cap of {daily_max} reached — new markets open \
                         unseeded until tomorrow. Raise IKENGA_SEED_DAILY_MAX if that is not what \
                         you want."
                    );
                }
                return None;
            }
            spent.1 += needed;
        }

        let mut placed = 0usize;
        for idx in 0..outcomes {
            if self.place_stake(market_id, HOUSE_AGENT, idx, per_outcome, now_ms).is_ok() {
                placed += 1;
            }
        }
        (placed > 0).then_some((placed, per_outcome))
    }

    /// What the house has put on the board today, and what it holds. For the operator console:
    /// a subsidy nobody can see is a subsidy nobody notices draining.
    pub fn seed_status(&self) -> (f64, f64, f64) {
        let spent = self.seed_spent_today.lock().unwrap().1;
        let held = self.get_balance(HOUSE_AGENT, crate::api::POINTS_ASSET);
        let cap = std::env::var("IKENGA_SEED_DAILY_MAX")
            .ok()
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(bankroll_default() / 4.0);
        (spent, cap, held)
    }

    /// Total staked on each outcome, without walking the stake list.
    ///
    /// Falls back to recomputing when there is no cached entry — a market recovered from the log
    /// before the cache was warmed, say — so a miss is slow rather than wrong.
    pub fn pools_for(&self, market_id: &str, outcome_count: usize) -> Vec<f64> {
        if let Some(v) = self.pool_cache.lock().unwrap().get(market_id) {
            if v.len() == outcome_count {
                return v.clone();
            }
        }
        let stakes = self.stakes.lock().unwrap();
        let computed = crate::prediction::pools(
            stakes.get(market_id).map(|v| v.as_slice()).unwrap_or(&[]),
            outcome_count,
        );
        drop(stakes);
        self.pool_cache.lock().unwrap().insert(market_id.to_string(), computed.clone());
        computed
    }

    /// Adds to the running totals. Called from every place a stake is recorded.
    fn credit_pool(&self, market_id: &str, outcome_idx: usize, amount: f64, outcome_count: usize) {
        let mut cache = self.pool_cache.lock().unwrap();
        let entry = cache.entry(market_id.to_string()).or_insert_with(|| vec![0.0; outcome_count]);
        if entry.len() < outcome_count {
            entry.resize(outcome_count, 0.0);
        }
        if let Some(slot) = entry.get_mut(outcome_idx) {
            *slot += amount;
        }
    }

    /// One side of a two-party bet says what happened.
    ///
    /// Returns what the caller should be told: whether it settled, is still waiting, or has gone
    /// to the operator because the two sides disagree.
    pub fn report_outcome(
        &self,
        market_id: &str,
        agent_id: &str,
        outcome: usize,
        now_ms: i64,
    ) -> Result<ReportResult, &'static str> {
        // Everything that decides whether the report is even allowed, read under one lock and
        // then released. Resolution happens afterwards through the ordinary settlement path,
        // which takes these locks itself — holding them across that call is how this codebase
        // has deadlocked twice.
        let (participants, outcome_count) = {
            let markets = self.markets.lock().unwrap();
            let Some(m) = markets.get(market_id) else { return Err("no such market") };
            if !matches!(m.resolution, crate::prediction::ResolutionSpec::MutualAgreement { .. }) {
                return Err("this market is not settled by agreement");
            }
            if m.status.is_final() {
                return Err("this market is already settled");
            }
            if now_ms < m.observed_at_ms {
                return Err("too early — wait until the observation time before reporting");
            }
            if outcome >= m.outcomes.len() {
                return Err("that is not one of this market's outcomes");
            }
            let stakes = self.stakes.lock().unwrap();
            let mut who: Vec<String> = stakes
                .get(market_id)
                .map(|v| v.iter().map(|s| s.agent_id.clone()).collect())
                .unwrap_or_default();
            who.sort();
            who.dedup();
            (who, m.outcomes.len())
        };
        let _ = outcome_count;

        if !participants.iter().any(|a| a == agent_id) {
            return Err("only the two sides of this bet can report its outcome");
        }

        let (agreed, waiting_on, conflict) = {
            let mut reports = self.outcome_reports.lock().unwrap();
            let entry = reports.entry(market_id.to_string()).or_default();
            // First answer stands. Letting someone revise turns "we agreed" into a race, where
            // whoever changes their answer last decides.
            if entry.contains_key(agent_id) {
                return Err("you have already reported this one");
            }
            entry.insert(agent_id.to_string(), outcome);
            let values: Vec<usize> = participants.iter().filter_map(|a| entry.get(a).copied()).collect();
            let all_in = values.len() == participants.len() && participants.len() >= 2;
            let same = values.windows(2).all(|w| w[0] == w[1]);
            (all_in && same, participants.len().saturating_sub(values.len()), all_in && !same)
        };

        if agreed {
            self.resolve_market(market_id, Some(outcome));
            return Ok(ReportResult::Settled(outcome));
        }
        if conflict {
            // Straight to the existing dispute machinery: the operator gets a review window and,
            // if nobody uses it, the backstop voids and refunds. Nobody wins an argument here.
            self.mark_disputed_for_review(
                market_id,
                "the two sides reported different outcomes",
                now_ms,
            );
            return Ok(ReportResult::Disagreed);
        }
        Ok(ReportResult::Waiting(waiting_on))
    }

    /// Puts a market in front of the operator, from any non-final state.
    ///
    /// `dispute_outcome` only works on a market with a *proposal* to dispute, which is right for
    /// pool markets and wrong here: a two-party bet whose sides disagree has no proposal at all,
    /// so the first version of this quietly did nothing and left the market sitting Closed until
    /// the seven-day backstop swept it up. Safe — the money came back — but a week late and
    /// invisible to the operator, who is the entire point of the state.
    ///
    /// # The griefing problem this creates, and why the operator has to be here
    ///
    /// A bet settled by agreement has an obvious attack: whoever is losing reports the wrong
    /// outcome and forces a disagreement. If disagreement simply voided, no one would ever lose a
    /// bet of this kind and the whole mechanism would be theatre.
    ///
    /// So disagreement does not void — it escalates. The operator reviews against the criteria
    /// both sides committed to before either had money at risk, and settles it. Only if nobody
    /// reviews within `DISPUTE_REVIEW_MS` does the backstop void and refund.
    ///
    /// That leaves one honest limitation, worth stating plainly rather than hiding: on a venue
    /// whose operator never looks at the queue, a determined liar can force a refund instead of a
    /// loss. Bets settled by agreement are therefore only as trustworthy as the operator's
    /// attention — which is exactly why they are confined to two-party challenges the
    /// participants opted into, and why the pool markets that make up the board settle from price
    /// feeds with no human in the path at all.
    pub fn mark_disputed_for_review(&self, market_id: &str, reason: &str, now_ms: i64) {
        let mut markets = self.markets.lock().unwrap();
        let Some(m) = markets.get_mut(market_id) else { return };
        if m.status.is_final() || m.status == crate::prediction::MarketStatus::Disputed {
            return;
        }
        m.status = crate::prediction::MarketStatus::Disputed;
        m.disputed_at_ms = Some(now_ms);
        drop(markets);

        self.wal.append(&[crate::wal::Record::MarketDisputed {
            market_id: market_id.to_string(),
            agent_id: "both-sides".to_string(),
            reason: reason.to_string(),
            disputed_at_ms: now_ms,
        }]);
        self.events.record(
            crate::events::EventKind::OutcomeDisputed,
            market_id,
            reason.to_string(),
            now_ms,
        );
    }

    /// What has been reported so far, for display.
    pub fn reports_for(&self, market_id: &str) -> Vec<(String, usize)> {
        self.outcome_reports
            .lock()
            .unwrap()
            .get(market_id)
            .map(|m| m.iter().map(|(a, o)| (a.clone(), *o)).collect())
            .unwrap_or_default()
    }

    pub fn reserves_for(&self, asset: &str) -> f64 {
        self.reserves.lock().unwrap().get(asset).copied().unwrap_or(0.0)
    }

    /// Records a confirmed deposit as reserves backing redeemable credits.
    pub fn credit_reserves(&self, asset: &str, amount: f64) {
        self.wal.append(&[crate::wal::Record::Reserve {
            asset: asset.to_string(),
            delta: amount,
        }]);
        *self.reserves.lock().unwrap().entry(asset.to_string()).or_insert(0.0) += amount;
    }

    /// Requests a withdrawal: debits immediately, then queues for an on-chain send.
    ///
    /// Debiting here rather than at send time is what stops an agent staking the same credits
    /// while a payout is already in flight.
    pub fn request_withdrawal(
        &self,
        agent_id: &str,
        asset: &str,
        amount: f64,
        destination: &str,
        now_ms: i64,
    ) -> Result<crate::credits::Withdrawal, crate::credits::CreditError> {
        use crate::credits::{CreditError, Withdrawal, WithdrawalStatus};

        // Redeemability is checked FIRST, on purpose. For promotional points the honest answer
        // is "these can never be withdrawn" — reporting "real money is disabled" instead would
        // imply that enabling it would one day let free points be cashed out, which is exactly
        // the thing that must never become true.
        if !crate::credits::is_redeemable(asset) {
            return Err(CreditError::NotRedeemable(asset.to_string()));
        }
        if !self.real_money_enabled {
            return Err(CreditError::RealMoneyDisabled);
        }
        if !(amount > 0.0) || !amount.is_finite() {
            return Err(CreditError::NonPositiveAmount);
        }
        if destination.trim().is_empty() {
            return Err(CreditError::EmptyDestination);
        }

        let mut balances = self.balances.lock().unwrap();
        let key = (agent_id.to_string(), asset.to_string());
        let have = balances.get(&key).copied().unwrap_or(0.0);
        if have < amount {
            return Err(CreditError::InsufficientBalance { have, need: amount });
        }

        // Solvency check before the debit, counting this request as a claim. Paying out into a
        // shortfall converts one agent's loss into everyone's.
        let mut withdrawals = self.withdrawals.lock().unwrap();
        let reserves = self.reserves.lock().unwrap();
        let projected = crate::credits::reserve_shortfall(&balances, &withdrawals, &reserves, asset);
        if projected > 1e-9 {
            return Err(CreditError::WouldBreakReserves { shortfall: projected });
        }
        drop(reserves);

        *balances.entry(key).or_insert(0.0) -= amount;
        drop(balances);

        let w = Withdrawal {
            withdrawal_id: format!(
                "wd_{}",
                crate::crypto::hex_encode(&crate::crypto::random_bytes_pooled(8))
            ),
            agent_id: agent_id.to_string(),
            asset: asset.to_string(),
            amount,
            destination: destination.to_string(),
            status: WithdrawalStatus::Pending,
            requested_at_ms: now_ms,
            settled_at_ms: None,
            tx_ref: None,
            note: None,
        };
        withdrawals.push(w.clone());
        drop(withdrawals);

        // One batch: the debit and the payout record land together or not at all. Split, a crash
        // between them leaves an agent debited with nothing owed back to them.
        self.wal.append(&[
            crate::wal::Record::Balance {
                agent_id: agent_id.to_string(),
                asset: asset.to_string(),
                delta: -amount,
            },
            crate::wal::Record::WithdrawalRequested {
                withdrawal_id: w.withdrawal_id.clone(),
                agent_id: agent_id.to_string(),
                asset: asset.to_string(),
                amount,
                destination: destination.to_string(),
                requested_at_ms: now_ms,
            },
        ]);
        Ok(w)
    }

    /// Marks a pending withdrawal as sent (with a chain reference) or rejected (refunding it).
    pub fn settle_withdrawal(
        &self,
        withdrawal_id: &str,
        sent: bool,
        tx_ref: Option<String>,
        now_ms: i64,
    ) -> Result<crate::credits::Withdrawal, crate::credits::CreditError> {
        use crate::credits::{CreditError, WithdrawalStatus};

        let mut withdrawals = self.withdrawals.lock().unwrap();
        let Some(w) = withdrawals.iter_mut().find(|w| w.withdrawal_id == withdrawal_id) else {
            return Err(CreditError::UnknownWithdrawal);
        };
        if w.status != WithdrawalStatus::Pending {
            return Err(CreditError::AlreadySettled);
        }
        w.status = if sent { WithdrawalStatus::Sent } else { WithdrawalStatus::Rejected };
        w.settled_at_ms = Some(now_ms);
        w.tx_ref = tx_ref;
        let snapshot = w.clone();
        let (agent, asset, amount) = (w.agent_id.clone(), w.asset.clone(), w.amount);
        drop(withdrawals);

        self.wal.append(&[crate::wal::Record::WithdrawalSettled {
            withdrawal_id: withdrawal_id.to_string(),
            sent,
            tx_ref: snapshot.tx_ref.clone(),
            settled_at_ms: now_ms,
        }]);
        if sent {
            // The money left the platform, so the reserve backing it is gone too.
            self.wal.append(&[crate::wal::Record::Reserve {
                asset: asset.clone(),
                delta: -amount,
            }]);
            *self.reserves.lock().unwrap().entry(asset.clone()).or_insert(0.0) -= amount;
        } else {
            // Refund: the claim is released back to the agent.
            self.adjust_balance(&agent, &asset, amount);
        }
        Ok(snapshot)
    }

    /// Assets an agent is allowed to stake. Real-money assets exist only when the deployment
    /// explicitly enables them; promotional points always do.
    /// Which assets a market may be denominated in.
    ///
    /// Exactly two, ever: promotional points, and the one redeemable settlement asset. The
    /// earlier version returned true for *any* asset once real money was on, which would have
    /// allowed a `DOGE` market whose pool nobody could fund and whose winners could not be paid.
    /// Pools are single-asset by construction; the set of assets is the point of the constraint.
    pub fn asset_is_stakeable(&self, asset: &str) -> bool {
        if asset == crate::api::POINTS_ASSET {
            return true;
        }
        asset == crate::credits::CREDITS && self.real_money_enabled
    }

    /// Records a proposed outcome and starts the dispute window. Nothing is paid here.
    ///
    /// Splitting proposal from payment is the single most important structural difference from
    /// the design that cost Polymarket $7M: an outcome that is merely *asserted* has a window in
    /// which it can be challenged before any money moves, and a challenge freezes the payout
    /// rather than triggering a vote that can be bought.
    pub fn propose_outcome(
        &self,
        market_id: &str,
        outcome: Option<usize>,
        evidence: String,
        automatic: bool,
        now_ms: i64,
    ) -> Result<crate::prediction::Proposal, &'static str> {
        let mut markets = self.markets.lock().unwrap();
        let Some(market) = markets.get_mut(market_id) else { return Err("no such market") };
        if market.status.is_final() {
            return Err("market is already settled");
        }
        if now_ms < market.observed_at_ms {
            return Err("cannot propose an outcome before the market's observation time");
        }
        if let Some(idx) = outcome {
            if idx >= market.outcomes.len() {
                return Err("proposed outcome does not exist in this market");
            }
        }
        let proposal = crate::prediction::Proposal {
            outcome,
            proposed_at_ms: now_ms,
            evidence,
            automatic,
            // A re-proposal is a fresh hearing: everyone who objected to the previous one may
            // object to this one too.
            disputed_by: Vec::new(),
        };
        market.proposal = Some(proposal.clone());
        market.status = crate::prediction::MarketStatus::Proposed;
        // The review happened: a new proposal is the operator's answer, so the freeze lifts and
        // the ordinary dispute window runs again on the new outcome.
        market.disputed_at_ms = None;
        drop(markets);
        self.events.record(
            crate::events::EventKind::OutcomeProposed,
            market_id,
            match outcome {
                Some(i) => format!("outcome {i} proposed; dispute window open"),
                None => "void proposed; dispute window open".to_string(),
            },
            now_ms,
        );

        self.wal.append(&[crate::wal::Record::MarketProposed {
            market_id: market_id.to_string(),
            outcome: outcome.map(|o| o as u32),
            proposed_at_ms: now_ms,
            evidence: proposal.evidence.clone(),
            automatic,
        }]);
        Ok(proposal)
    }

    /// Challenges a proposal, freezing payout until it is re-proposed or voided.
    /// Challenges a proposed outcome, freezing the payout until it is re-proposed or voided.
    ///
    /// Two limits, both of them anti-griefing rather than anti-challenge. A disputer must hold a
    /// stake in the market: an account with nothing in the pot has nothing to lose by freezing
    /// it, and a handful of such accounts could stall every settlement on the venue
    /// indefinitely. And each agent gets one objection per proposal, so the same account cannot
    /// re-freeze the same proposal in a loop. Neither limit narrows who can catch a bad
    /// resolution among the people whose money is actually at stake, which is the population
    /// that reliably checks.
    pub fn dispute_outcome(
        &self,
        market_id: &str,
        agent_id: &str,
        reason: &str,
    ) -> Result<(), &'static str> {
        let has_stake = self
            .stakes
            .lock()
            .unwrap()
            .get(market_id)
            .map(|v| v.iter().any(|s| s.agent_id == agent_id))
            .unwrap_or(false);

        let mut markets = self.markets.lock().unwrap();
        let Some(market) = markets.get_mut(market_id) else { return Err("no such market") };
        if market.status.is_final() {
            return Err("market is already settled — a settled outcome cannot be disputed");
        }
        let Some(proposal) = market.proposal.as_mut() else {
            return Err("nothing has been proposed yet");
        };
        if !has_stake {
            return Err(
                "only someone with a stake in this market can dispute its outcome — a challenge \
                 freezes everyone's payout, so it costs the challenger something too",
            );
        }
        if proposal.disputed_by.iter().any(|a| a == agent_id) {
            return Err(
                "you have already disputed this proposal; it stays frozen until it is \
                 re-proposed or voided, and a re-proposal can be disputed afresh",
            );
        }
        proposal.disputed_by.push(agent_id.to_string());
        market.status = crate::prediction::MarketStatus::Disputed;
        // Start the review clock. The freeze is temporary by construction: either the operator
        // decides within the window, or the market voids and everyone is refunded. An objection
        // nobody answers must not hold other people's money indefinitely.
        market.disputed_at_ms = Some(crate::api::now_ms_pub());
        drop(markets);
        self.events.record(
            crate::events::EventKind::OutcomeDisputed,
            market_id,
            "a participant challenged the proposed outcome; payout frozen",
            crate::api::now_ms_pub(),
        );
        self.wal.append(&[crate::wal::Record::MarketDisputed {
            market_id: market_id.to_string(),
            agent_id: agent_id.to_string(),
            reason: reason.to_string(),
            disputed_at_ms: crate::api::now_ms_pub(),
        }]);
        Ok(())
    }

    /// Resolves a market and pays everyone out. Returns the settlement for reporting.
    ///
    /// `winning_outcome: None` voids the market and refunds in full — the honest outcome when a
    /// question turns out to be unresolvable, and the reason the resolution rule has to be
    /// written before anyone stakes.
    pub fn resolve_market(
        &self,
        market_id: &str,
        winning_outcome: Option<usize>,
    ) -> Option<crate::prediction::Settlement> {
        let mut markets = self.markets.lock().unwrap();
        let market = markets.get_mut(market_id)?;
        if market.status.is_final() {
            return None; // already settled — never pay a market out twice
        }
        let outcome_count = market.outcomes.len();
        let asset = market.asset.clone();
        let stakes = self.stakes.lock().unwrap().get(market_id).cloned().unwrap_or_default();

        // The window the market took stakes over, so whoever was early can be paid for it out of
        // the rake. Taken from the market's own committed terms, never from the stakes themselves:
        // deriving "when did this open" from the first stake would let the first staker define
        // their own earliness by simply being the first staker.
        let window = crate::prediction::StakeWindow {
            opened_at_ms: market.created_at_ms,
            closes_at_ms: market.closes_at_ms,
        };

        let settlement = match winning_outcome {
            Some(w) => crate::prediction::settle_windowed(
                market_id,
                &stakes,
                outcome_count,
                w,
                crate::prediction::DEFAULT_RAKE_BPS,
                crate::prediction::DEFAULT_REBATE_SHARE_BPS,
                Some(window),
            ),
            None => crate::prediction::void(market_id, &stakes, "market voided by the operator"),
        };

        market.status = if settlement.refunded && winning_outcome.is_none() {
            crate::prediction::MarketStatus::Voided
        } else {
            crate::prediction::MarketStatus::Resolved
        };
        market.winning_outcome = winning_outcome;
        drop(markets);

        // One batch: the outcome, every payout, and the rake all land together or not at all.
        let mut records = vec![crate::wal::Record::MarketSettled {
            market_id: market_id.to_string(),
            winning_outcome: winning_outcome.map(|w| w as u32),
            voided: winning_outcome.is_none(),
        }];
        {
            let mut balances = self.balances.lock().unwrap();
            for (agent, amount) in &settlement.payouts {
                *balances.entry((agent.clone(), asset.clone())).or_insert(0.0) += *amount;
                records.push(crate::wal::Record::Balance {
                    agent_id: agent.clone(),
                    asset: asset.clone(),
                    delta: *amount,
                });
            }
        }
        // Move each participant's trust by how well they forecast this market. Without this the
        // trust store is written once at registration and never again, leaving every agent stuck
        // in the New band at 10 req/s permanently — see prediction::trust_delta_from_brier.
        // Two scores move here, and only one of them can be farmed with free money.
        //
        // Every settled market moves `score`, which governs rate limits and who may open a
        // market — that has to be earnable with points or a new agent never gets off the floor.
        // Only a market denominated in a redeemable asset moves `forecast_score`, which is the
        // one the paid feed weights by. See `trust::TrustStore` for why the two are separate.
        let real_money = crate::credits::is_redeemable(&asset);
        if let Some(winner) = winning_outcome {
            let mut participants: Vec<String> = stakes.iter().map(|s| s.agent_id.clone()).collect();
            participants.sort();
            participants.dedup();
            // Scored against the market's own consensus, not against the truth alone — see
            // prediction::trust_delta_vs_consensus for the farm that closes.
            let consensus =
                crate::prediction::consensus_brier(&stakes, outcome_count, winner).unwrap_or(0.0);
            for agent in participants {
                if let Some(brier) =
                    crate::prediction::brier_score(&stakes, &agent, outcome_count, winner)
                {
                    let delta = crate::prediction::trust_delta_vs_consensus(brier, consensus);
                    let current = self.trust.get_score(&agent) as i64;
                    let updated = (current + delta as i64).clamp(0, 1000) as u32;
                    self.trust.set_score(&agent, updated);
                    let forecast_updated = if real_money {
                        let cur = self.trust.get_forecast_score(&agent) as i64;
                        let up = (cur + delta as i64).clamp(0, 1000) as u32;
                        self.trust.set_forecast_score(&agent, up);
                        Some(up)
                    } else {
                        None
                    };
                    records.push(crate::wal::Record::Trust {
                        agent_id: agent,
                        score: updated,
                        forecast_score: forecast_updated,
                    });
                }
            }
        }

        self.events.record(
            match winning_outcome {
                Some(_) => crate::events::EventKind::MarketResolved,
                None => crate::events::EventKind::MarketVoided,
            },
            market_id,
            match winning_outcome {
                Some(w) => format!(
                    "outcome {w} final; pool {:.8} paid to {} participant(s); rake {:.8}; \
                     early-liquidity rebate {:.8}",
                    settlement.payouts.iter().map(|(_, a)| *a).sum::<f64>(),
                    settlement.payouts.len(),
                    settlement.house_take(),
                    settlement.rebate
                ),
                None => "voided; every stake refunded in full, no rake".to_string(),
            },
            crate::api::now_ms_pub(),
        );

        // Only what the house actually keeps. Crediting the gross rake here while the rebate has
        // already been paid out as a payout would book the same money twice and make the venue
        // report reserves it does not hold.
        let house_take = settlement.house_take();
        if house_take > 0.0 {
            *self.fee_ledger.lock().unwrap().entry(asset.clone()).or_insert(0.0) += house_take;
            records.push(crate::wal::Record::Fee { asset, amount: house_take });
        }
        self.wal.append(&records);

        Some(settlement)
    }

    /// The venue's attestation keypair seed, derived from the owner key.
    ///
    /// Derived rather than stored so there is no second secret to lose, and domain-separated so
    /// that a signature over a track record can never be replayed as anything else — notably not
    /// as the owner key itself, which the derivation is one-way from.
    fn attestation_seed(&self) -> [u8; 32] {
        crate::crypto::sha256(
            &[b"ikenga-attestation-v1".as_slice(), self.owner_key.as_slice()].concat(),
        )
    }

    /// The public half, published so anyone can check a record this venue signed.
    pub fn attestation_pubkey_hex(&self) -> Option<String> {
        crate::ed25519::public_from_seed(&self.attestation_seed())
            .map(|pk| crate::crypto::hex_encode(&pk))
    }

    /// Signs a statement as the venue. `None` when the signing backend is unavailable, which is
    /// reported to the caller rather than papered over with an empty string — an unsigned record
    /// presented as signed is worse than an honestly unsigned one.
    pub fn sign_attestation(&self, message: &[u8]) -> Option<String> {
        crate::ed25519::sign(&self.attestation_seed(), message)
            .map(|sig| crate::crypto::hex_encode(&sig))
    }

    pub fn owner_key_hex(&self) -> String {
        crate::crypto::hex_encode(&self.owner_key)
    }

    pub fn check_owner_key(&self, provided_hex: &str) -> bool {
        match crate::crypto::hex_decode(provided_hex) {
            Some(bytes) => crate::crypto::constant_time_eq(&self.owner_key, &bytes),
            None => false,
        }
    }

    /// Next value in the WS tick sequence counter (1-indexed: the first tick ever broadcast is
    /// seq 1, so a client's "haven't seen any ticks yet" state can stay distinguishable as 0).
    pub fn next_tick_seq(&self) -> u64 {
        self.tick_seq.fetch_add(1, Ordering::Relaxed) + 1
    }

    /// Locks the whole balances table for the duration of a risk-check + settle critical
    /// section. Needed because `get_balance`/`adjust_balance` each lock-and-release
    /// independently, which is fine for a single read or write but NOT atomic across a
    /// "check funds, then later deduct them" sequence — two orders on *different* symbols
    /// (e.g. BTC-USD and ETH-USD, both quoted in USD) could otherwise both pass a balance check
    /// against the same stale USD balance before either deducts, overdrawing the account. The
    /// per-symbol book lock alone doesn't prevent this (it's scoped to one book); holding this
    /// lock across both books' worth of the operation does. See api.rs::submit_order.
    pub fn lock_balances(&self) -> MutexGuard<'_, HashMap<(String, String), f64>> {
        self.balances.lock().unwrap()
    }

    pub fn asset_pair(symbol: &str) -> Option<(&str, &str)> {
        symbol.split_once('-')
    }

    /// Standalone balance read — fine on its own (e.g. GET /v1/account), but do NOT use this
    /// followed later by `adjust_balance` as a "check then deduct" sequence across two separate
    /// lock/unlock cycles: that's exactly the TOCTOU `lock_balances` exists to prevent. Order
    /// settlement goes through `lock_balances` instead.
    pub fn get_balance(&self, agent_id: &str, asset: &str) -> f64 {
        self.balances
            .lock()
            .unwrap()
            .get(&(agent_id.to_string(), asset.to_string()))
            .copied()
            .unwrap_or(0.0)
    }

    /// Registers an agent's PUBLIC key, durably. Without the WAL record here, every restart
    /// would silently forget every client's registration. The private key is never passed to,
    /// or held by, this function — see src/ed25519.rs.
    pub fn register_agent(&self, agent_id: &str, pubkey: [u8; 32], trust: u32) {
        self.wal.append(&[crate::wal::Record::Agent {
            agent_id: agent_id.to_string(),
            pubkey_hex: crate::crypto::hex_encode(&pubkey),
            trust,
        }]);
        self.agent_registry.register(agent_id, pubkey);
        self.trust.set_score(agent_id, trust);
    }

    /// Moves a balance. Refuses, loudly, to write a value that is not a finite number.
    ///
    /// The last line of defence rather than the first: callers validate their inputs, but this is
    /// the single funnel every credit and debit passes through, and a non-finite balance is not a
    /// wrong number — it is an unrecoverable one. Infinity propagates into the reserve arithmetic,
    /// turns the solvency check into NaN, and persists through restart because it is in the log.
    /// Better to drop one bad write and say so than to poison the ledger permanently.
    pub fn adjust_balance(&self, agent_id: &str, asset: &str, delta: f64) {
        if !delta.is_finite() {
            eprintln!(
                "REFUSED: non-finite balance change ({delta}) for {agent_id} {asset} — ignored"
            );
            return;
        }
        let key = (agent_id.to_string(), asset.to_string());
        {
            let mut balances = self.balances.lock().unwrap();
            let entry = balances.entry(key).or_insert(0.0);
            let updated = *entry + delta;
            if !updated.is_finite() {
                eprintln!(
                    "REFUSED: {agent_id} {asset} balance would become {updated} — ignored"
                );
                return;
            }
            *entry = updated;
        }
        self.wal.append(&[crate::wal::Record::Balance {
            agent_id: agent_id.to_string(),
            asset: asset.to_string(),
            delta,
        }]);
    }

    /// Broadcasts a tick frame to every connected WS subscriber, dropping any whose receiver
    /// has disconnected. Fire-and-forget — never blocks the caller on a slow/dead consumer
    /// (the channel is unbounded from the sender's point of view; a genuinely slow client would
    /// need a bounded channel + drop policy, noted as a gap for a real deployment).
    /// Fans a tick out to every live subscriber, dropping any that cannot keep up.
    ///
    /// `try_send` rather than `send`: a full queue means the client is behind, and the choice is
    /// between dropping the client and growing forever. Both failure modes — full and
    /// disconnected — remove the subscriber, so the list only ever holds sockets that are
    /// actually keeping pace.
    pub fn broadcast_tick(&self, frame: Vec<u8>) {
        let mut subs = self.tick_subscribers.lock().unwrap();
        subs.retain(|tx| tx.try_send(frame.clone()).is_ok());
    }

    /// How many frames a subscriber may fall behind before it is dropped. A couple of seconds of
    /// a busy book: enough to ride out a scheduling hiccup, far short of a memory problem.
    pub const TICK_QUEUE_DEPTH: usize = 256;

    /// Traded notional per agent, used to pick their fee tier (see fees.rs).
    ///
    /// GAP: this is cumulative, not a rolling 30-day window, so an agent never falls back down a
    /// tier once they've climbed it. Fixing that needs the durable, time-indexed store from
    /// ROADMAP phase 1 — an in-memory counter can't age entries out across restarts it doesn't
    /// survive anyway.
    pub fn record_volume(&self, agent_id: &str, notional_usd: f64) {
        self.wal.append(&[crate::wal::Record::Volume {
            agent_id: agent_id.to_string(),
            notional: notional_usd,
        }]);
        self.record_volume_nolog(agent_id, notional_usd);
    }

    /// Applies without logging. For callers batching a whole fill into one atomic WAL append —
    /// see api.rs::apply_fill. Using this without appending the matching record loses the change
    /// on restart.
    pub fn record_volume_nolog(&self, agent_id: &str, notional_usd: f64) {
        *self.volume_usd.lock().unwrap().entry(agent_id.to_string()).or_insert(0.0) +=
            notional_usd;
    }

    pub fn trailing_volume(&self, agent_id: &str) -> f64 {
        self.volume_usd.lock().unwrap().get(agent_id).copied().unwrap_or(0.0)
    }

    pub fn add_protocol_fee(&self, asset: &str, amount: f64) {
        self.wal.append(&[crate::wal::Record::Fee { asset: asset.to_string(), amount }]);
        self.add_protocol_fee_nolog(asset, amount);
    }

    /// See `record_volume_nolog`.
    pub fn add_protocol_fee_nolog(&self, asset: &str, amount: f64) {
        *self.fee_ledger.lock().unwrap().entry(asset.to_string()).or_insert(0.0) += amount;
    }

    pub fn fee_ledger_snapshot(&self) -> Vec<(String, f64)> {
        self.fee_ledger.lock().unwrap().iter().map(|(k, v)| (k.clone(), *v)).collect()
    }
}

#[cfg(test)]
mod concurrency_tests {
    use super::*;
    use std::sync::Arc;

    fn open_test_market(state: &AppState, id: &str, now: i64) {
        let mut m = crate::prediction::Market {
            market_id: id.to_string(),
            question: "Concurrency?".into(),
            outcomes: vec!["YES".into(), "NO".into()],
            resolution: crate::prediction::ResolutionSpec::OperatorDeclared {
                criteria: "test".into(),
                source: "test".into(),
            },
            asset: crate::api::POINTS_ASSET.to_string(),
            closes_at_ms: now + 3_600_000,
            observed_at_ms: now + 3_660_000,
            dispute_window_ms: 0,
            commitment: String::new(),
            status: crate::prediction::MarketStatus::Open,
            proposal: None,
            winning_outcome: None,
            created_at_ms: now,
        disputed_at_ms: None,
        };
        m.commitment = m.compute_commitment();
        state.open_market(m);
    }

    /// Two threads spending the same balance at the same moment must not both succeed.
    ///
    /// This is the classic check-then-act hole and it is the one that turns into free money: if
    /// the balance check and the debit are not under a single lock, N concurrent stakes can each
    /// pass a check against the same funds and the account goes negative.
    #[test]
    fn concurrent_stakes_cannot_overdraw_one_balance() {
        let state = Arc::new(AppState::new());
        let now = 1_800_000_000_000;
        open_test_market(&state, "mkt_race", now);
        state.adjust_balance("greedy", crate::api::POINTS_ASSET, 100.0);

        // Twenty threads each try to stake the entire balance.
        let mut handles = Vec::new();
        for _ in 0..20 {
            let s = Arc::clone(&state);
            handles.push(std::thread::spawn(move || {
                s.place_stake("mkt_race", "greedy", 0, 100.0, now).is_ok()
            }));
        }
        let wins = handles.into_iter().filter(|_| true).map(|h| h.join().unwrap()).filter(|ok| *ok).count();

        assert_eq!(wins, 1, "exactly one stake of the whole balance may succeed, {wins} did");
        let left = state.get_balance("greedy", crate::api::POINTS_ASSET);
        assert!(left >= 0.0, "balance went negative: {left}");
        assert!((left - 0.0).abs() < 1e-9, "balance should be exactly spent, was {left}");

        let staked: f64 = state
            .stakes
            .lock()
            .unwrap()
            .get("mkt_race")
            .map(|v| v.iter().map(|s| s.amount).sum())
            .unwrap_or(0.0);
        assert!((staked - 100.0).abs() < 1e-9, "pool holds {staked}, not the 100 debited");
    }

    /// A market must pay out exactly once even if several threads resolve it together.
    #[test]
    fn concurrent_settlement_cannot_pay_twice() {
        let state = Arc::new(AppState::new());
        let now = 1_800_000_000_000;
        open_test_market(&state, "mkt_double", now);
        for who in ["a", "b"] {
            state.adjust_balance(who, crate::api::POINTS_ASSET, 100.0);
        }
        state.place_stake("mkt_double", "a", 0, 100.0, now).unwrap();
        state.place_stake("mkt_double", "b", 1, 100.0, now).unwrap();

        let mut handles = Vec::new();
        for _ in 0..10 {
            let s = Arc::clone(&state);
            handles.push(std::thread::spawn(move || s.resolve_market("mkt_double", Some(0)).is_some()));
        }
        let settlements = handles.into_iter().map(|h| h.join().unwrap()).filter(|ok| *ok).count();
        assert_eq!(settlements, 1, "{settlements} threads settled the same market");

        let paid = state.get_balance("a", crate::api::POINTS_ASSET);
        let total_out = paid + state.get_balance("b", crate::api::POINTS_ASSET);
        assert!(
            total_out <= 200.0 + 1e-9,
            "payouts totalled {total_out} from a 200 pool — money was created"
        );
    }

    /// Concurrent withdrawals must not be able to jointly exceed the reserves backing them.
    #[test]
    fn concurrent_withdrawals_cannot_break_the_reserve_invariant() {
        let mut base = AppState::new();
        // Withdrawals are refused outright unless redeemable value is switched on, so without
        // this the test would pass for the wrong reason — nothing raced because nothing ran.
        base.real_money_enabled = true;
        let state = Arc::new(base);
        let asset = crate::credits::CREDITS;
        state.credit_reserves(asset, 100.0);
        state.adjust_balance("w", asset, 100.0);

        let mut handles = Vec::new();
        for i in 0..15 {
            let s = Arc::clone(&state);
            handles.push(std::thread::spawn(move || {
                s.request_withdrawal("w", asset, 100.0, &format!("addr{i}"), 1_800_000_000_000)
                    .is_ok()
            }));
        }
        let ok = handles.into_iter().map(|h| h.join().unwrap()).filter(|o| *o).count();
        assert_eq!(ok, 1, "{ok} withdrawals of the whole balance succeeded");

        let balances = state.balances.lock().unwrap();
        let withdrawals = state.withdrawals.lock().unwrap();
        let reserves = state.reserves.lock().unwrap();
        let shortfall =
            crate::credits::reserve_shortfall(&balances, &withdrawals, &reserves, asset);
        assert!(shortfall <= 1e-9, "reserves came up {shortfall} short");
    }

    #[test]
    fn pruning_never_forgets_a_market_that_still_owes_someone() {
        let state = AppState::new();
        let now = 1_800_000_000_000;
        // Ten settled markets and four in every non-final state.
        for i in 0..10 {
            open_test_market(&state, &format!("done{i}"), now + i);
            state.markets.lock().unwrap().get_mut(&format!("done{i}")).unwrap().status =
                crate::prediction::MarketStatus::Resolved;
        }
        for (i, status) in [
            crate::prediction::MarketStatus::Open,
            crate::prediction::MarketStatus::Closed,
            crate::prediction::MarketStatus::Proposed,
            crate::prediction::MarketStatus::Disputed,
        ]
        .into_iter()
        .enumerate()
        {
            let id = format!("live{i}");
            open_test_market(&state, &id, now - 1_000_000); // older than every settled one
            state.markets.lock().unwrap().get_mut(&id).unwrap().status = status;
        }

        let dropped = state.prune_settled_markets(3);
        assert_eq!(dropped, 7, "should drop all but the newest three settled");

        let markets = state.markets.lock().unwrap();
        assert_eq!(markets.len(), 3 + 4, "live markets must survive at any age");
        for i in 0..4 {
            assert!(
                markets.contains_key(&format!("live{i}")),
                "live{i} was evicted — that is an unpaid obligation forgotten"
            );
        }
        // The three kept must be the newest.
        for i in 7..10 {
            assert!(markets.contains_key(&format!("done{i}")), "done{i} should have been kept");
        }
    }

    #[test]
    fn pruning_drops_the_stakes_with_the_market() {
        let state = AppState::new();
        let now = 1_800_000_000_000;
        for i in 0..5 {
            let id = format!("m{i}");
            open_test_market(&state, &id, now + i);
            state.adjust_balance("a", crate::api::POINTS_ASSET, 100.0);
            state.place_stake(&id, "a", 0, 100.0, now).unwrap();
            state.markets.lock().unwrap().get_mut(&id).unwrap().status =
                crate::prediction::MarketStatus::Resolved;
        }
        assert_eq!(state.stakes.lock().unwrap().len(), 5);
        state.prune_settled_markets(2);
        assert_eq!(
            state.stakes.lock().unwrap().len(),
            2,
            "stake vectors must go with their markets or the leak just moves"
        );
    }

    #[test]
    fn pruning_is_a_no_op_below_the_limit() {
        let state = AppState::new();
        let now = 1_800_000_000_000;
        for i in 0..3 {
            open_test_market(&state, &format!("m{i}"), now + i);
            state.markets.lock().unwrap().get_mut(&format!("m{i}")).unwrap().status =
                crate::prediction::MarketStatus::Resolved;
        }
        assert_eq!(state.prune_settled_markets(1_000), 0);
        assert_eq!(state.markets.lock().unwrap().len(), 3);
    }

    #[test]
    fn a_subscriber_that_stops_reading_is_dropped_rather_than_buffered() {
        // The leak this guards: with an unbounded channel, a client that connects and never reads
        // grows the server forever, and the dead-subscriber prune never fires because send on an
        // unbounded channel does not fail.
        let state = AppState::new();
        let (tx, rx) = std::sync::mpsc::sync_channel::<Vec<u8>>(AppState::TICK_QUEUE_DEPTH);
        state.tick_subscribers.lock().unwrap().push(tx);

        // Never read from rx: this is the stalled client.
        for _ in 0..(AppState::TICK_QUEUE_DEPTH * 4) {
            state.broadcast_tick(vec![0u8; 34]);
        }

        assert!(
            state.tick_subscribers.lock().unwrap().is_empty(),
            "a subscriber that cannot keep up must be dropped, not queued for indefinitely"
        );
        // And what it did receive is bounded by the queue depth, not by how much we sent.
        let mut received = 0;
        while rx.try_recv().is_ok() {
            received += 1;
        }
        assert!(
            received <= AppState::TICK_QUEUE_DEPTH,
            "buffered {received} frames, cap is {}",
            AppState::TICK_QUEUE_DEPTH
        );
    }

    #[test]
    fn a_subscriber_that_keeps_up_is_never_dropped() {
        let state = AppState::new();
        let (tx, rx) = std::sync::mpsc::sync_channel::<Vec<u8>>(AppState::TICK_QUEUE_DEPTH);
        state.tick_subscribers.lock().unwrap().push(tx);
        for _ in 0..(AppState::TICK_QUEUE_DEPTH * 4) {
            state.broadcast_tick(vec![1u8; 34]);
            rx.try_recv().expect("a keeping-up client receives every frame");
        }
        assert_eq!(
            state.tick_subscribers.lock().unwrap().len(),
            1,
            "the fix must not disconnect healthy clients"
        );
    }


    /// Two takers racing the same offer: exactly one gets it, and only one is charged.
    ///
    /// The failure this guards is the expensive one. Without the claim and the debit under a
    /// single lock, both takers could pass the "is it open?" check, both would be debited, and
    /// the venue would hold three stakes for a two-sided bet — with one person's money in a
    /// market they are not in.
    #[test]
    fn only_one_taker_can_win_a_race_for_the_same_offer() {
        use std::sync::Arc;
        let state = Arc::new(AppState::new());
        let now = 1_800_000_000_000;
        let asset = crate::api::POINTS_ASSET;

        state.adjust_balance("proposer", asset, 1_000.0);
        for i in 0..12 {
            state.adjust_balance(&format!("taker{i}"), asset, 1_000.0);
        }

        let c = crate::challenge::Challenge {
            challenge_id: "ch_race".into(),
            proposer: "proposer".into(),
            question: "Race?".into(),
            outcomes: ["YES".into(), "NO".into()],
            proposer_outcome: 0,
            proposer_stake: 100.0,
            taker_stake: 100.0,
            asset: asset.to_string(),
            resolution: crate::prediction::ResolutionSpec::OperatorDeclared {
                criteria: "c".into(),
                source: "s".into(),
            },
            closes_at_ms: now + 3_600_000,
            observed_at_ms: now + 3_660_000,
            dispute_window_ms: 0,
            expires_at_ms: now + 600_000,
            created_at_ms: now,
            status: crate::challenge::ChallengeStatus::Open,
            taker: None,
            market_id: None,
        };
        state.open_challenge(c, now).expect("offer should post");

        let mut handles = Vec::new();
        for i in 0..12 {
            let s = Arc::clone(&state);
            handles.push(std::thread::spawn(move || {
                s.accept_challenge("ch_race", &format!("taker{i}"), now).is_ok()
            }));
        }
        let winners = handles.into_iter().filter(|_| true).map(|h| h.join().unwrap()).filter(|o| *o).count();
        assert_eq!(winners, 1, "{winners} takers matched the same offer");

        // Exactly one taker was charged.
        let charged = (0..12)
            .filter(|i| state.get_balance(&format!("taker{i}"), asset) < 1_000.0 - 1e-9)
            .count();
        assert_eq!(charged, 1, "{charged} takers were debited for one bet");

        // And the market holds exactly the two stakes.
        let market_id = state.challenges.get("ch_race").unwrap().market_id.expect("matched");
        let pool: f64 = state
            .stakes
            .lock()
            .unwrap()
            .get(&market_id)
            .map(|v| v.iter().map(|s| s.amount).sum())
            .unwrap_or(0.0);
        assert!((pool - 200.0).abs() < 1e-9, "market holds {pool}, expected exactly the two stakes");
    }

    /// A withdrawal racing the expiry sweeper must not refund the proposer twice.
    #[test]
    fn an_untaken_offer_is_only_ever_refunded_once() {
        use std::sync::Arc;
        let state = Arc::new(AppState::new());
        let now = 1_800_000_000_000;
        let asset = crate::api::POINTS_ASSET;
        state.adjust_balance("p", asset, 1_000.0);

        let c = crate::challenge::Challenge {
            challenge_id: "ch_once".into(),
            proposer: "p".into(),
            question: "Once?".into(),
            outcomes: ["YES".into(), "NO".into()],
            proposer_outcome: 0,
            proposer_stake: 400.0,
            taker_stake: 400.0,
            asset: asset.to_string(),
            resolution: crate::prediction::ResolutionSpec::OperatorDeclared {
                criteria: "c".into(),
                source: "s".into(),
            },
            closes_at_ms: now + 3_600_000,
            observed_at_ms: now + 3_660_000,
            dispute_window_ms: 0,
            expires_at_ms: now + 600_000,
            created_at_ms: now,
            status: crate::challenge::ChallengeStatus::Open,
            taker: None,
            market_id: None,
        };
        state.open_challenge(c, now).expect("offer should post");
        assert!((state.get_balance("p", asset) - 600.0).abs() < 1e-9);

        let mut handles = Vec::new();
        for i in 0..10 {
            let s = Arc::clone(&state);
            handles.push(std::thread::spawn(move || {
                let status = if i % 2 == 0 {
                    crate::challenge::ChallengeStatus::Withdrawn
                } else {
                    crate::challenge::ChallengeStatus::Expired
                };
                s.release_challenge("ch_once", None, status).is_ok()
            }));
        }
        let released = handles.into_iter().map(|h| h.join().unwrap()).filter(|o| *o).count();
        assert_eq!(released, 1, "{released} threads refunded the same offer");
        assert!(
            (state.get_balance("p", asset) - 1_000.0).abs() < 1e-9,
            "balance is {} — the stake was refunded more than once",
            state.get_balance("p", asset)
        );
    }

}

#[cfg(test)]
mod pool_cache_tests {
    use super::*;

    /// The cache decides the odds every caller sees, so it must never disagree with the stakes
    /// that decide the payouts. Anything else is a venue quoting one thing and paying another.
    #[test]
    fn pools_match_the_stakes_after_heavy_use() {
        let state = AppState::new();
        let now = crate::api::now_ms_pub();
        let mut market = crate::prediction::Market {
            market_id: "m1".into(),
            question: "does the cache hold?".into(),
            outcomes: vec!["A".into(), "B".into(), "C".into()],
            resolution: crate::prediction::ResolutionSpec::OperatorDeclared {
                criteria: "x".into(),
                source: "y".into(),
            },
            asset: crate::api::POINTS_ASSET.to_string(),
            closes_at_ms: now + 600_000,
            observed_at_ms: now + 700_000,
            dispute_window_ms: 0,
            commitment: String::new(),
            status: crate::prediction::MarketStatus::Open,
            proposal: None,
            winning_outcome: None,
            created_at_ms: now,
            disputed_at_ms: None,
        };
        market.commitment = market.compute_commitment();
        state.open_market(market);

        let mut expected = vec![0.0f64; 3];
        let mut seed = 12345u64;
        for i in 0..3000 {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            let outcome = (seed >> 33) as usize % 3;
            let amount = 1.0 + ((seed >> 20) % 500) as f64 / 100.0;
            let agent = format!("a{}", i % 40);
            state.adjust_balance(&agent, crate::api::POINTS_ASSET, amount);
            if state.place_stake("m1", &agent, outcome, amount, now).is_ok() {
                expected[outcome] += amount;
            }
        }

        let cached = state.pools_for("m1", 3);
        let stakes = state.stakes.lock().unwrap().get("m1").cloned().unwrap_or_default();
        let recomputed = crate::prediction::pools(&stakes, 3);
        for i in 0..3 {
            assert!(
                (cached[i] - recomputed[i]).abs() < 1e-6,
                "outcome {i}: cache says {} but the stakes say {}",
                cached[i], recomputed[i]
            );
            assert!((cached[i] - expected[i]).abs() < 1e-6, "outcome {i} drifted from the truth");
        }
        assert_eq!(stakes.len(), 3000, "every stake landed");
    }

    /// A cold cache — a market recovered from the log, say — must produce the same answer, just
    /// more slowly. A miss is allowed to be slow; it is not allowed to be wrong or empty.
    #[test]
    fn a_cold_cache_recomputes_rather_than_reporting_zero() {
        let state = AppState::new();
        let now = crate::api::now_ms_pub();
        let mut market = crate::prediction::Market {
            market_id: "m2".into(),
            question: "cold?".into(),
            outcomes: vec!["A".into(), "B".into()],
            resolution: crate::prediction::ResolutionSpec::OperatorDeclared {
                criteria: "x".into(),
                source: "y".into(),
            },
            asset: crate::api::POINTS_ASSET.to_string(),
            closes_at_ms: now + 600_000,
            observed_at_ms: now + 700_000,
            dispute_window_ms: 0,
            commitment: String::new(),
            status: crate::prediction::MarketStatus::Open,
            proposal: None,
            winning_outcome: None,
            created_at_ms: now,
            disputed_at_ms: None,
        };
        market.commitment = market.compute_commitment();
        state.open_market(market);
        state.adjust_balance("z", crate::api::POINTS_ASSET, 100.0);
        state.place_stake("m2", "z", 1, 40.0, now).expect("stake lands");

        state.pool_cache.lock().unwrap().clear();
        let pools = state.pools_for("m2", 2);
        assert_eq!(pools[1], 40.0, "a cold cache reported {pools:?} instead of the real pool");
        // And the miss must have populated it.
        assert_eq!(state.pools_for("m2", 2)[1], 40.0);
    }
}

#[cfg(test)]
mod invariant_tests {
    use super::*;
    use std::collections::HashSet;

    /// Random sequences of real operations, checking after every single step that the things
    /// which must always be true still are.
    ///
    /// # Why this is different from the other tests
    ///
    /// Every other test here asks "does this operation do the right thing?" That catches the bugs
    /// somebody thought to look for. Money bugs are usually not in one operation — they are in an
    /// *interleaving*: a settlement racing a stake, a withdrawal against a market that has not
    /// paid out yet, a void after a dispute after a re-proposal. There are too many orderings to
    /// enumerate by hand, so this generates them and checks the invariants rather than the steps.
    ///
    /// The three invariants, none of which may ever be false, at any point, for any sequence:
    ///
    /// 1. **No balance is negative.** The engine may never let anyone spend money they do not
    ///    have, however the operations are ordered.
    /// 2. **Value is conserved.** Everything credited in must equal what is held in balances, plus
    ///    what is locked in open pools, plus what the house took as rake. Money is never created
    ///    and never quietly disappears.
    /// 3. **Reserves cover claims.** For the redeemable asset, outstanding balances plus pending
    ///    withdrawals never exceed reserves held.
    struct Model {
        credited_in: f64,
        agents: Vec<String>,
        markets: Vec<String>,
        challenges: Vec<String>,
    }

    fn check_invariants(state: &AppState, model: &Model, step: usize, seed: u64) {
        let asset = crate::api::POINTS_ASSET;

        // 1. Nobody is negative.
        {
            let balances = state.balances.lock().unwrap();
            for ((who, a), bal) in balances.iter() {
                assert!(
                    *bal >= -1e-9,
                    "seed {seed} step {step}: {who} holds {bal} of {a}"
                );
                assert!(bal.is_finite(), "seed {seed} step {step}: {who} holds {bal}");
            }
        }

        // 2. Value is conserved: balances + money locked in unsettled pools + rake == credited in.
        let held: f64 = {
            let balances = state.balances.lock().unwrap();
            balances
                .iter()
                .filter(|((_, a), _)| a == asset)
                .map(|(_, v)| *v)
                .sum()
        };
        let locked: f64 = {
            let markets = state.markets.lock().unwrap();
            let stakes = state.stakes.lock().unwrap();
            markets
                .values()
                .filter(|m| !m.status.is_final() && m.asset == asset)
                .map(|m| {
                    stakes
                        .get(&m.market_id)
                        .map(|v| v.iter().map(|s| s.amount).sum::<f64>())
                        .unwrap_or(0.0)
                })
                .sum()
        };
        let rake: f64 = state
            .fee_ledger_snapshot()
            .iter()
            .filter(|(a, _)| a == asset)
            .map(|(_, v)| *v)
            .sum();

        // Money sitting in an untaken offer is in neither a balance nor a market. If challenges
        // ever failed to refund, or double-charged, or lost a stake between posting and matching,
        // it would show up here as value that does not add up — which is exactly why this term
        // exists rather than the challenge paths simply being trusted.
        let escrowed: f64 = state
            .challenges
            .open_escrow(asset);

        let total = held + locked + rake + escrowed;
        assert!(
            (total - model.credited_in).abs() < 1e-6,
            "seed {seed} step {step}: {} credited in but balances {held} + locked {locked} + \
             rake {rake} + escrowed {escrowed} = {total}",
            model.credited_in
        );
    }

    fn run_sequence(seed: u64, steps: usize) {
        let mut rng = seed;
        let mut next = move || {
            rng ^= rng << 13;
            rng ^= rng >> 7;
            rng ^= rng << 17;
            rng
        };

        let state = AppState::new();
        let mut model =
            Model { credited_in: 0.0, agents: Vec::new(), markets: Vec::new(), challenges: Vec::new() };
        let base_now = 1_800_000_000_000i64;
        let mut clock = base_now;
        let asset = crate::api::POINTS_ASSET;

        for i in 0..8 {
            let id = format!("agent{i}");
            state.adjust_balance(&id, asset, 1_000.0);
            model.credited_in += 1_000.0;
            model.agents.push(id);
        }

        for step in 0..steps {
            // Time moves forward erratically, so markets close and become observable at
            // unpredictable points relative to the operations around them.
            clock += (next() % 90_000) as i64;

            match next() % 100 {
                // Open a market.
                0..=17 => {
                    let id = format!("mkt{step}");
                    let mut m = crate::prediction::Market {
                        market_id: id.clone(),
                        question: format!("Q{step}?"),
                        outcomes: vec!["YES".into(), "NO".into(), "MAYBE".into()]
                            [..2 + (next() % 2) as usize]
                            .to_vec(),
                        resolution: crate::prediction::ResolutionSpec::OperatorDeclared {
                            criteria: "c".into(),
                            source: "s".into(),
                        },
                        asset: asset.to_string(),
                        closes_at_ms: clock + 1 + (next() % 200_000) as i64,
                        observed_at_ms: 0,
                        dispute_window_ms: 0,
                        commitment: String::new(),
                        status: crate::prediction::MarketStatus::Open,
                        proposal: None,
                        winning_outcome: None,
                        created_at_ms: clock,
        disputed_at_ms: None,
                    };
                    m.observed_at_ms = m.closes_at_ms + 1;
                    m.commitment = m.compute_commitment();
                    state.open_market(m);
                    model.markets.push(id);
                }
                // Stake.
                18..=64 => {
                    if model.markets.is_empty() {
                        continue;
                    }
                    let who = &model.agents[(next() as usize) % model.agents.len()];
                    let mkt = &model.markets[(next() as usize) % model.markets.len()];
                    let outcome = (next() % 3) as usize;
                    let amount = ((next() % 30_000) as f64) / 100.0;
                    let _ = state.place_stake(mkt, who, outcome, amount, clock);
                }
                // Settle or void.
                65..=88 => {
                    if model.markets.is_empty() {
                        continue;
                    }
                    let mkt = model.markets[(next() as usize) % model.markets.len()].clone();
                    let outcome = if next() % 5 == 0 { None } else { Some((next() % 2) as usize) };
                    let _ = state.resolve_market(&mkt, outcome);
                }
                // Credit more in, so the conserved total is not static.
                89..=94 => {
                    let who = model.agents[(next() as usize) % model.agents.len()].clone();
                    let amount = ((next() % 50_000) as f64) / 100.0;
                    state.adjust_balance(&who, asset, amount);
                    model.credited_in += amount;
                }
                // Post, take, and abandon head-to-head offers, so the escrow path is inside the
                // random interleaving rather than only in its own happy-path test.
                95..=97 => {
                    let proposer = model.agents[(next() as usize) % model.agents.len()].clone();
                    let stake = ((next() % 20_000) as f64) / 100.0;
                    let id = format!("chal{step}");
                    let c = crate::challenge::Challenge {
                        challenge_id: id.clone(),
                        proposer,
                        question: format!("C{step}?"),
                        outcomes: ["YES".into(), "NO".into()],
                        proposer_outcome: (next() % 2) as usize,
                        proposer_stake: stake.max(0.01),
                        taker_stake: (((next() % 20_000) as f64) / 100.0).max(0.01),
                        asset: asset.to_string(),
                        resolution: crate::prediction::ResolutionSpec::OperatorDeclared {
                            criteria: "c".into(),
                            source: "s".into(),
                        },
                        closes_at_ms: clock + 500_000,
                        observed_at_ms: clock + 560_000,
                        dispute_window_ms: 0,
                        expires_at_ms: clock + 1 + (next() % 400_000) as i64,
                        created_at_ms: clock,
                        status: crate::challenge::ChallengeStatus::Open,
                        taker: None,
                        market_id: None,
                    };
                    if state.open_challenge(c, clock).is_ok() {
                        model.challenges.push(id);
                    }
                }
                98 => {
                    if model.challenges.is_empty() {
                        continue;
                    }
                    let id = model.challenges[(next() as usize) % model.challenges.len()].clone();
                    let taker = model.agents[(next() as usize) % model.agents.len()].clone();
                    if let Ok((_, mkt)) = state.accept_challenge(&id, &taker, clock) {
                        model.markets.push(mkt);
                    }
                }
                // Propose and dispute, to exercise the frozen-payout path.
                _ => {
                    // Expired offers must be released, or their money is stranded — the same
                    // sweep the running server does.
                    for c in state.challenges.expired_unmatched(clock) {
                        let _ = state.release_challenge(
                            &c.challenge_id,
                            None,
                            crate::challenge::ChallengeStatus::Expired,
                        );
                    }
                    if model.markets.is_empty() {
                        continue;
                    }
                    let mkt = model.markets[(next() as usize) % model.markets.len()].clone();
                    let _ = state.propose_outcome(&mkt, Some(0), "evidence".into(), true, clock);
                    let who = model.agents[(next() as usize) % model.agents.len()].clone();
                    let _ = state.dispute_outcome(&mkt, &who, "disagree");
                }
            }

            check_invariants(&state, &model, step, seed);
        }

        // Every market that settled must have paid out to a subset of the people who staked in it.
        let markets = state.markets.lock().unwrap();
        let ids: HashSet<&String> = markets.keys().collect();
        assert!(!ids.is_empty() || steps < 10, "seed {seed}: no markets were ever created");
    }

    #[test]
    fn money_is_conserved_across_many_random_operation_sequences() {
        // Scaled to hundreds of thousands of operations. The invariants are checked after
        // *every* step, so this is roughly a million assertions over the money paths — which is
        // the only honest way to cover an interleaving space this large.
        for seed in 1..=800u64 {
            run_sequence(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15), 400);
        }
    }

    #[test]
    fn a_long_single_run_stays_consistent() {
        // One very long sequence, so states only reachable after a lot of history get exercised.
        run_sequence(0xDEAD_BEEF_CAFE_1234, 60_000);
    }
}

impl AppState {
    /// Posts a head-to-head offer, taking the proposer's money now.
    ///
    /// Debited immediately, on purpose. An offer whose author cannot cover it is worse than no
    /// offer: the board fills with bets that evaporate when someone tries to take them, and the
    /// taker finds out only after deciding they wanted it. Taking the money up front makes every
    /// listed offer real.
    pub fn open_challenge(
        &self,
        mut c: crate::challenge::Challenge,
        now_ms: i64,
    ) -> Result<crate::challenge::Challenge, crate::challenge::ChallengeError> {
        c.validate(now_ms)?;
        if !self.asset_is_stakeable(&c.asset) {
            return Err(crate::challenge::ChallengeError::Malformed(
                "that asset is not enabled on this deployment",
            ));
        }

        {
            let mut balances = self.balances.lock().unwrap();
            let key = (c.proposer.clone(), c.asset.clone());
            let have = balances.get(&key).copied().unwrap_or(0.0);
            if have < c.proposer_stake {
                return Err(crate::challenge::ChallengeError::InsufficientBalance {
                    have,
                    need: c.proposer_stake,
                });
            }
            *balances.entry(key).or_insert(0.0) -= c.proposer_stake;
        }

        self.wal.append(&[
            crate::wal::Record::Balance {
                agent_id: c.proposer.clone(),
                asset: c.asset.clone(),
                delta: -c.proposer_stake,
            },
            crate::wal::Record::ChallengeOpened {
                challenge_id: c.challenge_id.clone(),
                proposer: c.proposer.clone(),
                asset: c.asset.clone(),
                stake: c.proposer_stake,
            },
        ]);

        self.challenges.insert(c.clone());
        Ok(c)
    }

    /// Takes the other side. Both stakes are now locked and the bet is live.
    ///
    /// This is the moment a challenge becomes an ordinary market — same commitment, same
    /// lifecycle, same settlement engine, same log. There is no second payout path to keep
    /// correct; a head-to-head bet is a pool that happens to have two stakes in it.
    pub fn accept_challenge(
        &self,
        challenge_id: &str,
        taker: &str,
        now_ms: i64,
    ) -> Result<(crate::challenge::Challenge, String), crate::challenge::ChallengeError> {
        let c = self
            .challenges
            .get(challenge_id)
            .ok_or(crate::challenge::ChallengeError::NotFound)?;

        if c.status != crate::challenge::ChallengeStatus::Open {
            return Err(crate::challenge::ChallengeError::NotOpen);
        }
        if c.is_expired(now_ms) {
            return Err(crate::challenge::ChallengeError::Expired);
        }
        if c.proposer == taker {
            return Err(crate::challenge::ChallengeError::CannotAcceptOwn);
        }

        // Debit the taker and flip the offer to matched under one balance lock, so two takers
        // racing the same offer cannot both succeed — one gets the bet, the other a clean refusal.
        let market_id = format!(
            "mkt_{}",
            crate::crypto::hex_encode(&crate::crypto::random_bytes_pooled(8))
        );
        {
            let mut balances = self.balances.lock().unwrap();
            let key = (taker.to_string(), c.asset.clone());
            let have = balances.get(&key).copied().unwrap_or(0.0);
            if have < c.taker_stake {
                return Err(crate::challenge::ChallengeError::InsufficientBalance {
                    have,
                    need: c.taker_stake,
                });
            }

            let mut claimed = false;
            self.challenges.update(challenge_id, |ch| {
                if ch.status == crate::challenge::ChallengeStatus::Open {
                    ch.status = crate::challenge::ChallengeStatus::Matched;
                    ch.taker = Some(taker.to_string());
                    ch.market_id = Some(market_id.clone());
                    claimed = true;
                }
            });
            if !claimed {
                return Err(crate::challenge::ChallengeError::NotOpen);
            }
            *balances.entry(key).or_insert(0.0) -= c.taker_stake;
        }

        let market = c.to_market(market_id.clone(), now_ms);
        let proposer_stake = crate::prediction::Stake {
            agent_id: c.proposer.clone(),
            outcome_idx: c.proposer_outcome,
            amount: c.proposer_stake,
            placed_at_ms: c.created_at_ms,
        };
        let taker_stake = crate::prediction::Stake {
            agent_id: taker.to_string(),
            outcome_idx: c.taker_outcome(),
            amount: c.taker_stake,
            placed_at_ms: now_ms,
        };

        self.wal.append(&[
            crate::wal::Record::Balance {
                agent_id: taker.to_string(),
                asset: c.asset.clone(),
                delta: -c.taker_stake,
            },
            crate::wal::Record::ChallengeMatched {
                challenge_id: challenge_id.to_string(),
                taker: taker.to_string(),
                market_id: market_id.clone(),
            },
        ]);

        // open_market logs the market and announces it on the event stream.
        self.open_market(market);
        {
            let mut stakes = self.stakes.lock().unwrap();
            let entry = stakes.entry(market_id.clone()).or_default();
            entry.push(proposer_stake.clone());
            entry.push(taker_stake.clone());
        }
        self.wal.append(&[
            crate::wal::Record::StakePlaced {
                market_id: market_id.clone(),
                agent_id: c.proposer.clone(),
                outcome_idx: c.proposer_outcome as u32,
                amount: c.proposer_stake,
                placed_at_ms: c.created_at_ms,
            },
            crate::wal::Record::StakePlaced {
                market_id: market_id.clone(),
                agent_id: taker.to_string(),
                outcome_idx: c.taker_outcome() as u32,
                amount: c.taker_stake,
                placed_at_ms: now_ms,
            },
        ]);

        let updated = self.challenges.get(challenge_id).unwrap_or(c);
        Ok((updated, market_id))
    }

    /// Refunds an offer nobody took, in full, with no rake.
    ///
    /// Nothing was settled, so the house earned nothing. Used both by the proposer withdrawing and
    /// by the sweeper cleaning up expired offers.
    pub fn release_challenge(
        &self,
        challenge_id: &str,
        by: Option<&str>,
        status: crate::challenge::ChallengeStatus,
    ) -> Result<crate::challenge::Challenge, crate::challenge::ChallengeError> {
        let c = self
            .challenges
            .get(challenge_id)
            .ok_or(crate::challenge::ChallengeError::NotFound)?;
        if let Some(who) = by {
            if who != c.proposer {
                return Err(crate::challenge::ChallengeError::NotYours);
            }
        }

        // Flip the status first and only refund if this call is the one that did it, so a
        // withdrawal racing the expiry sweeper cannot pay the proposer twice.
        let mut released = false;
        self.challenges.update(challenge_id, |ch| {
            if ch.status == crate::challenge::ChallengeStatus::Open {
                ch.status = status;
                released = true;
            }
        });
        if !released {
            return Err(crate::challenge::ChallengeError::NotOpen);
        }

        self.adjust_balance(&c.proposer, &c.asset, c.proposer_stake);
        self.wal.append(&[crate::wal::Record::ChallengeReleased {
            challenge_id: challenge_id.to_string(),
            reason: status.label().to_string(),
        }]);
        Ok(self.challenges.get(challenge_id).unwrap_or(c))
    }
}
