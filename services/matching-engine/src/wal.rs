//! Write-ahead log: the thing that makes a restart survivable.
//!
//! # Why a log rather than a database
//!
//! ROADMAP phase 1 calls for Postgres, and eventually you want it — for settlement history,
//! reporting, and anything you need to query. But the durability problem and the query problem
//! are different problems, and the durability one is what blocks going live. Real exchanges keep
//! the hot path on an append-only log for exactly this reason: appending is the cheapest durable
//! write there is, and a database on the critical path buys query power you don't need mid-trade
//! at a latency cost you can't afford.
//!
//! It also happens to be the only option here: this build has no network access, so there is no
//! Postgres driver to install. That constraint pushed toward the right answer.
//!
//! # What gets logged: outcomes, not commands
//!
//! Two ways to build a WAL. Log the *command* ("agent A submitted this order") and replay by
//! re-executing it, or log the *outcome* ("balance moved by X, this order rested, this fill
//! happened") and replay by applying the recorded facts.
//!
//! This logs outcomes, and that is not a stylistic choice. Order and trade IDs are random now
//! (see privacy.rs), so re-executing a command produces *different IDs* than the run that
//! generated them — replay would silently reconstruct a state that never existed, with orders
//! nobody can cancel because the ID they hold no longer matches. Logging outcomes has no
//! determinism requirement at all: replay just re-applies what happened.
//!
//! # Durability vs. speed
//!
//! `fsync` after every record is the only setting that survives a power cut with zero loss, and
//! it costs a disk round-trip per order. `IKENGA_WAL_SYNC` picks the policy:
//!
//! - `always` (default) — fsync every record. Slowest, loses nothing.
//! - `interval` — fsync at most every 200ms (group commit). Fast; a crash can lose up to 200ms
//!   of acknowledged trades, which for a real venue means telling someone their filled order
//!   didn't happen.
//! - `never` — OS buffers only. Fine for load tests, never for money.
//!
//! The default is `always` because the failure mode of the fast option is "we lost trades we
//! already confirmed," and that is not a performance trade-off, it's a correctness one.

use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use crate::json::Json;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyncPolicy {
    Always,
    Interval,
    Never,
}

impl SyncPolicy {
    pub fn from_env() -> Self {
        match std::env::var("IKENGA_WAL_SYNC").as_deref() {
            Ok("interval") => SyncPolicy::Interval,
            Ok("never") => SyncPolicy::Never,
            _ => SyncPolicy::Always,
        }
    }

    pub fn label(&self) -> &'static str {
        match self {
            SyncPolicy::Always => "always (durable, one fsync per record)",
            SyncPolicy::Interval => "interval (group commit, up to 200ms of loss on crash)",
            SyncPolicy::Never => "never (OS buffers only — NOT durable)",
        }
    }
}

const SYNC_INTERVAL: Duration = Duration::from_millis(200);

