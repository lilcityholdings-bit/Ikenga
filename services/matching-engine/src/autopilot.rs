//! Runs the venue on its own.
//!
//! # What this is for
//!
//! Everything else in this codebase can *host* a market. Nothing in it *opens* one. Start the
//! server and you get a correct, well-tested, completely empty room — and an empty room is not a
//! product. The first agent to arrive finds nothing to bet on, leaves, and does not come back;
//! you do not get a second first impression.
//!
//! Autopilot closes that gap. On a schedule it reads a live price, opens a market on it, and
//! leaves. The settlement sweeper in `main.rs` already proposes and finalises machine-resolved
//! markets once their observation time passes, so a market opened here is born, traded, settled
//! and paid out with nobody touching it. A venue that has been running for a week has a week of
//! settled history and a live board, whether or not anyone was watching.
//!
//! # Why the threshold is always the current price
//!
//! The obvious design is a round number — "will BTC be above 70,000?" — and it is wrong twice
//! over. Most of the time the answer is already effectively known, which makes the market a
//! formality that pays the informed and bores everyone else; and a market whose answer is known
//! is exactly the shape `forecast.rs` throws out of the published accuracy, so the venue would be
//! generating its own junk.
//!
//! Instead the threshold is the price *at the moment the market opens*. "Will BTC be above where
//! it is right now, one hour from now?" has no free answer — it is close to a coin flip by
//! construction, it is genuinely about forecasting, and it cannot be farmed by whoever reads the
//! news fastest. It is also the question a trading agent most wants an opinion on.
//!
//! # Why it refuses to invent prices
//!
//! If the router cannot produce a cross-checked price, autopilot opens nothing. It does not
//! guess, does not fall back to the last known value, and does not use the demo sources. A market
//! whose threshold came from a made-up number is a market that will settle against a made-up
//! number, and the whole point of committing the terms up front is that the answer comes from
//! outside. Skipping a cycle is free; a bad market costs someone real money.

use std::sync::Arc;

use crate::prediction::{Comparator, Market, MarketStatus, ResolutionSpec};
use crate::state::AppState;

/// One kind of market autopilot knows how to open.
#[derive(Debug, Clone, PartialEq)]
pub struct Template {
    /// Pair to price, as `BASE-QUOTE`.
    pub symbol: String,
    /// How long the market accepts stakes.
    pub open_for_ms: i64,
    /// Gap between the market closing and the price being read. Never zero: a market that closes
    /// at the same instant it is observed lets the last stake in win for free.
    pub settle_after_ms: i64,
    /// How long anyone has to challenge the observation before payout.
    pub dispute_window_ms: i64,
    /// What the pool is denominated in.
    pub asset: String,
    /// How far the threshold sits from spot, in basis points. Zero is at-the-money.
    ///
    /// # Why a board of at-the-money markets is a board nobody should trade
    ///
    /// "Will BTC be above exactly where it is now, in an hour?" is, to a good approximation, a
    /// fair coin. Over a short horizon the drift is nothing next to the noise, so the honest
    /// answer is 50% and every participant knows it. A pool on a fair coin with a rake is a game
    /// with negative expected value for every player and no skill that can overcome it. An agent
    /// that computes expected value — which is the only kind worth attracting — correctly declines
    /// to play, forever, and the board stays empty for a perfectly good reason.
    ///
    /// The defect is not the rake. It is that an at-the-money market has no room for anyone to be
    /// *right*. Move the threshold away from spot and the question stops being a coin flip and
    /// starts being about the distribution: how far this thing moves in an hour, how fat the tail
    /// is, whether the recent range holds. Those are questions where two models genuinely disagree
    /// and one of them is genuinely better. That is the only kind of question a prediction market
    /// has ever paid anyone for answering.
    ///
    /// A ladder of strikes matters for a second reason. An agent that holds a real view — that
    /// this pair is calmer than usual, say — cannot express it at a single strike. Given several,
    /// it can back the near ones and lay the far ones, and the venue learns something from where
    /// it put its money. One strike collects a coin flip; a ladder collects a distribution.
    pub offset_bps: i32,
}

