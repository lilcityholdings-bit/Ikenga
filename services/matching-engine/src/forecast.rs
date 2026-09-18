//! The forecast data product: what gets packaged and sold, and what deliberately never leaves.
//!
//! # What is actually valuable here
//!
//! A prediction market's exhaust is a stream of probability estimates produced by participants
//! who are paying to be right. That is a *costly signal* — unlike a poll, a survey or a sentiment
//! score, a wrong answer costs the person giving it money. Costly signals are the reason market
//! prices carry information that opinion does not, and it is the only genuinely scarce thing this
//! platform produces.
//!
//! Better still, it comes with its own scorecard. Every market resolves against reality, so the
//! feed can be sold with a published, checkable accuracy record attached rather than a promise.
//! Almost nobody selling a "predictive signal" can show you their historical Brier score.
//!
//! # What is never in it
//!
//! No agent identities. No per-agent positions. No balances, no wallet addresses, no IPs, no
//! request logs, no anything that resolves to a participant. Not as a policy the operator
//! remembers to follow — the aggregation functions below take stakes and emit statistics, and
//! there is no code path from a `Snapshot` back to who staked what.
//!
//! That is not only an ethical position, it is a commercial one. Participants are agents whose
//! operators are choosing where to route capital, and a venue that resells their positions is a
//! venue they stop using — and one whose signal degrades as the informed money leaves. The data
//! product and the market are the same asset; selling out one destroys the other.
//!
//! # The three things worth selling
//!
//! 1. **Consensus probabilities.** Where the money sits, per market, over time. The raw signal.
//! 2. **Calibration-weighted consensus.** The same thing, but weighting each stake by how well
//!    that participant has forecast in the past. This is the differentiated product: a naive
//!    pool average treats a proven forecaster and a coin-flipper identically, and this does not.
//! 3. **The track record.** Resolved markets with the consensus at close against what actually
//!    happened, so a buyer can price the signal instead of trusting it.

use std::collections::HashMap;

use crate::json::Json;
use crate::prediction::{Market, MarketStatus, Stake};

/// One market's aggregate state, with nothing in it that identifies a participant.
#[derive(Debug, Clone, PartialEq)]
pub struct MarketSignal {
    pub market_id: String,
    pub question: String,
    pub outcomes: Vec<String>,
    /// Where the money sits, normalised. The raw crowd forecast.
    pub consensus: Vec<f64>,
    /// The same, weighting each stake by the staker's demonstrated calibration.
    pub weighted_consensus: Vec<f64>,
    /// Total staked. A size signal — a 50/50 split of $2 means much less than a 50/50 split of
    /// $2M, and a buyer needs to be able to tell those apart.
    pub total_pool: f64,
    /// How many distinct participants. Deliberately a count, never a list.
    pub participants: usize,
    /// Share of the pool held by its largest single account, 0.0–1.0. Published so a buyer can
    /// tell a thousand-agent consensus from one whale with a view.
    pub top_holder_share: f64,
    pub status: String,
    /// What the pool is denominated in. Published because it changes what the number is worth:
    /// a consensus backed by redeemable money is a costly signal, one backed by free promotional
    /// points is a survey.
    pub asset: String,
    pub closes_at_ms: i64,
    pub observed_at_ms: i64,
    /// Set once resolved, so the record is self-scoring.
    pub resolved_outcome: Option<usize>,
}

/// Weight a stake by its owner's demonstrated calibration.
///
/// A trust score of 0 does not mean "ignore" — a new participant with no record still carries
/// their own money, and money is the signal. It means "count only what they staked". Proven
/// forecasters count for more, up to double, and nobody counts for less than their stake.
///
/// The cap matters: without it, one highly-trusted agent could dominate the weighted consensus
/// and the product would be selling one opinion dressed as a crowd.
/// The largest share of a market's pool any single account may contribute to the published
/// signal. Not a limit on staking — a limit on influence over the number being sold.
pub const MAX_AGENT_INFLUENCE: f64 = 0.25;

pub fn calibration_weight(trust_score: u32) -> f64 {
    1.0 + (trust_score.min(1000) as f64 / 1000.0)
}


/// The largest contribution any one account may make to the published signal, in pool units.
///
/// Clamping each account to a fixed fraction of the *gross* pool is not enough: cap a whale
/// holding 90% down to 25% of the gross and, once everyone else's small stakes are normalised
/// against that reduced total, the whale still reads as 71% of the signal. The cap has to be on
/// the share of the *published* number, which is self-referential — the ceiling depends on the
/// total, and the total depends on the ceiling.
///
/// Solved directly rather than by iterating. If the `m` largest accounts end up capped at `C`
/// and the rest contribute `P` between them, then `C = cap × (P + m·C)`, so
/// `C = cap·P / (1 − cap·m)`. Walking `m` upward from zero and taking the first value consistent
/// with the sorted positions gives the exact fixed point in one pass.
///
/// The cap is raised to `1/k` for `k` accounts, because asking each of three participants to be
/// at most a quarter of the answer has no solution. With four or more accounts it is the flat
/// 25%; below that it degrades gracefully to an even split.
fn influence_ceiling(totals: &[f64]) -> f64 {
    let k = totals.len();
    if k == 0 {
        return f64::MAX;
    }
    let cap = MAX_AGENT_INFLUENCE.max(1.0 / k as f64);
    let mut sorted: Vec<f64> = totals.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

    for m in 0..k {
        let denom = 1.0 - cap * m as f64;
        if denom <= 1e-12 {
            break;
        }
        let uncapped: f64 = sorted[..k - m].iter().sum();
        let c = cap * uncapped / denom;
        // Consistent when every account we assumed uncapped really is under the ceiling, and
        // every account we assumed capped really is over it.
        let below_fits = c + 1e-9 >= sorted[k - m - 1];
        let above_fits = m == 0 || c <= sorted[k - m] + 1e-9;
        if below_fits && above_fits {
            return c;
        }
    }
    f64::MAX
}