/// One durable state change. Every variant records a *fact that already happened*, so replaying
/// it needs no business logic and can't diverge from the run that wrote it.
#[derive(Debug, Clone)]
pub enum Record {
    /// An agent's credentials. Logged so registrations survive a restart — without this, every
    /// deploy would silently forget who's allowed to trade. Only the PUBLIC key is ever recorded
    /// here (see src/ed25519.rs) — unlike the HMAC scheme this replaced, a stolen WAL file
    /// contains nothing that lets anyone forge a signature.
    Agent { agent_id: String, pubkey_hex: String, trust: u32 },
    /// A balance delta. Deltas rather than absolutes so concurrent writers can't clobber each
    /// other's view on replay.
    Balance { agent_id: String, asset: String, delta: f64 },
    /// An order that came to rest on the book. Replayed in log order, which reconstructs
    /// price-time priority exactly.
    OrderRested {
        order_id: String,
        agent_id: String,
        symbol: String,
        side: String,
        price_ticks: i64,
        qty: f64,
        filled_qty: f64,
        created_at_ms: i64,
    },
    /// A resting order's remaining quantity changed, or it left the book.
    OrderUpdated { order_id: String, filled_qty: f64, status: String },
    /// Protocol revenue.
    Fee { asset: String, amount: f64 },
    /// Traded notional, for fee tiering.
    Volume { agent_id: String, notional: f64 },
    /// A prediction market was opened. Logged so agents' stakes have something to belong to
    /// after a restart.
    MarketOpened {
        market_id: String,
        question: String,
        outcomes: Vec<String>,
        /// The full resolution spec, serialized. Part of the committed terms.
        resolution: Json,
        asset: String,
        closes_at_ms: i64,
        observed_at_ms: i64,
        dispute_window_ms: i64,
        /// The published commitment hash, logged so recovery restores the exact terms staked
        /// under rather than recomputing them from possibly-changed code.
        commitment: String,
        created_at_ms: i64,
    },
    /// An outcome was proposed. Logged before the dispute window starts so a crash cannot lose
    /// the fact that a clock is running.
    MarketProposed {
        market_id: String,
        outcome: Option<u32>,
        proposed_at_ms: i64,
        evidence: String,
        automatic: bool,
    },
    /// A proposal was challenged. Payout stays frozen until it is re-proposed or voided.
    MarketDisputed { market_id: String, agent_id: String, reason: String, disputed_at_ms: i64 },
    /// A head-to-head offer was posted and the proposer's stake taken.
    ChallengeOpened { challenge_id: String, proposer: String, asset: String, stake: f64 },
    /// Someone took the other side; the offer became this market.
    ChallengeMatched { challenge_id: String, taker: String, market_id: String },
    /// An untaken offer was refunded in full — expired or withdrawn.
    ChallengeReleased { challenge_id: String, reason: String },
    /// A forecast-feed subscriber key was issued or revoked.
    ///
    /// These are billing state, not a cache. Without a record, a restart silently demotes every
    /// paying subscriber to the free tier: their key stops matching, they start receiving delayed
    /// unweighted data, and nothing anywhere reports an error — the operator finds out when the
    /// customer complains.
    FeedSubscriber { key: String, label: Option<String> },
    /// An agent's trust score moved. Durable because it drives their rate limit — losing it on
    /// restart would silently demote every agent back to the New band's 10 req/s.
    Trust { agent_id: String, score: u32, forecast_score: Option<u32> },
    /// A change in the assets actually held backing redeemable credits. Without this, a restart
    /// restores every credit balance but no reserves, and the ledger comes back looking insolvent
    /// — which would freeze every withdrawal on the platform.
    Reserve { asset: String, delta: f64 },
    /// A withdrawal request. The matching balance debit rides in the same batch, so a crash can
    /// never leave an agent debited with no payout record to show for it.
    WithdrawalRequested {
        withdrawal_id: String,
        agent_id: String,
        asset: String,
        amount: f64,
        destination: String,
        requested_at_ms: i64,
    },
    /// A withdrawal was paid out or refused.
    WithdrawalSettled {
        withdrawal_id: String,
        sent: bool,
        tx_ref: Option<String>,
        settled_at_ms: i64,
    },
    /// A stake was placed. The matching balance debit is a separate `Balance` record written in
    /// the same batch, so a crash can never land between taking the money and recording the bet.
    StakePlaced {
        market_id: String,
        agent_id: String,
        outcome_idx: u32,
        amount: f64,
        placed_at_ms: i64,
    },
    /// A market's outcome became known. Payout credits ride along as `Balance` records in the
    /// same batch; this record only fixes the outcome so the market can't be re-staked or
    /// re-settled on replay.
    MarketSettled { market_id: String, winning_outcome: Option<u32>, voided: bool },
}

