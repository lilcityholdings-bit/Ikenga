//! Non-custodial swap routing: price the same trade at several venues, pick the best one, and
//! hand the agent back a route that *it* executes, with its own wallet and its own keys.
//!
//! # Why this exists alongside the order book
//!
//! `orderbook.rs` runs a custodial venue: agents deposit, this server's ledger owns the balance,
//! and a trade is two numbers moving inside `state.rs`. That model needs liquidity to exist here
//! before it's useful to anyone — someone has to be quoting, which means someone has to have put
//! up capital. It's also custodial, with everything that implies for what you're allowed to
//! operate and where.
//!
//! This module is the other answer, and it's the one that needs no capital at all: don't be the
//! counterparty. Price the trade against liquidity that already exists somewhere else, take a
//! disclosed cut for doing it well, and let the agent settle it from its own wallet. Nothing is
//! deposited here, so nothing is held here. The trade-off is real and worth stating plainly: a
//! router earns less per trade than a venue does, and it lives or dies on quote quality rather
//! than on captive order flow.
//!
//! # What the engine actually does
//!
//! The naive version of this — ask one API for a price, add a markup, return it — is worth close
//! to nothing and is dangerous besides. The three things that make routing a real product are all
//! refusals:
//!
//! 1. **Refuse stale prices.** A quote from eight seconds ago is not a price, it's a memory.
//!    Routing on it hands your agent a fill it didn't agree to.
//! 2. **Refuse a lone source.** One feed is one thing to manipulate. Quotes are cross-checked
//!    against the median of everything answering, and an outlier is dropped *before* the best
//!    price is picked — so a fake high quote can't win a route just by being the highest.
//! 3. **Refuse to route when sources disagree.** If the survivors don't agree closely enough to
//!    clear `min_sources`, this returns an error rather than a guess. An aggregator that always
//!    answers is an aggregator that sometimes lies.
//!
//! The fee is applied to the output and reported as three separate numbers — gross, fee, net —
//! rather than folded silently into the rate. Hidden markup is the standard way this category of
//! product makes money and it's exactly the thing that makes agents stop trusting a router.

use crate::fees;

/// One venue's answer for a specific trade.
#[derive(Debug, Clone, PartialEq)]
pub struct SourceQuote {
    pub source: String,
    /// How much of the sell asset this quote is for. Quotes are size-specific on purpose:
    /// real liquidity has depth, and a price for 0.1 BTC is not a price for 100 BTC.
    pub sell_amount: f64,
    /// How much of the buy asset the venue would give, before Ikenga's fee and after the
    /// venue's own.
    pub buy_amount: f64,
    pub as_of_ms: i64,
}

/// Anything that can price a swap. Implemented by the adapters in `liquidity.rs`.
///
/// Returning `None` means "I can't price this right now" — an unsupported pair, a failed
/// request, a malformed response. That is always treated as one fewer source, never as a zero
/// price, because a source that answers "0" and a source that doesn't answer must not be the
/// same thing to the router.
pub trait LiquiditySource: Send + Sync {
    fn name(&self) -> &str;
    fn quote(&self, sell_asset: &str, buy_asset: &str, sell_amount: f64) -> Option<SourceQuote>;
}

#[derive(Debug, Clone, PartialEq)]
pub enum RouteError {
    BadPair,
    NonPositiveAmount,
    NoSourcesConfigured,
    /// Not enough usable quotes survived staleness and outlier filtering.
    NotEnoughSources { usable: usize, required: usize },
    /// The fee would eat the whole output. Can only happen with an absurd fee config, but the
    /// alternative to checking is handing someone a route that takes their money and returns
    /// nothing.
    FeeExceedsOutput,
}

impl RouteError {
    pub fn code(&self) -> &'static str {
        match self {
            RouteError::BadPair => "BAD_PAIR",
            RouteError::NonPositiveAmount => "BAD_AMOUNT",
            RouteError::NoSourcesConfigured => "NO_LIQUIDITY_SOURCES",
            RouteError::NotEnoughSources { .. } => "INSUFFICIENT_SOURCES",
            RouteError::FeeExceedsOutput => "FEE_EXCEEDS_OUTPUT",
        }
    }

    pub fn message(&self) -> String {
        match self {
            RouteError::BadPair => "sell and buy must be two different non-empty assets".into(),
            RouteError::NonPositiveAmount => "sell_amount must be greater than zero".into(),
            RouteError::NoSourcesConfigured => {
                "no liquidity sources are configured — this deployment cannot route".into()
            }
            RouteError::NotEnoughSources { usable, required } => format!(
                "only {usable} usable quote(s), need {required}: sources were stale, unreachable, \
                 or disagreed too much to trust — refusing to route rather than guess"
            ),
            RouteError::FeeExceedsOutput => "routing fee exceeds the trade output".into(),
        }
    }
}

