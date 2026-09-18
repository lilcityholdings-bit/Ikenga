use std::collections::HashMap;
use std::sync::Mutex;

/// Trust score bands (0-1000) and the request-rate limit each band gets.
///
/// This implements the *scoring bands and resulting limits* from the spec. It does NOT
/// implement the fraud-detection heuristics that should actually move an agent's score
/// (wash-trade detection, sybil clustering, settlement-reliability tracking, decay, appeals).
/// Those are listed in docs/ROADMAP.md phase 3 as separate work — plugging in a fake/static
/// score here would be worse than being explicit that scoring inputs aren't built yet.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrustBand {
    New,
    Developing,
    Established,
    Trusted,
    HighlyTrusted,
    Elite,
}

impl TrustBand {
    pub fn from_score(score: u32) -> Self {
        match score {
            0..=199 => TrustBand::New,
            200..=399 => TrustBand::Developing,
            400..=599 => TrustBand::Established,
            600..=799 => TrustBand::Trusted,
            800..=949 => TrustBand::HighlyTrusted,
            _ => TrustBand::Elite,
        }
    }

    pub fn rate_limit_per_sec(&self) -> u32 {
        match self {
            TrustBand::New => 10,
            TrustBand::Developing => 50,
            TrustBand::Established => 100,
            TrustBand::Trusted => 500,
            TrustBand::HighlyTrusted => 1000,
            TrustBand::Elite => 2000,
        }
    }

    pub fn label(&self) -> &'static str {
        match self {
            TrustBand::New => "New",
            TrustBand::Developing => "Developing",
            TrustBand::Established => "Established",
            TrustBand::Trusted => "Trusted",
            TrustBand::HighlyTrusted => "Highly Trusted",
            TrustBand::Elite => "Elite",
        }
    }
}

#[derive(Default)]
pub struct TrustStore {
    scores: Mutex<HashMap<String, u32>>,
    /// Trust earned specifically on markets denominated in a redeemable asset.
    ///
    /// # Why two numbers instead of one
    ///
    /// A single trust score has to serve two jobs that want opposite things from it, and the
    /// conflict is exploitable.
    ///
    /// *Rate limits* want trust to be **easy to earn**, or a new agent is stuck at 10 req/s with
    /// no way out and never becomes a user. Free points exist precisely so onboarding costs
    /// nothing, so forecasting well with points has to count.
    ///
    /// *Calibration weight in the paid feed* wants trust to be **expensive to earn**, because it
    /// decides whose opinion the data product amplifies. Registration mints 1,000 free points to
    /// anyone who asks, so if points bought weight, a dozen throwaway accounts could push a
    /// market's consensus to the wrong answer with free money, have one account quietly take the
    /// other side, and farm the beating-the-crowd bonus at zero cost — buying influence over the
    /// signal being sold to paying subscribers.
    ///
    /// Splitting them resolves it: `score` is earned anywhere and governs rate limits and who may
    /// open a market; `forecast_score` is earned only where the agent's own money was at risk,
    /// and is the only one `forecast::calibration_weight` will look at. Being right with real
    /// money is not an attack — it is the thing the product is trying to find.
    forecast_scores: Mutex<HashMap<String, u32>>,
}

impl TrustStore {
    pub fn get_score(&self, agent_id: &str) -> u32 {
        // New agents default to 0 (band: New) rather than panicking on an unknown agent.
        self.scores.lock().unwrap().get(agent_id).copied().unwrap_or(0)
    }

    pub fn set_score(&self, agent_id: impl Into<String>, score: u32) {
        self.scores.lock().unwrap().insert(agent_id.into(), score.min(1000));
    }

    /// Trust earned with real money at stake. Used only for weighting the sold forecast.
    pub fn get_forecast_score(&self, agent_id: &str) -> u32 {
        self.forecast_scores.lock().unwrap().get(agent_id).copied().unwrap_or(0)
    }

    pub fn set_forecast_score(&self, agent_id: impl Into<String>, score: u32) {
        self.forecast_scores.lock().unwrap().insert(agent_id.into(), score.min(1000));
    }

    pub fn band(&self, agent_id: &str) -> TrustBand {
        TrustBand::from_score(self.get_score(agent_id))
    }
}

/// Simple fixed-window rate limiter, one window per agent. Adequate for a single-instance
/// demo; a real deployment needs this backed by Redis so limits hold across gateway replicas.
#[derive(Default)]
pub struct RateLimiter {
    windows: Mutex<HashMap<String, (i64, u32)>>, // agent_id -> (window_start_ms, count_in_window)
}

impl RateLimiter {
    /// Returns true if the request is allowed under `limit_per_sec`.
    pub fn allow(&self, agent_id: &str, limit_per_sec: u32, now_ms: i64) -> bool {
        self.allow_windowed(agent_id, limit_per_sec, 1000, now_ms)
    }

    /// Same fixed-window logic over an arbitrary window length, for limits that are naturally
    /// per-hour rather than per-second — signup and trial quotes, where "5 per second" is
    /// meaningless but "5 per hour" is the actual policy.
    ///
    /// Callers sharing one limiter must namespace their keys (`reg:<ip>`, `trial:<ip>`) so two
    /// different limits don't consume each other's budget.
    /// Entries older than this are dropped. A stale window carries no information — the counter
    /// is reset on sight anyway — so keeping it only costs memory.
    const STALE_AFTER_MS: i64 = 60 * 60 * 1000;