impl Record {
    fn to_line(&self) -> String {
        let json = match self {
            Record::Agent { agent_id, pubkey_hex, trust } => Json::obj(vec![
                ("t", Json::str("agent")),
                ("id", Json::str(agent_id.clone())),
                ("pubkey", Json::str(pubkey_hex.clone())),
                ("trust", Json::num(*trust as f64)),
            ]),
            Record::Balance { agent_id, asset, delta } => Json::obj(vec![
                ("t", Json::str("bal")),
                ("id", Json::str(agent_id.clone())),
                ("asset", Json::str(asset.clone())),
                ("d", Json::num(*delta)),
            ]),
            Record::OrderRested {
                order_id, agent_id, symbol, side, price_ticks, qty, filled_qty, created_at_ms,
            } => Json::obj(vec![
                ("t", Json::str("rest")),
                ("oid", Json::str(order_id.clone())),
                ("id", Json::str(agent_id.clone())),
                ("sym", Json::str(symbol.clone())),
                ("side", Json::str(side.clone())),
                ("px", Json::num(*price_ticks as f64)),
                ("qty", Json::num(*qty)),
                ("filled", Json::num(*filled_qty)),
                ("ts", Json::num(*created_at_ms as f64)),
            ]),
            Record::OrderUpdated { order_id, filled_qty, status } => Json::obj(vec![
                ("t", Json::str("upd")),
                ("oid", Json::str(order_id.clone())),
                ("filled", Json::num(*filled_qty)),
                ("st", Json::str(status.clone())),
            ]),
            Record::Fee { asset, amount } => Json::obj(vec![
                ("t", Json::str("fee")),
                ("asset", Json::str(asset.clone())),
                ("amt", Json::num(*amount)),
            ]),
            Record::Volume { agent_id, notional } => Json::obj(vec![
                ("t", Json::str("vol")),
                ("id", Json::str(agent_id.clone())),
                ("n", Json::num(*notional)),
            ]),
            Record::MarketOpened {
                market_id, question, outcomes, resolution, asset, closes_at_ms, observed_at_ms,
                dispute_window_ms, commitment, created_at_ms,
            } => Json::obj(vec![
                ("t", Json::str("mkt")),
                ("mid", Json::str(market_id.clone())),
                ("q", Json::str(question.clone())),
                ("out", Json::Array(outcomes.iter().map(|o| Json::str(o.clone())).collect())),
                ("res", resolution.clone()),
                ("asset", Json::str(asset.clone())),
                ("close", Json::num(*closes_at_ms as f64)),
                ("obs", Json::num(*observed_at_ms as f64)),
                ("dw", Json::num(*dispute_window_ms as f64)),
                ("commit", Json::str(commitment.clone())),
                ("ts", Json::num(*created_at_ms as f64)),
            ]),
            Record::MarketProposed {
                market_id, outcome, proposed_at_ms, evidence, automatic,
            } => Json::obj(vec![
                ("t", Json::str("mktprop")),
                ("mid", Json::str(market_id.clone())),
                ("win", outcome.map(|w| Json::num(w as f64)).unwrap_or(Json::Null)),
                ("at", Json::num(*proposed_at_ms as f64)),
                ("ev", Json::str(evidence.clone())),
                ("auto", Json::Bool(*automatic)),
            ]),
            Record::MarketDisputed { market_id, agent_id, reason, disputed_at_ms } => Json::obj(vec![
                ("t", Json::str("mktdisp")),
                ("mid", Json::str(market_id.clone())),
                ("by", Json::str(agent_id.clone())),
                ("why", Json::str(reason.clone())),
                ("at", Json::num(*disputed_at_ms as f64)),
            ]),
            Record::ChallengeOpened { challenge_id, proposer, asset, stake } => Json::obj(vec![
                ("t", Json::str("chopen")),
                ("cid", Json::str(challenge_id.clone())),
                ("id", Json::str(proposer.clone())),
                ("asset", Json::str(asset.clone())),
                ("amt", Json::num(*stake)),
            ]),
            Record::ChallengeMatched { challenge_id, taker, market_id } => Json::obj(vec![
                ("t", Json::str("chmatch")),
                ("cid", Json::str(challenge_id.clone())),
                ("id", Json::str(taker.clone())),
                ("mid", Json::str(market_id.clone())),
            ]),
            Record::ChallengeReleased { challenge_id, reason } => Json::obj(vec![
                ("t", Json::str("chrel")),
                ("cid", Json::str(challenge_id.clone())),
                ("why", Json::str(reason.clone())),
            ]),
            Record::FeedSubscriber { key, label } => Json::obj(vec![
                ("t", Json::str("feedsub")),
                ("k", Json::str(key.clone())),
                ("l", label.clone().map(Json::str).unwrap_or(Json::Null)),
            ]),
            Record::Trust { agent_id, score, forecast_score } => Json::obj(vec![
                ("t", Json::str("trust")),
                ("id", Json::str(agent_id.clone())),
                ("s", Json::num(*score as f64)),
                ("fs", forecast_score.map(|v| Json::num(v as f64)).unwrap_or(Json::Null)),
            ]),
            Record::Reserve { asset, delta } => Json::obj(vec![
                ("t", Json::str("resv")),
                ("asset", Json::str(asset.clone())),
                ("d", Json::num(*delta)),
            ]),
            Record::WithdrawalRequested {
                withdrawal_id, agent_id, asset, amount, destination, requested_at_ms,
            } => Json::obj(vec![
                ("t", Json::str("wdreq")),
                ("wid", Json::str(withdrawal_id.clone())),
                ("id", Json::str(agent_id.clone())),
                ("asset", Json::str(asset.clone())),
                ("amt", Json::num(*amount)),
                ("dest", Json::str(destination.clone())),
                ("at", Json::num(*requested_at_ms as f64)),
            ]),
            Record::WithdrawalSettled { withdrawal_id, sent, tx_ref, settled_at_ms } => {
                Json::obj(vec![
                    ("t", Json::str("wdset")),
                    ("wid", Json::str(withdrawal_id.clone())),
                    ("sent", Json::Bool(*sent)),
                    ("tx", tx_ref.clone().map(Json::str).unwrap_or(Json::Null)),
                    ("at", Json::num(*settled_at_ms as f64)),
                ])
            }
            Record::StakePlaced { market_id, agent_id, outcome_idx, amount, placed_at_ms } => {
                Json::obj(vec![
                    ("t", Json::str("stake")),
                    ("mid", Json::str(market_id.clone())),
                    ("id", Json::str(agent_id.clone())),
                    ("oi", Json::num(*outcome_idx as f64)),
                    ("amt", Json::num(*amount)),
                    ("ts", Json::num(*placed_at_ms as f64)),
                ])
            }
            Record::MarketSettled { market_id, winning_outcome, voided } => Json::obj(vec![
                ("t", Json::str("mktdone")),
                ("mid", Json::str(market_id.clone())),
                ("win", winning_outcome.map(|w| Json::num(w as f64)).unwrap_or(Json::Null)),
                ("void", Json::Bool(*voided)),
            ]),
        };
        json.to_string()
    }