/// Why a source's quote was thrown away. Returned with every route so an agent can see what was
/// excluded and why — a router that quietly drops sources is impossible to debug or audit.
#[derive(Debug, Clone, PartialEq)]
pub struct RejectedQuote {
    pub source: String,
    pub reason: String,
}

#[derive(Debug, Clone)]
pub struct Route {
    pub sell_asset: String,
    pub buy_asset: String,
    pub sell_amount: f64,
    /// The venue this route executes against.
    pub source: String,
    /// What the venue gives, before Ikenga's cut.
    pub gross_buy_amount: f64,
    pub fee_bps: f64,
    /// Ikenga's cut, denominated in the buy asset.
    pub fee_amount: f64,
    /// What the agent actually receives: gross minus fee.
    pub net_buy_amount: f64,
    /// The floor to put in the transaction itself. If the trade would fill below this, it should
    /// revert. This is the single most important number here for a non-custodial trade — it's
    /// what stops a sandwich or a moved market from turning a good quote into a bad fill.
    pub min_buy_amount: f64,
    pub slippage_bps: f64,
    /// Median across usable sources, so an agent can see how far the winning quote sits from the
    /// consensus rather than trusting that "best" means "honest".
    pub median_buy_amount: f64,
    pub quotes: Vec<SourceQuote>,
    pub rejected: Vec<RejectedQuote>,
    pub as_of_ms: i64,
}

#[derive(Debug, Clone)]
pub struct RouterConfig {
    /// Ikenga's cut of a routed swap, in basis points.
    pub fee_bps: f64,
    /// Older than this and a quote is discarded rather than used.
    pub max_quote_age_ms: i64,
    /// How many usable quotes must survive filtering before a route is returned. Two is the
    /// minimum that means anything: with one, there is nothing to cross-check against.
    pub min_sources: usize,
    /// How far from the median a quote may sit before it's treated as manipulated or broken.
    pub max_deviation_bps: f64,
}

impl Default for RouterConfig {
    fn default() -> Self {
        RouterConfig {
            fee_bps: fees::ROUTE_FEE_BPS,
            max_quote_age_ms: 10_000,
            min_sources: 2,
            max_deviation_bps: 200.0, // 2%
        }
    }
}

impl RouterConfig {
    /// Reads the knobs an operator is likely to want to change per-deployment. Anything unset
    /// keeps the default above.
    pub fn from_env() -> Self {
        let mut cfg = RouterConfig::default();
        if let Ok(v) = std::env::var("IKENGA_ROUTE_FEE_BPS") {
            if let Ok(parsed) = v.parse::<f64>() {
                if parsed >= 0.0 {
                    cfg.fee_bps = parsed;
                }
            }
        }
        if let Ok(v) = std::env::var("IKENGA_ROUTE_MIN_SOURCES") {
            if let Ok(parsed) = v.parse::<usize>() {
                // Deliberately allows 1, for a deployment that has only one integration wired up
                // and knows it. It's logged loudly at startup, because it removes the only
                // defence against a single manipulated feed.
                cfg.min_sources = parsed.max(1);
            }
        }
        if let Ok(v) = std::env::var("IKENGA_ROUTE_MAX_AGE_MS") {
            if let Ok(parsed) = v.parse::<i64>() {
                if parsed > 0 {
                    cfg.max_quote_age_ms = parsed;
                }
            }
        }
        cfg
    }
}

pub struct Router {
    sources: Vec<Box<dyn LiquiditySource>>,
    config: RouterConfig,
}

impl Router {
    pub fn new(config: RouterConfig) -> Self {
        Router { sources: Vec::new(), config }
    }

    pub fn with_sources(sources: Vec<Box<dyn LiquiditySource>>, config: RouterConfig) -> Self {
        Router { sources, config }
    }

