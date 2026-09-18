//! Pari-mutuel prediction markets for agents.
//!
//! # Why pari-mutuel and not an order book
//!
//! The order book in `orderbook.rs` needs a counterparty: your bid does nothing until someone
//! posts the matching ask. That is the cold-start problem in its purest form, and it is why a new
//! venue with real liquidity requirements is unlaunchable without capital.
//!
//! Pari-mutuel has no counterparty at all. Every stake goes into one pool; when the outcome is
//! known, the pool is divided among whoever was right. **Any number of participants ≥ 1 produces
//! a valid settlement.** Nobody waits to be matched, nobody has to make a market, and the
//! operator never takes the other side of a bet — so no capital is required and no position is
//! ever held. Racetracks have run on this for a century for exactly these reasons.
//!
//! # The rake, and why it comes only from the losing pool
//!
//! The textbook version rakes the whole pool, which means a correct forecaster still loses money
//! to the house, and a lone participant who is right gets back less than they put in. For a venue
//! trying to attract its first users that is precisely backwards — it taxes being early.
//!
//! Here the rake is taken **only from the losing pool**:
//!
//! ```text
//! winning_pool = stakes on the outcome that happened
//! losing_pool  = everything else
//! rake         = losing_pool × 1%
//! payout(i)    = stake(i) + (losing_pool − rake) × stake(i) / winning_pool
//! ```
//!
//! Consequences worth stating plainly, because they are the product:
//!
//! - A winner **always** gets at least their stake back. The house cannot make a correct
//!   forecaster poorer.
//! - If everyone agreed (no losing pool), the rake is zero and everyone is simply refunded. The
//!   house earns only when the market resolved genuine disagreement — which is the only case
//!   where it did any work.
//! - A single participant, alone and correct, loses nothing. There is no early-adopter penalty.
//!
//! The operator's revenue is therefore a function of disagreement volume, not of trading volume,
//! and it is collected without ever holding a position.
//!
//! # The invariant
//!
//! `sum(payouts) + rake ≤ total_pool`, always, including under floating-point rounding. The
//! engine must never distribute money that was not staked. This is asserted in the tests below
//! and is the equivalent here of the capture invariant in `fees.rs` — load-bearing, not
//! decorative.
//!
//! # What this is denominated in
//!
//! Stakes are in whatever asset the caller names, and the intended default is a points asset with
//! no cash value. Running this with a real-money asset is a different product in the eyes of
//! essentially every regulator — see `docs/COMPLIANCE-NOTES.md`. The engine is identical either
//! way; the legal exposure is not.

use std::collections::HashMap;

use crate::json::Json;

/// Basis points of the winnings taken by the house. 100 = 1%.
///
/// # How this compares, with real numbers
///
/// | Venue | What they charge |
/// |---|---|
/// | Polymarket (global) | 2% of net profits, at withdrawal |
/// | Kalshi | `0.07 × contracts × price × (1−price)`, peaking at ~1.75% at 50/50 odds |
/// | Polymarket (US) | 0.10% taker on every trade, win or lose |
/// | Robinhood | $0.02 per contract, flat |
/// | **Ikenga** | **1% of winnings, and nothing else** |
///
/// One percent is half Polymarket's profit cut and comfortably under Kalshi's peak. But the rate
/// is the smaller half of the story — what it is charged *on* matters more:
///
/// - **Never on your stake.** The rake comes out of the losing pool only, so a correct forecast
///   always returns at least what it staked. Kalshi and Polymarket US both charge on the trade,
///   which means you can pay a fee on a position that loses.
/// - **Never when everyone agreed.** No losing pool, no rake, everyone simply refunded. The house
///   earns only when it settled a real disagreement.
/// - **Nothing else, anywhere.** No maker fee, no taker fee, no deposit fee, no withdrawal fee,
///   no per-contract charge.
pub const DEFAULT_RAKE_BPS: f64 = 100.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MarketStatus {
    /// Accepting stakes.
    Open,
    /// Past its close time; no new stakes, awaiting the observation.
    Closed,
    /// An outcome has been proposed and the dispute window is running. Nothing has been paid.
    Proposed,
    /// Someone challenged the proposal. Payout is frozen until it is re-proposed or voided.
    Disputed,
    /// Outcome final, payouts made.
    Resolved,
    /// Could not be resolved to the declared standard — everyone refunded in full, no rake.
    Voided,
}

impl MarketStatus {
    pub fn label(&self) -> &'static str {
        match self {
            MarketStatus::Open => "Open",
            MarketStatus::Closed => "Closed",
            MarketStatus::Proposed => "Proposed",
            MarketStatus::Disputed => "Disputed",
            MarketStatus::Resolved => "Resolved",
            MarketStatus::Voided => "Voided",
        }
    }

    /// Settled one way or the other; nothing further can happen to it.
    pub fn is_final(&self) -> bool {
        matches!(self, MarketStatus::Resolved | MarketStatus::Voided)
    }
}

/// How a market's outcome is determined — fixed at creation and covered by the commitment hash,
/// so it cannot be reinterpreted after money is down.
///
/// The distinction matters more than it looks. Polymarket's $7M false resolution in March 2025
/// happened because the outcome was decided by a *vote*: a holder split 5M UMA across three
/// accounts, carried a quarter of the round, and the market paid out on an event that never
/// happened. A vote is an attack surface with a price tag. A declared data source is not.
#[derive(Debug, Clone, PartialEq)]
pub enum ResolutionSpec {
    /// Machine-resolved by comparing a price to a threshold at the observation time. No human
    /// discretion exists in this path at all — which is the point.
    PriceThreshold {
        symbol: String,
        comparator: Comparator,
        threshold: f64,
        /// Outcome index when the comparison holds. The other outcome(s) are the complement.
        if_true_outcome: usize,
        if_false_outcome: usize,
    },
    /// Human-resolved, for questions no feed can answer. Discretion is permitted here but it is
    /// named, bounded by criteria written before anyone staked, and every use is logged with
    /// evidence.
    OperatorDeclared { criteria: String, source: String },
    /// Settled by the two people in the bet agreeing on what happened.
    ///
    /// # Why a third kind of resolution exists
    ///
    /// A head-to-head bet may be about anything — a football result, whether it rains, whether a
    /// deploy ships on Friday. No price feed answers those. The venue previously refused them
    /// outright, and correctly: an agent that writes the question *and* settles it wins by
    /// writing the rule, which is the oldest scam in the business.
    ///
    /// But that reasoning only forbids *one* side deciding. A bet between exactly two parties has
    /// an oracle sitting inside it already — both of them. If they agree on what happened, there
    /// is nothing left to argue about and no discretion anyone could abuse. Neither can settle
    /// alone, so neither can steal.
    ///
    /// Disagreement is the interesting case, and it resolves *downward*: the market becomes
    /// Disputed, the operator has the ordinary review window to look at it, and if nobody does,
    /// the existing backstop voids it and refunds both sides in full. Silence resolves the same
    /// way. So the worst outcome of a contested bet is that everyone gets their money back — never
    /// that the loudest party wins, and never that the money is stuck.
    ///
    /// Only ever attached to a two-party challenge. On a pool with many stakers "both agree" has
    /// no meaning, and `validate` refuses it there.
    MutualAgreement { criteria: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Comparator {
    Above,
    AtOrAbove,
    Below,
    AtOrBelow,
}

impl Comparator {
    pub fn parse(s: &str) -> Option<Comparator> {
        match s {
            "above" | ">" => Some(Comparator::Above),
            "at_or_above" | ">=" => Some(Comparator::AtOrAbove),
            "below" | "<" => Some(Comparator::Below),
            "at_or_below" | "<=" => Some(Comparator::AtOrBelow),
            _ => None,
        }
    }

    pub fn label(&self) -> &'static str {
        match self {
            Comparator::Above => "above",
            Comparator::AtOrAbove => "at_or_above",
            Comparator::Below => "below",
            Comparator::AtOrBelow => "at_or_below",
        }
    }

    pub fn holds(&self, observed: f64, threshold: f64) -> bool {
        match self {
            Comparator::Above => observed > threshold,
            Comparator::AtOrAbove => observed >= threshold,
            Comparator::Below => observed < threshold,
            Comparator::AtOrBelow => observed <= threshold,
        }
    }
}

impl ResolutionSpec {
    pub fn kind(&self) -> &'static str {
        match self {
            ResolutionSpec::PriceThreshold { .. } => "price_threshold",
            ResolutionSpec::OperatorDeclared { .. } => "operator_declared",
            ResolutionSpec::MutualAgreement { .. } => "mutual_agreement",
        }
    }

    /// True when no human can influence the outcome.
    pub fn is_machine_resolved(&self) -> bool {
        matches!(self, ResolutionSpec::PriceThreshold { .. })
    }

    pub fn to_json(&self) -> Json {
        match self {
            ResolutionSpec::PriceThreshold {
                symbol, comparator, threshold, if_true_outcome, if_false_outcome,
            } => Json::obj(vec![
                ("kind", Json::str("price_threshold")),
                ("symbol", Json::str(symbol.clone())),
                ("comparator", Json::str(comparator.label())),
                ("threshold", Json::num(*threshold)),
                ("if_true_outcome", Json::num(*if_true_outcome as f64)),
                ("if_false_outcome", Json::num(*if_false_outcome as f64)),
            ]),
            ResolutionSpec::OperatorDeclared { criteria, source } => Json::obj(vec![
                ("kind", Json::str("operator_declared")),
                ("criteria", Json::str(criteria.clone())),
                ("source", Json::str(source.clone())),
            ]),
            ResolutionSpec::MutualAgreement { criteria } => Json::obj(vec![
                ("kind", Json::str("mutual_agreement")),
                ("criteria", Json::str(criteria.clone())),
                (
                    "how",
                    Json::str(
                        "Both sides POST /v1/markets/{id}/report with the outcome after the \
                         observation time. Agree and it pays. Disagree, or one side never \
                         reports, and it voids with both stakes returned in full.",
                    ),
                ),
            ]),
        }
    }