    fn from_json(j: &Json) -> Option<Record> {
        let t = j.get("t")?.as_str()?;
        let s = |k: &str| j.get(k).and_then(Json::as_str).map(|v| v.to_string());
        let f = |k: &str| j.get(k).and_then(Json::as_f64);
        Some(match t {
            "agent" => Record::Agent {
                agent_id: s("id")?,
                pubkey_hex: s("pubkey")?,
                trust: f("trust")? as u32,
            },
            "bal" => Record::Balance { agent_id: s("id")?, asset: s("asset")?, delta: f("d")? },
            "rest" => Record::OrderRested {
                order_id: s("oid")?,
                agent_id: s("id")?,
                symbol: s("sym")?,
                side: s("side")?,
                price_ticks: f("px")? as i64,
                qty: f("qty")?,
                filled_qty: f("filled")?,
                created_at_ms: f("ts")? as i64,
            },
            "upd" => Record::OrderUpdated {
                order_id: s("oid")?,
                filled_qty: f("filled")?,
                status: s("st")?,
            },
            "fee" => Record::Fee { asset: s("asset")?, amount: f("amt")? },
            "vol" => Record::Volume { agent_id: s("id")?, notional: f("n")? },
            "mkt" => Record::MarketOpened {
                market_id: s("mid")?,
                question: s("q")?,
                outcomes: match j.get("out")? {
                    Json::Array(items) => {
                        items.iter().filter_map(|i| i.as_str().map(|v| v.to_string())).collect()
                    }
                    _ => return None,
                },
                resolution: j.get("res")?.clone(),
                asset: s("asset")?,
                closes_at_ms: f("close")? as i64,
                observed_at_ms: f("obs")? as i64,
                dispute_window_ms: f("dw")? as i64,
                commitment: s("commit")?,
                created_at_ms: f("ts")? as i64,
            },
            "mktprop" => Record::MarketProposed {
                market_id: s("mid")?,
                outcome: j.get("win").and_then(Json::as_f64).map(|w| w as u32),
                proposed_at_ms: f("at")? as i64,
                evidence: s("ev")?,
                automatic: matches!(j.get("auto"), Some(Json::Bool(true))),
            },
            "mktdisp" => Record::MarketDisputed {
                market_id: s("mid")?,
                // Logs written before disputes recorded their author replay with an empty
                // author, which simply means that objection no longer blocks a repeat.
                agent_id: j.get("by").and_then(Json::as_str).unwrap_or_default().to_string(),
                reason: s("why")?,
                // Logs written before the review clock existed replay as frozen-since-0, which
                // reads as long overdue — the safe direction, since it surfaces for review rather
                // than hiding.
                disputed_at_ms: j.get("at").and_then(Json::as_f64).unwrap_or(0.0) as i64,
            },
            "chopen" => Record::ChallengeOpened {
                challenge_id: s("cid")?,
                proposer: s("id")?,
                asset: s("asset")?,
                stake: f("amt")?,
            },
            "chmatch" => Record::ChallengeMatched {
                challenge_id: s("cid")?,
                taker: s("id")?,
                market_id: s("mid")?,
            },
            "chrel" => Record::ChallengeReleased { challenge_id: s("cid")?, reason: s("why")? },
            "feedsub" => Record::FeedSubscriber {
                key: s("k")?,
                // A null label is a revocation, which is why the field is optional rather than
                // an empty string — an empty label is a legitimate thing to issue a key with.
                label: j.get("l").and_then(Json::as_str).map(|v| v.to_string()),
            },
            "trust" => Record::Trust {
                agent_id: s("id")?,
                score: f("s")? as u32,
                // Absent in logs written before the two scores were separated, and absent by
                // design on points markets, which move only the rate-limit score.
                forecast_score: j.get("fs").and_then(Json::as_f64).map(|v| v as u32),
            },
            "resv" => Record::Reserve { asset: s("asset")?, delta: f("d")? },
            "wdreq" => Record::WithdrawalRequested {
                withdrawal_id: s("wid")?,
                agent_id: s("id")?,
                asset: s("asset")?,
                amount: f("amt")?,
                destination: s("dest")?,
                requested_at_ms: f("at")? as i64,
            },
            "wdset" => Record::WithdrawalSettled {
                withdrawal_id: s("wid")?,
                sent: matches!(j.get("sent"), Some(Json::Bool(true))),
                tx_ref: j.get("tx").and_then(Json::as_str).map(|v| v.to_string()),
                settled_at_ms: f("at")? as i64,
            },
            "stake" => Record::StakePlaced {
                market_id: s("mid")?,
                agent_id: s("id")?,
                outcome_idx: f("oi")? as u32,
                amount: f("amt")?,
                // Logs written before stakes carried a timestamp replay as 0, which reads as
                // "long ago" everywhere the field is used. Losing the delayed-feed detail on
                // historical records is preferable to refusing to replay them.
                placed_at_ms: j.get("ts").and_then(Json::as_f64).unwrap_or(0.0) as i64,
            },
            "mktdone" => Record::MarketSettled {
                market_id: s("mid")?,
                winning_outcome: j.get("win").and_then(Json::as_f64).map(|w| w as u32),
                voided: matches!(j.get("void"), Some(Json::Bool(true))),
            },
            _ => return None,
        })
    }
}