    pub fn add_source(&mut self, source: Box<dyn LiquiditySource>) {
        self.sources.push(source);
    }

    pub fn config(&self) -> &RouterConfig {
        &self.config
    }

    pub fn source_names(&self) -> Vec<String> {
        self.sources.iter().map(|s| s.name().to_string()).collect()
    }

    pub fn is_configured(&self) -> bool {
        !self.sources.is_empty()
    }

    /// Prices a swap across every configured source and returns the best executable route.
    ///
    /// The order of operations matters and is the whole safety argument: collect, drop stale,
    /// drop outliers *against the median*, and only then take the maximum. Taking the maximum
    /// first would mean a single fake quote — the highest one, by construction — decides the
    /// route.
    pub fn route(
        &self,
        sell_asset: &str,
        buy_asset: &str,
        sell_amount: f64,
        slippage_bps: f64,
        now_ms: i64,
    ) -> Result<Route, RouteError> {
        if sell_asset.is_empty()
            || buy_asset.is_empty()
            || sell_asset.eq_ignore_ascii_case(buy_asset)
        {
            return Err(RouteError::BadPair);
        }
        if !(sell_amount > 0.0) || !sell_amount.is_finite() {
            return Err(RouteError::NonPositiveAmount);
        }
        if self.sources.is_empty() {
            return Err(RouteError::NoSourcesConfigured);
        }

        let mut rejected: Vec<RejectedQuote> = Vec::new();
        let mut fresh: Vec<SourceQuote> = Vec::new();

        for source in &self.sources {
            let Some(q) = source.quote(sell_asset, buy_asset, sell_amount) else {
                rejected.push(RejectedQuote {
                    source: source.name().to_string(),
                    reason: "no quote returned (unsupported pair, unreachable, or bad response)"
                        .into(),
                });
                continue;
            };
            if !(q.buy_amount > 0.0) || !q.buy_amount.is_finite() {
                rejected.push(RejectedQuote {
                    source: q.source,
                    reason: "quoted a non-positive or non-finite output".into(),
                });
                continue;
            }
            let age = now_ms - q.as_of_ms;
            if age > self.config.max_quote_age_ms {
                rejected.push(RejectedQuote {
                    source: q.source,
                    reason: format!("stale by {age}ms (max {}ms)", self.config.max_quote_age_ms),
                });
                continue;
            }
            fresh.push(q);
        }

        if fresh.len() < self.config.min_sources {
            return Err(RouteError::NotEnoughSources {
                usable: fresh.len(),
                required: self.config.min_sources,
            });
        }

        let median = median_of(fresh.iter().map(|q| q.buy_amount));

        let mut usable: Vec<SourceQuote> = Vec::new();
        for q in fresh {
            let deviation_bps = ((q.buy_amount - median) / median).abs() * 10_000.0;
            if deviation_bps > self.config.max_deviation_bps {
                rejected.push(RejectedQuote {
                    source: q.source,
                    reason: format!(
                        "{deviation_bps:.0}bps from the median of {median:.8} (max {}bps) — \
                         treated as manipulated or broken",
                        self.config.max_deviation_bps
                    ),
                });
                continue;
            }
            usable.push(q);
        }

        if usable.len() < self.config.min_sources {
            return Err(RouteError::NotEnoughSources {
                usable: usable.len(),
                required: self.config.min_sources,
            });
        }

        // Safe to take the maximum now: anything that got here already agrees with the consensus.
        let best = usable
            .iter()
            .max_by(|a, b| a.buy_amount.partial_cmp(&b.buy_amount).unwrap_or(std::cmp::Ordering::Equal))
            .expect("usable is non-empty — min_sources is at least 1")
            .clone();

        let fee_amount = best.buy_amount * self.config.fee_bps / 10_000.0;
        let net_buy_amount = best.buy_amount - fee_amount;
        if !(net_buy_amount > 0.0) {
            return Err(RouteError::FeeExceedsOutput);
        }

        let slippage_bps = slippage_bps.clamp(0.0, 10_000.0);
        let min_buy_amount = net_buy_amount * (1.0 - slippage_bps / 10_000.0);

        // Recomputed across the survivors so the reported consensus matches what was actually
        // routed against, not the pre-filter set that included the outliers.
        let usable_median = median_of(usable.iter().map(|q| q.buy_amount));

        Ok(Route {
            sell_asset: sell_asset.to_string(),
            buy_asset: buy_asset.to_string(),
            sell_amount,
            source: best.source.clone(),
            gross_buy_amount: best.buy_amount,
            fee_bps: self.config.fee_bps,
            fee_amount,
            net_buy_amount,
            min_buy_amount,
            slippage_bps,
            median_buy_amount: usable_median,
            quotes: usable,
            rejected,
            as_of_ms: now_ms,
        })
    }
}