    pub fn from_json(j: &Json) -> Option<ResolutionSpec> {
        match j.get("kind")?.as_str()? {
            "price_threshold" => Some(ResolutionSpec::PriceThreshold {
                symbol: j.get("symbol")?.as_str()?.to_string(),
                comparator: Comparator::parse(j.get("comparator")?.as_str()?)?,
                threshold: j.get("threshold")?.as_f64()?,
                if_true_outcome: j.get("if_true_outcome")?.as_f64()? as usize,
                if_false_outcome: j.get("if_false_outcome")?.as_f64()? as usize,
            }),
            "operator_declared" => Some(ResolutionSpec::OperatorDeclared {
                criteria: j.get("criteria")?.as_str()?.to_string(),
                source: j.get("source")?.as_str()?.to_string(),
            }),
            "mutual_agreement" => Some(ResolutionSpec::MutualAgreement {
                criteria: j.get("criteria")?.as_str()?.to_string(),
            }),
            _ => None,
        }
    }

    /// Canonical text, hashed into the market commitment. Every field that could change who wins
    /// must appear here — anything omitted is a field the operator could quietly alter later.
    fn canonical(&self) -> String {
        match self {
            ResolutionSpec::PriceThreshold {
                symbol, comparator, threshold, if_true_outcome, if_false_outcome,
            } => format!(
                "price_threshold|{symbol}|{}|{threshold:.8}|{if_true_outcome}|{if_false_outcome}",
                comparator.label()
            ),
            ResolutionSpec::OperatorDeclared { criteria, source } => {
                format!("operator_declared|{criteria}|{source}")
            }
            ResolutionSpec::MutualAgreement { criteria } => {
                format!("mutual_agreement|{criteria}")
            }
        }
    }
}

