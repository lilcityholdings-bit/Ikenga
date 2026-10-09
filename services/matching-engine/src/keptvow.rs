//! Keptvow: the outside referee for disputed markets, and the public trust score shown next to
//! every agent.
//!
//! Keptvow (https://github.com/lilcityholdings-bit/Agenttrust) settles disagreements between
//! two bots — by a named arbiter or a randomly drawn jury — and keeps a public 0–1000 trust score
//! for every bot built only from how those settlements went. Ikenga uses it for two things:
//!
//! 1. **Dispute resolution.** When someone with a stake disputes a proposed outcome, the market
//!    freezes (that part is unchanged — see `state::dispute_outcome`). This module then opens an
//!    Keptvow agreement between the resolver that proposed the outcome and the agent that
//!    disputed it, each side reports what it says happened, and Keptvow decides. Its verdict
//!    is applied here and paid out; "couldn't decide" voids the market and refunds everyone.
//!    The operator can still step in and re-propose at any time, and the 24-hour review backstop
//!    still voids anything nobody decided — Keptvow being unreachable never traps anyone's
//!    points.
//! 2. **Trust scores.** `GET /v1/agents/{id}/keptvow` returns the agent's Keptvow score and
//!    verdict, cached for a few minutes, so the dashboard and other agents can see it.
//!
//! Everything sent to Keptvow is denominated in free play points (`asset: "IKENGA_POINTS"`).
//! No real money is held or moved by either service.
//!
//! Ikenga agents appear in Keptvow as `ikenga-<pseudonym>`, where the pseudonym is an HMAC of
//! the agent id under `KEPTVOW_SECRET` (or the owner key). Two reasons:
//!
//! - **Privacy.** Keptvow's trust profiles and audit feed are public. `privacy.rs` promises an
//!   agent's real id never leaves this server, so it must not appear there — not as the id, and
//!   not in evidence text.
//! - **No squatting.** Keptvow ids are claimed first-come by whoever presents a secret first.
//!   A predictable id (`ikenga-<agent id>`, or a fixed resolver name) could be claimed by an
//!   attacker beforehand, so Ikenga's report for that side would be refused and the other side
//!   would win by default. An HMAC nobody else can compute can't be claimed in advance.
//!
//! Keptvow identifies each bot by a claimed secret; Ikenga derives that from the same key, so
//! nothing extra is stored and the same agent always gets the same id and secret.
//!
//! Like `liquidity.rs`, HTTP goes through the system `curl` — this build has no crates.io access.
//! Requests are fed to curl on stdin as a config file, so API keys and per-agent secrets never
//! appear on a command line where `ps` could show them.

use std::collections::HashMap;
use std::io::Write;
use std::process::{Command, Stdio};
use std::sync::Mutex;

use crate::json::{self, Json};

pub const DEFAULT_URL: &str = "https://keptvow.com";
/// Stands in for "whoever proposed the outcome under dispute" when deriving its Keptvow id.
/// Not a valid Ikenga agent id, so it can't collide with one.
const RESOLVER_SEED: &str = "#resolver";
/// Upper bound on cached trust lookups, so the cache can't be grown without limit.
const TRUST_CACHE_MAX: usize = 10_000;
/// How long a looked-up trust score is reused. Public lookups are rate-limited per IP on
/// Keptvow's side, and a score only moves when a settlement happens.
const TRUST_CACHE_MS: i64 = 5 * 60 * 1000;
/// Minimum gap between two checks of the same pending verdict. Juries and arbiters take hours,
/// and each check spends one of Keptvow's free hourly lookups, so there's no point asking often.
pub const POLL_EVERY_MS: i64 = 2 * 60 * 1000;
/// Errors carrying this are temporary (Keptvow unreachable or rate-limited) and worth retrying.
pub const RETRY_MARKER: &str = "try again later";

/// An open referral: the market is frozen and waiting on this Keptvow agreement.
#[derive(Debug, Clone, PartialEq)]
pub struct Referral {
    pub agreement_id: String,
    pub last_polled_ms: i64,
}

/// What Keptvow decided about a referred dispute.
#[derive(Debug, Clone, PartialEq)]
pub enum Verdict {
    /// Not decided yet.
    Wait,
    /// Pay out this market outcome.
    Outcome(usize),
    /// Couldn't be decided (or the ruling was "void"): refund every stake.
    Void,
}

