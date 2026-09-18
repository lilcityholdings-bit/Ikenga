//! Head-to-head bets: an offer that only becomes a market once someone takes the other side.
//!
//! # Why this exists alongside the pools
//!
//! A pari-mutuel pool pays winners out of the losers' money. That is a real mechanism, but it has
//! an honest limit that is easy to talk past: **with nobody on the other side there is no prize.**
//! Stake alone, be right, and settlement hands back exactly what you put in. Nothing was won,
//! because nothing was lost to you. "It settles correctly with one participant" is true and
//! beside the point.
//!
//! A challenge makes the requirement explicit instead of hiding it. You say what you think, you
//! put money on it, and it sits there as an offer. Nothing is live until somebody disagrees
//! enough to fund the other side. Then both stakes are locked and the winner takes the pot.
//!
//! # Why this is the better shape for a venue with few users
//!
//! Pools are the right mechanism at scale: a thousand participants is natural, and no matching is
//! needed. But at two participants a pool is a strange, thin thing, whereas a head-to-head bet is
//! *exactly* the right size — two people disagreeing is the whole design, not a degenerate case
//! of it. So challenges are what works on day one, and pools are what works on day one thousand.
//! Both settle through the same engine.
//!
//! # Why the proposer's money is taken up front
//!
//! An offer nobody can fund is worse than no offer. If the stake were only debited on acceptance,
//! the board would fill with bets their authors could not honour, and a taker would discover this
//! at the moment they tried to accept — having already decided they wanted it. So proposing debits
//! immediately, and an offer that expires unmatched refunds in full, with no rake: nothing was
//! settled, so the house earned nothing.
//!
//! # Odds
//!
//! A challenge names both stakes, so the odds are whatever the two sides agree. Offer 100 against
//! 300 and you are asking for 3:1. The taker sees both numbers before accepting, which is the
//! entire negotiation — there is no order book, no price to slide, and nothing to be filled at a
//! worse number than you read.

use std::collections::HashMap;
use std::sync::Mutex;

use crate::json::Json;
use crate::prediction::{Market, MarketStatus, ResolutionSpec};

/// An offer to bet, waiting for someone to take the other side.
#[derive(Debug, Clone, PartialEq)]
pub struct Challenge {
    pub challenge_id: String,
    pub proposer: String,
    pub question: String,
    /// Exactly two, by construction: this is a head-to-head bet, and "the other side" has to be a
    /// single unambiguous thing for an acceptance to mean anything.
    pub outcomes: [String; 2],
    /// Which outcome the proposer is backing (0 or 1).
    pub proposer_outcome: usize,
    /// What the proposer put up. Already debited.
    pub proposer_stake: f64,
    /// What the taker must put up to accept. The ratio between the two is the odds.
    pub taker_stake: f64,
    pub asset: String,
    pub resolution: ResolutionSpec,
    /// When betting would close, if it gets taken.
    pub closes_at_ms: i64,
    pub observed_at_ms: i64,
    pub dispute_window_ms: i64,
    /// When this offer gives up waiting and refunds the proposer.
    pub expires_at_ms: i64,
    pub created_at_ms: i64,
    pub status: ChallengeStatus,
    /// Set once taken.
    pub taker: Option<String>,
    /// The market this became, once taken.
    pub market_id: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChallengeStatus {
    /// Money is up, waiting for someone to disagree.
    Open,
    /// Taken. A market exists and the bet is live.
    Matched,
    /// Nobody took it in time; the proposer was refunded in full.
    Expired,
    /// The proposer pulled it before anyone took it; refunded in full.
    Withdrawn,
}

impl ChallengeStatus {
    pub fn label(&self) -> &'static str {
        match self {
            ChallengeStatus::Open => "open",
            ChallengeStatus::Matched => "matched",
            ChallengeStatus::Expired => "expired",
            ChallengeStatus::Withdrawn => "withdrawn",
        }
    }