/// A proposed outcome, awaiting the end of the dispute window.
#[derive(Debug, Clone, PartialEq)]
pub struct Proposal {
    /// `None` proposes voiding the market.
    pub outcome: Option<usize>,
    pub proposed_at_ms: i64,
    /// What the proposal is based on: the observed price and sources, or the operator's stated
    /// evidence. Recorded so a challenge has something concrete to argue with.
    pub evidence: String,
    /// True when produced by the machine path with no human involved.
    pub automatic: bool,
    /// Agents who have already challenged *this* proposal.
    ///
    /// Disputing is deliberately open — the whole point is that a bad outcome can be stopped by
    /// anyone watching, not only by a privileged committee. Open and *unlimited* is a different
    /// thing: one account could re-challenge every re-proposal forever and freeze the payout of
    /// every market on the venue. One objection each, per proposal, bounds the griefing while
    /// leaving the defence intact — a re-proposal is a new proposal and earns a fresh hearing.
    pub disputed_by: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Market {
    pub market_id: String,
    pub question: String,
    /// Mutually exclusive, collectively exhaustive outcomes. Two entries is the binary case.
    pub outcomes: Vec<String>,
    /// How this resolves. Fixed at creation and covered by `commitment`.
    pub resolution: ResolutionSpec,
    /// The asset stakes are denominated in.
    pub asset: String,
    /// Last moment a stake is accepted.
    pub closes_at_ms: i64,
    /// When the outcome is measured. Strictly after `closes_at_ms` — the gap is what stops
    /// anyone betting on information they can already see.
    pub observed_at_ms: i64,
    /// How long a proposed outcome sits challengeable before it can be finalised and paid.
    pub dispute_window_ms: i64,
    /// SHA-256 over every field above, in canonical form. Published with the market so a
    /// participant can prove the terms they were settled under are the terms they staked under.
    pub commitment: String,
    pub status: MarketStatus,
    pub proposal: Option<Proposal>,
    pub winning_outcome: Option<usize>,
    pub created_at_ms: i64,
    /// When the current freeze started, if a proposal is under challenge.
    ///
    /// A dispute is a *pause for review*, not a veto. Without a clock on it, one objection froze a
    /// payout until the operator happened to look — which on an unattended venue is indefinitely,
    /// and the money frozen is other people's. This is what makes the freeze temporary: the
    /// operator has a window to review, and if that window passes with no decision the market
    /// voids and every stake is refunded in full. Nobody's funds are ever trapped by an objection
    /// nobody got round to answering.
    pub disputed_at_ms: Option<i64>,
}

/// Everything wrong with a market's terms, caught before it can accept a single stake.
#[derive(Debug, Clone, PartialEq)]
pub enum SpecError {
    TooFewOutcomes,
    DuplicateOutcomes,
    EmptyField(&'static str),
    /// Observation must be strictly after close, or a bettor can act on the answer.
    NoSettlementGap,
    ClosesInThePast,
    NegativeDisputeWindow,
    OutcomeIndexOutOfRange,
    BadThreshold,
}

impl SpecError {
    pub fn message(&self) -> &'static str {
        match self {
            SpecError::TooFewOutcomes => "a market needs at least two outcomes",
            SpecError::DuplicateOutcomes => "outcomes must be distinct",
            SpecError::EmptyField(f) => f,
            SpecError::NoSettlementGap => {
                "observed_at_ms must be strictly after closes_at_ms, or a bettor could stake on \
                 an outcome they can already observe"
            }
            SpecError::ClosesInThePast => "closes_at_ms must be in the future",
            SpecError::NegativeDisputeWindow => "dispute_window_ms cannot be negative",
            SpecError::OutcomeIndexOutOfRange => {
                "a resolution outcome index does not exist in this market"
            }
            SpecError::BadThreshold => "threshold must be a finite number",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Stake {
    pub agent_id: String,
    pub outcome_idx: usize,
    pub amount: f64,
    /// When the stake landed. Settlement does not care — a pari-mutuel pool pays the same
    /// regardless of arrival order — but the forecast feed does: a delayed tier has to be able to
    /// reconstruct what the consensus *was* at a past moment, not merely hide recent markets.
    pub placed_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub enum MarketError {
    NotOpen,
    Closed,
    /// The market's terms no longer match the commitment published when it opened.
    TermsAltered,
    UnknownOutcome { given: usize, outcomes: usize },
    NonPositiveStake,
    /// Not enough of the market's asset to cover the stake.
    InsufficientBalance { have: f64, need: f64 },
    MalformedMarket(&'static str),
}

impl MarketError {
    pub fn code(&self) -> &'static str {
        match self {
            MarketError::NotOpen => "MARKET_NOT_OPEN",
            MarketError::Closed => "MARKET_CLOSED",
            MarketError::TermsAltered => "TERMS_ALTERED",
            MarketError::UnknownOutcome { .. } => "UNKNOWN_OUTCOME",
            MarketError::NonPositiveStake => "BAD_STAKE",
            MarketError::InsufficientBalance { .. } => "INSUFFICIENT_BALANCE",
            MarketError::MalformedMarket(_) => "MALFORMED_MARKET",
        }
    }

    pub fn message(&self) -> String {
        match self {
            MarketError::NotOpen => "this market is not accepting stakes".into(),
            MarketError::Closed => "this market closed before the stake arrived".into(),
            MarketError::TermsAltered => {
                "this market's terms do not match its published commitment — refusing to accept \
                 stakes on terms that have been altered since it opened"
                    .into()
            }
            MarketError::UnknownOutcome { given, outcomes } => {
                format!("outcome index {given} does not exist (market has {outcomes})")
            }
            MarketError::NonPositiveStake => "stake must be greater than zero".into(),
            MarketError::InsufficientBalance { have, need } => {
                format!("need {need}, have {have}")
            }
            MarketError::MalformedMarket(why) => format!("market is malformed: {why}"),
        }
    }
}

/// The result of resolving a market. Nothing here is applied to balances — the caller does that,
/// so settlement stays a pure function that can be tested and re-derived from the log.
#[derive(Debug, Clone, PartialEq)]
pub struct Settlement {
    pub market_id: String,
    /// agent_id -> amount credited back (stake returned plus winnings, or a refund).
    pub payouts: Vec<(String, f64)>,
    /// The gross rake taken from the losing pool, before the early-liquidity rebate is paid back
    /// out of it. This is *not* what the house keeps — see `house_take`.
    pub rake: f64,
    /// The part of the rake handed back to whoever showed up early. Always `<= rake`.
    pub rebate: f64,
    pub total_pool: f64,
    pub winning_pool: f64,
    pub losing_pool: f64,
    /// True when nobody could be paid on merit and everyone was refunded instead.
    pub refunded: bool,
    pub reason: &'static str,
}

impl Settlement {
    /// What the house actually keeps: the rake less whatever it paid out for early liquidity.
    ///
    /// The distinction matters for the reserve invariant. Crediting `rake` to the fee ledger while
    /// also paying the rebate would credit the same money twice and report the venue as holding
    /// more than it does.
    pub fn house_take(&self) -> f64 {
        (self.rake - self.rebate).max(0.0)
    }
}

impl Market {
    /// The exact bytes hashed into `commitment`.
    ///
    /// Canonical and total: every field that could change who gets paid appears here. If a field
    /// influences the outcome but is left out of this string, the operator can alter it after
    /// stakes are placed and no one can prove it happened — which is the whole failure this
    /// commitment exists to prevent.
    pub fn commitment_input(&self) -> String {
        format!(
            "ikenga-market-v1\n\
             question:{}\n\
             outcomes:{}\n\
             resolution:{}\n\
             asset:{}\n\
             closes_at_ms:{}\n\
             observed_at_ms:{}\n\
             dispute_window_ms:{}\n",
            self.question,
            self.outcomes.join("\u{1f}"),
            self.resolution.canonical(),
            self.asset,
            self.closes_at_ms,
            self.observed_at_ms,
            self.dispute_window_ms,
        )
    }

    /// SHA-256 of `commitment_input`, hex-encoded. Recomputable by anyone from the published
    /// market, which is what makes the promise checkable rather than merely stated.
    pub fn compute_commitment(&self) -> String {
        crate::crypto::hex_encode(&crate::crypto::sha256(self.commitment_input().as_bytes()))
    }

    /// True when the published commitment still matches the market's terms. A false here means
    /// the terms were altered after creation.
    pub fn commitment_is_intact(&self) -> bool {
        self.compute_commitment() == self.commitment
    }

    /// Rejects terms that would be unfair or exploitable, before the market can take a stake.
    pub fn validate_spec(&self, now_ms: i64) -> Result<(), SpecError> {
        if self.outcomes.len() < 2 {
            return Err(SpecError::TooFewOutcomes);
        }
        if self.question.trim().is_empty() {
            return Err(SpecError::EmptyField("question must not be empty"));
        }
        if self.outcomes.iter().any(|o| o.trim().is_empty()) {
            return Err(SpecError::EmptyField("outcome names must not be empty"));
        }
        for i in 0..self.outcomes.len() {
            for j in (i + 1)..self.outcomes.len() {
                if self.outcomes[i] == self.outcomes[j] {
                    return Err(SpecError::DuplicateOutcomes);
                }
            }
        }
        if self.closes_at_ms <= now_ms {
            return Err(SpecError::ClosesInThePast);
        }
        // The settlement gap. Without it, a market could close at or after the moment its answer
        // becomes visible, and the last stake in wins for free.
        if self.observed_at_ms <= self.closes_at_ms {
            return Err(SpecError::NoSettlementGap);
        }
        if self.dispute_window_ms < 0 {
            return Err(SpecError::NegativeDisputeWindow);
        }
        match &self.resolution {
            ResolutionSpec::PriceThreshold {
                symbol, threshold, if_true_outcome, if_false_outcome, ..
            } => {
                if symbol.trim().is_empty() {
                    return Err(SpecError::EmptyField("resolution symbol must not be empty"));
                }
                if !threshold.is_finite() {
                    return Err(SpecError::BadThreshold);
                }
                if *if_true_outcome >= self.outcomes.len()
                    || *if_false_outcome >= self.outcomes.len()
                    || if_true_outcome == if_false_outcome
                {
                    return Err(SpecError::OutcomeIndexOutOfRange);
                }
            }
            ResolutionSpec::OperatorDeclared { criteria, source } => {
                if criteria.trim().is_empty() {
                    return Err(SpecError::EmptyField(
                        "resolution criteria must be written before anyone stakes",
                    ));
                }
                if source.trim().is_empty() {
                    return Err(SpecError::EmptyField("a resolution source must be named"));
                }
            }
            ResolutionSpec::MutualAgreement { criteria } => {
                if criteria.trim().is_empty() {
                    return Err(SpecError::EmptyField(
                        "say how the two of you will know who won — a bet with no stated test is \
                         an argument with money on it",
                    ));
                }
                // Two outcomes because it is a two-party bet: with three, two people agreeing
                // still leaves an outcome nobody chose, and "they agreed" stops meaning the
                // question is settled.
                if self.outcomes.len() != 2 {
                    return Err(SpecError::EmptyField(
                        "a bet settled by agreement has exactly two sides",
                    ));
                }
            }
        }
        Ok(())
    }

    /// Which outcome a machine-resolved market settles to, given the observed value.
    /// `None` for markets that no feed can decide.
    pub fn outcome_for_observation(&self, observed: f64) -> Option<usize> {
        match &self.resolution {
            ResolutionSpec::PriceThreshold {
                comparator, threshold, if_true_outcome, if_false_outcome, ..
            } => Some(if comparator.holds(observed, *threshold) {
                *if_true_outcome
            } else {
                *if_false_outcome
            }),
            // Neither of these has a feed to read: one waits for the operator, the other for
            // the two parties to say the same thing.
            ResolutionSpec::OperatorDeclared { .. } | ResolutionSpec::MutualAgreement { .. } => None,
        }
    }

    /// Whether a proposal has sat unchallenged long enough to be paid out.
    pub fn dispute_window_elapsed(&self, now_ms: i64) -> bool {
        match &self.proposal {
            Some(p) => now_ms >= p.proposed_at_ms + self.dispute_window_ms,
            None => false,
        }
    }

    pub fn is_open_at(&self, now_ms: i64) -> bool {
        self.status == MarketStatus::Open && now_ms < self.closes_at_ms
    }

    /// The status as of `now`, accounting for a close time that has passed without anything
    /// having explicitly moved the market on.
    ///
    /// Staking was already correctly refused past `closes_at_ms` — `validate_stake` checks the
    /// clock, not just the flag — but the *displayed* status stayed "Open", which reads to an
    /// agent (or a bot polling the market list) as an invitation to stake into something that
    /// will reject it. Derive it rather than needing a background task to keep a field truthful.

    /// How long the operator has to review a challenge before the market voids itself.
    ///
    /// Deliberately generous — a real dispute deserves a considered answer, and voiding is a
    /// worse outcome than a correct late resolution. But it is finite, because the alternative is
    /// an objection nobody answers holding everyone's money forever.
    pub const DISPUTE_REVIEW_MS: i64 = 24 * 60 * 60 * 1000;

    /// Whether a frozen payout has waited longer than anyone should have to.
    pub fn review_overdue(&self, now_ms: i64) -> bool {
        match self.disputed_at_ms {
            Some(at) => now_ms >= at + Self::DISPUTE_REVIEW_MS,
            None => false,
        }
    }

    /// How long is left on the review clock, in milliseconds. `None` when nothing is frozen.
    pub fn review_remaining_ms(&self, now_ms: i64) -> Option<i64> {
        self.disputed_at_ms.map(|at| (at + Self::DISPUTE_REVIEW_MS - now_ms).max(0))
    }

    pub fn effective_status(&self, now_ms: i64) -> MarketStatus {
        if self.status == MarketStatus::Open && now_ms >= self.closes_at_ms {
            MarketStatus::Closed
        } else {
            self.status
        }
    }

    /// Test-only: mutate a term without updating the commitment, to prove tampering is detected.
    #[cfg(test)]
    fn threshold_tamper(&mut self) {
        if let ResolutionSpec::PriceThreshold { threshold, .. } = &mut self.resolution {
            *threshold = 1.0;
        }
    }

    /// Validates a stake against this market. Separated from pool bookkeeping so the caller can
    /// check affordability against balances before committing anything.
    pub fn validate_stake(
        &self,
        outcome_idx: usize,
        amount: f64,
        now_ms: i64,
    ) -> Result<(), MarketError> {
        if self.outcomes.len() < 2 {
            return Err(MarketError::MalformedMarket("needs at least two outcomes"));
        }
        // Refuse to take money for a market whose published terms no longer hash to what was
        // committed. Cheap to check, and it is the difference between a promise and a proof.
        if !self.commitment_is_intact() {
            return Err(MarketError::TermsAltered);
        }
        if self.status != MarketStatus::Open {
            return Err(MarketError::NotOpen);
        }
        if now_ms >= self.closes_at_ms {
            return Err(MarketError::Closed);
        }
        if outcome_idx >= self.outcomes.len() {
            return Err(MarketError::UnknownOutcome {
                given: outcome_idx,
                outcomes: self.outcomes.len(),
            });
        }
        // Same ceiling as every other money path: a stake beyond it cannot be paid out without
        // the arithmetic overflowing, and an unpayable stake is worse than a refused one.
        if !crate::credits::is_sane_amount(amount) {
            return Err(MarketError::NonPositiveStake);
        }
        Ok(())
    }
}

/// Totals staked per outcome.
pub fn pools(stakes: &[Stake], outcome_count: usize) -> Vec<f64> {
    let mut out = vec![0.0; outcome_count];
    for s in stakes {
        if s.outcome_idx < outcome_count {
            out[s.outcome_idx] += s.amount;
        }
    }
    out
}

/// Implied probability of each outcome, from where the money actually sits.
///
/// This is the market's forecast, and it is the thing worth selling: a consensus produced by
/// participants who are paying to be right, which is a stronger signal than an opinion poll.
/// Returns None when nothing has been staked yet — an empty market has no opinion, and inventing
/// a uniform prior here would let a market with no participants masquerade as a 50/50 forecast.
pub fn implied_probabilities(stakes: &[Stake], outcome_count: usize) -> Option<Vec<f64>> {
    let p = pools(stakes, outcome_count);
    let total: f64 = p.iter().sum();
    if total <= 0.0 {
        return None;
    }
    Some(p.iter().map(|x| x / total).collect())
}

/// Refunds every stake in full, with no rake. Used when a market cannot be resolved, and when
/// nobody picked the winning outcome.
pub fn void(market_id: &str, stakes: &[Stake], reason: &'static str) -> Settlement {
    let mut by_agent: HashMap<&str, f64> = HashMap::new();
    for s in stakes {
        *by_agent.entry(s.agent_id.as_str()).or_insert(0.0) += s.amount;
    }
    let total: f64 = stakes.iter().map(|s| s.amount).sum();
    let mut payouts: Vec<(String, f64)> =
        by_agent.into_iter().map(|(a, v)| (a.to_string(), v)).collect();
    payouts.sort_by(|a, b| a.0.cmp(&b.0)); // deterministic order, so replay matches

    Settlement {
        market_id: market_id.to_string(),
        payouts,
        rake: 0.0,
        rebate: 0.0,
        total_pool: total,
        winning_pool: 0.0,
        losing_pool: 0.0,
        refunded: true,
        reason,
    }
}

/// Settles a resolved market.
///
/// See the module header for the payout formula and why the rake comes only from the losing pool.
/// When nobody backed the winning outcome there is no one to pay and no work the house did worth
/// charging for, so everyone is refunded rather than the house taking the entire pool — which
/// would otherwise create a quiet incentive to write markets nobody can get right.
/// The span a market accepted stakes over, used to work out who was early.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct StakeWindow {
    pub opened_at_ms: i64,
    pub closes_at_ms: i64,
}

impl StakeWindow {
    /// How early a stake was, as 1.0 at the opening bell falling to 0.0 at the close.
    ///
    /// Clamped at both ends so a stake recorded fractionally outside the window — a clock nudge,
    /// a market whose close was reached mid-request — can never earn more than a stake placed at
    /// the very start, and can never earn a negative weight.
    pub fn earliness(&self, placed_at_ms: i64) -> f64 {
        let span = self.closes_at_ms - self.opened_at_ms;
        if span <= 0 {
            return 0.0;
        }
        let remaining = (self.closes_at_ms - placed_at_ms) as f64 / span as f64;
        remaining.clamp(0.0, 1.0)
    }
}

/// Share of the rake handed back to early stakers. 25% of the house's cut.
///
/// # Why the house pays for this
///
/// Pari-mutuel has a structural defect that only bites when the participants are programs: your
/// payout depends on the *final* pool, which you cannot see when you stake. Every later bettor
/// gets strictly more information than you did and can size against you. So the dominant strategy
/// is to wait, and if every participant is a program running the same reasoning, every participant
/// waits. The board sits empty until the last second and then clears badly, or does not clear at
/// all. Humans bet early anyway because betting is entertainment. Nothing about an agent is
/// entertained.
///
/// The fix is to pay for the thing you need: whoever stakes first is carrying the risk that makes
/// the market exist, so they are paid for it. Critically the payment comes out of the *house's*
/// cut and never out of another participant's stake, so no one is worse off than they would have
/// been under a flat rake — winners get slightly more, and the operator earns slightly less on
/// markets that needed help getting started.
///
/// # Why it cannot be farmed
///
/// The obvious attack is to stake both sides early and collect the rebate on both. It always
/// loses: the most an agent can collect back is the whole rebate pool, which is
/// `rake × REBATE_SHARE`, while it pays the full `rake` on its losing side. For any share below
/// 100% the round trip is negative, whatever the amounts, and `no_self_dealing_profit` fuzzes
/// that.
pub const DEFAULT_REBATE_SHARE_BPS: f64 = 2_500.0;

/// Settles with a flat rake and no early-liquidity rebate.
///
/// Kept as the plain form because most of the settlement tests are about the payout rule itself,
/// and they should not have to describe a stake window to say something about pro-rata splits.
pub fn settle(
    market_id: &str,
    stakes: &[Stake],
    outcome_count: usize,
    winning_outcome: usize,
    rake_bps: f64,
) -> Settlement {
    settle_windowed(market_id, stakes, outcome_count, winning_outcome, rake_bps, 0.0, None)
}

/// Settles a resolved market, paying early stakers a rebate out of the house's rake.
pub fn settle_windowed(
    market_id: &str,
    stakes: &[Stake],
    outcome_count: usize,
    winning_outcome: usize,
    rake_bps: f64,
    rebate_share_bps: f64,
    window: Option<StakeWindow>,
) -> Settlement {
    if winning_outcome >= outcome_count {
        return void(market_id, stakes, "winning outcome was not one of this market's outcomes");
    }

    let p = pools(stakes, outcome_count);
    let total_pool: f64 = p.iter().sum();
    let winning_pool = p[winning_outcome];
    let losing_pool = total_pool - winning_pool;

    if total_pool <= 0.0 {
        return Settlement {
            market_id: market_id.to_string(),
            payouts: Vec::new(),
            rake: 0.0,
            rebate: 0.0,
            total_pool: 0.0,
            winning_pool: 0.0,
            losing_pool: 0.0,
            refunded: false,
            reason: "no stakes were placed",
        };
    }

    if winning_pool <= 0.0 {
        return void(market_id, stakes, "nobody backed the winning outcome — all stakes refunded");
    }

    let rake_bps = rake_bps.clamp(0.0, 10_000.0);
    let rake = losing_pool * rake_bps / 10_000.0;
    let distributable = losing_pool - rake;

    let mut by_agent: HashMap<&str, f64> = HashMap::new();
    for s in stakes.iter().filter(|s| s.outcome_idx == winning_outcome) {
        let share = s.amount / winning_pool;
        *by_agent.entry(s.agent_id.as_str()).or_insert(0.0) += s.amount + distributable * share;
    }

    // The early-liquidity rebate, paid out of the rake to everyone who staked before they had to,
    // winners and losers alike. It is weighted by stake size *and* by how early the stake landed,
    // because both are what a market needs to exist: a small late bet adds almost nothing another
    // bettor could not have supplied a second later.
    //
    // Losing stakers can receive a rebate. That is intended — the agent who opened the book took
    // the real risk whichever way the answer went — and it is safe because the rebate is strictly
    // smaller than the rake that same losing stake paid.
    let rebate_share_bps = rebate_share_bps.clamp(0.0, 9_999.0);
    let mut rebate_paid = 0.0;
    if let Some(w) = window {
        let rebate_pool = rake * rebate_share_bps / 10_000.0;
        if rebate_pool > 0.0 {
            let weights: Vec<f64> =
                stakes.iter().map(|s| s.amount * w.earliness(s.placed_at_ms)).collect();
            let total_weight: f64 = weights.iter().sum();
            // Every stake landing exactly at the close carries zero weight. There is then nobody
            // the rebate is owed to, and it stays with the house rather than being invented into
            // existence somewhere.
            if total_weight > 0.0 && total_weight.is_finite() {
                for (s, weight) in stakes.iter().zip(weights) {
                    let cut = rebate_pool * weight / total_weight;
                    if cut > 0.0 {
                        *by_agent.entry(s.agent_id.as_str()).or_insert(0.0) += cut;
                        rebate_paid += cut;
                    }
                }
            }
        }
    }

    let mut payouts: Vec<(String, f64)> =
        by_agent.into_iter().map(|(a, v)| (a.to_string(), v)).collect();
    payouts.sort_by(|a, b| a.0.cmp(&b.0));

    // Floating-point shares can sum to a hair over the pool. Paying out money that was never
    // staked is the one failure this engine must not have, so any excess is trimmed from the
    // largest payout — the holder least harmed in relative terms by losing a few atoms.
    let paid: f64 = payouts.iter().map(|(_, v)| v).sum();
    let budget = total_pool - rake + rebate_paid;
    if paid > budget {
        let excess = paid - budget;
        if let Some(largest) = payouts
            .iter_mut()
            .max_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal))
        {
            largest.1 -= excess;
        }
    }