fn median_of(values: impl Iterator<Item = f64>) -> f64 {
    let mut v: Vec<f64> = values.collect();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = v.len();
    if n == 0 {
        return 0.0;
    }
    if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A source that answers with whatever it's told to. Tests only.
    struct FakeSource {
        name: String,
        buy_amount: f64,
        age_ms: i64,
        answers: bool,
    }

    impl FakeSource {
        fn new(name: &str, buy_amount: f64) -> Self {
            FakeSource { name: name.into(), buy_amount, age_ms: 0, answers: true }
        }
        fn stale_by(mut self, ms: i64) -> Self {
            self.age_ms = ms;
            self
        }
        fn silent(mut self) -> Self {
            self.answers = false;
            self
        }
        fn boxed(self) -> Box<dyn LiquiditySource> {
            Box::new(self)
        }
    }

    impl LiquiditySource for FakeSource {
        fn name(&self) -> &str {
            &self.name
        }
        fn quote(&self, _s: &str, _b: &str, sell_amount: f64) -> Option<SourceQuote> {
            if !self.answers {
                return None;
            }
            Some(SourceQuote {
                source: self.name.clone(),
                sell_amount,
                buy_amount: self.buy_amount,
                as_of_ms: NOW - self.age_ms,
            })
        }
    }

    const NOW: i64 = 1_800_000_000_000;

    fn router(sources: Vec<Box<dyn LiquiditySource>>) -> Router {
        Router::with_sources(sources, RouterConfig { fee_bps: 10.0, ..Default::default() })
    }

    #[test]
    fn picks_the_best_of_several_honest_quotes() {
        let r = router(vec![
            FakeSource::new("venue_a", 64_000.0).boxed(),
            FakeSource::new("venue_b", 64_500.0).boxed(),
            FakeSource::new("venue_c", 64_100.0).boxed(),
        ]);
        let route = r.route("BTC", "USD", 1.0, 50.0, NOW).unwrap();
        assert_eq!(route.source, "venue_b");
        assert_eq!(route.gross_buy_amount, 64_500.0);
    }

    #[test]
    fn fee_is_taken_from_the_output_and_reported_separately() {
        let r = router(vec![
            FakeSource::new("venue_a", 10_000.0).boxed(),
            FakeSource::new("venue_b", 10_000.0).boxed(),
        ]);
        let route = r.route("BTC", "USD", 1.0, 0.0, NOW).unwrap();
        // 10bps of 10,000 = 10.
        assert!((route.fee_amount - 10.0).abs() < 1e-9);
        assert!((route.net_buy_amount - 9_990.0).abs() < 1e-9);
        assert!(
            (route.gross_buy_amount - (route.net_buy_amount + route.fee_amount)).abs() < 1e-9,
            "gross must always equal net plus fee — that identity is the whole transparency claim"
        );
    }

    #[test]
    fn a_single_manipulated_source_cannot_win_the_route() {
        // The attack: one source claims a wildly better price to capture the route, then fills
        // badly (or not at all). It's the highest quote, so a naive "pick the max" router picks
        // it every time.
        let r = router(vec![
            FakeSource::new("honest_a", 64_000.0).boxed(),
            FakeSource::new("honest_b", 64_100.0).boxed(),
            FakeSource::new("liar", 90_000.0).boxed(),
        ]);
        let route = r.route("BTC", "USD", 1.0, 50.0, NOW).unwrap();
        assert_ne!(route.source, "liar", "the manipulated quote won the route");
        assert_eq!(route.source, "honest_b");
        assert!(route.rejected.iter().any(|r| r.source == "liar"));
    }

    #[test]
    fn stale_quotes_are_dropped() {
        let r = router(vec![
            FakeSource::new("fresh_a", 64_000.0).boxed(),
            FakeSource::new("fresh_b", 64_050.0).boxed(),
            FakeSource::new("ancient", 64_900.0).stale_by(60_000).boxed(),
        ]);
        let route = r.route("BTC", "USD", 1.0, 50.0, NOW).unwrap();
        assert_ne!(route.source, "ancient");
        assert!(route.rejected.iter().any(|r| r.source == "ancient" && r.reason.contains("stale")));
    }

    #[test]
    fn two_sources_that_disagree_wildly_refuse_to_route() {
        // With no majority to appeal to, there is no way to tell which one is lying. Refusing is
        // the only honest answer — this is the case a "always return something" router gets wrong.
        let r = router(vec![
            FakeSource::new("venue_a", 64_000.0).boxed(),
            FakeSource::new("venue_b", 90_000.0).boxed(),
        ]);
        let err = r.route("BTC", "USD", 1.0, 50.0, NOW).unwrap_err();
        assert!(matches!(err, RouteError::NotEnoughSources { .. }));
    }

    #[test]
    fn one_lone_source_is_not_enough_by_default() {
        let r = router(vec![
            FakeSource::new("only_one", 64_000.0).boxed(),
            FakeSource::new("offline", 0.0).silent().boxed(),
        ]);
        let err = r.route("BTC", "USD", 1.0, 50.0, NOW).unwrap_err();
        assert_eq!(err.code(), "INSUFFICIENT_SOURCES");
    }

    #[test]
    fn min_buy_amount_applies_slippage_below_the_net() {
        let r = router(vec![
            FakeSource::new("venue_a", 10_000.0).boxed(),
            FakeSource::new("venue_b", 10_000.0).boxed(),
        ]);
        let route = r.route("BTC", "USD", 1.0, 100.0, NOW).unwrap(); // 1% tolerance
        // net 9,990 less 1% = 9,890.1
        assert!((route.min_buy_amount - 9_890.1).abs() < 1e-6);
        assert!(
            route.min_buy_amount < route.net_buy_amount,
            "the transaction floor must sit below the quote, or it can never fill"
        );
    }

    #[test]
    fn rejects_nonsense_inputs() {
        let r = router(vec![
            FakeSource::new("a", 1.0).boxed(),
            FakeSource::new("b", 1.0).boxed(),
        ]);
        assert_eq!(r.route("BTC", "BTC", 1.0, 0.0, NOW).unwrap_err(), RouteError::BadPair);
        assert_eq!(r.route("", "USD", 1.0, 0.0, NOW).unwrap_err(), RouteError::BadPair);
        assert_eq!(
            r.route("BTC", "USD", 0.0, 0.0, NOW).unwrap_err(),
            RouteError::NonPositiveAmount
        );
        assert_eq!(
            r.route("BTC", "USD", f64::NAN, 0.0, NOW).unwrap_err(),
            RouteError::NonPositiveAmount
        );
    }

    #[test]
    fn a_deployment_with_no_sources_says_so_instead_of_inventing_a_price() {
        let r = router(vec![]);
        assert_eq!(
            r.route("BTC", "USD", 1.0, 0.0, NOW).unwrap_err(),
            RouteError::NoSourcesConfigured
        );
    }

    #[test]
    fn an_absurd_fee_is_refused_rather_than_zeroing_the_output() {
        let r = Router::with_sources(
            vec![FakeSource::new("a", 100.0).boxed(), FakeSource::new("b", 100.0).boxed()],
            RouterConfig { fee_bps: 10_000.0, ..Default::default() },
        );
        assert_eq!(
            r.route("BTC", "USD", 1.0, 0.0, NOW).unwrap_err(),
            RouteError::FeeExceedsOutput
        );
    }

    #[test]
    fn the_routing_fee_is_always_positive_for_a_positive_trade() {
        // The order book's equivalent invariant (fees.rs) is that the venue can never pay out
        // more than it takes in. The router's is simpler but just as load-bearing: routing is
        // never free by accident, because a zero-fee route is indistinguishable from a bug.
        let r = router(vec![
            FakeSource::new("a", 1_000.0).boxed(),
            FakeSource::new("b", 1_000.0).boxed(),
        ]);
        let route = r.route("BTC", "USD", 1.0, 0.0, NOW).unwrap();
        assert!(route.fee_amount > 0.0);
        assert!(route.net_buy_amount < route.gross_buy_amount);
    }
}