/// Aggregates one market into a sellable signal. Takes a trust lookup rather than the store
/// itself so this stays a pure function and can be tested without a running platform.
pub fn signal_for_market(
    market: &Market,
    stakes: &[Stake],
    trust_of: &dyn Fn(&str) -> u32,
    now_ms: i64,
) -> MarketSignal {
    signal_as_of(market, stakes, trust_of, now_ms, i64::MAX)
}

/// The same aggregation, restricted to stakes placed at or before `as_of_ms`.
///
/// This is what makes the delayed tier honest. Hiding recent *markets* is not a delay: a market
/// opened yesterday would still publish this second's consensus for free, and nobody pays for a
/// live feed they can already read. Reconstructing the pool as it stood at `as_of_ms` delays the
/// *number*, which is the thing being sold.
pub fn signal_as_of(
    market: &Market,
    stakes: &[Stake],
    trust_of: &dyn Fn(&str) -> u32,
    now_ms: i64,
    as_of_ms: i64,
) -> MarketSignal {
    let stakes: Vec<Stake> = stakes.iter().filter(|s| s.placed_at_ms <= as_of_ms).cloned().collect();
    let stakes = stakes.as_slice();
    let n = market.outcomes.len();
    let mut raw = vec![0.0; n];
    let mut weighted = vec![0.0; n];
    let mut participants: Vec<&str> = Vec::new();

    // Sum each agent's position first, so influence can be capped per account rather than per
    // stake — otherwise splitting one bet into a hundred would walk straight around the cap.
    let mut by_agent: Vec<(&str, Vec<f64>)> = Vec::new();
    for s in stakes {
        if s.outcome_idx >= n {
            continue;
        }
        raw[s.outcome_idx] += s.amount;
        if !participants.contains(&s.agent_id.as_str()) {
            participants.push(&s.agent_id);
        }
        match by_agent.iter_mut().find(|(a, _)| *a == s.agent_id.as_str()) {
            Some((_, v)) => v[s.outcome_idx] += s.amount,
            None => {
                let mut v = vec![0.0; n];
                v[s.outcome_idx] = s.amount;
                by_agent.push((s.agent_id.as_str(), v));
            }
        }
    }

    // Cap how far any one account can move the *published* signal.
    //
    // The pool pays out strictly pro rata — that is the contract and it is not touched here. But
    // the feed is a product, and a pari-mutuel pool is unusually cheap to steer: bet both sides
    // from two accounts and the only cost is the rake on whichever side loses, so a few hundred
    // dollars can set what the consensus *reads* on a market. Whoever is trading on that number
    // elsewhere is the mark. Capping each account's contribution at a fixed share of the total
    // means moving the published figure requires moving genuinely separate money, which is the
    // expensive thing sybils are trying to avoid.
    //
    // Below the cap this is exactly the money-weighted consensus. It bites only on pools that one
    // account already dominates — pools whose unadjusted number was not worth much anyway.
    let totals: Vec<f64> = by_agent.iter().map(|(_, v)| v.iter().sum()).collect();
    let ceiling = influence_ceiling(&totals);
    for ((agent, position), total) in by_agent.iter().zip(totals.iter()) {
        let scale = if *total > ceiling && *total > 0.0 { ceiling / total } else { 1.0 };
        let w = calibration_weight(trust_of(agent));
        for (i, amount) in position.iter().enumerate() {
            weighted[i] += amount * scale * w;
        }
    }

    let total: f64 = raw.iter().sum();
    let weighted_total: f64 = weighted.iter().sum();
    let normalise = |v: &[f64], t: f64| -> Vec<f64> {
        if t > 0.0 {
            v.iter().map(|x| x / t).collect()
        } else {
            // No stakes means no opinion. Emitting a uniform prior here would publish a
            // confident-looking 50/50 for a market nobody has touched, which is worse than
            // publishing nothing.
            vec![0.0; v.len()]
        }
    };

    MarketSignal {
        market_id: market.market_id.clone(),
        question: market.question.clone(),
        outcomes: market.outcomes.clone(),
        consensus: normalise(&raw, total),
        weighted_consensus: normalise(&weighted, weighted_total),
        total_pool: total,
        participants: participants.len(),
        top_holder_share: if total > 0.0 {
            by_agent
                .iter()
                .map(|(_, v)| v.iter().sum::<f64>() / total)
                .fold(0.0, f64::max)
        } else {
            0.0
        },
        status: market.effective_status(now_ms).label().to_string(),
        asset: market.asset.clone(),
        closes_at_ms: market.closes_at_ms,
        observed_at_ms: market.observed_at_ms,
        resolved_outcome: market.winning_outcome,
    }
}

impl MarketSignal {
    pub fn to_json(&self) -> Json {
        Json::obj(vec![
            ("market_id", Json::str(self.market_id.clone())),
            ("question", Json::str(self.question.clone())),
            (
                "outcomes",
                Json::Array(
                    self.outcomes
                        .iter()
                        .enumerate()
                        .map(|(i, name)| {
                            Json::obj(vec![
                                ("name", Json::str(name.clone())),
                                ("consensus", Json::num(self.consensus[i])),
                                ("weighted_consensus", Json::num(self.weighted_consensus[i])),
                            ])
                        })
                        .collect(),
                ),
            ),
            ("total_pool", Json::num(self.total_pool)),
            ("participants", Json::num(self.participants as f64)),
            ("top_holder_share", Json::num(self.top_holder_share)),
            ("status", Json::str(self.status.clone())),
            ("asset", Json::str(self.asset.clone())),
            ("backed_by_real_money", Json::Bool(crate::credits::is_redeemable(&self.asset))),
            ("closes_at_ms", Json::num(self.closes_at_ms as f64)),
            ("observed_at_ms", Json::num(self.observed_at_ms as f64)),
            (
                "resolved_outcome",
                self.resolved_outcome.map(|o| Json::num(o as f64)).unwrap_or(Json::Null),
            ),
        ])
    }
}