    Settlement {
        market_id: market_id.to_string(),
        payouts,
        rake,
        rebate: rebate_paid,
        total_pool,
        winning_pool,
        losing_pool,
        refunded: false,
        reason: "settled",
    }
}

/// Brier score for one agent's implied forecast in a resolved market: the squared error between
/// where they put their money and what actually happened. Lower is better; 0 is perfect.
///
/// The stake allocation *is* the probability statement — an agent putting 70% of its stake on YES
/// is saying 70%, and it is paying for the privilege. That makes this a costly signal rather than
/// a survey answer, which is the whole reason a market's consensus beats a poll.
///
/// Returns None for an agent with no stake in the market — no forecast, nothing to score.
pub fn brier_score(
    stakes: &[Stake],
    agent_id: &str,
    outcome_count: usize,
    winning_outcome: usize,
) -> Option<f64> {
    let mut mine = vec![0.0; outcome_count];
    let mut total = 0.0;
    for s in stakes.iter().filter(|s| s.agent_id == agent_id) {
        if s.outcome_idx < outcome_count {
            mine[s.outcome_idx] += s.amount;
            total += s.amount;
        }
    }
    if total <= 0.0 || winning_outcome >= outcome_count {
        return None;
    }
    let score = mine
        .iter()
        .enumerate()
        .map(|(i, amount)| {
            let forecast = amount / total;
            let actual = if i == winning_outcome { 1.0 } else { 0.0 };
            (forecast - actual).powi(2)
        })
        .sum();
    Some(score)
}

/// How much trust a resolved market awards, from how well the agent forecast it.
///
/// This fixes a real dead end rather than adding decoration: before it, `TrustStore` was only
/// ever written at registration, so every self-registered agent sat at score 0 — the New band,
/// 10 requests per second — **forever**, however long or well it participated. There was no path
/// to a higher rate limit at all, which left the whole trust-tier system inert.
///
/// The award is driven by the Brier score, so it is earned by being *calibrated* rather than by
/// being busy. A confident correct call (Brier 0) earns the full award; a hedge (0.5) earns
/// nothing; a confident wrong call (2.0) costs the same as being right earned. Volume alone earns
/// nothing, which is what stops an agent farming a rate limit by spraying tiny stakes at coin
/// flips.
pub fn trust_delta_from_brier(brier: f64) -> i32 {
    // Brier for a two-outcome forecast runs 0 (perfect) to 2 (perfectly wrong), with 0.5 the
    // coin flip. Map that to +10..-10, crossing zero exactly at the coin flip.
    let scaled = (0.5 - brier) * 20.0;
    scaled.clamp(-10.0, 10.0).round() as i32
}

/// Trust awarded for a forecast, measured against the market's own consensus rather than against
/// the truth alone.
///
/// # The exploit this closes
///
/// Rewarding raw accuracy looks right and is farmable the moment agents can open their own
/// markets: create "will BTC be above $1", stake YES, score a perfect Brier, collect the maximum
/// award, repeat until you hold the top rate limit. The market is real, the forecast is correct,
/// and the whole thing is worthless — being right about a certainty demonstrates nothing.
///
/// So the award is a **skill score**: how much better your forecast was than the pooled consensus
/// everyone could already see. Agreeing with a 99%-certain crowd earns nothing, because the crowd
/// already knew. Beating the crowd earns, and being confidently wrong against it costs.
///
/// Two consequences worth noting, both deliberate:
///
/// - A market where everyone agreed moves nobody's trust at all, mirroring the rake rule — no
///   disagreement resolved, nothing earned, by either the house or the participants.
/// - You can be *wrong* and still gain, if you were less wrong than the crowd. That is correct:
///   calibration is about the quality of the estimate, not the luck of the outcome.
pub fn trust_delta_vs_consensus(agent_brier: f64, consensus_brier: f64) -> i32 {
    // Positive when the agent beat the pool. Scaled so a decisive edge reaches the same ±10 as
    // the absolute version, and rounded toward zero so noise doesn't drift scores upward.
    let edge = consensus_brier - agent_brier;
    (edge * 20.0).clamp(-10.0, 10.0).trunc() as i32
}

/// The Brier score of the market's own pooled forecast — the benchmark every participant is
/// scored against.
pub fn consensus_brier(
    stakes: &[Stake],
    outcome_count: usize,
    winning_outcome: usize,
) -> Option<f64> {
    let probs = implied_probabilities(stakes, outcome_count)?;
    if winning_outcome >= outcome_count {
        return None;
    }
    Some(
        probs
            .iter()
            .enumerate()
            .map(|(i, p)| {
                let actual = if i == winning_outcome { 1.0 } else { 0.0 };
                (p - actual).powi(2)
            })
            .sum(),
    )
}

#[cfg(test)]
mod rebate_tests {
    use super::*;

