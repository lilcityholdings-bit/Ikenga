//! Redeemable credits: how points become something that can be cashed out, and the barrier that
//! stops free points ever crossing that line.
//!
//! # Two tiers, and why the wall between them is absolute
//!
//! - **`PTS` — promotional points.** Granted free at registration. Stakeable. **Never
//!   redeemable.**
//! - **`USDC` — redeemable balance.** Created only by a confirmed deposit, one-for-one against crypto the
//!   platform actually holds. Stakeable, and withdrawable back out to a chain address.
//!
//! The wall matters more than anything else in this file. Registration hands out 1,000 points to
//! anyone who asks, which is the right call for onboarding and a catastrophic one if those points
//! can become money: a scripted loop of registrations would mint currency straight out of the
//! promo budget. So `is_redeemable` is the only function that decides what can leave, it is
//! consulted on every withdrawal path, and the promotional asset can never satisfy it.
//!
//! Markets are single-asset by construction (see `prediction.rs`), so a promo pool and a credit
//! pool can never mix and a payout always comes back in the tier it was staked from. That is not
//! a convention to remember — it falls out of the market spec, which names exactly one asset and
//! commits to it.
//!
//! # The reserve invariant
//!
//! ```text
//! outstanding credits + pending withdrawals ≤ recorded reserves
//! ```
//!
//! Credits are a claim on crypto held elsewhere. If that inequality ever breaks, some claim
//! cannot be honoured, and the platform is insolvent whether or not anyone has noticed yet.
//! `reserve_shortfall` computes it and `GET /v1/reserves` publishes it, so the question "is this
//! place good for the money" has a checkable answer instead of a reassuring paragraph.
//!
//! # Withdrawal flow, and why balance is debited immediately
//!
//! 1. Agent requests a withdrawal. **Balance is debited right then**, before anything is sent.
//! 2. The request sits `Pending` for the operator to execute on-chain.
//! 3. Operator marks it `Sent` with a transaction id, or `Rejected` — which refunds it.
//!
//! Debiting at request time rather than at send time closes the obvious double-spend: otherwise
//! an agent could request a withdrawal and stake the same credits before the payout goes out,
//! and whichever settles second is paid with money that is already gone.
//!
//! # What this deliberately does not do
//!
//! It does not touch a blockchain. Crediting a deposit and marking a withdrawal sent are operator
//! actions against this ledger; actually watching for incoming transactions and signing outgoing
//! ones needs a chain integration and key custody that this build has neither the network access
//! nor the security model for. `ChainAdapter` below is the seam that work plugs into. Until it is
//! filled, the operator is the bridge, and the audit trail here is what makes that reviewable.

use std::collections::HashMap;

/// The promotional asset. Free, stakeable, and never redeemable.
pub const POINTS: &str = "PTS";
/// The redeemable asset: USDC, one-for-one with deposited stablecoin.
///
/// # Why pools are single-currency, and why that currency is a stablecoin
///
/// A prediction market's payout has to be denominated in something that does not move between
/// the stake and the settlement. Denominate a pool in BTC and a correct forecaster can still
/// lose money because BTC fell while the market was open — which turns every forecast into a
/// forecast *plus* an unhedged currency bet nobody asked for. Worse for the operator: the rake
/// is a slice of that pool, so revenue inherits the same volatility.
///
/// So: **pools settle in USDC only.** Users can still pay in whatever they hold — the router in
/// `router.rs` already prices a swap across venues, so other coins are converted at deposit and
/// the pool never carries FX risk. One unit of account inside, any currency at the door.
pub const CREDITS: &str = "USDC";

/// The only place that decides what may leave the platform.
///
/// Deliberately a whitelist rather than a blacklist: a new asset added later is non-redeemable
/// until someone explicitly makes it redeemable, which is the safe direction to fail in.
/// The largest amount any single operation may move.
///
/// # Why there has to be a ceiling at all
///
/// `is_finite() && > 0.0` accepts `1e308`, which is finite, positive, and catastrophic: credit it
/// twice and the balance becomes infinity. From there `outstanding + pending` is infinity,
/// `claims - held` is `inf - inf` = NaN, and the solvency report starts saying `fully_backed:
/// false` and `shortfall: 0` in the same breath — permanently, and through a restart, because the
/// poisoned value is in the log. One mistyped deposit destroys the only number that tells the
/// operator whether they can pay their users.
///
/// 1e15 also keeps every balance inside the range where f64 represents whole numbers exactly
/// (2^53 is about 9.0e15), so cent-level arithmetic stays exact rather than merely close.
pub const MAX_AMOUNT: f64 = 1e15;