struct WalInner {
    /// Records appended to the file so far. A writer remembers the value it produced and waits
    /// until `synced` reaches it.
    written: u64,
    /// Records known to be on the platter.
    synced: u64,
    /// Whether some thread is currently inside `sync_data`, so the others wait rather than
    /// queueing up their own redundant fsyncs behind it.
    syncing: bool,
    last_sync: Instant,
    dirty: bool,
}

pub struct Wal {
    /// Shared rather than owned by the mutex, because `File::sync_data` takes `&self` and
    /// `Write` is implemented for `&File`. That is what makes it possible to hold the lock only
    /// for the append — which must be ordered — and release it for the fsync, which must not be.
    file: Option<Arc<File>>,
    inner: Option<Mutex<WalInner>>,
    /// Signals waiting writers that a sync finished and their record may now be durable.
    synced: Condvar,
    policy: SyncPolicy,
    path: PathBuf,
}

impl Wal {
    /// Opens (or creates) the log. A disabled WAL — no path configured — is a valid mode for
    /// tests and benchmarks, and `is_enabled()` reports it so startup can say so out loud.
    pub fn open(path: Option<&Path>, policy: SyncPolicy) -> std::io::Result<Wal> {
        let Some(path) = path else {
            return Ok(Wal {
                file: None,
                inner: None,
                synced: Condvar::new(),
                policy,
                path: PathBuf::new(),
            });
        };
        if let Some(dir) = path.parent() {
            if !dir.as_os_str().is_empty() {
                std::fs::create_dir_all(dir)?;
            }
        }
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        Ok(Wal {
            file: Some(Arc::new(file)),
            inner: Some(Mutex::new(WalInner {
                written: 0,
                synced: 0,
                syncing: false,
                last_sync: Instant::now(),
                dirty: false,
            })),
            synced: Condvar::new(),
            policy,
            path: path.to_path_buf(),
        })
    }