    const OPEN: i64 = 1_000_000;
    const CLOSE: i64 = 1_000_000 + 60_000;
    const W: StakeWindow = StakeWindow { opened_at_ms: OPEN, closes_at_ms: CLOSE };

    fn stake(agent: &str, outcome: usize, amount: f64, at: i64) -> Stake {
        Stake {
            agent_id: agent.to_string(),
            outcome_idx: outcome,
            amount,
            placed_at_ms: at,
        }
    }

    fn paid(s: &Settlement, agent: &str) -> f64 {
        s.payouts.iter().find(|(a, _)| a == agent).map(|(_, v)| *v).unwrap_or(0.0)
    }

    fn settled(stakes: &[Stake], winner: usize) -> Settlement {
        settle_windowed("m", stakes, 2, winner, DEFAULT_RAKE_BPS, DEFAULT_REBATE_SHARE_BPS, Some(W))
    }

    #[test]
    fn showing_up_first_pays_better_than_showing_up_last() {
        // The whole point. Two agents, same side, same size, same result — the one who made the
        // market exist is paid more than the one who waited to see what it looked like.
        let stakes = vec![
            stake("early", 0, 100.0, OPEN),
            stake("late", 0, 100.0, CLOSE - 1),
            stake("loser", 1, 400.0, OPEN + 30_000),
        ];
        let s = settled(&stakes, 0);
        assert!(
            paid(&s, "early") > paid(&s, "late"),
            "early {} should beat late {}",
            paid(&s, "early"),
            paid(&s, "late")
        );
    }

    #[test]
    fn the_rebate_comes_out_of_the_house_and_nobody_else() {
        let stakes = vec![
            stake("a", 0, 100.0, OPEN),
            stake("b", 1, 100.0, OPEN),
        ];
        let flat = settle("m", &stakes, 2, 0, DEFAULT_RAKE_BPS);
        let with_rebate = settled(&stakes, 0);

        assert!(
            with_rebate.house_take() < flat.rake,
            "the operator is the one funding this"
        );
        for (agent, amount) in &flat.payouts {
            assert!(
                paid(&with_rebate, agent) >= *amount - 1e-9,
                "{agent} was paid less than a flat rake would have paid them"
            );
        }
    }

    #[test]
    fn a_losing_stake_can_earn_a_rebate_but_never_more_than_it_lost() {
        let stakes = vec![
            stake("winner", 0, 100.0, CLOSE - 1),
            stake("early_loser", 1, 100.0, OPEN),
        ];
        let s = settled(&stakes, 0);
        let back = paid(&s, "early_loser");
        assert!(back > 0.0, "the agent who opened the book got nothing back");
        assert!(back < 100.0, "a losing stake must still lose: got {back} of 100 back");
    }

    #[test]
    fn no_self_dealing_profit() {
        // Stake both sides early and collect the rebate on both. It must always lose money,
        // whatever the amounts, whatever the share, or the rebate is a faucet.
        let mut seed = 0x5eed_u64;
        let mut rand = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            ((seed >> 33) as f64) / (u32::MAX as f64)
        };
        for _ in 0..2_000 {
            let a = 1.0 + rand() * 10_000.0;
            let b = 1.0 + rand() * 10_000.0;
            let t0 = OPEN + (rand() * 60_000.0) as i64;
            let t1 = OPEN + (rand() * 60_000.0) as i64;
            let winner = if rand() > 0.5 { 0 } else { 1 };
            let stakes = vec![stake("wash", 0, a, t0), stake("wash", 1, b, t1)];
            let s = settled(&stakes, winner);
            let staked = a + b;
            let back = paid(&s, "wash");
            assert!(
                back < staked,
                "wash trade profited: staked {staked}, got back {back}"
            );
        }
    }

    #[test]
    fn the_money_still_adds_up_exactly() {
        let mut seed = 0xd15ea5e_u64;
        let mut rand = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            ((seed >> 33) as f64) / (u32::MAX as f64)
        };
        for _ in 0..2_000 {
            let n = 1 + (rand() * 8.0) as usize;
            let stakes: Vec<Stake> = (0..n)
                .map(|i| {
                    stake(
                        &format!("a{i}"),
                        if rand() > 0.5 { 0 } else { 1 },
                        1.0 + rand() * 5_000.0,
                        OPEN + (rand() * 60_000.0) as i64,
                    )
                })
                .collect();
            let s = settled(&stakes, if rand() > 0.5 { 0 } else { 1 });
            let out: f64 = s.payouts.iter().map(|(_, v)| v).sum();
            let staked: f64 = stakes.iter().map(|x| x.amount).sum();
            assert!(
                out + s.house_take() <= staked + 1e-6,
                "paid out {out} + kept {} from a pool of {staked}",
                s.house_take()
            );
            assert!(s.rebate <= s.rake + 1e-9, "rebated more than was raked");
            assert!(s.house_take() >= 0.0, "the house paid to run a market");
        }
    }

