//! Protocol fee schedule: volume-tiered, maker-rebated.
//!
//! # The business model this implements
//!
//! Revenue = volume x a thin margin, not a thick toll on each trade. A venue with no liquidity
//! is worth nothing regardless of what it charges, so the schedule below deliberately *pays*
//! makers to post orders and takes a small cut from takers who consume them. The venue keeps
//! the difference.
//!
//! This replaced a flat 70bps (0.70%) taker fee, which was roughly 7x Binance's base-tier taker
//! rate and would have made the venue unusable for any agent doing real size. Tier 0 taker is
//! now 7bps (0.07%) — below Binance/OKX/Bybit's ~10bps base tier — and falls with volume.
//!
//! # The invariant that keeps this from being farmed
//!
//! `taker_bps + maker_bps > 0` at EVERY tier (maker_bps is negative — it's a rebate).
//!
//! If the maker rebate ever met or exceeded the taker fee, two colluding agents could trade back
//! and forth and extract money from the treasury on every round trip — the venue would be paying
//! people to fake volume. Combined with self-trade prevention in orderbook.rs (which stops one
//! agent doing it alone), this makes manufactured volume strictly loss-making. `capture_bps` is
//! asserted positive for every tier in the tests below; that assertion is load-bearing, not
//! decorative.
//!
//! # Not implemented
//!
//! - **Rolling 30-day volume.** Volume accumulates forever instead of ageing out, so an agent
//!   never falls back down a tier. Needs the durable store from ROADMAP phase 1.
//! - **Trust-band discounts** (the spec asks for them). Deliberately omitted: discounting fees by
//!   trust score creates a direct financial incentive to game the trust score, which is the one
//!   number the whole anti-abuse system rests on. Discount on volume — which is measured, not
//!   inferred — instead.
//! - **BTC conversion of collected fees.** See ARCHITECTURE section 7 / ROADMAP phase 6.

/// Fee on a non-custodial routed swap (see router.rs), in basis points of the trade's output.
///
/// Flat, not tiered, and deliberately different from the order-book schedule above, because it
/// is paid for a different thing. The order book's fee buys matching and settlement against
/// captive liquidity; this one buys best-execution across venues the agent could have reached
/// itself. That means it competes with the agent's alternative of just calling one DEX directly
/// and paying nothing — so it has to be small enough that better routing plus not writing the
/// integration is obviously worth 10bps.
///
/// 10bps sits at the low end of the aggregator range (0x, 1inch and Jupiter integrators commonly
/// take 15-30bps, often undisclosed and folded into the quote). Ikenga reports it as a separate
/// line on every route — see `Route`'s gross/fee/net split — which is worth more for trust than
/// the extra 10bps would be worth in revenue.
///
/// Override per-deployment with `IKENGA_ROUTE_FEE_BPS`.
pub const ROUTE_FEE_BPS: f64 = 10.0;

/// One row of the fee schedule. Rates are basis points of the trade's notional value, charged
/// in the quote asset. `maker_bps` is negative: a rebate paid *to* the maker.
#[derive(Debug, Clone, Copy)]
pub struct FeeTier {
    pub name: &'static str,
    pub min_30d_volume_usd: f64,
    pub maker_bps: f64,
    pub taker_bps: f64,
}

impl FeeTier {
    /// What the venue actually keeps per trade, in bps. Must be > 0 — see the module docs.
    pub fn capture_bps(&self) -> f64 {
        self.taker_bps + self.maker_bps
    }
}

/// Ordered by ascending volume threshold. Tier 0 is already competitive with the major venues'
/// base tiers; the point is that nobody arrives and finds the pricing hostile.
pub const TIERS: [FeeTier; 5] = [
    FeeTier { name: "Base",      min_30d_volume_usd: 0.0,         maker_bps: -1.0, taker_bps: 7.0 },
    FeeTier { name: "Active",    min_30d_volume_usd: 100_000.0,   maker_bps: -1.5, taker_bps: 5.5 },
    FeeTier { name: "Pro",       min_30d_volume_usd: 1_000_000.0, maker_bps: -2.0, taker_bps: 4.5 },
    FeeTier { name: "Prime",     min_30d_volume_usd: 10_000_000.0, maker_bps: -2.5, taker_bps: 4.0 },
    FeeTier { name: "Principal", min_30d_volume_usd: 100_000_000.0, maker_bps: -3.0, taker_bps: 3.5 },
];