    /// Whether the proposer's money is still committed to this offer.
    pub fn is_live(&self) -> bool {
        matches!(self, ChallengeStatus::Open)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum ChallengeError {
    NotFound,
    NotOpen,
    Expired,
    /// You cannot take your own bet. Both sides would be the same balance, so nothing is at risk
    /// and the only effect is a rake charged to yourself — plus a fake participant in the data.
    CannotAcceptOwn,
    InsufficientBalance { have: f64, need: f64 },
    NotYours,
    Malformed(&'static str),
}

impl ChallengeError {
    pub fn code(&self) -> &'static str {
        match self {
            ChallengeError::NotFound => "NO_SUCH_CHALLENGE",
            ChallengeError::NotOpen => "CHALLENGE_NOT_OPEN",
            ChallengeError::Expired => "CHALLENGE_EXPIRED",
            ChallengeError::CannotAcceptOwn => "CANNOT_ACCEPT_OWN_CHALLENGE",
            ChallengeError::InsufficientBalance { .. } => "INSUFFICIENT_BALANCE",
            ChallengeError::NotYours => "NOT_YOUR_CHALLENGE",
            ChallengeError::Malformed(_) => "MALFORMED_CHALLENGE",
        }
    }

    pub fn message(&self) -> String {
        match self {
            ChallengeError::NotFound => "no such challenge".into(),
            ChallengeError::NotOpen => {
                "this challenge is no longer open — it was taken, withdrawn or expired".into()
            }
            ChallengeError::Expired => "this challenge expired before anyone took it".into(),
            ChallengeError::CannotAcceptOwn => {
                "you cannot take the other side of your own bet — nothing would be at risk".into()
            }
            ChallengeError::InsufficientBalance { have, need } => {
                format!("need {need} to take this side, you have {have}")
            }
            ChallengeError::NotYours => "only the proposer can withdraw a challenge".into(),
            ChallengeError::Malformed(why) => (*why).to_string(),
        }
    }
}

impl Challenge {
    pub fn taker_outcome(&self) -> usize {
        1 - self.proposer_outcome
    }

    /// The whole pot if this gets taken.
    pub fn pot(&self) -> f64 {
        self.proposer_stake + self.taker_stake
    }

    /// Odds as the taker sees them: what they stand to win per unit risked.
    pub fn taker_odds(&self) -> f64 {
        if self.taker_stake > 0.0 {
            self.proposer_stake / self.taker_stake
        } else {
            0.0
        }
    }

    pub fn is_expired(&self, now_ms: i64) -> bool {
        now_ms >= self.expires_at_ms
    }

    /// Checks an offer is coherent before any money is taken for it.
    pub fn validate(&self, now_ms: i64) -> Result<(), ChallengeError> {
        if self.question.trim().is_empty() {
            return Err(ChallengeError::Malformed("question must not be empty"));
        }
        if self.outcomes[0].trim().is_empty() || self.outcomes[1].trim().is_empty() {
            return Err(ChallengeError::Malformed("both outcomes must be named"));
        }
        if self.outcomes[0] == self.outcomes[1] {
            return Err(ChallengeError::Malformed("the two outcomes must differ"));
        }
        if self.proposer_outcome > 1 {
            return Err(ChallengeError::Malformed("proposer_outcome must be 0 or 1"));
        }
        if !crate::credits::is_sane_amount(self.proposer_stake)
            || !crate::credits::is_sane_amount(self.taker_stake)
        {
            return Err(ChallengeError::Malformed(
                "both stakes must be positive and within the ledger limit",
            ));
        }
        if self.closes_at_ms <= now_ms {
            return Err(ChallengeError::Malformed("closes_at_ms must be in the future"));
        }
        // The same settlement gap the pools require: a bet that is still open at the moment its
        // answer becomes visible can be taken for free by whoever looks first.
        if self.observed_at_ms <= self.closes_at_ms {
            return Err(ChallengeError::Malformed(
                "observed_at_ms must be strictly after closes_at_ms",
            ));
        }
        // An offer that outlives its own market is nonsense: accepting it after betting closed
        // would create a market nobody could ever have bet in.
        if self.expires_at_ms > self.closes_at_ms {
            return Err(ChallengeError::Malformed(
                "expires_at_ms must not be after closes_at_ms — an offer cannot be taken once \
                 betting has closed",
            ));
        }
        if self.expires_at_ms <= now_ms {
            return Err(ChallengeError::Malformed("expires_at_ms must be in the future"));
        }
        if self.dispute_window_ms < 0 {
            return Err(ChallengeError::Malformed("dispute_window_ms cannot be negative"));
        }
        Ok(())
    }

    /// The market this becomes once taken.
    ///
    /// Deliberately an ordinary `Market`, so a matched challenge settles through exactly the same
    /// engine as everything else — same commitment hash over the terms, same propose/dispute/
    /// finalise lifecycle, same payout maths, same write-ahead log. A head-to-head bet is a pool
    /// with two stakes in it; there is no second settlement path to keep correct.
    pub fn to_market(&self, market_id: String, now_ms: i64) -> Market {
        let mut m = Market {
            market_id,
            question: self.question.clone(),
            outcomes: vec![self.outcomes[0].clone(), self.outcomes[1].clone()],
            resolution: self.resolution.clone(),
            asset: self.asset.clone(),
            closes_at_ms: self.closes_at_ms,
            observed_at_ms: self.observed_at_ms,
            dispute_window_ms: self.dispute_window_ms,
            commitment: String::new(),
            status: MarketStatus::Open,
            proposal: None,
            winning_outcome: None,
            created_at_ms: now_ms,
        disputed_at_ms: None,
        };
        m.commitment = m.compute_commitment();
        m
    }

    pub fn to_json(&self) -> Json {
        Json::obj(vec![
            ("challenge_id", Json::str(self.challenge_id.clone())),
            ("question", Json::str(self.question.clone())),
            ("status", Json::str(self.status.label())),
            ("asset", Json::str(self.asset.clone())),
            (
                "proposer_side",
                Json::obj(vec![
                    ("outcome", Json::num(self.proposer_outcome as f64)),
                    ("name", Json::str(self.outcomes[self.proposer_outcome].clone())),
                    ("stake", Json::num(self.proposer_stake)),
                ]),
            ),
            (
                "open_side",
                Json::obj(vec![
                    ("outcome", Json::num(self.taker_outcome() as f64)),
                    ("name", Json::str(self.outcomes[self.taker_outcome()].clone())),
                    ("stake_required", Json::num(self.taker_stake)),
                ]),
            ),
            ("pot", Json::num(self.pot())),
            ("taker_wins_per_unit_risked", Json::num(self.taker_odds())),
            ("closes_at_ms", Json::num(self.closes_at_ms as f64)),
            ("observed_at_ms", Json::num(self.observed_at_ms as f64)),
            ("expires_at_ms", Json::num(self.expires_at_ms as f64)),
            (
                "market_id",
                self.market_id.clone().map(Json::str).unwrap_or(Json::Null),
            ),
            ("resolution", self.resolution.to_json()),
            (
                "note",
                Json::str(
                    "Nothing is at risk for either side until this is taken. Accepting locks both \
                     stakes; the winner takes the pot, less the house's cut of the losing stake \
                     only. Unmatched at expiry, the proposer is refunded in full and the house \
                     earns nothing.",
                ),
            ),
        ])
    }
}

/// Every challenge, open or finished.
#[derive(Default)]
pub struct ChallengeBook {
    inner: Mutex<HashMap<String, Challenge>>,
}

impl ChallengeBook {
    pub fn insert(&self, c: Challenge) {
        self.inner.lock().unwrap().insert(c.challenge_id.clone(), c);
    }

    pub fn get(&self, id: &str) -> Option<Challenge> {
        self.inner.lock().unwrap().get(id).cloned()
    }

    pub fn update<F: FnOnce(&mut Challenge)>(&self, id: &str, f: F) -> bool {
        match self.inner.lock().unwrap().get_mut(id) {
            Some(c) => {
                f(c);
                true
            }
            None => false,
        }
    }

    /// Open offers, soonest to expire first — the ones a taker has least time to think about.
    pub fn open(&self, now_ms: i64) -> Vec<Challenge> {
        let inner = self.inner.lock().unwrap();
        let mut out: Vec<Challenge> = inner
            .values()
            .filter(|c| c.status == ChallengeStatus::Open && !c.is_expired(now_ms))
            .cloned()
            .collect();
        out.sort_by(|a, b| a.expires_at_ms.cmp(&b.expires_at_ms));
        out
    }

    /// Offers that have run out of time and still hold the proposer's money.
    pub fn expired_unmatched(&self, now_ms: i64) -> Vec<Challenge> {
        let inner = self.inner.lock().unwrap();
        inner
            .values()
            .filter(|c| c.status == ChallengeStatus::Open && c.is_expired(now_ms))
            .cloned()
            .collect()
    }

    pub fn for_agent(&self, agent_id: &str) -> Vec<Challenge> {
        let inner = self.inner.lock().unwrap();
        let mut out: Vec<Challenge> = inner
            .values()
            .filter(|c| c.proposer == agent_id || c.taker.as_deref() == Some(agent_id))
            .cloned()
            .collect();
        out.sort_by(|a, b| b.created_at_ms.cmp(&a.created_at_ms));
        out
    }

    /// Money currently held by offers nobody has taken yet.
    ///
    /// Neither a balance nor a market pool, so anything auditing where value lives has to ask for
    /// it explicitly — see the conservation check in `state.rs`.
    pub fn open_escrow(&self, asset: &str) -> f64 {
        let inner = self.inner.lock().unwrap();
        inner
            .values()
            .filter(|c| c.status.is_live() && c.asset == asset)
            .map(|c| c.proposer_stake)
            .sum()
    }

    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn challenge(now: i64) -> Challenge {
        Challenge {
            challenge_id: "ch_1".into(),
            proposer: "alice".into(),
            question: "Will BTC be above 70000 tomorrow?".into(),
            outcomes: ["YES".into(), "NO".into()],
            proposer_outcome: 0,
            proposer_stake: 100.0,
            taker_stake: 100.0,
            asset: "PTS".into(),
            resolution: ResolutionSpec::OperatorDeclared {
                criteria: "as reported by the named source".into(),
                source: "example.test".into(),
            },
            closes_at_ms: now + 3_600_000,
            observed_at_ms: now + 3_660_000,
            dispute_window_ms: 60_000,
            expires_at_ms: now + 600_000,
            created_at_ms: now,
            status: ChallengeStatus::Open,
            taker: None,
            market_id: None,
        }
    }

    #[test]
    fn a_coherent_offer_validates() {
        let now = 1_800_000_000_000;
        challenge(now).validate(now).expect("should be valid");
    }

    #[test]
    fn the_two_sides_are_always_opposite() {
        let now = 1_800_000_000_000;
        let mut c = challenge(now);
        assert_eq!(c.taker_outcome(), 1);
        c.proposer_outcome = 1;
        assert_eq!(c.taker_outcome(), 0);
    }

    #[test]
    fn the_stakes_are_the_odds() {
        let now = 1_800_000_000_000;
        let mut c = challenge(now);
        // Even money.
        assert!((c.taker_odds() - 1.0).abs() < 1e-9);
        // Proposer risks 300 to win 100: the taker is being offered 3:1.
        c.proposer_stake = 300.0;
        c.taker_stake = 100.0;
        assert!((c.taker_odds() - 3.0).abs() < 1e-9, "got {}", c.taker_odds());
        assert!((c.pot() - 400.0).abs() < 1e-9);
    }

    #[test]
    fn an_offer_cannot_outlive_the_betting_it_would_create() {
        // Accepting after the market has closed would produce a market nobody could have bet in —
        // the taker's money would be locked in something already over.
        let now = 1_800_000_000_000;
        let mut c = challenge(now);
        c.expires_at_ms = c.closes_at_ms + 1;
        assert!(c.validate(now).is_err());
        c.expires_at_ms = c.closes_at_ms;
        assert!(c.validate(now).is_ok(), "expiring exactly at close is fine");
    }

    #[test]
    fn there_is_always_a_gap_between_closing_and_observing() {
        let now = 1_800_000_000_000;
        let mut c = challenge(now);
        c.observed_at_ms = c.closes_at_ms;
        assert!(c.validate(now).is_err(), "the last bet in must not win for free");
    }

    #[test]
    fn nonsense_offers_are_refused_before_any_money_moves() {
        let now = 1_800_000_000_000;
        for mutate in [
            (|c: &mut Challenge| c.question = "  ".into()) as fn(&mut Challenge),
            |c: &mut Challenge| c.outcomes = ["SAME".into(), "SAME".into()],
            |c: &mut Challenge| c.outcomes = ["".into(), "NO".into()],
            |c: &mut Challenge| c.proposer_outcome = 2,
            |c: &mut Challenge| c.proposer_stake = 0.0,
            |c: &mut Challenge| c.taker_stake = -1.0,
            |c: &mut Challenge| c.proposer_stake = f64::INFINITY,
            |c: &mut Challenge| c.taker_stake = 1e308,
            |c: &mut Challenge| c.closes_at_ms = 0,
            |c: &mut Challenge| c.expires_at_ms = 0,
            |c: &mut Challenge| c.dispute_window_ms = -1,
        ] {
            let mut c = challenge(now);
            mutate(&mut c);
            assert!(c.validate(now).is_err(), "should have been refused: {c:?}");
        }
    }

    #[test]
    fn the_market_it_becomes_passes_the_engines_own_validation() {
        let now = 1_800_000_000_000;
        let c = challenge(now);
        let m = c.to_market("mkt_x".into(), now);
        m.validate_spec(now).expect("a matched challenge must produce a legal market");
        assert!(m.commitment_is_intact(), "terms must be hash-committed like any other market");
        assert_eq!(m.outcomes.len(), 2);
    }

    #[test]
    fn an_offer_names_both_sides_and_the_pot_before_anyone_takes_it() {
        let now = 1_800_000_000_000;
        let mut c = challenge(now);
        c.proposer_stake = 250.0;
        c.taker_stake = 50.0;
        let blob = c.to_json().to_string();
        // A taker must be able to see exactly what they put in and what they can win, up front.
        assert!(blob.contains("stake_required"), "{blob}");
        assert!(blob.contains("\"pot\":300"), "{blob}");
        assert!(blob.contains("taker_wins_per_unit_risked"), "{blob}");
    }

    #[test]
    fn the_book_lists_only_offers_someone_could_still_take() {
        let now = 1_800_000_000_000;
        let book = ChallengeBook::default();

        let mut open = challenge(now);
        open.challenge_id = "open".into();
        book.insert(open);

        let mut gone = challenge(now);
        gone.challenge_id = "expired".into();
        gone.expires_at_ms = now - 1;
        book.insert(gone);

        let mut taken = challenge(now);
        taken.challenge_id = "taken".into();
        taken.status = ChallengeStatus::Matched;
        book.insert(taken);

        let listed: Vec<String> = book.open(now).iter().map(|c| c.challenge_id.clone()).collect();
        assert_eq!(listed, vec!["open".to_string()]);

        let stale: Vec<String> =
            book.expired_unmatched(now).iter().map(|c| c.challenge_id.clone()).collect();
        assert_eq!(stale, vec!["expired".to_string()], "expired offers still hold money");
    }
}