impl Template {
    /// Parses one template from `SYMBOL:OPEN_SECONDS[:ASSET]`, e.g. `BTC-USD:3600` or
    /// `ETH-USD:900:USDC`.
    ///
    /// A deliberately small format. Autopilot decides what markets exist on a venue that may be
    /// running unattended for weeks, so the configuration is the kind of thing a person should be
    /// able to read correctly at a glance and get right in one attempt.
    pub fn parse(spec: &str) -> Result<Template, String> {
        Self::parse_ladder(spec).map(|mut v| v.remove(0))
    }

    /// Parses one template spec into the rung or rungs it describes.
    ///
    /// `BTC-USD:3600` is a single at-the-money market, unchanged. `BTC-USD:3600@-50,0,+50` is a
    /// three-rung ladder on the same pair and window: one market half a percent below spot, one
    /// at it, one half a percent above.
    pub fn parse_ladder(spec: &str) -> Result<Vec<Template>, String> {
        let (head, offsets) = match spec.split_once('@') {
            Some((h, tail)) => {
                let mut parsed = Vec::new();
                for raw in tail.split(',').map(str::trim).filter(|o| !o.is_empty()) {
                    let bps: i32 = raw
                        .trim_start_matches('+')
                        .parse()
                        .map_err(|_| format!("'{raw}' is not an offset in basis points"))?;
                    // A strike a whole percent-and-a-half out on an hourly crypto market is
                    // already almost never reached; ten percent is a market with one answer, which
                    // is a market nobody can be right about and a rake on nothing.
                    if bps.abs() > 1_000 {
                        return Err(format!(
                            "offset {bps}bps is more than 10% from spot — nobody would take the \
                             other side"
                        ));
                    }
                    if parsed.contains(&bps) {
                        return Err(format!("offset {bps} is listed twice"));
                    }
                    parsed.push(bps);
                }
                if parsed.is_empty() {
                    return Err(format!("'{spec}' has an empty offset list after '@'"));
                }
                (h, parsed)
            }
            None => (spec, vec![0]),
        };
        let base = Self::parse_one(head)?;
        Ok(offsets
            .into_iter()
            .map(|offset_bps| Template { offset_bps, ..base.clone() })
            .collect())
    }

    fn parse_one(spec: &str) -> Result<Template, String> {
        let parts: Vec<&str> = spec.split(':').map(str::trim).collect();
        if parts.len() < 2 || parts.len() > 3 {
            return Err(format!(
                "'{spec}' should be SYMBOL:OPEN_SECONDS or SYMBOL:OPEN_SECONDS:ASSET"
            ));
        }
        let symbol = parts[0].to_ascii_uppercase();
        if !symbol.contains('-') || symbol.starts_with('-') || symbol.ends_with('-') {
            return Err(format!("'{}' is not a BASE-QUOTE symbol", parts[0]));
        }
        let open_secs: i64 = parts[1]
            .parse()
            .map_err(|_| format!("'{}' is not a number of seconds", parts[1]))?;
        if open_secs < 60 {
            return Err(format!(
                "{open_secs}s is too short to open a market for; use at least 60"
            ));
        }
        let asset = parts.get(2).map(|a| a.to_ascii_uppercase()).unwrap_or_else(|| {
            crate::api::POINTS_ASSET.to_string()
        });

        let open_for_ms = open_secs * 1000;
        Ok(Template {
            symbol,
            open_for_ms,
            // A quarter of the trading window, floored at two minutes and capped at fifteen.
            //
            // This gap is what a last-second bettor is actually forecasting over, and it used to
            // be a tenth — sixty seconds on a ten-minute market. Predicting a price sixty seconds
            // out is far easier than predicting it ten minutes out, so a bet placed at the close
            // was a much better bet than the same one at the open, and the house's static seed
            // was sitting there at even odds to take the other side of it. Widening the gap does
            // not remove that advantage (no market can — later information is always better) but
            // it stops the closing bet from being close to a certainty.
            //
            // The floor stays at a minute rather than rising with the ratio: a sixty-second
            // market is a test fixture, and making it wait two minutes to observe turns every
            // short-market test into a timeout without protecting anything real. It is the
            // *ratio* that matters on markets people actually trade.
            settle_after_ms: (open_for_ms / 4).clamp(60_000, 900_000),
            // Proportional to the market, not a flat five minutes: a one-minute market that
            // takes five to pay reads as broken, and a daily one deserves longer than a token
            // pause. Floored at a minute so there is always a real window in which a bad
            // observation can be challenged before anyone is paid.
            dispute_window_ms: (open_for_ms / 10).clamp(60_000, 600_000),
            asset,
            offset_bps: 0,
        })
    }