/// One agent's public trust profile, as far as Ikenga shows it.
#[derive(Debug, Clone, PartialEq)]
pub struct TrustView {
    pub score: Option<f64>,
    pub verdict: Option<String>,
}

pub struct Keptvow {
    /// `None` when switched off with `KEPTVOW_URL=off`.
    pub base_url: Option<String>,
    api_key: Option<String>,
    arbiter: Option<String>,
    secret_key: Vec<u8>,
    trust_cache: Mutex<HashMap<String, (i64, Option<TrustView>)>>,
    /// market_id -> the agreement deciding its dispute.
    pub referrals: Mutex<HashMap<String, Referral>>,
    /// market_id -> (disputer, the outcome they say is right; `None` = "can't be determined").
    /// Held only until the referral is opened, a few seconds later.
    pub pending: Mutex<HashMap<String, (String, Option<usize>, String)>>,
    /// Head-to-head bets where both sides have reported, waiting to be recorded on Keptvow.
    pub bets: Mutex<Vec<BetRecord>>,
}

/// A two-sided bet, both answers in, to be put on both bots' Keptvow records.
#[derive(Debug, Clone, PartialEq)]
pub struct BetRecord {
    pub market_id: String,
    pub a: (String, usize),
    pub b: (String, usize),
    /// How many times recording has been tried; gives up after `MAX_BET_ATTEMPTS`.
    pub attempts: u32,
}

/// Retries for a bet that couldn't be recorded yet (Keptvow down or its hourly limit hit). The
/// referee runs every 10 seconds, so this is a little over an hour — long enough to outlast the
/// free tier's hourly window.
pub const MAX_BET_ATTEMPTS: u32 = 400;

/// Whether a string from Keptvow (or replayed from the log) is safe to put in a URL path and
/// a curl config line. Agreement ids look like `agr_12`; anything else is refused rather than
/// escaped, because a newline or quote here would let the other side inject curl options.
pub fn is_safe_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-'))
}

/// Keptvow must be reached over HTTPS: the API key and per-agent secrets travel in the
/// request. Plain HTTP is allowed only to this machine, for tests.
fn url_allowed(u: &str) -> bool {
    u.starts_with("https://")
        || u.starts_with("http://127.0.0.1")
        || u.starts_with("http://localhost")
}

impl Keptvow {
    pub fn from_env(owner_key: &[u8]) -> Self {
        // KEPTVOW_*, or the AGENTTRUST_* names from before the rename.
        let env = |k: &str| {
            std::env::var(format!("KEPTVOW_{k}"))
                .or_else(|_| std::env::var(format!("AGENTTRUST_{k}")))
                .ok()
        };
        let raw = env("URL").unwrap_or_else(|| DEFAULT_URL.to_string());
        let base_url = match raw.trim() {
            "" | "off" | "0" => None,
            u if url_allowed(u) => Some(u.trim_end_matches('/').to_string()),
            u => {
                eprintln!("keptvow: KEPTVOW_URL {u} is not https:// — Keptvow disabled");
                None
            }
        };
        let nonempty = |k: &str| env(k).filter(|v| !v.trim().is_empty());
        let secret_key = nonempty("SECRET")
            .map(|s| s.into_bytes())
            .unwrap_or_else(|| owner_key.to_vec());
        Self::new(base_url, nonempty("API_KEY"), nonempty("ARBITER"), secret_key)
    }

    pub fn new(
        base_url: Option<String>,
        api_key: Option<String>,
        arbiter: Option<String>,
        secret_key: Vec<u8>,
    ) -> Self {
        Keptvow {
            base_url,
            api_key,
            arbiter,
            secret_key,
            trust_cache: Mutex::new(HashMap::new()),
            referrals: Mutex::new(HashMap::new()),
            pending: Mutex::new(HashMap::new()),
            bets: Mutex::new(Vec::new()),
        }
    }

    /// Whether disputes and head-to-head bets go to Keptvow. Keptvow's free tier opens deals with
    /// no key; a platform key (`KEPTVOW_API_KEY`) only lifts its hourly limit and makes the deals
    /// count toward a bot's "good" and "excellent" levels.
    pub fn resolves_disputes(&self) -> bool {
        self.base_url.is_some()
    }