    #[test]
    fn a_winner_is_never_paid_less_than_they_staked() {
        // The rebate must not be able to break the one promise the venue makes.
        let mut seed = 0xfeed_u64;
        let mut rand = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            ((seed >> 33) as f64) / (u32::MAX as f64)
        };
        for _ in 0..1_000 {
            let n = 2 + (rand() * 6.0) as usize;
            let stakes: Vec<Stake> = (0..n)
                .map(|i| {
                    stake(
                        &format!("a{i}"),
                        i % 2,
                        1.0 + rand() * 1_000.0,
                        OPEN + (rand() * 60_000.0) as i64,
                    )
                })
                .collect();
            let s = settled(&stakes, 0);
            for st in stakes.iter().filter(|x| x.outcome_idx == 0) {
                assert!(
                    paid(&s, &st.agent_id) >= st.amount - 1e-6,
                    "{} staked {} and got {}",
                    st.agent_id,
                    st.amount,
                    paid(&s, &st.agent_id)
                );
            }
        }
    }

    #[test]
    fn a_market_that_took_every_stake_at_the_bell_keeps_the_rebate_with_the_house() {
        let stakes = vec![stake("a", 0, 100.0, CLOSE), stake("b", 1, 100.0, CLOSE)];
        let s = settled(&stakes, 0);
        assert_eq!(s.rebate, 0.0, "nobody was early, so nobody is owed anything");
        assert!((s.house_take() - s.rake).abs() < 1e-12);
    }

    #[test]
    fn a_zero_length_window_cannot_divide_by_zero() {
        let w = StakeWindow { opened_at_ms: OPEN, closes_at_ms: OPEN };
        let stakes = vec![stake("a", 0, 100.0, OPEN), stake("b", 1, 100.0, OPEN)];
        let s = settle_windowed("m", &stakes, 2, 0, DEFAULT_RAKE_BPS, DEFAULT_REBATE_SHARE_BPS, Some(w));
        assert!(s.payouts.iter().all(|(_, v)| v.is_finite()), "produced a non-finite payout");
        assert_eq!(s.rebate, 0.0);
    }

    #[test]
    fn earliness_is_clamped_at_both_ends() {
        assert_eq!(W.earliness(OPEN - 10_000), 1.0, "a stake before the open is not worth extra");
        assert_eq!(W.earliness(CLOSE + 10_000), 0.0, "a stake after the close is not worth less than nothing");
        assert!((W.earliness(OPEN + 30_000) - 0.5).abs() < 1e-9);
    }

    #[test]
    fn a_void_pays_no_rebate_because_it_took_no_rake() {
        let s = void("m", &[stake("a", 0, 100.0, OPEN)], "test");
        assert_eq!(s.rake, 0.0);
        assert_eq!(s.rebate, 0.0);
        assert_eq!(s.house_take(), 0.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stake(agent: &str, outcome: usize, amount: f64) -> Stake {
        Stake { agent_id: agent.into(), outcome_idx: outcome, amount, placed_at_ms: 0 }
    }

    fn paid(s: &Settlement, agent: &str) -> f64 {
        s.payouts.iter().find(|(a, _)| a == agent).map(|(_, v)| *v).unwrap_or(0.0)
    }

    fn market(now: i64) -> Market {
        let mut m = Market {
            market_id: "m_test".into(),
            question: "Will BTC be above 70000 at the observation time?".into(),
            outcomes: vec!["YES".into(), "NO".into()],
            resolution: ResolutionSpec::PriceThreshold {
                symbol: "BTC-USD".into(),
                comparator: Comparator::Above,
                threshold: 70_000.0,
                if_true_outcome: 0,
                if_false_outcome: 1,
            },
            asset: "PTS".into(),
            closes_at_ms: now + 60_000,
            observed_at_ms: now + 120_000,
            dispute_window_ms: 30_000,
            commitment: String::new(),
            status: MarketStatus::Open,
            proposal: None,
            winning_outcome: None,
            created_at_ms: now,
        disputed_at_ms: None,
        };
        m.commitment = m.compute_commitment();
        m
    }

    // ---- the property that makes this launchable at all -------------------------------------

    #[test]
    fn a_single_participant_alone_and_correct_loses_nothing() {
        // The whole cold-start argument. One bot, no counterparty, no market maker, no other
        // users — and the settlement is still valid and costs them nothing.
        let stakes = vec![stake("agent_a", 0, 100.0)];
        let s = settle("m", &stakes, 2, 0, DEFAULT_RAKE_BPS);
        assert_eq!(paid(&s, "agent_a"), 100.0);
        assert_eq!(s.rake, 0.0, "there was no losing pool, so the house earns nothing");
        assert!(!s.refunded, "this is a real settlement, not a refund");
    }

    #[test]
    fn a_single_participant_alone_and_wrong_simply_loses_their_stake() {
        let stakes = vec![stake("agent_a", 1, 100.0)];
        let s = settle("m", &stakes, 2, 0, DEFAULT_RAKE_BPS);
        // Nobody backed the winner, so there is nothing to distribute and nothing earned.
        assert!(s.refunded);
        assert_eq!(paid(&s, "agent_a"), 100.0, "refunded rather than confiscated");
        assert_eq!(s.rake, 0.0);
    }

    // ---- the money math ----------------------------------------------------------------------

    #[test]
    fn winners_split_the_losing_pool_pro_rata_after_rake() {
        // 100 + 300 on YES, 400 on NO. YES wins.
        // losing_pool 400, rake 3% = 12, distributable 388.
        // a: 100 + 388*(100/400) = 197.0 ; b: 300 + 388*(300/400) = 591.0
        let stakes = vec![
            stake("agent_a", 0, 100.0),
            stake("agent_b", 0, 300.0),
            stake("agent_c", 1, 400.0),
        ];
        let s = settle("m", &stakes, 2, 0, 300.0);
        assert!((s.rake - 12.0).abs() < 1e-9, "rake was {}", s.rake);
        assert!((paid(&s, "agent_a") - 197.0).abs() < 1e-9);
        assert!((paid(&s, "agent_b") - 591.0).abs() < 1e-9);
        assert_eq!(paid(&s, "agent_c"), 0.0, "the loser gets nothing back");
    }

    #[test]
    fn a_winner_never_receives_less_than_they_staked() {
        // The core promise of taking the rake from the losing pool only.
        for rake in [0.0, 100.0, 500.0, 2_000.0, 10_000.0] {
            let stakes = vec![
                stake("winner", 0, 10.0),
                stake("loser", 1, 1_000_000.0),
            ];
            let s = settle("m", &stakes, 2, 0, rake);
            assert!(
                paid(&s, "winner") >= 10.0 - 1e-9,
                "rake {rake}bps left a correct forecaster worse off: got {}",
                paid(&s, "winner")
            );
        }
    }

    #[test]
    fn unanimous_agreement_costs_nobody_anything() {
        // Everyone picked the same outcome and it happened: no disagreement was resolved, so the
        // house earns nothing and everyone is simply made whole.
        let stakes = vec![stake("a", 0, 50.0), stake("b", 0, 25.0), stake("c", 0, 25.0)];
        let s = settle("m", &stakes, 2, 0, DEFAULT_RAKE_BPS);
        assert_eq!(s.rake, 0.0);
        assert!((paid(&s, "a") - 50.0).abs() < 1e-9);
        assert!((paid(&s, "b") - 25.0).abs() < 1e-9);
        assert!((paid(&s, "c") - 25.0).abs() < 1e-9);
    }

    #[test]
    fn the_house_never_pays_out_more_than_was_staked() {
        // Adversarial split sizes chosen to make float shares awkward.
        let stakes = vec![
            stake("a", 0, 1.0 / 3.0),
            stake("b", 0, 2.0 / 7.0),
            stake("c", 0, 1e-9),
            stake("d", 1, 999.9999),
            stake("e", 1, 0.0001),
        ];
        let s = settle("m", &stakes, 2, 0, 250.0);
        let out: f64 = s.payouts.iter().map(|(_, v)| v).sum();
        assert!(
            out + s.rake <= s.total_pool + 1e-9,
            "distributed {out} + rake {} exceeds pool {}",
            s.rake,
            s.total_pool
        );
    }

    #[test]
    fn conservation_holds_across_many_random_shapes() {
        // Cheap deterministic pseudo-random sweep — no rand crate available, and a fixed
        // generator makes failures reproducible anyway.
        let mut seed: u64 = 0x5eed_1234;
        let mut next = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            ((seed >> 33) as f64) / (u32::MAX as f64)
        };
        for round in 0..500 {
            let n = 1 + (next() * 8.0) as usize;
            let outcomes = 2 + (next() * 3.0) as usize;
            let stakes: Vec<Stake> = (0..n)
                .map(|i| {
                    stake(
                        &format!("agent_{i}"),
                        (next() * outcomes as f64) as usize % outcomes,
                        (next() * 10_000.0) + 0.000_001,
                    )
                })
                .collect();
            let winner = (next() * outcomes as f64) as usize % outcomes;
            let s = settle("m", &stakes, outcomes, winner, 300.0);
            let out: f64 = s.payouts.iter().map(|(_, v)| v).sum();
            assert!(
                out + s.rake <= s.total_pool + 1e-6,
                "round {round}: distributed {out} + rake {} > pool {}",
                s.rake,
                s.total_pool
            );
            for (agent, amount) in &s.payouts {
                assert!(*amount >= -1e-9, "round {round}: negative payout to {agent}: {amount}");
            }
        }
    }

    #[test]
    fn nobody_backed_the_winner_means_refunds_not_a_windfall_for_the_house() {
        // Otherwise the operator profits most from markets nobody can get right, which is a
        // direct incentive to write bad questions.
        let stakes = vec![stake("a", 0, 100.0), stake("b", 0, 200.0)];
        let s = settle("m", &stakes, 3, 2, DEFAULT_RAKE_BPS);
        assert!(s.refunded);
        assert_eq!(s.rake, 0.0);
        assert!((paid(&s, "a") - 100.0).abs() < 1e-9);
        assert!((paid(&s, "b") - 200.0).abs() < 1e-9);
    }

    #[test]
    fn an_empty_market_settles_to_nothing_rather_than_erroring() {
        let s = settle("m", &[], 2, 0, DEFAULT_RAKE_BPS);
        assert!(s.payouts.is_empty());
        assert_eq!(s.total_pool, 0.0);
        assert_eq!(s.rake, 0.0);
    }

    #[test]
    fn multi_outcome_markets_work_not_just_binary() {
        // 3-way: 100 on A, 100 on B, 200 on C. B wins.
        // losing 300, rake 3% = 9, distributable 291. b: 100 + 291 = 391.
        let stakes = vec![stake("a", 0, 100.0), stake("b", 1, 100.0), stake("c", 2, 200.0)];
        let s = settle("m", &stakes, 3, 1, 300.0);
        assert!((s.rake - 9.0).abs() < 1e-9);
        assert!((paid(&s, "b") - 391.0).abs() < 1e-9);
    }

    #[test]
    fn one_agent_hedging_across_outcomes_nets_out_correctly() {
        // The same agent on both sides: it must be paid on its winning leg and lose the other,
        // not have them silently cancel.
        let stakes = vec![
            stake("hedger", 0, 100.0),
            stake("hedger", 1, 100.0),
            stake("other", 1, 100.0),
        ];
        let s = settle("m", &stakes, 2, 0, 0.0);
        // Winning pool is the hedger's 100; losing pool 200, no rake. Hedger gets 100 + 200 = 300.
        assert!((paid(&s, "hedger") - 300.0).abs() < 1e-9);
    }

    #[test]
    fn a_voided_market_refunds_everyone_in_full() {
        let stakes = vec![stake("a", 0, 10.0), stake("b", 1, 90.0), stake("a", 1, 5.0)];
        let s = void("m", &stakes, "resolution source unavailable");
        assert!(s.refunded);
        assert_eq!(s.rake, 0.0);
        assert!((paid(&s, "a") - 15.0).abs() < 1e-9, "both of a's stakes come back");
        assert!((paid(&s, "b") - 90.0).abs() < 1e-9);
    }

    #[test]
    fn payouts_are_deterministically_ordered_so_replay_matches() {
        let stakes = vec![stake("zeta", 0, 1.0), stake("alpha", 0, 1.0), stake("mid", 0, 1.0)];
        let s = settle("m", &stakes, 2, 0, 0.0);
        let names: Vec<&str> = s.payouts.iter().map(|(a, _)| a.as_str()).collect();
        assert_eq!(names, vec!["alpha", "mid", "zeta"]);
    }

    // ---- market state ------------------------------------------------------------------------

    #[test]
    fn stakes_are_refused_after_close() {
        let now = 1_800_000_000_000;
        let m = market(now);
        assert!(m.validate_stake(0, 10.0, now).is_ok());
        assert_eq!(m.validate_stake(0, 10.0, now + 60_001), Err(MarketError::Closed));
    }

    #[test]
    fn stakes_are_validated_before_anything_is_committed() {
        let now = 1_800_000_000_000;
        let m = market(now);
        assert_eq!(
            m.validate_stake(5, 10.0, now),
            Err(MarketError::UnknownOutcome { given: 5, outcomes: 2 })
        );
        assert_eq!(m.validate_stake(0, 0.0, now), Err(MarketError::NonPositiveStake));
        assert_eq!(m.validate_stake(0, -5.0, now), Err(MarketError::NonPositiveStake));
        assert_eq!(m.validate_stake(0, f64::NAN, now), Err(MarketError::NonPositiveStake));
        assert_eq!(m.validate_stake(0, f64::INFINITY, now), Err(MarketError::NonPositiveStake));
    }

    #[test]
    fn a_resolved_market_takes_no_more_stakes() {
        let now = 1_800_000_000_000;
        let mut m = market(now);
        m.status = MarketStatus::Resolved;
        assert_eq!(m.validate_stake(0, 10.0, now), Err(MarketError::NotOpen));
    }

    // ---- the forecast product ------------------------------------------------------------------

    #[test]
    fn implied_probabilities_track_where_the_money_is() {
        let stakes = vec![stake("a", 0, 70.0), stake("b", 1, 30.0)];
        let p = implied_probabilities(&stakes, 2).unwrap();
        assert!((p[0] - 0.70).abs() < 1e-9);
        assert!((p[1] - 0.30).abs() < 1e-9);
    }

    #[test]
    fn an_empty_market_has_no_opinion_rather_than_a_fake_fifty_fifty() {
        assert!(implied_probabilities(&[], 2).is_none());
    }

    #[test]
    fn brier_rewards_confident_correctness_and_punishes_confident_error() {
        let confident_right = vec![stake("a", 0, 100.0)];
        let confident_wrong = vec![stake("b", 1, 100.0)];
        let hedged = vec![stake("c", 0, 50.0), stake("c", 1, 50.0)];

        assert_eq!(brier_score(&confident_right, "a", 2, 0), Some(0.0));
        assert_eq!(brier_score(&confident_wrong, "b", 2, 0), Some(2.0));
        assert_eq!(brier_score(&hedged, "c", 2, 0), Some(0.5));
    }

    #[test]
    fn agreeing_with_a_certainty_earns_no_trust() {
        // The farm this closes: an agent opens "will BTC be above $1", everyone piles onto YES,
        // it resolves YES, and under a raw-accuracy rule every participant banks a perfect score
        // for knowing nothing. Scored against the consensus, a unanimous market moves nobody.
        let unanimous = vec![stake("a", 0, 100.0), stake("b", 0, 100.0)];
        let consensus = consensus_brier(&unanimous, 2, 0).unwrap();
        let mine = brier_score(&unanimous, "a", 2, 0).unwrap();
        assert_eq!(mine, 0.0, "the forecast was correct...");
        assert_eq!(consensus, 0.0, "...but so was the crowd's");
        assert_eq!(trust_delta_vs_consensus(mine, consensus), 0, "so it demonstrated nothing");
    }

    #[test]
    fn beating_the_crowd_is_what_earns_trust() {
        // The crowd sits at 90% on NO; one agent goes all-in on YES, and YES happens.
        let stakes = vec![
            stake("contrarian", 0, 100.0),
            stake("crowd_1", 1, 450.0),
            stake("crowd_2", 1, 450.0),
        ];
        let consensus = consensus_brier(&stakes, 2, 0).unwrap();
        let contrarian = brier_score(&stakes, "contrarian", 2, 0).unwrap();
        let crowd = brier_score(&stakes, "crowd_1", 2, 0).unwrap();

        assert!(trust_delta_vs_consensus(contrarian, consensus) > 0, "the contrarian was right against the pool");
        assert!(trust_delta_vs_consensus(crowd, consensus) < 0, "the crowd was wrong and paid for it");
    }

    #[test]
    fn being_less_wrong_than_the_crowd_still_earns() {
        // Deliberate: calibration is about the quality of the estimate, not the luck of the
        // outcome. A hedged forecast that lost is still better than a confident one that lost.
        let stakes = vec![
            stake("hedged", 0, 50.0),
            stake("hedged", 1, 50.0),
            stake("confident", 1, 900.0),
        ];
        let consensus = consensus_brier(&stakes, 2, 0).unwrap();
        let hedged = brier_score(&stakes, "hedged", 2, 0).unwrap();
        assert!(
            trust_delta_vs_consensus(hedged, consensus) > 0,
            "hedging beat a crowd that was heavily wrong"
        );
    }

    #[test]
    fn an_empty_market_has_no_consensus_to_score_against() {
        assert!(consensus_brier(&[], 2, 0).is_none());
    }

    #[test]
    fn trust_is_earned_by_calibration_not_by_volume() {
        // Confident and right earns the most, a coin flip earns nothing, confident and wrong
        // costs. This is what makes the rate limit something an agent climbs by forecasting well
        // rather than by making noise.
        assert_eq!(trust_delta_from_brier(0.0), 10);
        assert!(trust_delta_from_brier(0.25) > 0);
        assert_eq!(trust_delta_from_brier(0.5), 0, "a coin flip should earn nothing");
        assert!(trust_delta_from_brier(1.0) < 0);
        assert_eq!(trust_delta_from_brier(2.0), -10);
    }

    #[test]
    fn an_agent_with_no_stake_has_no_brier_score() {
        let stakes = vec![stake("a", 0, 100.0)];
        assert_eq!(brier_score(&stakes, "nobody", 2, 0), None);
    }

    // ---- pre-stipulation: the terms cannot move after money is down ---------------------------

    #[test]
    fn the_commitment_covers_every_field_that_decides_who_wins() {
        let now = 1_800_000_000_000;
        let base = market(now);
        let original = base.compute_commitment();

        // Each of these is a way an operator could tilt a market after stakes were placed.
        // Every one must change the hash.
        let mut altered = base.clone();
        altered.question = "Will BTC be above 1 at the observation time?".into();
        assert_ne!(altered.compute_commitment(), original, "question not covered");

        let mut altered = base.clone();
        altered.outcomes = vec!["NO".into(), "YES".into()];
        assert_ne!(altered.compute_commitment(), original, "outcome order not covered");

        let mut altered = base.clone();
        altered.resolution = ResolutionSpec::PriceThreshold {
            symbol: "BTC-USD".into(),
            comparator: Comparator::Above,
            threshold: 1.0, // moved the goalposts
            if_true_outcome: 0,
            if_false_outcome: 1,
        };
        assert_ne!(altered.compute_commitment(), original, "threshold not covered");

        let mut altered = base.clone();
        altered.resolution = ResolutionSpec::PriceThreshold {
            symbol: "BTC-USD".into(),
            comparator: Comparator::Below, // flipped the comparison
            threshold: 70_000.0,
            if_true_outcome: 0,
            if_false_outcome: 1,
        };
        assert_ne!(altered.compute_commitment(), original, "comparator not covered");

        let mut altered = base.clone();
        altered.observed_at_ms = now + 999_000;
        assert_ne!(altered.compute_commitment(), original, "observation time not covered");

        let mut altered = base.clone();
        altered.closes_at_ms = now + 5;
        assert_ne!(altered.compute_commitment(), original, "close time not covered");

        let mut altered = base.clone();
        altered.dispute_window_ms = 0;
        assert_ne!(altered.compute_commitment(), original, "dispute window not covered");

        let mut altered = base.clone();
        altered.asset = "CRD".into();
        assert_ne!(altered.compute_commitment(), original, "asset not covered");
    }

    #[test]
    fn the_commitment_is_stable_for_unchanged_terms() {
        let m = market(1_800_000_000_000);
        assert_eq!(m.compute_commitment(), m.compute_commitment());
        assert!(m.commitment_is_intact());
    }

    #[test]
    fn a_market_whose_terms_were_altered_refuses_stakes() {
        let now = 1_800_000_000_000;
        let mut m = market(now);
        m.threshold_tamper();
        assert!(!m.commitment_is_intact());
        assert_eq!(m.validate_stake(0, 10.0, now), Err(MarketError::TermsAltered));
    }

    // ---- the settlement gap -------------------------------------------------------------------

    #[test]
    fn a_market_cannot_be_observed_before_or_when_it_closes() {
        let now = 1_800_000_000_000;
        let mut m = market(now);

        m.observed_at_ms = m.closes_at_ms;
        assert_eq!(m.validate_spec(now), Err(SpecError::NoSettlementGap));

        m.observed_at_ms = m.closes_at_ms - 1;
        assert_eq!(m.validate_spec(now), Err(SpecError::NoSettlementGap));

        m.observed_at_ms = m.closes_at_ms + 1;
        assert!(m.validate_spec(now).is_ok());
    }

    #[test]
    fn malformed_terms_are_refused_up_front() {
        let now = 1_800_000_000_000;

        let mut m = market(now);
        m.outcomes = vec!["ONLY".into()];
        assert_eq!(m.validate_spec(now), Err(SpecError::TooFewOutcomes));

        let mut m = market(now);
        m.outcomes = vec!["SAME".into(), "SAME".into()];
        assert_eq!(m.validate_spec(now), Err(SpecError::DuplicateOutcomes));

        let mut m = market(now);
        m.closes_at_ms = now - 1;
        assert_eq!(m.validate_spec(now), Err(SpecError::ClosesInThePast));

        let mut m = market(now);
        m.resolution = ResolutionSpec::PriceThreshold {
            symbol: "BTC-USD".into(),
            comparator: Comparator::Above,
            threshold: 70_000.0,
            if_true_outcome: 0,
            if_false_outcome: 7, // does not exist
        };
        assert_eq!(m.validate_spec(now), Err(SpecError::OutcomeIndexOutOfRange));

        let mut m = market(now);
        m.resolution = ResolutionSpec::OperatorDeclared {
            criteria: "   ".into(),
            source: "somewhere".into(),
        };
        assert!(matches!(m.validate_spec(now), Err(SpecError::EmptyField(_))));
    }

    // ---- machine resolution --------------------------------------------------------------------

    #[test]
    fn a_price_market_resolves_itself_with_no_human_in_the_loop() {
        let m = market(1_800_000_000_000); // YES if BTC > 70000
        assert_eq!(m.outcome_for_observation(70_001.0), Some(0));
        assert_eq!(m.outcome_for_observation(69_999.0), Some(1));
        // Exactly at the threshold, "above" does not hold.
        assert_eq!(m.outcome_for_observation(70_000.0), Some(1));
        assert!(m.resolution.is_machine_resolved());
    }

    #[test]
    fn comparators_do_what_they_say_at_the_boundary() {
        assert!(Comparator::Above.holds(10.0, 9.99));
        assert!(!Comparator::Above.holds(10.0, 10.0));
        assert!(Comparator::AtOrAbove.holds(10.0, 10.0));
        assert!(Comparator::Below.holds(9.99, 10.0));
        assert!(!Comparator::Below.holds(10.0, 10.0));
        assert!(Comparator::AtOrBelow.holds(10.0, 10.0));
    }

    #[test]
    fn an_operator_market_has_no_machine_answer() {
        let mut m = market(1_800_000_000_000);
        m.resolution = ResolutionSpec::OperatorDeclared {
            criteria: "as reported by the named source".into(),
            source: "example.test".into(),
        };
        assert_eq!(m.outcome_for_observation(123.0), None);
        assert!(!m.resolution.is_machine_resolved());
    }

    // ---- the dispute window ---------------------------------------------------------------------

    #[test]
    fn a_proposal_cannot_be_paid_out_until_the_window_elapses() {
        let now = 1_800_000_000_000;
        let mut m = market(now);
        m.dispute_window_ms = 60_000;
        m.proposal = Some(Proposal {
            outcome: Some(0),
            proposed_at_ms: now,
            evidence: "observed 70500 from 3 sources".into(),
            automatic: true,
            disputed_by: Vec::new(),
        });
        assert!(!m.dispute_window_elapsed(now));
        assert!(!m.dispute_window_elapsed(now + 59_999));
        assert!(m.dispute_window_elapsed(now + 60_000));
    }

    #[test]
    fn with_no_proposal_there_is_nothing_to_finalise() {
        let now = 1_800_000_000_000;
        let m = market(now);
        assert!(!m.dispute_window_elapsed(now + 10_000_000));
    }

    #[test]
    fn the_pool_invariant_survives_adversarial_magnitudes() {
        // The existing property test uses reasonable numbers. This one deliberately mixes values
        // that are hard for f64: a whale beside dust, amounts near the precision limit, and many
        // small stakes whose sum is where rounding error accumulates.
        let mut seed = 0xC0FFEEu64;
        let mut next = move || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        let magnitudes = [1e-9, 1e-3, 1.0, 1e3, 1e9, 1e12];
        for case in 0..400 {
            let n = 1 + (next() % 40) as usize;
            let outcomes = 2 + (next() % 3) as usize;
            let mut stakes = Vec::new();
            for i in 0..n {
                let mag = magnitudes[(next() % magnitudes.len() as u64) as usize];
                let amount = mag * (1 + next() % 1000) as f64;
                stakes.push(stake(&format!("a{i}"), (next() % outcomes as u64) as usize, amount));
            }
            let total: f64 = stakes.iter().map(|s| s.amount).sum();
            let winner = (next() % outcomes as u64) as usize;
            let s = settle("m", &stakes, outcomes, winner, DEFAULT_RAKE_BPS);

            let paid: f64 = s.payouts.iter().map(|(_, a)| *a).sum();
            // The engine must never distribute money that was not staked. A relative tolerance,
            // because at 1e12 an absolute epsilon is meaningless.
            assert!(
                paid + s.rake <= total * (1.0 + 1e-9) + 1e-6,
                "case {case}: paid {paid} + rake {} exceeded pool {total}",
                s.rake
            );
            assert!(s.rake >= 0.0, "case {case}: negative rake {}", s.rake);
            for (who, amount) in &s.payouts {
                assert!(
                    amount.is_finite() && *amount >= 0.0,
                    "case {case}: {who} was paid {amount}"
                );
            }
        }
    }

    #[test]
    fn a_winner_is_never_paid_less_than_they_staked() {
        // The core promise the whole rake design exists to keep, checked across shapes rather
        // than on one example.
        let mut seed = 0xBEEF_1234u64;
        let mut next = move || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        for case in 0..400 {
            let n = 2 + (next() % 20) as usize;
            let mut stakes = Vec::new();
            for i in 0..n {
                let amount = (1 + next() % 1_000_000) as f64 / 100.0;
                stakes.push(stake(&format!("a{i}"), (next() % 2) as usize, amount));
            }
            let winner = (next() % 2) as usize;
            let s = settle("m", &stakes, 2, winner, DEFAULT_RAKE_BPS);
            for st in stakes.iter().filter(|st| st.outcome_idx == winner) {
                let paid: f64 = s
                    .payouts
                    .iter()
                    .filter(|(who, _)| *who == st.agent_id)
                    .map(|(_, a)| *a)
                    .sum();
                assert!(
                    paid >= st.amount * (1.0 - 1e-9),
                    "case {case}: {} staked {} on the winner and got {paid}",
                    st.agent_id,
                    st.amount
                );
            }
        }
    }

    #[test]
    fn a_voided_market_never_takes_a_rake_and_always_refunds_exactly() {
        let mut seed = 0x5151_5151u64;
        let mut next = move || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        for _ in 0..200 {
            let n = 1 + (next() % 25) as usize;
            let mut stakes = Vec::new();
            for i in 0..n {
                stakes.push(stake(
                    &format!("a{i}"),
                    (next() % 3) as usize,
                    (1 + next() % 500_000) as f64 / 10.0,
                ));
            }
            let s = void("m", &stakes, "unresolvable");
            assert_eq!(s.rake, 0.0, "a void must never earn the house anything");
            for st in &stakes {
                let paid: f64 = s
                    .payouts
                    .iter()
                    .filter(|(who, _)| *who == st.agent_id)
                    .map(|(_, a)| *a)
                    .sum();
                let staked: f64 = stakes
                    .iter()
                    .filter(|o| o.agent_id == st.agent_id)
                    .map(|o| o.amount)
                    .sum();
                assert!(
                    (paid - staked).abs() < 1e-9,
                    "{} staked {staked} and a void returned {paid}",
                    st.agent_id
                );
            }
        }
    }

    #[test]
    fn a_freeze_is_temporary_and_its_clock_runs_down() {
        let now = 1_800_000_000_000;
        let mut m = market(now);
        assert!(!m.review_overdue(now), "nothing frozen, nothing overdue");
        assert_eq!(m.review_remaining_ms(now), None);

        m.status = MarketStatus::Disputed;
        m.disputed_at_ms = Some(now);
        assert!(!m.review_overdue(now), "the clock has only just started");
        assert_eq!(m.review_remaining_ms(now), Some(Market::DISPUTE_REVIEW_MS));

        let nearly = now + Market::DISPUTE_REVIEW_MS - 1;
        assert!(!m.review_overdue(nearly), "still inside the window");
        assert_eq!(m.review_remaining_ms(nearly), Some(1));

        assert!(
            m.review_overdue(now + Market::DISPUTE_REVIEW_MS),
            "past the window the freeze must end, or an unanswered objection holds other \
             people's money forever"
        );
        assert_eq!(m.review_remaining_ms(now + Market::DISPUTE_REVIEW_MS * 2), Some(0));
    }

    #[test]
    fn the_review_window_is_long_enough_to_be_fair_and_short_enough_to_be_finite() {
        // Voiding is a worse outcome than a correct late decision, so the window is generous —
        // but it has to be finite, which is the whole point.
        assert!(Market::DISPUTE_REVIEW_MS >= 60 * 60 * 1000, "too short to review properly");
        assert!(Market::DISPUTE_REVIEW_MS <= 7 * 24 * 60 * 60 * 1000, "money cannot sit that long");
    }
}