/// The published accuracy record: how well the consensus has actually done.
///
/// This is what turns the feed from a claim into a product. A buyer evaluating a signal needs to
/// know its historical Brier score, and almost nobody selling one can produce it.
#[derive(Debug, Clone, PartialEq)]
pub struct TrackRecord {
    pub resolved_markets: usize,
    /// Resolved markets left out because the pool was promotional points, not money.
    ///
    /// Free points are minted to anyone who registers, so a points market's consensus can be set
    /// by throwaway accounts at no cost. Letting those markets into the number a subscriber
    /// judges the feed on would mean selling an accuracy figure that sybils can move. The
    /// headline is therefore money-backed markets only, and the excluded count is published so
    /// the omission can be checked rather than taken on faith.
    pub promotional_excluded: usize,
    /// Mean Brier score of the raw consensus. Lower is better; 0.25 is what a permanent 50/50
    /// guess scores on a binary market, so anything above it is worse than no opinion.
    pub mean_brier: Option<f64>,
    /// The same for the calibration-weighted consensus. If weighting is doing any work, this
    /// should be the lower of the two — and if it never is, the weighting should be dropped
    /// rather than sold.
    pub mean_weighted_brier: Option<f64>,
    /// How often the highest-probability outcome was the one that happened.
    pub top_pick_accuracy: Option<f64>,
    /// Resolved markets left out of the scores because the pool was effectively unanimous.
    ///
    /// This exists because anyone can open a market here, and the headline accuracy is the number
    /// a buyer judges the feed on. "Will BTC be above $1?" resolves at a Brier score of roughly
    /// zero and would drag the average toward perfection for free — the same farming shape as
    /// the trust exploit that `trust_delta_vs_consensus` closes, aimed at the marketing number
    /// instead of the rate limit. Scoring only contested markets removes the incentive, and
    /// publishing the count keeps the exclusion honest rather than hidden.
    pub uncontested_excluded: usize,
}

/// A market this one-sided at close was not a forecast, it was a formality.
pub const UNCONTESTED_THRESHOLD: f64 = 0.95;

/// One agent's own history: what it called, what happened, and how well it scored.
///
/// # Why this exists at all
///
/// Points are not redeemable, deliberately and permanently, which leaves a real question with no
/// good answer: why would an agent spend its operator's compute on a market it cannot cash out of?
/// "It is fun" is available to humans and to nobody else. "You might win points" is worth exactly
/// what the points are worth, which is nothing.
///
/// This is the answer. Playing here produces a *verifiable forecasting record* — every call
/// timestamped before the fact, against a question whose terms were hash-committed before any
/// stake was accepted, resolved from a source fixed in advance. That is a genuinely scarce thing.
/// Almost anyone can claim their model is well calibrated; almost nobody can hand you a signed
/// history of pre-committed predictions to check the claim against. An agent that runs here for a
/// month leaves with something it can show, and its operator gets a reason to spend the compute
/// that does not depend on the chips being worth anything.
///
/// # Why it is checkable rather than merely signed
///
/// Every entry carries the market's commitment hash, so a sceptic does not have to trust this
/// venue's arithmetic or its signature. They can fetch each market independently, recompute the
/// commitment from its published terms, confirm the question was fixed before the stake landed,
/// and re-derive the score themselves. The signature says who is asserting it; the commitments are
/// what make the assertion falsifiable.
#[derive(Debug, Clone, PartialEq)]
pub struct AgentRecord {
    pub agent_id: String,
    /// Markets that resolved to an outcome and that this agent had money in.
    pub scored_markets: usize,
    /// Mean Brier score over the agent's own implied probabilities. Lower is better; 0.25 is what
    /// a permanent coin flip scores on a binary market.
    pub mean_brier: Option<f64>,
    /// How often the outcome the agent backed hardest was the one that happened.
    pub hit_rate: Option<f64>,
    /// Markets excluded because the pool was effectively unanimous — see `UNCONTESTED_THRESHOLD`.
    /// Being right about a formality is not evidence of anything, and letting those in would make
    /// the score farmable by opening trivial markets and betting the obvious side.
    pub uncontested_excluded: usize,
    /// Scored markets whose pool was real money rather than promotional points. Published beside
    /// the headline because a record built entirely on free chips is a weaker claim, and hiding
    /// which one this is would make every record on the venue worth less.
    pub money_backed: usize,
    pub entries: Vec<RecordEntry>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RecordEntry {
    pub market_id: String,
    pub question: String,
    /// The hash the market's terms were fixed under, so this line can be checked independently.
    pub commitment: String,
    pub asset: String,
    pub outcome_count: usize,
    pub winning_outcome: usize,
    /// The agent's own implied probability for the outcome that actually happened.
    pub probability_on_winner: f64,
    pub brier: f64,
    pub staked: f64,
    pub resolved_at_ms: i64,
}

/// Builds one agent's record from the resolved markets it took part in.
///
/// The agent's "forecast" is its own stake distribution across the outcomes, normalised — betting
/// 30 on YES and 10 on NO is a 75% call. That is the honest reading: it is the probability the
/// agent was willing to put money behind, which is the only kind a market can observe.
pub fn agent_record(
    agent_id: &str,
    resolved: &[(&Market, &[Stake])],
    now_ms: i64,
) -> AgentRecord {
    let mut entries = Vec::new();
    let mut briers = Vec::new();
    let mut hits = 0usize;
    let mut uncontested = 0usize;
    let mut money_backed = 0usize;

    for (market, stakes) in resolved {
        let Some(winner) = market.winning_outcome else { continue };
        if market.effective_status(now_ms) != MarketStatus::Resolved {
            continue;
        }
        let n = market.outcomes.len();
        if n == 0 || winner >= n {
            continue;
        }

        // What this agent alone put where.
        let mut mine = vec![0.0f64; n];
        for st in stakes.iter().filter(|s| s.agent_id == agent_id) {
            if st.outcome_idx < n {
                mine[st.outcome_idx] += st.amount;
            }
        }
        let staked: f64 = mine.iter().sum();
        if staked <= 0.0 {
            continue; // the agent was not in this market
        }

        // Skip markets the crowd had already decided. Same farming shape the feed's own record
        // excludes, pointed at an individual score instead of the venue's.
        let pool_total: f64 = stakes.iter().map(|s| s.amount).sum();
        if pool_total > 0.0 {
            let mut pools = vec![0.0f64; n];
            for st in stakes.iter() {
                if st.outcome_idx < n {
                    pools[st.outcome_idx] += st.amount;
                }
            }
            if pools.iter().cloned().fold(0.0, f64::max) / pool_total >= UNCONTESTED_THRESHOLD {
                uncontested += 1;
                continue;
            }
        }

        let p: Vec<f64> = mine.iter().map(|v| v / staked).collect();
        let brier: f64 = p
            .iter()
            .enumerate()
            .map(|(i, pi)| (pi - if i == winner { 1.0 } else { 0.0 }).powi(2))
            .sum();

        let top = p
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.partial_cmp(b.1).unwrap_or(std::cmp::Ordering::Equal))
            .map(|(i, _)| i);
        if top == Some(winner) {
            hits += 1;
        }
        if crate::credits::is_redeemable(&market.asset) {
            money_backed += 1;
        }
        briers.push(brier);
        entries.push(RecordEntry {
            market_id: market.market_id.clone(),
            question: market.question.clone(),
            commitment: market.commitment.clone(),
            asset: market.asset.clone(),
            outcome_count: n,
            winning_outcome: winner,
            probability_on_winner: p[winner],
            brier,
            staked,
            resolved_at_ms: market.observed_at_ms,
        });
    }