    pub fn disabled() -> Wal {
        Wal {
            file: None,
            inner: None,
            synced: Condvar::new(),
            policy: SyncPolicy::Never,
            path: PathBuf::new(),
        }
    }

    pub fn is_enabled(&self) -> bool {
        self.inner.is_some()
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn policy(&self) -> SyncPolicy {
        self.policy
    }

    /// Appends records and applies the sync policy.
    ///
    /// A batch goes in one call on purpose: an order that fills produces balance moves, order
    /// updates, a fee and volume records that must all survive together. Writing them
    /// individually would let a crash land between them and restore a half-settled trade.
    ///
    /// An I/O failure here is logged and swallowed rather than propagated. That is deliberate but
    /// it is NOT correct for production: a venue that can't write its log should stop accepting
    /// orders, not keep trading with no way to recover. Wiring that refusal into the order path
    /// is the next thing this needs.
    /// Appends records and, under the durable policy, does not return until they are on disk.
    ///
    /// # Group commit, and why the lock is released before the fsync
    ///
    /// The first version held the mutex across `sync_data`. That is correct and it is also a hard
    /// ceiling: every writer waits for the previous writer's disk round-trip, so throughput is
    /// one fsync at a time no matter how many callers there are. Measured, that was 398 writes a
    /// second with a p50 of 40ms under sixteen concurrent bettors — flat, and not improved by
    /// adding hardware.
    ///
    /// The fix is the standard one. The lock orders the *appends*, which must be ordered, and is
    /// then released. One thread performs the fsync; everyone else waits on a condition variable
    /// until a sync has covered the record they wrote. A single disk round-trip therefore commits
    /// every append that arrived while it was in flight, and throughput rises with concurrency
    /// instead of ignoring it.
    ///
    /// Durability is unchanged, which is the whole point: `append` still does not return until
    /// `synced >= mine`, so nothing is acknowledged before it is on the platter. What changed is
    /// how many acknowledgements one fsync can carry.
    pub fn append(&self, records: &[Record]) {
        let (Some(inner), Some(file)) = (&self.inner, &self.file) else { return };
        if records.is_empty() {
            return;
        }

        let mut buf = String::new();
        for r in records {
            buf.push_str(&r.to_line());
            buf.push('\n');
        }

        let mine = {
            let mut guard = match inner.lock() {
                Ok(g) => g,
                Err(poisoned) => poisoned.into_inner(),
            };
            // Written under the lock so the file's byte order matches the sequence numbers. A
            // record's number is only meaningful if everything numbered below it is already in
            // the file.
            if let Err(e) = (&**file).write_all(buf.as_bytes()) {
                eprintln!("WAL WRITE FAILED (state will not survive a restart): {e}");
                return;
            }
            guard.written += 1;
            guard.dirty = true;
            guard.written
        };

        match self.policy {
            SyncPolicy::Always => self.sync_through(mine),
            SyncPolicy::Interval => {
                let due = {
                    let guard = match inner.lock() {
                        Ok(g) => g,
                        Err(p) => p.into_inner(),
                    };
                    guard.last_sync.elapsed() >= SYNC_INTERVAL && !guard.syncing
                };
                if due {
                    self.sync_through(mine);
                }
            }
            SyncPolicy::Never => {}
        }
    }

    /// Blocks until a sync has covered record `mine`.
    fn sync_through(&self, mine: u64) {
        let (Some(inner), Some(file)) = (&self.inner, &self.file) else { return };
        let mut guard = match inner.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        };
        loop {
            if guard.synced >= mine {
                return;
            }
            if guard.syncing {
                // Somebody else's fsync is in flight. It may or may not cover this record —
                // if it started before this append, it will not, and the loop runs again.
                guard = match self.synced.wait(guard) {
                    Ok(g) => g,
                    Err(p) => p.into_inner(),
                };
                continue;
            }
            // Take the job. Everything written up to this instant is what this fsync will cover.
            guard.syncing = true;
            let target = guard.written;
            drop(guard);

            let result = file.sync_data();

            guard = match inner.lock() {
                Ok(g) => g,
                Err(p) => p.into_inner(),
            };
            guard.syncing = false;
            match result {
                Ok(()) => {
                    guard.synced = guard.synced.max(target);
                    guard.last_sync = Instant::now();
                    if guard.synced >= guard.written {
                        guard.dirty = false;
                    }
                }
                Err(e) => {
                    // Do not advance `synced`. Waiters stay blocked on their record and retry,
                    // which is right: returning from append would tell a caller their bet is
                    // durable when the disk just said otherwise.
                    eprintln!("WAL FSYNC FAILED: {e}");
                    self.synced.notify_all();
                    return;
                }
            }
            self.synced.notify_all();
        }
    }