    /// How this rung is written into its question, so one rung of a ladder is never mistaken for
    /// another.
    ///
    /// The at-the-money rung carries a real phrase rather than an empty string, and that is not
    /// cosmetic. `already_running` asks whether a live market's question contains this label, and
    /// every string contains the empty string — so with `""` here the at-the-money rung matched
    /// the first market of any rung and was skipped as a duplicate forever. A three-rung ladder
    /// quietly opened two markets, every cycle, with nothing in the log to say why. Found by
    /// watching a live board come up short, not by a test.
    pub fn strike_label(&self) -> String {
        match self.offset_bps {
            0 => "(the price it opened at)".to_string(),
            bps if bps > 0 => format!("({}% above where it opened)", bps as f64 / 100.0),
            bps => format!("({}% below where it opened)", (-bps) as f64 / 100.0),
        }
    }

    /// The question text, with the threshold written into it.
    ///
    /// The number appears in the question and not only in the machine-readable rule, because the
    /// question is what a human reads and the commitment hash covers both. If they ever disagreed,
    /// the market would be misleading and provably so.
    pub fn question(&self, threshold: f64, open_for_ms: i64) -> String {
        let minutes = open_for_ms / 60_000;
        let window = if minutes % 60 == 0 && minutes >= 60 {
            let hours = minutes / 60;
            format!("{hours} hour{}", if hours == 1 { "" } else { "s" })
        } else {
            format!("{minutes} minute{}", if minutes == 1 { "" } else { "s" })
        };
        // The distance from spot goes in the question because it is the part a reader needs to
        // judge the bet, and because the commitment hash covers the question text: a market that
        // said "above 68,412" without saying that was half a percent up could be quietly reissued
        // at a different distance and read identically.
        format!(
            "Will {} be above {} {} in {}?",
            self.symbol,
            format_price(threshold),
            self.strike_label(),
            window
        )
    }
}

/// Prices are written into the question, so they are formatted for a reader rather than dumped
/// at full float precision — but never rounded so hard that the printed number stops matching
/// the number the market actually settles against.
pub fn format_price(p: f64) -> String {
    let decimals = if p >= 1_000.0 {
        2
    } else if p >= 1.0 {
        4
    } else {
        8
    };
    let s = format!("{p:.decimals$}");
    // Trim trailing zeros so 70000.00 reads as 70000, without turning 0.5 into 0.
    let s = if s.contains('.') {
        s.trim_end_matches('0').trim_end_matches('.').to_string()
    } else {
        s
    };
    s
}

/// Reads the configured templates from `IKENGA_AUTOPILOT`.
///
/// Comma-separated, e.g. `BTC-USD:3600,ETH-USD:900`. Empty or unset means autopilot is off, which
/// is the default: a venue should not start inventing markets because someone ran the binary.
pub fn templates_from_env() -> (Vec<Template>, Vec<String>) {
    templates_from_spec(&std::env::var("IKENGA_AUTOPILOT").unwrap_or_default())
}