    entries.sort_by_key(|e| std::cmp::Reverse(e.resolved_at_ms));
    let scored = briers.len();
    AgentRecord {
        agent_id: agent_id.to_string(),
        scored_markets: scored,
        mean_brier: if scored > 0 {
            Some(briers.iter().sum::<f64>() / scored as f64)
        } else {
            None
        },
        hit_rate: if scored > 0 { Some(hits as f64 / scored as f64) } else { None },
        uncontested_excluded: uncontested,
        money_backed,
        entries,
    }
}

impl AgentRecord {
    /// The exact bytes the venue signs. Canonical and total: every number a reader would rely on
    /// appears here, so a record cannot be re-presented with a better score under the same
    /// signature.
    pub fn attestation_input(&self) -> String {
        let mut out = format!(
            "ikenga-agent-record-v1\nagent:{}\nscored:{}\nmoney_backed:{}\nbrier:{}\nhit_rate:{}\n",
            self.agent_id,
            self.scored_markets,
            self.money_backed,
            self.mean_brier.map(|b| format!("{b:.10}")).unwrap_or_else(|| "none".into()),
            self.hit_rate.map(|h| format!("{h:.10}")).unwrap_or_else(|| "none".into()),
        );
        for e in &self.entries {
            out.push_str(&format!(
                "market:{}:{}:{}:{:.10}\n",
                e.market_id, e.commitment, e.winning_outcome, e.probability_on_winner
            ));
        }
        out
    }

    pub fn to_json(&self, pubkey: Option<String>, signature: Option<String>) -> Json {
        Json::obj(vec![
            ("agent_id", Json::str(self.agent_id.clone())),
            ("scored_markets", Json::num(self.scored_markets as f64)),
            ("money_backed_markets", Json::num(self.money_backed as f64)),
            ("mean_brier", self.mean_brier.map(Json::num).unwrap_or(Json::Null)),
            ("hit_rate", self.hit_rate.map(Json::num).unwrap_or(Json::Null)),
            ("uncontested_excluded", Json::num(self.uncontested_excluded as f64)),
            (
                "coin_flip_brier",
                Json::num(0.25),
            ),
            (
                "beats_a_coin_flip",
                self.mean_brier.map(|b| Json::Bool(b < 0.25)).unwrap_or(Json::Null),
            ),
            (
                "markets",
                Json::Array(
                    self.entries
                        .iter()
                        .map(|e| {
                            Json::obj(vec![
                                ("market_id", Json::str(e.market_id.clone())),
                                ("question", Json::str(e.question.clone())),
                                // Named exactly as GET /v1/markets/{id} names it, so a verifier
                                // comparing the two is comparing fields with the same name. The
                                // first version of this called it "commitment" here and
                                // "commitment_sha256" there, and the check silently had nothing
                                // to compare.
                                ("commitment_sha256", Json::str(e.commitment.clone())),
                                ("asset", Json::str(e.asset.clone())),
                                ("winning_outcome", Json::num(e.winning_outcome as f64)),
                                (
                                    "your_probability_on_winner",
                                    Json::num(e.probability_on_winner),
                                ),
                                ("brier", Json::num(e.brier)),
                                ("staked", Json::num(e.staked)),
                                ("resolved_at_ms", Json::num(e.resolved_at_ms as f64)),
                            ])
                        })
                        .collect(),
                ),
            ),
            ("attestation_input", Json::str(self.attestation_input())),
            (
                "signature",
                signature.map(Json::str).unwrap_or(Json::Null),
            ),
            ("venue_public_key", pubkey.map(Json::str).unwrap_or(Json::Null)),
            ("signature_algorithm", Json::str("ed25519")),
            (
                "how_to_verify",
                Json::str(
                    "Ed25519-verify `signature` over the raw bytes of `attestation_input` using \
                     `venue_public_key`. Then check each market independently: GET \
                     /v1/markets/{market_id}, recompute SHA-256 over its published \
                     commitment_input, and confirm it matches the commitment_sha256 listed here. \
                     The venue cannot restate a market's question after the fact without breaking \
                     that hash.",
                ),
            ),
        ])
    }
}