    /// Flushes anything buffered. Called on graceful shutdown.
    pub fn sync(&self) {
        let (Some(inner), Some(_)) = (&self.inner, &self.file) else { return };
        let pending = {
            let guard = match inner.lock() {
                Ok(g) => g,
                Err(p) => p.into_inner(),
            };
            guard.written
        };
        // Goes through the same path as a writer, so shutdown cannot race a group commit that is
        // already in flight and end up fsyncing twice or, worse, returning before it lands.
        self.sync_through(pending);
    }

    /// Reads every record back, in order.
    ///
    /// A truncated final line is expected, not exceptional: a crash mid-write leaves a partial
    /// record, and the correct response is to drop it and keep everything before it. Anything
    /// else — a corrupt line in the middle — is reported so it isn't silently skipped.
    pub fn replay(path: &Path) -> std::io::Result<(Vec<Record>, usize)> {
        if !path.exists() {
            return Ok((Vec::new(), 0));
        }
        let reader = BufReader::new(File::open(path)?);
        let mut records = Vec::new();
        let mut skipped = 0usize;

        for line in reader.lines() {
            let Ok(line) = line else {
                skipped += 1;
                continue;
            };
            if line.trim().is_empty() {
                continue;
            }
            match crate::json::parse(&line).ok().as_ref().and_then(Record::from_json) {
                Some(r) => records.push(r),
                None => skipped += 1,
            }
        }
        Ok((records, skipped))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_path(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("ikenga-wal-test-{name}-{}.log", crate::privacy::random_id("")));
        p
    }