    /// Above this many tracked keys, a sweep runs before inserting a new one.
    ///
    /// The map is keyed by agent id and by remote address, and nothing ever removed from it. Every
    /// distinct caller left a permanent entry: a few dozen bytes each, but the set of "distinct
    /// callers" over months is unbounded and attacker-chosen — anyone with a range of addresses,
    /// or simply time, grows it forever. Rate limiting is supposed to be the defence against
    /// resource exhaustion, not a source of it.
    const SWEEP_THRESHOLD: usize = 10_000;

    /// Whether this key has already used its budget — *without* spending any of it.
    ///
    /// Needed to put a cheap check in front of an expensive one. Signature verification costs a
    /// process spawn (~3.5ms), and `allow` consumes budget on every call, so it cannot be used as
    /// a pre-flight test without charging honest callers for the check itself.
    pub fn exhausted(&self, key: &str, limit: u32, window_ms: i64, now_ms: i64) -> bool {
        let window_ms = window_ms.max(1);
        let window_start = now_ms - (now_ms % window_ms);
        let windows = self.windows.lock().unwrap();
        match windows.get(key) {
            Some((start, count)) => *start == window_start && *count >= limit,
            None => false,
        }
    }

    pub fn allow_windowed(&self, key: &str, limit: u32, window_ms: i64, now_ms: i64) -> bool {
        let window_ms = window_ms.max(1);
        let window_start = now_ms - (now_ms % window_ms);
        let mut windows = self.windows.lock().unwrap();
        if windows.len() > Self::SWEEP_THRESHOLD && !windows.contains_key(key) {
            windows.retain(|_, (start, _)| now_ms - *start < Self::STALE_AFTER_MS);
        }
        let entry = windows.entry(key.to_string()).or_insert((window_start, 0));
        if entry.0 != window_start {
            *entry = (window_start, 0);
        }
        if entry.1 >= limit {
            return false;
        }
        entry.1 += 1;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bands_match_spec_table() {
        assert_eq!(TrustBand::from_score(0).rate_limit_per_sec(), 10);
        assert_eq!(TrustBand::from_score(199).rate_limit_per_sec(), 10);
        assert_eq!(TrustBand::from_score(200).rate_limit_per_sec(), 50);
        assert_eq!(TrustBand::from_score(599).rate_limit_per_sec(), 100);
        assert_eq!(TrustBand::from_score(600).rate_limit_per_sec(), 500);
        assert_eq!(TrustBand::from_score(950).rate_limit_per_sec(), 2000);
        assert_eq!(TrustBand::from_score(1000).rate_limit_per_sec(), 2000);
    }

    #[test]
    fn rate_limiter_blocks_after_limit_within_same_window() {
        let limiter = RateLimiter::default();
        let now = 1_000_000_i64;
        for _ in 0..10 {
            assert!(limiter.allow("agent_x", 10, now));
        }
        assert!(!limiter.allow("agent_x", 10, now));
    }

    #[test]
    fn rate_limiter_resets_on_new_window() {
        let limiter = RateLimiter::default();
        for _ in 0..10 {
            assert!(limiter.allow("agent_x", 10, 1_000));
        }
        assert!(!limiter.allow("agent_x", 10, 1_000));
        assert!(limiter.allow("agent_x", 10, 2_000)); // next second, window resets
    }

    #[test]
    fn hourly_window_holds_across_a_whole_hour_then_resets() {
        let limiter = RateLimiter::default();
        const HOUR: i64 = 3_600_000;
        let t = 5 * HOUR; // start of some hour
        for _ in 0..5 {
            assert!(limiter.allow_windowed("reg:1.2.3.4", 5, HOUR, t));
        }
        assert!(!limiter.allow_windowed("reg:1.2.3.4", 5, HOUR, t));
        // Still blocked 59 minutes later — a per-second limiter would have let this through.
        assert!(!limiter.allow_windowed("reg:1.2.3.4", 5, HOUR, t + 59 * 60_000));
        assert!(limiter.allow_windowed("reg:1.2.3.4", 5, HOUR, t + HOUR));
    }

    #[test]
    fn namespaced_keys_do_not_consume_each_others_budget() {
        let limiter = RateLimiter::default();
        const HOUR: i64 = 3_600_000;
        assert!(limiter.allow_windowed("reg:1.2.3.4", 1, HOUR, 0));
        assert!(!limiter.allow_windowed("reg:1.2.3.4", 1, HOUR, 0));
        // Same IP, different limit — must have its own budget.
        assert!(limiter.allow_windowed("trial:1.2.3.4", 1, HOUR, 0));
        // And a different IP is unaffected.
        assert!(limiter.allow_windowed("reg:5.6.7.8", 1, HOUR, 0));
    }

    #[test]
    fn the_limiter_does_not_grow_without_bound() {
        let rl = RateLimiter::default();
        let now = 1_000_000_000_000;
        // Twenty thousand one-shot callers, as a spray of addresses would produce.
        for i in 0..20_000 {
            rl.allow_windowed(&format!("ip:{i}"), 5, 1000, now);
        }
        // Then time passes and a new caller arrives.
        let later = now + 2 * 60 * 60 * 1000;
        rl.allow_windowed("ip:fresh", 5, 1000, later);
        let held = rl.windows.lock().unwrap().len();
        assert!(
            held < 20_000,
            "stale windows must be swept, still holding {held}"
        );
    }
}