/// Splits a configuration string into individual template specs.
///
/// The comma does double duty — it separates templates *and* the rungs of a ladder — so a naive
/// `split(',')` turns `BTC-USD:3600@-50,0,+50` into three fragments, two of which are not template
/// specs at all. It failed exactly that way the first time, and the ladder silently collapsed to
/// one market with two parse errors logged beside it.
///
/// The rule that disambiguates them: a fragment continues the previous one only if the previous
/// one opened an offset list with `@` *and* this fragment has no colon of its own. Both halves are
/// needed. Without the colon test, `ETH-USD:900` following a ladder would be swallowed into it;
/// without the `@` test, an ordinary typo like `BTC-USD:3600, nonsense` would be glued onto its
/// innocent neighbour and take it down too, turning one bad entry into two lost markets.
fn split_specs(spec: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for piece in spec.split(',').map(str::trim).filter(|p| !p.is_empty()) {
        let continues_a_ladder =
            !piece.contains(':') && out.last().is_some_and(|prev| prev.contains('@'));
        if !continues_a_ladder {
            out.push(piece.to_string());
        } else {
            // Belongs to the previous spec's offset list.
            let last = out.last_mut().expect("checked non-empty above");
            last.push(',');
            last.push_str(piece);
        }
    }
    out
}

pub fn templates_from_spec(spec: &str) -> (Vec<Template>, Vec<String>) {
    let mut out = Vec::new();
    let mut problems = Vec::new();
    for part in split_specs(spec) {
        match Template::parse_ladder(&part) {
            Ok(rungs) => out.extend(rungs),
            Err(e) => problems.push(e),
        }
    }
    (out, problems)
}

/// Whether a template already has a live market, so autopilot tops the board up rather than
/// stacking duplicates.
///
/// Keyed on the symbol and the trading window, not on the question text: two BTC markets closing
/// an hour apart are different products, two opened in the same cycle are a bug.
pub fn already_running(state: &AppState, t: &Template, now_ms: i64) -> bool {
    let markets = state.markets.lock().unwrap();
    markets.values().any(|m| {
        m.status == MarketStatus::Open
            && m.closes_at_ms > now_ms
            && m.asset == t.asset
            // Matched on the question text as well as the pair, because the rungs of one ladder
            // share a symbol and a window and are nonetheless different markets. Without this the
            // second rung would be mistaken for a duplicate of the first and never open, and the
            // board would silently collapse back to a single at-the-money coin flip.
            && matches!(
                &m.resolution,
                ResolutionSpec::PriceThreshold { symbol, .. } if *symbol == t.symbol
            )
            && m.question.contains(&t.strike_label())
            // Same intended duration, within a tolerance, so an hourly and a 15-minute market on
            // the same pair coexist.
            && (m.closes_at_ms - m.created_at_ms - t.open_for_ms).abs() < t.open_for_ms / 4
    })
}

/// Why a cycle produced no market. Each of these is a normal thing to happen, not a fault.
#[derive(Debug, Clone, PartialEq)]
pub enum Skipped {
    /// A market of this shape is already taking stakes.
    AlreadyRunning,
    /// No cross-checked price was available. Autopilot never invents one.
    NoPrice(String),
    /// The price came back as zero, negative or not a number.
    UnusablePrice(f64),
    /// The venue does not accept this asset (real money off, or an unknown asset).
    AssetNotEnabled(String),
}

impl Skipped {
    pub fn message(&self) -> String {
        match self {
            Skipped::AlreadyRunning => "a market of this shape is already open".to_string(),
            Skipped::NoPrice(why) => format!(
                "no cross-checked price: {why} — run `./ikenga sources` on this machine. Two \
                 sources must answer before anything can settle, and a venue that does not serve \
                 your country never will"
            ),
            Skipped::UnusablePrice(p) => format!("price {p} is not usable as a threshold"),
            Skipped::AssetNotEnabled(a) => format!("asset {a} is not enabled here"),
        }
    }
}