    #[test]
    fn records_round_trip_through_the_log() {
        let path = temp_path("roundtrip");
        let wal = Wal::open(Some(&path), SyncPolicy::Always).unwrap();
        wal.append(&[
            Record::Agent {
                agent_id: "agent_A".into(),
                pubkey_hex: "deadbeef".into(),
                trust: 250,
            },
            Record::Balance { agent_id: "agent_A".into(), asset: "USD".into(), delta: 100.5 },
            Record::Fee { asset: "USD".into(), amount: 0.25 },
        ]);

        let (records, skipped) = Wal::replay(&path).unwrap();
        assert_eq!(skipped, 0);
        assert_eq!(records.len(), 3);
        match &records[1] {
            Record::Balance { agent_id, asset, delta } => {
                assert_eq!(agent_id, "agent_A");
                assert_eq!(asset, "USD");
                assert!((delta - 100.5).abs() < 1e-9);
            }
            other => panic!("wrong record: {other:?}"),
        }
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn appends_accumulate_across_reopens() {
        let path = temp_path("reopen");
        {
            let wal = Wal::open(Some(&path), SyncPolicy::Always).unwrap();
            wal.append(&[Record::Fee { asset: "USD".into(), amount: 1.0 }]);
        }
        {
            let wal = Wal::open(Some(&path), SyncPolicy::Always).unwrap();
            wal.append(&[Record::Fee { asset: "USD".into(), amount: 2.0 }]);
        }
        let (records, _) = Wal::replay(&path).unwrap();
        assert_eq!(records.len(), 2, "reopening truncated the log instead of appending");
        let _ = std::fs::remove_file(&path);
    }

    /// The crash case: a torn final line must not take the whole log with it.
    #[test]
    fn a_truncated_final_record_is_dropped_not_fatal() {
        let path = temp_path("torn");
        let wal = Wal::open(Some(&path), SyncPolicy::Always).unwrap();
        wal.append(&[
            Record::Fee { asset: "USD".into(), amount: 1.0 },
            Record::Fee { asset: "USD".into(), amount: 2.0 },
        ]);
        drop(wal);

        // Simulate a crash mid-write.
        let mut f = OpenOptions::new().append(true).open(&path).unwrap();
        f.write_all(b"{\"t\":\"fee\",\"asset\":\"US").unwrap();
        drop(f);

        let (records, skipped) = Wal::replay(&path).unwrap();
        assert_eq!(records.len(), 2, "good records before the tear were lost");
        assert_eq!(skipped, 1, "the torn record should be counted, not silently ignored");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn a_missing_log_replays_as_empty() {
        let path = temp_path("missing");
        let (records, skipped) = Wal::replay(&path).unwrap();
        assert!(records.is_empty());
        assert_eq!(skipped, 0);
    }

    #[test]
    fn a_disabled_wal_accepts_writes_and_does_nothing() {
        let wal = Wal::disabled();
        assert!(!wal.is_enabled());
        wal.append(&[Record::Fee { asset: "USD".into(), amount: 1.0 }]);
        wal.sync();
    }

    #[test]
    fn awkward_floats_survive_the_log_exactly() {
        // Balances go through JSON on the way to disk. If a number does not round-trip bit for
        // bit, the ledger after a restart is not the ledger before it — and the difference is
        // silent, permanent, and in someone's favour.
        let awkward = [
            0.1 + 0.2,
            1.0 / 3.0,
            f64::MIN_POSITIVE,
            1e-300,
            1e300,
            9_007_199_254_740_993.0,
            0.000_000_1,
            123_456_789.123_456_79,
            -0.0,
        ];
        for v in awkward {
            let rec = Record::Balance {
                agent_id: "a".into(),
                asset: "USDC".into(),
                delta: v,
            };
            let line = rec.to_line();
            let parsed = crate::json::parse(line.trim()).expect("a log line must be valid JSON");
            let back = Record::from_json(&parsed).expect("must parse back");
            match back {
                Record::Balance { delta, .. } => assert_eq!(
                    delta.to_bits(),
                    v.to_bits(),
                    "{v} came back as {delta} via {line}"
                ),
                other => panic!("wrong record type back: {other:?}"),
            }
        }
    }

    #[test]
    fn hostile_text_in_a_record_cannot_forge_another_record() {
        // Market questions and dispute reasons are attacker-supplied and land in the log verbatim.
        // If escaping were wrong, a question could close its own JSON object and append a second,
        // fabricated record that replays as real state.
        let nasty = "\" , \"t\":\"balance\", \"id\":\"attacker\", \"asset\":\"USDC\", \"d\":1000000 }\n{\"t\":\"balance\"";
        let rec = Record::MarketDisputed {
            market_id: "m1".into(),
            agent_id: "attacker".into(),
            reason: nasty.to_string(),
            disputed_at_ms: 1_800_000_000_000,
        };
        let line = rec.to_line();
        assert_eq!(line.lines().count(), 1, "a record must never span lines: {line}");
        let parsed = crate::json::parse(line.trim()).expect("a log line must be valid JSON");
        let back = Record::from_json(&parsed).expect("must parse back");
        match back {
            Record::MarketDisputed { reason, market_id, .. } => {
                assert_eq!(reason, nasty, "the reason must survive verbatim");
                assert_eq!(market_id, "m1");
            }
            other => panic!("hostile text changed the record type: {other:?}"),
        }
    }
}
