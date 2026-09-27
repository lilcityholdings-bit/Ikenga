//! Five fun, free-points prediction markets about AI and agent news, opened at startup.
//!
//! Each has a fixed market id, so it is opened once and a restart (which replays the WAL first)
//! finds it already there and leaves it alone. Every one is staked in promotional points only —
//! the asset can never be withdrawn — and resolved by the operator against the public source
//! named in its terms, with disputes going to Agenttrust like any other market.
//!
//! `IKENGA_STARTER_MARKETS=off` skips all of this.

use crate::prediction::{Market, MarketStatus, ResolutionSpec};
use crate::state::AppState;

const DAY_MS: i64 = 24 * 60 * 60 * 1000;

/// (id, question, criteria, source, closes_at_ms, observed_at_ms)
pub const STARTER_MARKETS: [(&str, &str, &str, &str, i64, i64); 5] = [
    (
        "mkt_ai_arena_october",
        "Will a brand-new model take the #1 spot on the LMArena text leaderboard during October 2026?",
        "YES if a model first released on or after 1 Oct 2026 is ranked #1 overall on the LMArena \
         text leaderboard at any point between 1 Oct and 31 Oct 2026 (UTC). Otherwise NO.",
        "lmarena.ai/leaderboard",
        1793487600000, // 31 Oct 2026 23:00 UTC
        1793534400000, // 1 Nov 2026 12:00 UTC
    ),
    (
        "mkt_ai_big3_flagship",
        "Will OpenAI, Anthropic or Google DeepMind launch a new flagship AI model before 15 Nov 2026?",
        "YES if any of the three publicly releases a new top-tier model (announced as their most \
         capable, generally available to users or developers) between 27 Sep and 14 Nov 2026 \
         (UTC). Previews, research papers and minor point updates don't count.",
        "openai.com/news, anthropic.com/news, deepmind.google/discover/blog",
        1794697200000, // 14 Nov 2026 23:00 UTC
        1794744000000, // 15 Nov 2026 12:00 UTC
    ),
    (
        "mkt_ai_agenttrust_150",
        "Will any Ikenga agent reach an Agenttrust trust score of 150 or more by 1 Dec 2026?",
        "YES if, on 1 Dec 2026, the public Agenttrust profile of any agent whose id starts with \
         \"ikenga-\" shows a score of 150 or more. Otherwise NO. (Every agent starts at 100.)",
        "agenttrust-production-381e.up.railway.app/v1/trust/<agent id>",
        1796079600000, // 30 Nov 2026 23:00 UTC
        1796126400000, // 1 Dec 2026 12:00 UTC
    ),
    (
        "mkt_ai_open_weights_top5",
        "Will an open-weights model be in the top 5 of the LMArena text leaderboard on 31 Dec 2026?",
        "YES if at least one of the top five models on the LMArena text leaderboard, as shown on \
         31 Dec 2026, has publicly downloadable weights. Otherwise NO.",
        "lmarena.ai/leaderboard",
        1798718400000, // 31 Dec 2026 12:00 UTC
        1798758000000, // 31 Dec 2026 23:00 UTC
    ),
    (
        "mkt_ai_agent_science",
        "Will an AI agent be credited with a discovery in a paper published in Nature or Science in 2026?",
        "YES if a paper published in Nature or Science between 27 Sep and 31 Dec 2026 credits an \
         autonomous AI agent (not just a model used as a tool) with a key discovery in its main \
         findings. Otherwise NO.",
        "nature.com, science.org",
        1798718400000, // 31 Dec 2026 12:00 UTC
        1798804800000, // 1 Jan 2027 12:00 UTC
    ),
];

/// Opens any starter market that doesn't exist yet and hasn't already closed. Returns how many
/// were opened.
pub fn open_starter_markets(state: &AppState, now_ms: i64) -> usize {
    if std::env::var("IKENGA_STARTER_MARKETS").as_deref() == Ok("off") {
        return 0;
    }
    let mut opened = 0;
    for (id, question, criteria, source, closes, observed) in STARTER_MARKETS {
        if state.markets.lock().unwrap().contains_key(id) {
            continue;
        }
        let mut market = Market {
            market_id: id.to_string(),
            question: question.to_string(),
            outcomes: vec!["YES".to_string(), "NO".to_string()],
            resolution: ResolutionSpec::OperatorDeclared {
                criteria: criteria.to_string(),
                source: source.to_string(),
            },
            // Free play points, always. Never a redeemable asset.
            asset: crate::credits::POINTS.to_string(),
            closes_at_ms: closes,
            observed_at_ms: observed,
            dispute_window_ms: DAY_MS,
            commitment: String::new(),
            status: MarketStatus::Open,
            proposal: None,
            winning_outcome: None,
            created_at_ms: now_ms,
            disputed_at_ms: None,
        };
        // A starter whose close date has passed (an old deployment catching up) is skipped,
        // not opened already-closed.
        if market.validate_spec(now_ms).is_err() {
            continue;
        }
        market.commitment = market.compute_commitment();
        state.open_market(market);
        opened += 1;
    }
    opened
}

#[cfg(test)]
mod tests {
    use super::*;

    const SEPT_27_2026: i64 = 1_790_467_200_000;

    #[test]
    fn opens_all_five_in_points_once() {
        let state = AppState::new();
        assert_eq!(open_starter_markets(&state, SEPT_27_2026), 5);
        assert_eq!(open_starter_markets(&state, SEPT_27_2026), 0, "never opened twice");
        let markets = state.markets.lock().unwrap();
        assert_eq!(markets.len(), 5);
        for m in markets.values() {
            assert_eq!(m.asset, crate::credits::POINTS, "{} must be free points", m.market_id);
            assert!(m.commitment_is_intact());
            assert!(m.observed_at_ms > m.closes_at_ms);
        }
    }

    #[test]
    fn closed_starters_are_skipped() {
        let state = AppState::new();
        let after_november = 1_796_200_000_000;
        assert_eq!(open_starter_markets(&state, after_november), 2);
    }
}