/// Whether an amount is safe to put into the ledger: finite, positive, and inside the ceiling.
pub fn is_sane_amount(amount: f64) -> bool {
    amount.is_finite() && amount > 0.0 && amount <= MAX_AMOUNT
}

pub fn is_redeemable(asset: &str) -> bool {
    asset == CREDITS
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WithdrawalStatus {
    /// Requested and debited; awaiting an on-chain send.
    Pending,
    /// Sent on-chain, with a transaction reference.
    Sent,
    /// Refused; the balance was returned.
    Rejected,
}

impl WithdrawalStatus {
    pub fn label(&self) -> &'static str {
        match self {
            WithdrawalStatus::Pending => "Pending",
            WithdrawalStatus::Sent => "Sent",
            WithdrawalStatus::Rejected => "Rejected",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Withdrawal {
    pub withdrawal_id: String,
    pub agent_id: String,
    pub asset: String,
    pub amount: f64,
    /// Where the funds are to be sent. Recorded exactly as given — this is never parsed or
    /// "corrected", because a silently rewritten payout address is how money goes missing.
    pub destination: String,
    pub status: WithdrawalStatus,
    pub requested_at_ms: i64,
    pub settled_at_ms: Option<i64>,
    /// On-chain transaction reference, once sent.
    pub tx_ref: Option<String>,
    pub note: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum CreditError {
    NotRedeemable(String),
    NonPositiveAmount,
    InsufficientBalance { have: f64, need: f64 },
    EmptyDestination,
    RealMoneyDisabled,
    UnknownWithdrawal,
    AlreadySettled,
    /// Paying this out would leave the platform unable to honour its remaining claims.
    WouldBreakReserves { shortfall: f64 },
}

impl CreditError {
    pub fn code(&self) -> &'static str {
        match self {
            CreditError::NotRedeemable(_) => "NOT_REDEEMABLE",
            CreditError::NonPositiveAmount => "BAD_AMOUNT",
            CreditError::InsufficientBalance { .. } => "INSUFFICIENT_BALANCE",
            CreditError::EmptyDestination => "MISSING_DESTINATION",
            CreditError::RealMoneyDisabled => "REAL_MONEY_DISABLED",
            CreditError::UnknownWithdrawal => "UNKNOWN_WITHDRAWAL",
            CreditError::AlreadySettled => "ALREADY_SETTLED",
            CreditError::WouldBreakReserves { .. } => "RESERVE_SHORTFALL",
        }
    }

    pub fn message(&self) -> String {
        match self {
            CreditError::NotRedeemable(asset) => format!(
                "{asset} cannot be withdrawn. Promotional points are granted free and have no \
                 cash value — only deposit-backed credits ({CREDITS}) can leave the platform."
            ),
            CreditError::NonPositiveAmount => "amount must be greater than zero".into(),
            CreditError::InsufficientBalance { have, need } => {
                format!("need {need}, have {have}")
            }
            CreditError::EmptyDestination => "a destination address is required".into(),
            CreditError::RealMoneyDisabled => {
                "this deployment has no redeemable assets enabled".into()
            }
            CreditError::UnknownWithdrawal => "no such withdrawal".into(),
            CreditError::AlreadySettled => "this withdrawal has already been settled".into(),
            CreditError::WouldBreakReserves { shortfall } => format!(
                "refusing: paying this would leave a reserve shortfall of {shortfall} — \
                 outstanding claims would exceed the assets actually held"
            ),
        }
    }
}

/// Total credits sitting in agent balances for one asset.
pub fn outstanding(balances: &HashMap<(String, String), f64>, asset: &str) -> f64 {
    balances
        .iter()
        .filter(|((_, a), _)| a == asset)
        .map(|(_, v)| *v)
        .filter(|v| *v > 0.0)
        .sum()
}

/// Credits already debited but not yet paid out.
pub fn pending_withdrawals(withdrawals: &[Withdrawal], asset: &str) -> f64 {
    withdrawals
        .iter()
        .filter(|w| w.asset == asset && w.status == WithdrawalStatus::Pending)
        .map(|w| w.amount)
        .sum()
}

/// How much the platform is short, if anything. Zero or negative means solvent.
///
/// Note that pending withdrawals count as a claim even though they have already left agent
/// balances: the obligation exists until the funds are actually sent, and a reserve check that
/// ignored it would let an operator drain the float one pending request at a time.
pub fn reserve_shortfall(
    balances: &HashMap<(String, String), f64>,
    withdrawals: &[Withdrawal],
    reserves: &HashMap<String, f64>,
    asset: &str,
) -> f64 {
    let claims = outstanding(balances, asset) + pending_withdrawals(withdrawals, asset);
    let held = reserves.get(asset).copied().unwrap_or(0.0);
    let shortfall = claims - held;
    // A ledger that has somehow gone non-finite must not report a comfortable zero. `inf - inf`
    // is NaN, and NaN loses every comparison silently — including the one that decides whether
    // it is safe to pay someone. Reporting infinity is the honest answer: unknown, and not safe.
    if shortfall.is_nan() {
        return f64::INFINITY;
    }
    shortfall
}

/// The seam a real chain integration plugs into.
///
/// Nothing implements this yet, and that is stated rather than stubbed with something that looks
/// like it works. A fake implementation that "confirms" deposits would be indistinguishable from
/// a real one right up until it credited money that never arrived.
pub trait ChainAdapter: Send + Sync {
    /// A deposit address for this agent, on the adapter's chain.
    fn deposit_address(&self, agent_id: &str) -> Option<String>;
    /// Confirmed incoming deposits since a cursor, so the ledger can be credited.
    fn confirmed_deposits(&self, since_cursor: &str) -> Option<(Vec<ObservedDeposit>, String)>;
    /// Broadcast a payout. Returns a transaction reference.
    fn send(&self, destination: &str, amount: f64) -> Option<String>;
}

#[derive(Debug, Clone, PartialEq)]
pub struct ObservedDeposit {
    pub agent_id: String,
    pub asset: String,
    pub amount: f64,
    pub tx_ref: String,
    pub confirmations: u32,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bal(entries: &[(&str, &str, f64)]) -> HashMap<(String, String), f64> {
        entries
            .iter()
            .map(|(a, asset, v)| ((a.to_string(), asset.to_string()), *v))
            .collect()
    }

    fn withdrawal(agent: &str, asset: &str, amount: f64, status: WithdrawalStatus) -> Withdrawal {
        Withdrawal {
            withdrawal_id: format!("w_{agent}"),
            agent_id: agent.into(),
            asset: asset.into(),
            amount,
            destination: "addr1".into(),
            status,
            requested_at_ms: 0,
            settled_at_ms: None,
            tx_ref: None,
            note: None,
        }
    }

    // ---- the wall between free points and real money ------------------------------------------

    #[test]
    fn promotional_points_are_never_redeemable() {
        // The single most important assertion in this module. Registration mints points freely;
        // if this ever returns true, the free grant becomes a money printer.
        assert!(!is_redeemable(POINTS));
        assert!(!is_redeemable("PTS"));
    }

    #[test]
    fn only_the_credit_asset_is_redeemable() {
        assert!(is_redeemable(CREDITS));
        // Anything else, including plausible-looking assets and future additions, is refused
        // until explicitly whitelisted.
        for asset in ["USD", "BTC", "ETH", "PTS", "CRD_TEST", "crd", "", "POINTS"] {
            if asset != CREDITS {
                assert!(!is_redeemable(asset), "{asset} must not be redeemable by default");
            }
        }
    }

    // ---- the reserve invariant -----------------------------------------------------------------

    #[test]
    fn a_fully_backed_ledger_shows_no_shortfall() {
        let balances = bal(&[("a", CREDITS, 60.0), ("b", CREDITS, 40.0)]);
        let reserves: HashMap<String, f64> = [(CREDITS.to_string(), 100.0)].into_iter().collect();
        assert!(reserve_shortfall(&balances, &[], &reserves, CREDITS) <= 0.0);
    }

    #[test]
    fn pending_withdrawals_still_count_as_claims() {
        // The balance has already been debited, but the obligation is live until it is sent.
        // Ignoring it here would let an operator drain the float one pending request at a time.
        let balances = bal(&[("a", CREDITS, 60.0)]);
        let pend = vec![withdrawal("b", CREDITS, 40.0, WithdrawalStatus::Pending)];
        let reserves: HashMap<String, f64> = [(CREDITS.to_string(), 100.0)].into_iter().collect();
        assert!((reserve_shortfall(&balances, &pend, &reserves, CREDITS) - 0.0).abs() < 1e-9);

        let thin: HashMap<String, f64> = [(CREDITS.to_string(), 90.0)].into_iter().collect();
        assert!((reserve_shortfall(&balances, &pend, &thin, CREDITS) - 10.0).abs() < 1e-9);
    }

    #[test]
    fn sent_and_rejected_withdrawals_are_no_longer_claims() {
        let balances = bal(&[("a", CREDITS, 10.0)]);
        let settled = vec![
            withdrawal("b", CREDITS, 500.0, WithdrawalStatus::Sent),
            withdrawal("c", CREDITS, 500.0, WithdrawalStatus::Rejected),
        ];
        let reserves: HashMap<String, f64> = [(CREDITS.to_string(), 10.0)].into_iter().collect();
        assert!(reserve_shortfall(&balances, &settled, &reserves, CREDITS) <= 0.0);
    }

    #[test]
    fn an_unbacked_ledger_reports_the_exact_shortfall() {
        let balances = bal(&[("a", CREDITS, 250.0)]);
        let reserves: HashMap<String, f64> = [(CREDITS.to_string(), 100.0)].into_iter().collect();
        assert!((reserve_shortfall(&balances, &[], &reserves, CREDITS) - 150.0).abs() < 1e-9);
    }

    #[test]
    fn points_balances_do_not_pollute_the_credit_reserve_check() {
        // A million promotional points must not make the credit ledger look insolvent, and must
        // not be backed by anything either.
        let balances = bal(&[("a", POINTS, 1_000_000.0), ("a", CREDITS, 5.0)]);
        let reserves: HashMap<String, f64> = [(CREDITS.to_string(), 5.0)].into_iter().collect();
        assert!(reserve_shortfall(&balances, &[], &reserves, CREDITS) <= 0.0);
    }

    #[test]
    fn negative_balances_do_not_offset_someone_elses_claim() {
        // A negative balance would be a bug elsewhere, but it must not quietly reduce the
        // apparent liability and mask a shortfall.
        let balances = bal(&[("a", CREDITS, 100.0), ("b", CREDITS, -50.0)]);
        let reserves: HashMap<String, f64> = [(CREDITS.to_string(), 60.0)].into_iter().collect();
        assert!(
            reserve_shortfall(&balances, &[], &reserves, CREDITS) > 39.0,
            "a negative balance masked a real shortfall"
        );
    }

    #[test]
    fn amounts_that_would_overflow_the_ledger_are_refused() {
        assert!(is_sane_amount(1.0));
        assert!(is_sane_amount(MAX_AMOUNT));
        assert!(!is_sane_amount(MAX_AMOUNT * 10.0));
        assert!(!is_sane_amount(1e308), "1e308 is finite and positive but ruinous");
        assert!(!is_sane_amount(f64::INFINITY));
        assert!(!is_sane_amount(f64::NAN));
        assert!(!is_sane_amount(0.0));
        assert!(!is_sane_amount(-1.0));
    }

    #[test]
    fn a_poisoned_ledger_reports_unknown_rather_than_solvent() {
        // If a non-finite value ever does get in, the solvency answer must not be a comfortable
        // zero. NaN loses every comparison silently, which is how "shortfall: 0" and
        // "fully_backed: false" ended up being printed side by side.
        let mut balances = HashMap::new();
        balances.insert(("a".to_string(), CREDITS.to_string()), f64::INFINITY);
        let mut reserves = HashMap::new();
        reserves.insert(CREDITS.to_string(), f64::INFINITY);
        let shortfall = reserve_shortfall(&balances, &[], &reserves, CREDITS);
        assert!(shortfall.is_infinite(), "got {shortfall}");
        assert!(shortfall > 0.0, "an unknowable shortfall must not read as backed");
    }
}