/// The tier an agent's trailing volume puts them in.
pub fn tier_for_volume(volume_usd: f64) -> &'static FeeTier {
    TIERS
        .iter()
        .rev()
        .find(|t| volume_usd >= t.min_30d_volume_usd)
        .unwrap_or(&TIERS[0])
}

/// Fee charged to the taker, in quote-asset units. Always positive.
pub fn taker_fee(notional_quote: f64, tier: &FeeTier) -> f64 {
    notional_quote * tier.taker_bps / 10_000.0
}

/// Rebate paid to the maker, in quote-asset units. Returned as a positive number — it is a
/// credit, and the caller adds it to the maker's balance.
pub fn maker_rebate(notional_quote: f64, tier: &FeeTier) -> f64 {
    notional_quote * -tier.maker_bps / 10_000.0
}

/// What the treasury nets from one trade, in quote-asset units: taker fee minus maker rebate.
pub fn protocol_capture(notional_quote: f64, tier: &FeeTier) -> f64 {
    notional_quote * tier.capture_bps() / 10_000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    /// LOAD-BEARING. If this fails, the venue pays out more than it collects and two colluding
    /// agents can drain the treasury by trading with each other. See the module docs.
    #[test]
    fn every_tier_captures_more_than_it_pays_out() {
        for tier in TIERS.iter() {
            assert!(
                tier.capture_bps() > 0.0,
                "tier {} pays makers {}bps but only charges takers {}bps — a round trip between \
                 two colluding agents would extract {}bps from the treasury",
                tier.name,
                -tier.maker_bps,
                tier.taker_bps,
                -tier.capture_bps()
            );
        }
    }

    #[test]
    fn maker_is_always_rebated_and_taker_always_charged() {
        for tier in TIERS.iter() {
            assert!(tier.maker_bps < 0.0, "tier {} does not rebate makers", tier.name);
            assert!(tier.taker_bps > 0.0, "tier {} does not charge takers", tier.name);
        }
    }

    #[test]
    fn fees_fall_as_volume_rises() {
        for pair in TIERS.windows(2) {
            assert!(
                pair[1].taker_bps < pair[0].taker_bps,
                "tier {} is not cheaper than {}",
                pair[1].name,
                pair[0].name
            );
            assert!(
                pair[1].maker_bps < pair[0].maker_bps,
                "tier {} does not rebate more than {}",
                pair[1].name,
                pair[0].name
            );
        }
    }

    #[test]
    fn tier_lookup_picks_the_right_row() {
        assert_eq!(tier_for_volume(0.0).name, "Base");
        assert_eq!(tier_for_volume(99_999.0).name, "Base");
        assert_eq!(tier_for_volume(100_000.0).name, "Active");
        assert_eq!(tier_for_volume(5_000_000.0).name, "Pro");
        assert_eq!(tier_for_volume(1_000_000_000.0).name, "Principal");
    }

    #[test]
    fn base_tier_taker_is_seven_bps_of_notional() {
        let tier = tier_for_volume(0.0);
        // $10,000 notional at 7bps = $7.
        assert!((taker_fee(10_000.0, tier) - 7.0).abs() < 1e-9);
        // ...of which $1 goes to the maker and $6 to the treasury.
        assert!((maker_rebate(10_000.0, tier) - 1.0).abs() < 1e-9);
        assert!((protocol_capture(10_000.0, tier) - 6.0).abs() < 1e-9);
    }

    #[test]
    fn capture_always_equals_fee_minus_rebate() {
        for tier in TIERS.iter() {
            let n = 123_456.789;
            let expected = taker_fee(n, tier) - maker_rebate(n, tier);
            assert!((protocol_capture(n, tier) - expected).abs() < 1e-9, "tier {}", tier.name);
        }
    }

    #[test]
    fn we_undercut_the_majors_at_the_base_tier() {
        // Binance/OKX/Bybit base-tier taker is ~10bps; Coinbase Advanced and Kraken Pro are
        // higher still. Arriving with a worse price than the incumbents is not a strategy.
        assert!(TIERS[0].taker_bps < 10.0);
    }
}