/// Computes the track record from resolved markets and their final stake distributions.
pub fn track_record(
    resolved: &[(&Market, &[Stake])],
    trust_of: &dyn Fn(&str) -> u32,
    now_ms: i64,
) -> TrackRecord {
    let mut briers = Vec::new();
    let mut weighted_briers = Vec::new();
    let mut hits = 0usize;
    let mut scored = 0usize;
    let mut uncontested = 0usize;
    let mut promotional = 0usize;

    for (market, stakes) in resolved {
        // Only markets that actually resolved to an outcome can score. A void has no truth to
        // measure against, and counting it either way would flatter or punish the record falsely.
        let Some(winner) = market.winning_outcome else { continue };
        if market.effective_status(now_ms) != MarketStatus::Resolved {
            continue;
        }
        if !crate::credits::is_redeemable(&market.asset) {
            promotional += 1;
            continue;
        }
        let sig = signal_for_market(market, stakes, trust_of, now_ms);
        if sig.total_pool <= 0.0 {
            continue;
        }
        if sig.consensus.iter().cloned().fold(0.0, f64::max) >= UNCONTESTED_THRESHOLD {
            uncontested += 1;
            continue;
        }

        let brier: f64 = sig
            .consensus
            .iter()
            .enumerate()
            .map(|(i, p)| (p - if i == winner { 1.0 } else { 0.0 }).powi(2))
            .sum();
        let wbrier: f64 = sig
            .weighted_consensus
            .iter()
            .enumerate()
            .map(|(i, p)| (p - if i == winner { 1.0 } else { 0.0 }).powi(2))
            .sum();
        briers.push(brier);
        weighted_briers.push(wbrier);

        let top = sig
            .consensus
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.partial_cmp(b.1).unwrap_or(std::cmp::Ordering::Equal))
            .map(|(i, _)| i);
        if top == Some(winner) {
            hits += 1;
        }
        scored += 1;
    }

    let mean = |v: &[f64]| -> Option<f64> {
        if v.is_empty() {
            None
        } else {
            Some(v.iter().sum::<f64>() / v.len() as f64)
        }
    };

    TrackRecord {
        resolved_markets: scored,
        mean_brier: mean(&briers),
        mean_weighted_brier: mean(&weighted_briers),
        top_pick_accuracy: if scored > 0 { Some(hits as f64 / scored as f64) } else { None },
        uncontested_excluded: uncontested,
        promotional_excluded: promotional,
    }
}

impl TrackRecord {
    pub fn to_json(&self) -> Json {
        Json::obj(vec![
            ("resolved_markets", Json::num(self.resolved_markets as f64)),
            ("mean_brier", self.mean_brier.map(Json::num).unwrap_or(Json::Null)),
            (
                "mean_weighted_brier",
                self.mean_weighted_brier.map(Json::num).unwrap_or(Json::Null),
            ),
            (
                "top_pick_accuracy",
                self.top_pick_accuracy.map(Json::num).unwrap_or(Json::Null),
            ),
            ("uncontested_excluded", Json::num(self.uncontested_excluded as f64)),
            ("promotional_excluded", Json::num(self.promotional_excluded as f64)),
            (
                "scoring_note",
                Json::str(
                    "Markets whose pool was at least 95% on one outcome are excluded from these \
                     scores. Anyone can open a market here, so a foregone conclusion would \
                     otherwise be an easy way to make this number look better than the feed is. \
                     The count of exclusions is published so the omission is auditable.",
                ),
            ),
            (
                "baseline_note",
                Json::str(
                    "Scored on money-backed markets only, excluding any whose pool was at least \
                     95% one-sided. A permanent 50/50 guess scores 0.25 on a binary market, so a \
                     mean_brier above that is worse than having no opinion — published either \
                     way, because a signal you cannot evaluate is not a signal.",
                ),
            ),
        ])
    }
}

/// Access tiers for the feed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FeedTier {
    /// Free and public: current consensus on open markets, delayed, no weighting.
    Public,
    /// Paid: live consensus, calibration weighting, full history.
    Subscriber,
}

impl FeedTier {
    /// How stale the public tier's numbers are.
    ///
    /// A delay rather than a cut-down: the free tier has to be genuinely useful or nobody
    /// discovers the product, but live-and-weighted has to be worth paying for. Fifteen minutes
    /// is useless to anyone trading on it and perfectly fine for evaluating whether to buy.
    ///
    /// The length is a pricing lever, so it is configurable with `IKENGA_FEED_DELAY_MS` — but it
    /// is floored at one minute. A zero-delay public tier is not a cheaper product, it is the
    /// paid product given away, and the floor makes that a decision someone has to take on
    /// purpose rather than by leaving an env var set to 0 in a deploy script.
    pub fn delay_ms(&self) -> i64 {
        match self {
            FeedTier::Public => Self::public_delay_ms(),
            FeedTier::Subscriber => 0,
        }
    }

    pub const DEFAULT_PUBLIC_DELAY_MS: i64 = 15 * 60 * 1000;
    pub const MIN_PUBLIC_DELAY_MS: i64 = 60 * 1000;

    fn public_delay_ms() -> i64 {
        std::env::var("IKENGA_FEED_DELAY_MS")
            .ok()
            .and_then(|v| v.trim().parse::<i64>().ok())
            .map(|v| v.max(Self::MIN_PUBLIC_DELAY_MS))
            .unwrap_or(Self::DEFAULT_PUBLIC_DELAY_MS)
    }

    pub fn label(&self) -> &'static str {
        match self {
            FeedTier::Public => "public",
            FeedTier::Subscriber => "subscriber",
        }
    }
}

/// A rendered feed response, kept so the next caller does not pay to rebuild it.
///
/// `get_feed` is unauthenticated and, done naively, is the most expensive endpoint on the
/// service: it aggregates every market and rescores the whole resolved history on every request,
/// holding the markets and stakes locks the entire time. That is not merely slow — those are the
/// same locks staking and settlement need, so anyone with a loop and a socket could stall the
/// actual product from outside, with no account and no cost. Serving a snapshot bounds the work
/// to once per window however hard the endpoint is hit.
///
/// One entry per tier, because the tiers legitimately differ. The window is deliberately far
/// shorter than the public delay, so caching never makes the free tier fresher or staler than
/// the delay already says it is.
pub struct FeedCache {
    entries: std::sync::Mutex<HashMap<&'static str, (i64, String)>>,
}

impl Default for FeedCache {
    fn default() -> Self {
        FeedCache { entries: std::sync::Mutex::new(HashMap::new()) }
    }
}