/// Builds one market from a template and a price. Pure, so the interesting decisions are testable
/// without a running venue or a price feed.
pub fn market_from(t: &Template, spot: f64, now_ms: i64) -> Market {
    let closes_at_ms = now_ms + t.open_for_ms;
    // The strike is spot shifted by the template's offset. Derived here and nowhere else, so the
    // number written into the question, the number hashed into the commitment and the number the
    // market settles against are one value that cannot drift apart.
    let threshold = spot * (1.0 + t.offset_bps as f64 / 10_000.0);
    let mut m = Market {
        market_id: format!(
            "mkt_{}",
            crate::crypto::hex_encode(&crate::crypto::random_bytes_pooled(8))
        ),
        question: t.question(threshold, t.open_for_ms),
        outcomes: vec!["YES".to_string(), "NO".to_string()],
        resolution: ResolutionSpec::PriceThreshold {
            symbol: t.symbol.clone(),
            comparator: Comparator::Above,
            threshold,
            if_true_outcome: 0,
            if_false_outcome: 1,
        },
        asset: t.asset.clone(),
        closes_at_ms,
        observed_at_ms: closes_at_ms + t.settle_after_ms,
        dispute_window_ms: t.dispute_window_ms,
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

/// Runs one cycle for one template: check, price, open.
pub fn run_once(state: &AppState, t: &Template, now_ms: i64) -> Result<String, Skipped> {
    if !state.asset_is_stakeable(&t.asset) {
        return Err(Skipped::AssetNotEnabled(t.asset.clone()));
    }
    if already_running(state, t, now_ms) {
        return Err(Skipped::AlreadyRunning);
    }

    let (base, quote) = t
        .symbol
        .split_once('-')
        .map(|(b, q)| (b.to_string(), q.to_string()))
        .ok_or_else(|| Skipped::NoPrice("symbol is not BASE-QUOTE".to_string()))?;

    // The same cross-checked median the settlement path uses. Deliberately: a market whose
    // threshold came from one source and whose answer comes from a consensus of several is a
    // market that can be opened at a price it will never be judged against.
    let price = match state.router.route(&base, &quote, 1.0, 0.0, now_ms) {
        Ok(route) => route.median_buy_amount,
        Err(e) => return Err(Skipped::NoPrice(format!("{e:?}"))),
    };
    if !price.is_finite() || price <= 0.0 {
        return Err(Skipped::UnusablePrice(price));
    }

    let market = market_from(t, price, now_ms);
    let id = market.market_id.clone();
    state.open_market(market);
    Ok(id)
}

/// Starts the background thread. Off unless `IKENGA_AUTOPILOT` names at least one template.
pub fn install(state: Arc<AppState>) {
    let (templates, problems) = templates_from_env();
    for p in &problems {
        eprintln!("WARNING: IKENGA_AUTOPILOT: {p}");
    }
    if templates.is_empty() {
        println!("autopilot: off (set IKENGA_AUTOPILOT=BTC-USD:3600,ETH-USD:900 to run markets)");
        return;
    }

    let interval_ms = std::env::var("IKENGA_AUTOPILOT_INTERVAL_MS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(30_000)
        .max(1_000);

    println!(
        "autopilot: on — {} template(s), checked every {}s",
        templates.len(),
        interval_ms / 1000
    );
    for t in &templates {
        println!(
            "  {} every {}m, settles {}m after close, in {}",
            t.symbol,
            t.open_for_ms / 60_000,
            t.settle_after_ms / 60_000,
            t.asset
        );
    }

    std::thread::spawn(move || {
        // A short delay before the first cycle, so price sources have a chance to be polled once.
        // Otherwise every start logs a "no price" skip that resolves itself seconds later and
        // teaches the operator to ignore the warning.
        std::thread::sleep(std::time::Duration::from_secs(5));
        loop {
            let now = crate::api::now_ms_pub();
            for t in &templates {
                match run_once(&state, t, now) {
                    Ok(id) => {
                        // Seed it in the same breath as opening it. A market that is live but
                        // empty is one every arriving agent correctly declines, so the gap
                        // between opening and seeding is a gap in which the board looks broken.
                        match state.seed_market(&id, now) {
                            Some((n, each)) => println!(
                                "autopilot: opened {} on {} (seeded {each} on each of {n} outcome(s))",
                                id, t.symbol
                            ),
                            None => println!("autopilot: opened {} on {}", id, t.symbol),
                        }
                    }
                    // AlreadyRunning is the steady state, not news. Logging it every cycle would
                    // bury the skips that mean something.
                    Err(Skipped::AlreadyRunning) => {}
                    Err(why) => {
                        eprintln!("autopilot: skipped {} — {}", t.symbol, why.message())
                    }
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(interval_ms));
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_ladder_spec_opens_one_market_per_rung() {
        let (ts, problems) = templates_from_spec("BTC-USD:3600@-50,0,+50");
        assert!(problems.is_empty(), "{problems:?}");
        assert_eq!(ts.len(), 3);
        let offsets: Vec<i32> = ts.iter().map(|t| t.offset_bps).collect();
        assert_eq!(offsets, vec![-50, 0, 50]);
        for t in &ts {
            assert_eq!(t.symbol, "BTC-USD");
            assert_eq!(t.open_for_ms, 3_600_000);
        }
    }

    #[test]
    fn a_plain_spec_is_still_one_at_the_money_market() {
        let (ts, problems) = templates_from_spec("ETH-USD:900");
        assert!(problems.is_empty());
        assert_eq!(ts.len(), 1);
        assert_eq!(ts[0].offset_bps, 0, "the old format must not change meaning");
    }

    #[test]
    fn the_strike_actually_moves_away_from_spot() {
        let up = Template::parse_ladder("BTC-USD:3600@+100").unwrap().remove(0);
        let m = market_from(&up, 50_000.0, 1_000);
        match &m.resolution {
            ResolutionSpec::PriceThreshold { threshold, .. } => {
                assert!((threshold - 50_500.0).abs() < 1e-6, "got {threshold}");
            }
            _ => panic!("expected a price threshold"),
        }

        let down = Template::parse_ladder("BTC-USD:3600@-200").unwrap().remove(0);
        let m2 = market_from(&down, 50_000.0, 1_000);
        match &m2.resolution {
            ResolutionSpec::PriceThreshold { threshold, .. } => {
                assert!((threshold - 49_000.0).abs() < 1e-6, "got {threshold}");
            }
            _ => panic!("expected a price threshold"),
        }
    }

    #[test]
    fn every_rung_says_which_rung_it_is() {
        let ts = Template::parse_ladder("BTC-USD:3600@-50,0,+50").unwrap();
        let questions: Vec<String> =
            ts.iter().map(|t| market_from(t, 50_000.0, 1_000).question).collect();
        let mut unique = questions.clone();
        unique.sort();
        unique.dedup();
        assert_eq!(unique.len(), 3, "two rungs read identically: {questions:?}");
        assert!(questions.iter().any(|q| q.contains("above where it opened")));
        assert!(questions.iter().any(|q| q.contains("below where it opened")));
    }

    #[test]
    fn the_printed_strike_still_matches_the_settling_strike_on_every_rung() {
        for offset in [-1000, -137, -1, 0, 1, 137, 1000] {
            let t = Template::parse_ladder(&format!("BTC-USD:3600@{offset}")).unwrap().remove(0);
            let m = market_from(&t, 43_211.77, 1_000);
            match &m.resolution {
                ResolutionSpec::PriceThreshold { threshold, .. } => {
                    let printed = format_price(*threshold);
                    assert!(
                        m.question.contains(&printed),
                        "offset {offset}: question {:?} lacks its own threshold {printed}",
                        m.question
                    );
                }
                _ => panic!("expected a price threshold"),
            }
        }
    }

    #[test]
    fn a_ladder_rung_is_not_mistaken_for_a_duplicate_of_its_neighbour() {
        // Every rung's label must appear in its own question and in NO other rung's. The empty
        // string passes the first half and fails the second, which is exactly how the
        // at-the-money rung silently stopped opening.
        let ts = Template::parse_ladder("BTC-USD:3600@-50,0,+50,+137").unwrap();
        let markets: Vec<Market> = ts.iter().map(|t| market_from(t, 50_000.0, 1_000)).collect();
        for (i, t) in ts.iter().enumerate() {
            let label = t.strike_label();
            assert!(!label.is_empty(), "rung {} has no label to tell it apart", t.offset_bps);
            for (j, m) in markets.iter().enumerate() {
                assert_eq!(
                    m.question.contains(&label),
                    i == j,
                    "rung {}'s label {label:?} {} rung {}'s question {:?}",
                    ts[i].offset_bps,
                    if i == j { "is missing from" } else { "wrongly matches" },
                    ts[j].offset_bps,
                    m.question
                );
            }
        }
    }

    #[test]
    fn absurd_and_malformed_offsets_are_refused() {
        for bad in ["BTC-USD:3600@", "BTC-USD:3600@abc", "BTC-USD:3600@5000", "BTC-USD:3600@-9999", "BTC-USD:3600@50,50"] {
            assert!(Template::parse_ladder(bad).is_err(), "{bad:?} should not parse");
        }
    }

    #[test]
    fn parses_the_short_form_and_defaults_to_points() {
        let t = Template::parse("btc-usd:3600").unwrap();
        assert_eq!(t.symbol, "BTC-USD", "symbols are normalised to upper case");
        assert_eq!(t.open_for_ms, 3_600_000);
        assert_eq!(t.asset, crate::api::POINTS_ASSET);
    }

    #[test]
    fn parses_an_explicit_asset() {
        let t = Template::parse("ETH-USD:900:usdc").unwrap();
        assert_eq!(t.asset, "USDC");
        assert_eq!(t.open_for_ms, 900_000);
    }

    #[test]
    fn refuses_specs_that_would_produce_a_broken_market() {
        for bad in ["", "BTC-USD", "BTCUSD:3600", "BTC-USD:soon", "BTC-USD:30", "-USD:3600", "a:b:c:d"] {
            assert!(Template::parse(bad).is_err(), "{bad:?} should not parse");
        }
    }

    #[test]
    fn there_is_always_a_gap_between_closing_and_observing() {
        // Without it the last stake in wins for free — the market is still taking money at the
        // moment its answer becomes visible.
        for secs in [60, 300, 900, 3600, 86_400] {
            let t = Template::parse(&format!("BTC-USD:{secs}")).unwrap();
            assert!(t.settle_after_ms >= 60_000, "{secs}s gave {}", t.settle_after_ms);
            let m = market_from(&t, 100.0, 0);
            assert!(
                m.observed_at_ms > m.closes_at_ms,
                "{secs}s market observes at or before it closes"
            );
        }
    }

    #[test]
    fn the_generated_market_passes_the_engines_own_validation() {
        let t = Template::parse("BTC-USD:3600").unwrap();
        let now = 1_800_000_000_000;
        let m = market_from(&t, 68_412.5, now);
        m.validate_spec(now).expect("autopilot must not generate a market the engine rejects");
        assert!(m.commitment_is_intact(), "commitment must cover the generated terms");
        assert!(m.resolution.is_machine_resolved(), "must settle without a human");
    }

    #[test]
    fn the_threshold_is_the_price_at_open_so_the_answer_is_not_already_known() {
        let t = Template::parse("BTC-USD:3600").unwrap();
        let m = market_from(&t, 68_412.5, 0);
        match &m.resolution {
            ResolutionSpec::PriceThreshold { threshold, comparator, .. } => {
                assert_eq!(*threshold, 68_412.5);
                assert_eq!(*comparator, Comparator::Above);
            }
            _ => panic!("expected a price threshold"),
        }
        // A market at exactly the current price is the one shape that cannot be farmed by knowing
        // the answer in advance, and the one shape forecast.rs will actually score.
        assert!(m.question.contains("68412.5"), "question was {:?}", m.question);
    }

    #[test]
    fn the_question_and_the_settlement_rule_never_disagree() {
        // Both are covered by the commitment hash, so a mismatch would be a provable lie.
        for price in [0.00001234, 0.5, 1.25, 999.999, 68_412.5, 1_000_000.0] {
            let t = Template::parse("X-USD:3600").unwrap();
            let m = market_from(&t, price, 0);
            let printed = format_price(price);
            assert!(
                m.question.contains(&printed),
                "question {:?} does not contain its own threshold {printed}",
                m.question
            );
            assert_eq!(
                printed.parse::<f64>().unwrap(),
                price,
                "the printed threshold must round-trip to the settling threshold"
            );
        }
    }

    #[test]
    fn windows_read_naturally() {
        let t = Template::parse("BTC-USD:3600").unwrap();
        assert!(t.question(1.0, 3_600_000).contains("1 hour"));
        assert!(t.question(1.0, 7_200_000).contains("2 hours"));
        assert!(t.question(1.0, 900_000).contains("15 minutes"));
        assert!(t.question(1.0, 60_000).contains("1 minute?"), "singular minute must not read '1 minutes'");
    }

    #[test]
    fn a_ladder_and_a_plain_template_can_share_one_setting() {
        let (ts, problems) = templates_from_spec("BTC-USD:3600@-50,+50, ETH-USD:900");
        assert!(problems.is_empty(), "{problems:?}");
        assert_eq!(ts.len(), 3, "two rungs and one plain market");
        assert_eq!(ts[2].symbol, "ETH-USD", "the plain template was swallowed by the ladder");
        assert_eq!(ts[2].offset_bps, 0);
    }

    #[test]
    fn a_bad_entry_does_not_discard_the_good_ones() {
        let (ts, problems) = templates_from_spec("BTC-USD:3600, nonsense, ETH-USD:900");
        assert_eq!(ts.len(), 2, "good templates must survive a bad neighbour");
        assert_eq!(problems.len(), 1);
    }

    #[test]
    fn an_empty_setting_means_off_rather_than_an_error() {
        let (ts, problems) = templates_from_spec("   ");
        assert!(ts.is_empty());
        assert!(problems.is_empty(), "off is not a misconfiguration");
    }

    #[test]
    fn the_dispute_window_is_real_but_proportionate() {
        for secs in [60, 300, 3600, 86_400] {
            let t = Template::parse(&format!("BTC-USD:{secs}")).unwrap();
            assert!(
                t.dispute_window_ms >= 60_000,
                "{secs}s market had a {}ms window — too short to challenge a bad price",
                t.dispute_window_ms
            );
            assert!(t.dispute_window_ms <= 600_000, "{secs}s window was {}", t.dispute_window_ms);
        }
        let hourly = Template::parse("BTC-USD:3600").unwrap();
        let minute = Template::parse("BTC-USD:60").unwrap();
        assert!(
            hourly.dispute_window_ms > minute.dispute_window_ms,
            "an hourly market should wait longer than a one-minute one"
        );
        // A one-minute market must pay within a couple of minutes of closing, or the venue looks
        // broken to anyone watching it settle.
        assert!(minute.settle_after_ms + minute.dispute_window_ms <= 180_000);
    }
}