    /// The pseudonymous id an Ikenga agent has on Keptvow. See the module docs.
    pub fn keptvow_id(&self, agent_id: &str) -> String {
        let mac = crate::crypto::hmac_sha256(
            &self.secret_key,
            format!("agenttrust-id:{agent_id}").as_bytes(),
        );
        format!("ikenga-{}", crate::crypto::hex_encode(&mac[..10]))
    }

    fn resolver_id(&self) -> String {
        self.keptvow_id(RESOLVER_SEED)
    }

    pub fn profile_url(&self, agent_id: &str) -> Option<String> {
        self.base_url.as_ref().map(|b| format!("{b}/trust/{}", self.keptvow_id(agent_id)))
    }

    fn secret_for(&self, at_id: &str) -> String {
        crate::crypto::hex_encode(&crate::crypto::hmac_sha256(
            &self.secret_key,
            format!("agenttrust-secret:{at_id}").as_bytes(),
        ))
    }

    /// One HTTP call. Returns (status, parsed body). `with_key` sends the platform API key —
    /// only for the calls that need it, so public lookups are never billed to the platform.
    fn call(&self, method: &str, path: &str, body: Option<&Json>, with_key: bool) -> Option<(u16, Json)> {
        let base = self.base_url.as_ref()?;
        // Every value lands inside a double-quoted curl config string. Quotes and backslashes
        // are escaped; control characters (newlines above all) would end the line and start a
        // new option, so any value containing one is refused outright.
        if [base.as_str(), path].iter().any(|v| v.chars().any(char::is_control)) {
            return None;
        }
        let q = |s: &str| s.replace('\\', "\\\\").replace('"', "\\\"");
        let mut cfg = String::new();
        cfg.push_str(&format!("url = \"{}\"\n", q(&format!("{base}{path}"))));
        cfg.push_str(&format!("request = \"{method}\"\n"));
        cfg.push_str("silent\nshow-error\nmax-time = 10\nmax-filesize = 1048576\n");
        cfg.push_str("proto = \"=http,https\"\n");
        cfg.push_str("header = \"Accept: application/json\"\n");
        if let (true, Some(k)) = (with_key, &self.api_key) {
            if k.chars().any(char::is_control) {
                return None;
            }
            cfg.push_str(&format!("header = \"Authorization: Bearer {}\"\n", q(k)));
        }
        if let Some(b) = body {
            // Json::to_string escapes every control character, so the body is always one line.
            cfg.push_str("header = \"Content-Type: application/json\"\n");
            cfg.push_str(&format!("data-binary = \"{}\"\n", q(&b.to_string())));
        }
        cfg.push_str("write-out = \"\\n%{http_code}\"\n");

        let mut child = Command::new("curl")
            .args(["--config", "-"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .ok()?;
        child.stdin.take()?.write_all(cfg.as_bytes()).ok()?;
        let out = child.wait_with_output().ok()?;
        let text = String::from_utf8(out.stdout).ok()?;
        let (body, code) = text.rsplit_once('\n')?;
        let code: u16 = code.trim().parse().ok()?;
        if code == 0 {
            return None;
        }
        Some((code, json::parse(body).unwrap_or(Json::Null)))
    }

    /// The agent's Keptvow score and verdict. `None` if Keptvow couldn't be reached.
    pub fn trust(&self, agent_id: &str, now_ms: i64) -> Option<TrustView> {
        let at_id = self.keptvow_id(agent_id);
        if let Some((at, v)) = self.trust_cache.lock().unwrap().get(&at_id) {
            if now_ms - at < TRUST_CACHE_MS {
                return v.clone();
            }
        }
        let fetched = self.call("GET", &format!("/v1/trust/{at_id}"), None, false).and_then(|(code, j)| {
            match code {
                200 => Some(TrustView {
                    score: j.get("score").and_then(Json::as_f64),
                    verdict: j.get("trust_level").and_then(Json::as_str).map(str::to_string),
                }),
                // Never dealt on Keptvow yet: a real answer, not an outage.
                404 => Some(TrustView { score: None, verdict: Some("unknown".into()) }),
                _ => None,
            }
        });
        // Cache only real answers, so a blip isn't remembered for five minutes.
        if fetched.is_some() {
            let mut cache = self.trust_cache.lock().unwrap();
            if cache.len() >= TRUST_CACHE_MAX {
                cache.retain(|_, (at, _)| now_ms - *at < TRUST_CACHE_MS);
                if cache.len() >= TRUST_CACHE_MAX {
                    cache.clear();
                }
            }
            cache.insert(at_id, (now_ms, fetched.clone()));
        }
        fetched
    }

    /// Opens a Keptvow agreement between two sides and files each side's answer. Returns the
    /// agreement id.
    ///
    /// `outcomes` are the market's outcome names; Keptvow gets one more, "void", meaning "this
    /// can't be determined", at index `outcomes.len()`. Each side is (Keptvow id, the outcome it
    /// says happened, its evidence). Evidence is published on Keptvow, so it must never carry an
    /// Ikenga agent id — see the module docs.
    fn open_case(
        &self,
        outcomes: &[String],
        stake_points: f64,
        a: (&str, usize, String),
        b: (&str, usize, String),
    ) -> Result<String, String> {
        if !self.resolves_disputes() {
            return Err("Keptvow is switched off on this deployment".into());
        }
        let mut labels: Vec<String> =
            outcomes.iter().map(|o| o.chars().take(64).collect::<String>()).collect();
        labels.push("void".into());
        let mut lower: Vec<String> = labels.iter().map(|l| l.trim().to_lowercase()).collect();
        lower.sort();
        lower.dedup();
        let outcomes_json = if lower.len() == labels.len()
            && labels.len() <= 16
            && labels.iter().all(|l| !l.trim().is_empty())
        {
            Json::Array(labels.into_iter().map(Json::str).collect())
        } else {
            Json::num((outcomes.len() + 1) as f64)
        };

        let mut fields = vec![
            ("parties", Json::Array(vec![Json::str(a.0), Json::str(b.0)])),
            ("outcomes", outcomes_json),
            ("stake", Json::num(stake_points.max(0.0))),
            ("asset", Json::str("IKENGA_POINTS")),
            ("domain", Json::str("wagering")),
            ("secret", Json::str(self.secret_for(a.0))),
        ];
        if let Some(arb) = &self.arbiter {
            fields.push(("arbiter", Json::str(arb.clone())));
        }
        let (code, created) = match self.call("POST", "/v1/agreements", Some(&Json::obj(fields)), true) {
            None => return Err(format!("Keptvow could not be reached; {RETRY_MARKER}")),
            Some((429, _)) => return Err(format!("Keptvow's hourly limit was reached; {RETRY_MARKER}")),
            Some(r) => r,
        };
        let agreement_id = created
            .get("agreement_id")
            .and_then(Json::as_str)
            .map(str::to_string)
            .ok_or_else(|| format!("Keptvow refused the agreement ({code}): {}", created.to_string()))?;
        if !is_safe_id(&agreement_id) {
            return Err("Keptvow returned a malformed agreement id; refusing it".into());
        }

        let short = |s: String| s.chars().take(900).collect::<String>();
        let report = |(who, outcome, evidence): (&str, usize, String)| {
            self.call(
                "POST",
                &format!("/v1/agreements/{agreement_id}/report"),
                Some(&Json::obj(vec![
                    ("agent_id", Json::str(who)),
                    ("outcome", Json::num(outcome as f64)),
                    ("secret", Json::str(self.secret_for(who))),
                    ("evidence", Json::str(short(evidence))),
                ])),
                true,
            )
            .map(|(code, _)| (200..300).contains(&code))
            .unwrap_or(false)
        };
        // Both sides must be on record. If either report was refused, the agreement would settle
        // "by default" for the side that did report — not a ruling anyone should be paid on, and
        // not a fair mark on the other side's record. The unaccepted agreement cancels itself on
        // Keptvow's side with no penalty to anyone.
        let a_ok = report(a);
        let b_ok = report(b);
        if !(a_ok && b_ok) {
            return Err(format!("Keptvow refused a report on {agreement_id}"));
        }
        Ok(agreement_id)
    }

    /// Sends a disputed market to Keptvow: the resolver (whoever proposed the outcome) against
    /// the agent that disputed it. `proposed`/`claimed` of `None` mean "void".
    pub fn refer(
        &self,
        market_id: &str,
        question: &str,
        outcomes: &[String],
        stake_points: f64,
        proposed: Option<usize>,
        proposal_evidence: &str,
        disputer: &str,
        claimed: Option<usize>,
        reason: &str,
    ) -> Result<String, String> {
        let void_idx = outcomes.len();
        let resolver_id = self.resolver_id();
        let disputer_id = self.keptvow_id(disputer);
        self.open_case(
            outcomes,
            stake_points,
            (
                &resolver_id,
                proposed.unwrap_or(void_idx),
                format!("Ikenga market {market_id}: \"{question}\". Resolver's evidence: {proposal_evidence}"),
            ),
            (&disputer_id, claimed.unwrap_or(void_idx), format!("The disputer's reason: {reason}")),
        )
    }

    /// Records a settled-by-agreement head-to-head bet on Keptvow, once both sides have reported.
    /// If they agree, it is a clean deal on both records. If not, Keptvow's arbiter or jury
    /// decides, and the returned agreement id becomes the market's referral.
    pub fn record_bet(
        &self,
        market_id: &str,
        question: &str,
        outcomes: &[String],
        stake_points: f64,
        a: (&str, usize),
        b: (&str, usize),
    ) -> Result<String, String> {
        let label = |i: usize| outcomes.get(i).cloned().unwrap_or_else(|| format!("#{i}"));
        let side = |(agent, outcome): (&str, usize)| {
            (
                self.keptvow_id(agent),
                outcome,
                format!(
                    "Ikenga head-to-head bet {market_id}: \"{question}\". This side reported: {}",
                    label(outcome)
                ),
            )
        };
        let (a, b) = (side(a), side(b));
        self.open_case(outcomes, stake_points, (&a.0, a.1, a.2), (&b.0, b.1, b.2))
    }

    /// Asks Keptvow whether the referred dispute has been decided. Keptvow advances its own
    /// deadlines every 20 seconds, so this only reads.
    pub fn verdict(&self, agreement_id: &str, outcome_count: usize) -> Option<Verdict> {
        if !is_safe_id(agreement_id) {
            return None;
        }
        let (code, j) = self.call("GET", &format!("/v1/agreements/{agreement_id}"), None, true)?;
        if code != 200 {
            return None;
        }
        let s = j.get("settlement")?;
        Some(match s.get("instruction").and_then(Json::as_str)? {
            "pay_out" => match s.get("outcome").and_then(Json::as_f64) {
                Some(o) if (o as usize) < outcome_count => Verdict::Outcome(o as usize),
                _ => Verdict::Void,
            },
            "return_stakes" => Verdict::Void,
            _ => Verdict::Wait,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read};
    use std::net::TcpListener;
    use std::sync::Arc;

    #[test]
    fn ids_are_pseudonyms_that_never_contain_the_agent_id() {
        let at = Keptvow::new(None, None, None, b"k".to_vec());
        let id = at.keptvow_id("agent_ABC");
        assert!(id.starts_with("ikenga-") && id.len() == "ikenga-".len() + 20, "{id}");
        assert!(!id.contains("agent_ABC"), "the real id must never leave the server");
        assert!(is_safe_id(&id));
        assert_eq!(id, at.keptvow_id("agent_ABC"), "stable per agent");
        assert_ne!(id, at.keptvow_id("agent_ABD"));
        let other_key = Keptvow::new(None, None, None, b"k2".to_vec());
        assert_ne!(id, other_key.keptvow_id("agent_ABC"), "unpredictable without the key");
        assert_ne!(at.resolver_id(), "ikenga-resolver", "the resolver id is not guessable either");
    }

    #[test]
    fn only_safe_agreement_ids_are_accepted() {
        assert!(is_safe_id("agr_12"));
        for bad in ["", "agr 1", "a\nb", "a\"b", "../x", "a/b", "x?y=1", &"a".repeat(65)] {
            assert!(!is_safe_id(bad), "{bad:?} must be refused");
        }
    }

    #[test]
    fn keptvow_must_be_https_except_on_this_machine() {
        assert!(url_allowed("https://keptvow.example"));
        assert!(url_allowed("http://127.0.0.1:9000"));
        assert!(!url_allowed("http://keptvow.example"));
        assert!(!url_allowed("file:///etc/passwd"));
    }

    #[test]
    fn a_control_character_never_reaches_curl() {
        let at = Keptvow::new(Some("http://127.0.0.1:1".into()), Some("k".into()), None, vec![1]);
        assert_eq!(at.call("GET", "/v1/x\noutput = \"/tmp/pwned\"", None, true), None);
        assert!(!std::path::Path::new("/tmp/pwned").exists());
    }

    #[test]
    fn secrets_are_stable_per_agent_and_distinct() {
        let at = Keptvow::new(None, None, None, b"k".to_vec());
        assert_eq!(at.secret_for("x"), at.secret_for("x"));
        assert_ne!(at.secret_for("x"), at.secret_for("y"));
        let other = Keptvow::new(None, None, None, b"k2".to_vec());
        assert_ne!(at.secret_for("x"), other.secret_for("x"));
    }

    #[test]
    fn only_switching_keptvow_off_stops_referrals() {
        // Keptvow's free tier needs no key; only switching it off stops referrals.
        assert!(!Keptvow::new(None, Some("k".into()), None, vec![1]).resolves_disputes());
        assert!(Keptvow::new(Some("http://x".into()), None, None, vec![1]).resolves_disputes());
        let off = Keptvow::new(None, None, None, vec![1]);
        assert!(off.refer("m", "q", &["YES".into(), "NO".into()], 1.0, Some(0), "e", "a", None, "r").is_err());
    }

    /// A tiny stand-in for Keptvow on localhost that records every request and answers from
    /// a fixed script, so the real curl path and parsing are exercised end to end.
    fn fake_keptvow(
        answers: Vec<(&'static str, u16, String)>,
    ) -> (String, Arc<Mutex<Vec<(String, String, String)>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let seen = Arc::new(Mutex::new(Vec::new()));
        let seen2 = Arc::clone(&seen);
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let mut parts = line.split_whitespace();
                let method = parts.next().unwrap_or("").to_string();
                let path = parts.next().unwrap_or("").to_string();
                let mut len = 0usize;
                let mut auth = String::new();
                loop {
                    let mut h = String::new();
                    reader.read_line(&mut h).unwrap();
                    if h == "\r\n" || h.is_empty() {
                        break;
                    }
                    let lower = h.to_ascii_lowercase();
                    if let Some(v) = lower.strip_prefix("content-length:") {
                        len = v.trim().parse().unwrap_or(0);
                    }
                    if lower.starts_with("authorization:") {
                        auth = h.trim().to_string();
                    }
                }
                let mut body = vec![0u8; len];
                reader.read_exact(&mut body).unwrap();
                let body = String::from_utf8_lossy(&body).to_string();
                seen2.lock().unwrap().push((format!("{method} {path}"), body, auth));
                let (code, resp) = answers
                    .iter()
                    .find(|(p, _, _)| path.starts_with(p))
                    .map(|(_, c, b)| (*c, b.clone()))
                    .unwrap_or((404, "{\"error\":\"no\"}".into()));
                let _ = write!(
                    stream,
                    "HTTP/1.1 {code} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{resp}",
                    resp.len()
                );
            }
        });
        (url, seen)
    }

    #[test]
    fn refer_opens_an_agreement_and_files_both_reports() {
        let (url, seen) = fake_keptvow(vec![
            ("/v1/agreements/agr_7/report", 200, "{\"result\":\"waiting\"}".into()),
            ("/v1/agreements", 201, "{\"agreement_id\":\"agr_7\"}".into()),
        ]);
        let at = Keptvow::new(Some(url), Some("at_live_key".into()), Some("judge".into()), b"s".to_vec());
        let id = at
            .refer("mkt_1", "Will it?", &["YES".into(), "NO".into()], 250.0, Some(0), "saw it", "agent_B", Some(1), "wrong")
            .unwrap();
        assert_eq!(id, "agr_7");
        let seen = seen.lock().unwrap();
        assert_eq!(seen.len(), 3);
        assert_eq!(seen[0].0, "POST /v1/agreements");
        let create = json::parse(&seen[0].1).unwrap();
        assert_eq!(create.get("asset").and_then(Json::as_str), Some("IKENGA_POINTS"));
        assert_eq!(create.get("arbiter").and_then(Json::as_str), Some("judge"));
        assert_eq!(seen[0].2, "Authorization: Bearer at_live_key");
        let parties = create.get("parties").and_then(Json::as_array).unwrap();
        assert_eq!(parties[1].as_str(), Some(at.keptvow_id("agent_B").as_str()));
        assert!(!seen[0].1.contains("agent_B") && !seen[1].1.contains("agent_B") && !seen[2].1.contains("agent_B"),
                "no real agent id in anything sent to Keptvow");
        let outcomes = create.get("outcomes").and_then(Json::as_array).unwrap();
        assert_eq!(outcomes.len(), 3, "the market's outcomes plus void");
        let r1 = json::parse(&seen[1].1).unwrap();
        let r2 = json::parse(&seen[2].1).unwrap();
        assert_eq!(r1.get("agent_id").and_then(Json::as_str), Some(at.resolver_id().as_str()));
        assert_eq!(r1.get("outcome").and_then(Json::as_f64), Some(0.0));
        assert_eq!(r2.get("agent_id").and_then(Json::as_str), Some(at.keptvow_id("agent_B").as_str()));
        assert_eq!(r2.get("outcome").and_then(Json::as_f64), Some(1.0));
        assert_ne!(r1.get("secret"), r2.get("secret"));
    }

    #[test]
    fn a_disputer_with_no_claim_argues_for_void() {
        let (url, seen) = fake_keptvow(vec![
            ("/v1/agreements/agr_1/report", 200, "{}".into()),
            ("/v1/agreements", 201, "{\"agreement_id\":\"agr_1\"}".into()),
        ]);
        let at = Keptvow::new(Some(url), Some("k".into()), None, b"s".to_vec());
        at.refer("m", "q", &["A".into(), "B".into(), "C".into()], 0.0, Some(2), "e", "x", None, "r").unwrap();
        let r2 = json::parse(&seen.lock().unwrap()[2].1).unwrap();
        assert_eq!(r2.get("outcome").and_then(Json::as_f64), Some(3.0), "index 3 is void");
    }

    #[test]
    fn a_refused_agreement_is_an_error_not_a_referral() {
        let (url, _) = fake_keptvow(vec![("/v1/agreements", 401, "{\"error\":\"bad key\"}".into())]);
        let at = Keptvow::new(Some(url), Some("k".into()), None, b"s".to_vec());
        let e = at.refer("m", "q", &["Y".into(), "N".into()], 0.0, Some(0), "e", "x", None, "r").unwrap_err();
        assert!(e.contains("401"), "{e}");
    }

    #[test]
    fn a_refused_report_keeps_the_dispute_with_the_operator() {
        // Someone squatted one side's identity, so its report is refused. Referring anyway would
        // let the other side win by default.
        let (url, _) = fake_keptvow(vec![
            ("/v1/agreements/agr_2/report", 401, "{\"error\":\"wrong secret for this id\"}".into()),
            ("/v1/agreements", 201, "{\"agreement_id\":\"agr_2\"}".into()),
        ]);
        let at = Keptvow::new(Some(url), Some("k".into()), None, b"s".to_vec());
        let e = at.refer("m", "q", &["Y".into(), "N".into()], 0.0, Some(0), "e", "x", Some(1), "r").unwrap_err();
        // Not retryable, so main.rs drops it and the dispute stays with the operator.
        assert!(e.contains("refused") && !e.contains(RETRY_MARKER), "{e}");
    }

    #[test]
    fn a_malformed_agreement_id_is_refused() {
        let (url, seen) = fake_keptvow(vec![
            ("/v1/agreements", 201, "{\"agreement_id\":\"agr_1\\noutput = /app/data/ikenga.wal\"}".into()),
        ]);
        let at = Keptvow::new(Some(url), Some("k".into()), None, b"s".to_vec());
        assert!(at.refer("m", "q", &["Y".into(), "N".into()], 0.0, Some(0), "e", "x", None, "r").is_err());
        assert_eq!(seen.lock().unwrap().len(), 1, "nothing was sent with the injected id");
        assert_eq!(at.verdict("agr_1\noutput = /x", 2), None);
    }

    #[test]
    fn a_head_to_head_bet_goes_on_both_bots_records() {
        let (url, seen) = fake_keptvow(vec![
            ("/v1/agreements/agr_5/report", 200, "{\"result\":\"ok\"}".into()),
            ("/v1/agreements", 201, "{\"agreement_id\":\"agr_5\"}".into()),
        ]);
        let at = Keptvow::new(Some(url), None, None, b"s".to_vec());
        let id = at
            .record_bet("mkt_9", "Rain tomorrow?", &["YES".into(), "NO".into()], 200.0, ("agent_A", 1), ("agent_B", 1))
            .unwrap();
        assert_eq!(id, "agr_5");
        let seen = seen.lock().unwrap();
        let create = json::parse(&seen[0].1).unwrap();
        let parties = create.get("parties").and_then(Json::as_array).unwrap();
        assert_eq!(parties[0].as_str(), Some(at.keptvow_id("agent_A").as_str()));
        assert_eq!(parties[1].as_str(), Some(at.keptvow_id("agent_B").as_str()));
        assert_eq!(seen[0].2, "", "the free tier needs no key");
        for (_, body, _) in seen.iter() {
            assert!(!body.contains("agent_A") && !body.contains("agent_B"), "no real ids: {body}");
        }
        let r1 = json::parse(&seen[1].1).unwrap();
        assert_eq!(r1.get("outcome").and_then(Json::as_f64), Some(1.0));
        assert!(r1.get("evidence").and_then(Json::as_str).unwrap().contains("reported: NO"));
    }

    #[test]
    fn keptvows_hourly_limit_is_retried_not_dropped() {
        let (url, _) = fake_keptvow(vec![("/v1/agreements", 429, "{\"error\":\"free tier limit\"}".into())]);
        let at = Keptvow::new(Some(url), None, None, b"s".to_vec());
        let e = at.record_bet("m", "q", &["Y".into(), "N".into()], 0.0, ("a", 0), ("b", 0)).unwrap_err();
        assert!(e.contains(RETRY_MARKER), "{e}");
    }

    #[test]
    fn checking_a_verdict_only_reads() {
        let (url, seen) = fake_keptvow(vec![
            ("/v1/agreements/agr_3", 200, "{\"settlement\":{\"instruction\":\"wait\"}}".into()),
        ]);
        let at = Keptvow::new(Some(url), None, None, b"s".to_vec());
        assert_eq!(at.verdict("agr_3", 2), Some(Verdict::Wait));
        let seen = seen.lock().unwrap();
        assert_eq!(seen.len(), 1, "Keptvow advances its own clock; no sweep call");
        assert_eq!(seen[0].0, "GET /v1/agreements/agr_3");
    }

    #[test]
    fn verdicts_map_onto_market_outcomes() {
        let (url, _) = fake_keptvow(vec![
            ("/v1/agreements/paid", 200, "{\"settlement\":{\"instruction\":\"pay_out\",\"outcome\":1}}".into()),
            ("/v1/agreements/voidpick", 200, "{\"settlement\":{\"instruction\":\"pay_out\",\"outcome\":2}}".into()),
            ("/v1/agreements/refund", 200, "{\"settlement\":{\"instruction\":\"return_stakes\",\"outcome\":null}}".into()),
            ("/v1/agreements/open", 200, "{\"settlement\":{\"instruction\":\"wait\"}}".into()),
        ]);
        let at = Keptvow::new(Some(url), Some("k".into()), None, b"s".to_vec());
        assert_eq!(at.verdict("paid", 2), Some(Verdict::Outcome(1)));
        assert_eq!(at.verdict("voidpick", 2), Some(Verdict::Void), "the extra outcome means void");
        assert_eq!(at.verdict("refund", 2), Some(Verdict::Void));
        assert_eq!(at.verdict("open", 2), Some(Verdict::Wait));
        assert_eq!(at.verdict("missing", 2), None);
    }

    #[test]
    fn trust_scores_are_read_and_cached() {
        let (url, seen) = fake_keptvow(vec![
            ("/v1/trust/ikenga-", 200, "{\"score\":412,\"trust_level\":\"fair\"}".into()),
        ]);
        let at = Keptvow::new(Some(url), Some("secret_platform_key".into()), None, b"s".to_vec());
        let v = at.trust("good", 1_000).unwrap();
        assert_eq!(v.score, Some(412.0));
        assert_eq!(v.verdict.as_deref(), Some("fair"));
        at.trust("good", 2_000).unwrap();
        assert_eq!(seen.lock().unwrap().len(), 1, "second read came from the cache");
        assert_eq!(seen.lock().unwrap()[0].2, "", "public lookups never carry the platform key");
    }

    #[test]
    fn an_unreachable_keptvow_is_none_not_a_panic() {
        let at = Keptvow::new(Some("http://127.0.0.1:1".into()), Some("k".into()), None, vec![1]);
        assert_eq!(at.trust("a", 0), None);
        assert_eq!(at.verdict("x", 2), None);
        assert!(at.refer("m", "q", &["Y".into(), "N".into()], 0.0, None, "e", "x", None, "r").is_err());
    }
}