/// How long a rendered feed stays warm.
pub const FEED_CACHE_MS: i64 = 5_000;

impl FeedCache {
    pub fn get(&self, tier: FeedTier, now_ms: i64) -> Option<String> {
        let entries = self.entries.lock().unwrap();
        match entries.get(tier.label()) {
            Some((at, body)) if now_ms - *at < FEED_CACHE_MS && now_ms >= *at => {
                Some(body.clone())
            }
            _ => None,
        }
    }

    pub fn put(&self, tier: FeedTier, now_ms: i64, body: String) {
        self.entries.lock().unwrap().insert(tier.label(), (now_ms, body));
    }
}

/// Subscribers to the paid feed, by key. Kept separate from agent identity on purpose: buying
/// the data does not require being a participant, and being a participant does not expose you to
/// the buyers.
#[derive(Default)]
pub struct FeedSubscribers {
    keys: std::sync::Mutex<HashMap<String, String>>, // key -> label
}

impl FeedSubscribers {
    pub fn add(&self, key: impl Into<String>, label: impl Into<String>) {
        self.keys.lock().unwrap().insert(key.into(), label.into());
    }

    pub fn remove(&self, key: &str) -> bool {
        self.keys.lock().unwrap().remove(key).is_some()
    }

    pub fn tier_for(&self, key: Option<&str>) -> FeedTier {
        match key {
            Some(k) if self.keys.lock().unwrap().contains_key(k) => FeedTier::Subscriber,
            _ => FeedTier::Public,
        }
    }

    pub fn count(&self) -> usize {
        self.keys.lock().unwrap().len()
    }

    pub fn labels(&self) -> Vec<String> {
        let mut v: Vec<String> = self.keys.lock().unwrap().values().cloned().collect();
        v.sort();
        v
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::prediction::{Comparator, MarketStatus, ResolutionSpec};

    fn stake_at(agent: &str, outcome: usize, amount: f64, at: i64) -> Stake {
        Stake { agent_id: agent.into(), outcome_idx: outcome, amount, placed_at_ms: at }
    }

    fn stake(agent: &str, outcome: usize, amount: f64) -> Stake {
        Stake { agent_id: agent.into(), outcome_idx: outcome, amount, placed_at_ms: 0 }
    }

    fn market(id: &str, winner: Option<usize>) -> Market {
        let mut m = Market {
            market_id: id.into(),
            question: "Will it?".into(),
            outcomes: vec!["YES".into(), "NO".into()],
            resolution: ResolutionSpec::PriceThreshold {
                symbol: "BTC-USD".into(),
                comparator: Comparator::Above,
                threshold: 1.0,
                if_true_outcome: 0,
                if_false_outcome: 1,
            },
            asset: "USDC".into(),
            closes_at_ms: 1_000,
            observed_at_ms: 2_000,
            dispute_window_ms: 0,
            commitment: String::new(),
            status: if winner.is_some() { MarketStatus::Resolved } else { MarketStatus::Open },
            proposal: None,
            winning_outcome: winner,
            created_at_ms: 0,
            disputed_at_ms: None,
        };
        m.commitment = m.compute_commitment();
        m
    }

    const NOW: i64 = 10_000;
    fn no_trust(_: &str) -> u32 {
        0
    }

    // ---- the privacy guarantee, which is the product's foundation -----------------------------

    #[test]
    fn a_signal_contains_no_participant_identity_anywhere() {
        // If this ever fails, the data product is selling its own users out — and the signal
        // degrades as informed money leaves, so it destroys the asset it is selling.
        let m = market("mkt_1", None);
        // Distinctive amounts that cannot collide with a timestamp or a count — an earlier
        // version of this test searched for "100" and tripped over `closes_at_ms: 1000`.
        let stakes = vec![
            stake("agent_SECRET_ONE", 0, 137.77),
            stake("agent_SECRET_TWO", 1, 51.13),
        ];
        let sig = signal_for_market(&m, &stakes, &no_trust, NOW);
        let serialised = sig.to_json().to_string();

        assert!(!serialised.contains("agent_SECRET_ONE"), "leaked an agent id");
        assert!(!serialised.contains("agent_SECRET_TWO"), "leaked an agent id");
        assert!(!serialised.contains("137.77"), "leaked an individual stake size");
        assert!(!serialised.contains("51.13"), "leaked an individual stake size");
        // The aggregate is the product; the count is fine, the list never is.
        assert!(serialised.contains("total_pool"));
        assert_eq!(sig.participants, 2);
    }

    #[test]
    fn consensus_reflects_where_the_money_is() {
        let m = market("mkt_1", None);
        let stakes = vec![stake("a", 0, 70.0), stake("b", 1, 30.0)];
        let sig = signal_for_market(&m, &stakes, &no_trust, NOW);
        assert!((sig.consensus[0] - 0.7).abs() < 1e-9);
        assert!((sig.consensus[1] - 0.3).abs() < 1e-9);
        assert_eq!(sig.total_pool, 100.0);
    }

    #[test]
    fn an_untouched_market_publishes_no_opinion_rather_than_a_fake_coin_flip() {
        let m = market("mkt_empty", None);
        let sig = signal_for_market(&m, &[], &no_trust, NOW);
        assert_eq!(sig.consensus, vec![0.0, 0.0]);
        assert_eq!(sig.total_pool, 0.0);
        assert_eq!(sig.participants, 0);
    }

    // ---- calibration weighting -----------------------------------------------------------------

    #[test]
    fn a_proven_forecaster_counts_for_more_but_never_for_everything() {
        let m = market("mkt_1", None);
        let stakes = vec![stake("proven", 0, 100.0), stake("rookie", 1, 100.0)];
        let trust = |a: &str| if a == "proven" { 1000 } else { 0 };
        let sig = signal_for_market(&m, &stakes, &trust, NOW);

        assert!((sig.consensus[0] - 0.5).abs() < 1e-9, "raw consensus is an even split");
        assert!(sig.weighted_consensus[0] > 0.5, "the proven forecaster should pull it");
        assert!(
            sig.weighted_consensus[0] < 0.7,
            "but not dominate — weighting is capped at 2x so the feed stays a crowd, not one voice"
        );
    }

    #[test]
    fn weighting_never_counts_anyone_for_less_than_their_money() {
        assert_eq!(calibration_weight(0), 1.0);
        assert_eq!(calibration_weight(1000), 2.0);
        assert_eq!(calibration_weight(u32::MAX), 2.0, "capped even on absurd input");
        assert!(calibration_weight(500) > 1.0 && calibration_weight(500) < 2.0);
    }

    // ---- the track record ----------------------------------------------------------------------

    #[test]
    fn the_track_record_scores_the_consensus_against_reality() {
        let m1 = market("m1", Some(0));
        let s1 = vec![stake("a", 0, 90.0), stake("b", 1, 10.0)]; // 90% on the winner
        let m2 = market("m2", Some(1));
        let s2 = vec![stake("a", 0, 20.0), stake("b", 1, 80.0)]; // 80% on the winner

        let rec = track_record(&[(&m1, &s1), (&m2, &s2)], &no_trust, NOW);
        assert_eq!(rec.resolved_markets, 2);
        assert_eq!(rec.top_pick_accuracy, Some(1.0));
        // Brier for 0.9 on the winner = 0.01 + 0.01 = 0.02; for 0.8 = 0.04 + 0.04 = 0.08.
        assert!((rec.mean_brier.unwrap() - 0.05).abs() < 1e-9);
        assert!(rec.mean_brier.unwrap() < 0.25, "should beat a permanent coin flip");
    }

    #[test]
    fn voided_markets_are_excluded_rather_than_flattering_the_record() {
        // A void has no truth to score against. Counting it as a hit would inflate the record;
        // counting it as a miss would punish honesty about uncertainty. It simply isn't scored.
        let mut voided = market("m_void", None);
        voided.status = MarketStatus::Voided;
        let s = vec![stake("a", 0, 100.0)];
        let rec = track_record(&[(&voided, &s)], &no_trust, NOW);
        assert_eq!(rec.resolved_markets, 0);
        assert_eq!(rec.mean_brier, None);
    }

    #[test]
    fn an_empty_history_reports_nothing_rather_than_a_perfect_score() {
        let rec = track_record(&[], &no_trust, NOW);
        assert_eq!(rec.resolved_markets, 0);
        assert_eq!(rec.mean_brier, None);
        assert_eq!(rec.top_pick_accuracy, None);
    }

    // ---- tiering ---------------------------------------------------------------------------------

    #[test]
    fn the_free_tier_is_delayed_and_the_paid_tier_is_live() {
        assert!(FeedTier::Public.delay_ms() > 0);
        assert_eq!(FeedTier::Subscriber.delay_ms(), 0);
    }

    #[test]
    fn only_a_known_key_gets_the_live_feed() {
        let subs = FeedSubscribers::default();
        subs.add("key_abc", "Acme Research");
        assert_eq!(subs.tier_for(Some("key_abc")), FeedTier::Subscriber);
        assert_eq!(subs.tier_for(Some("key_wrong")), FeedTier::Public);
        assert_eq!(subs.tier_for(None), FeedTier::Public);
        assert_eq!(subs.count(), 1);
        assert!(subs.remove("key_abc"));
        assert_eq!(subs.tier_for(Some("key_abc")), FeedTier::Public);
    }

    #[test]
    fn the_delayed_tier_reports_the_pool_as_it_stood_not_as_it_is() {
        let m = market("m1", None);
        // Early money says outcome 0. A late flood reverses it.
        let stakes = vec![
            stake_at("early", 0, 100.0, 1_000),
            stake_at("late", 1, 900.0, 9_000),
        ];
        let trust = |_: &str| 0u32;

        let live = signal_as_of(&m, &stakes, &trust, 10_000, 10_000);
        assert!((live.consensus[1] - 0.9).abs() < 1e-9, "live feed sees the flood: {live:?}");

        let delayed = signal_as_of(&m, &stakes, &trust, 10_000, 5_000);
        assert!(
            (delayed.consensus[0] - 1.0).abs() < 1e-9,
            "the delayed tier must not see money placed after the cutoff: {delayed:?}"
        );
        assert_eq!(delayed.total_pool, 100.0);
        assert_eq!(delayed.participants, 1);
    }

    #[test]
    fn a_market_with_no_stakes_before_the_cutoff_publishes_no_opinion() {
        let m = market("m1", None);
        let stakes = vec![stake_at("a", 0, 50.0, 9_000)];
        let s = signal_as_of(&m, &stakes, &|_| 0, 10_000, 1_000);
        assert_eq!(s.total_pool, 0.0);
        assert_eq!(s.participants, 0);
        assert!(
            s.consensus.iter().all(|p| *p == 0.0),
            "an untouched pool must not be dressed up as a 50/50: {s:?}"
        );
    }

    #[test]
    fn the_public_delay_can_be_tuned_but_never_switched_off() {
        // Env is process-global, so this test owns the variable for its duration.
        std::env::set_var("IKENGA_FEED_DELAY_MS", "300000");
        assert_eq!(FeedTier::Public.delay_ms(), 300_000);
        std::env::set_var("IKENGA_FEED_DELAY_MS", "0");
        assert_eq!(
            FeedTier::Public.delay_ms(),
            FeedTier::MIN_PUBLIC_DELAY_MS,
            "a zero delay gives the paid feed away for free"
        );
        std::env::set_var("IKENGA_FEED_DELAY_MS", "-5");
        assert_eq!(FeedTier::Public.delay_ms(), FeedTier::MIN_PUBLIC_DELAY_MS);
        std::env::set_var("IKENGA_FEED_DELAY_MS", "nonsense");
        assert_eq!(FeedTier::Public.delay_ms(), FeedTier::DEFAULT_PUBLIC_DELAY_MS);
        std::env::remove_var("IKENGA_FEED_DELAY_MS");
        assert_eq!(FeedTier::Public.delay_ms(), FeedTier::DEFAULT_PUBLIC_DELAY_MS);
        assert_eq!(FeedTier::Subscriber.delay_ms(), 0);
    }

    #[test]
    fn a_foregone_conclusion_cannot_pad_the_published_accuracy() {
        let mut easy = market("easy", Some(0));
        easy.status = MarketStatus::Resolved;
        // 99% of the money on the outcome that happened: nobody forecast anything here.
        let easy_stakes = vec![stake("whale", 0, 990.0), stake("b", 1, 10.0)];

        let mut hard = market("hard", Some(0));
        hard.status = MarketStatus::Resolved;
        let hard_stakes = vec![stake("a", 0, 55.0), stake("b", 1, 45.0)];

        let trust = |_: &str| 0u32;
        let only_hard = track_record(&[(&hard, hard_stakes.as_slice())], &trust, 0);
        let both = track_record(
            &[(&easy, easy_stakes.as_slice()), (&hard, hard_stakes.as_slice())],
            &trust,
            0,
        );

        assert_eq!(both.resolved_markets, 1, "the uncontested market must not be scored");
        assert_eq!(both.uncontested_excluded, 1, "and its exclusion must be published");
        assert_eq!(
            both.mean_brier, only_hard.mean_brier,
            "adding a foregone conclusion must not move the headline score"
        );
    }

    #[test]
    fn one_account_cannot_set_the_published_consensus_on_its_own() {
        let m = market("whale", None);
        // A whale holds 90% of the pool on outcome 0; four small accounts disagree.
        let stakes = vec![
            stake("whale", 0, 9_000.0),
            stake("a", 1, 250.0),
            stake("b", 1, 250.0),
            stake("c", 1, 250.0),
            stake("d", 1, 250.0),
        ];
        let s = signal_for_market(&m, &stakes, &|_| 0, 0);

        // The raw pool share is untouched — that is what the payout is computed from.
        assert!((s.consensus[0] - 0.9).abs() < 1e-9, "raw consensus must stay pro rata: {s:?}");
        // The weighted signal that subscribers buy is not the whale's alone.
        assert!(
            (s.weighted_consensus[0] - MAX_AGENT_INFLUENCE).abs() < 1e-6,
            "one account must not exceed its cap in the sold signal, got {:?}",
            s.weighted_consensus
        );
        assert!((s.top_holder_share - 0.9).abs() < 1e-9, "concentration must be published");
    }

    #[test]
    fn splitting_a_position_across_many_stakes_does_not_evade_the_cap() {
        let m = market("split", None);
        let one_bet = vec![
            stake("whale", 0, 9_000.0),
            stake("a", 1, 500.0),
            stake("b", 1, 500.0),
        ];
        // The same whale, same money, dribbled in as nine separate stakes.
        let mut many_bets = vec![stake("a", 1, 500.0), stake("b", 1, 500.0)];
        for _ in 0..9 {
            many_bets.push(stake("whale", 0, 1_000.0));
        }
        let a = signal_for_market(&m, &one_bet, &|_| 0, 0);
        let b = signal_for_market(&m, &many_bets, &|_| 0, 0);
        assert!(
            (a.weighted_consensus[0] - b.weighted_consensus[0]).abs() < 1e-9,
            "the cap is per account, not per stake: {:?} vs {:?}",
            a.weighted_consensus,
            b.weighted_consensus
        );
    }

    #[test]
    fn a_normal_market_is_unaffected_by_the_cap() {
        let m = market("normal", None);
        let stakes = vec![
            stake("a", 0, 100.0),
            stake("b", 0, 120.0),
            stake("c", 1, 90.0),
            stake("d", 1, 110.0),
            stake("e", 1, 80.0),
        ];
        let s = signal_for_market(&m, &stakes, &|_| 0, 0);
        // Nobody holds more than 25%, so the weighted signal is just the money split.
        for (w, r) in s.weighted_consensus.iter().zip(s.consensus.iter()) {
            assert!((w - r).abs() < 1e-9, "cap must not bite below the threshold: {s:?}");
        }
    }

    #[test]
    fn a_lone_participant_is_not_capped_into_meaninglessness() {
        let m = market("solo", None);
        let s = signal_for_market(&m, &[stake("only", 0, 100.0)], &|_| 0, 0);
        assert!((s.weighted_consensus[0] - 1.0).abs() < 1e-9, "{s:?}");
        assert!((s.top_holder_share - 1.0).abs() < 1e-9);
    }

    #[test]
    fn the_cap_holds_for_every_account_on_random_pools() {
        // Property check: whatever the shape of the pool, no account's share of the published
        // signal exceeds the cap (or an even split, when there are too few accounts for the cap
        // to be satisfiable at all).
        let mut seed = 0x5eed_1234_u64;
        let mut next = move || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        let m = market("prop", None);
        for _ in 0..500 {
            let k = 1 + (next() % 9) as usize;
            let mut stakes = Vec::new();
            for i in 0..k {
                let amount = 1.0 + (next() % 100_000) as f64 / 10.0;
                stakes.push(stake(&format!("a{i}"), (next() % 2) as usize, amount));
            }
            let s = signal_for_market(&m, &stakes, &|_| 0, 0);

            let mut per_agent: Vec<f64> = Vec::new();
            for i in 0..k {
                let id = format!("a{i}");
                let mine: f64 =
                    stakes.iter().filter(|s| s.agent_id == id).map(|s| s.amount).sum();
                per_agent.push(mine);
            }
            let ceiling = influence_ceiling(&per_agent);
            let contributed: f64 =
                per_agent.iter().map(|t| t.min(ceiling)).sum();
            let allowed = MAX_AGENT_INFLUENCE.max(1.0 / k as f64) + 1e-6;
            for t in &per_agent {
                let share = t.min(ceiling) / contributed;
                assert!(
                    share <= allowed,
                    "account share {share} exceeded {allowed} with positions {per_agent:?}"
                );
            }
            // And the signal is still a probability distribution.
            let sum: f64 = s.weighted_consensus.iter().sum();
            assert!((sum - 1.0).abs() < 1e-9, "weighted consensus must normalise: {sum}");
        }
    }
}
