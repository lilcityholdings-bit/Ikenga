//! Agenttrust: the outside referee for disputed markets, and the public trust score shown next to
//! every agent.
//!
//! Agenttrust (https://github.com/lilcityholdings-bit/Agenttrust) settles disagreements between
//! two bots — by a named arbiter or a randomly drawn jury — and keeps a public 0–1000 trust score
//! for every bot built only from how those settlements went. Ikenga uses it for two things:
//!
//! 1. **Dispute resolution.** When someone with a stake disputes a proposed outcome, the market
//!    freezes (that part is unchanged — see `state::dispute_outcome`). This module then opens an
//!    Agenttrust agreement between the resolver that proposed the outcome and the agent that
//!    disputed it, each side reports what it says happened, and Agenttrust decides. Its verdict
//!    is applied here and paid out; "couldn't decide" voids the market and refunds everyone.
//!    The operator can still step in and re-propose at any time, and the 24-hour review backstop
//!    still voids anything nobody decided — Agenttrust being unreachable never traps anyone's
//!    points.
//! 2. **Trust scores.** `GET /v1/agents/{id}/agenttrust` returns the agent's Agenttrust score and
//!    verdict, cached for a few minutes, so the dashboard and other agents can see it.
//!
//! Everything sent to Agenttrust is denominated in free play points (`asset: "IKENGA_POINTS"`).
//! No real money is held or moved by either service.
//!
//! Ikenga agents appear in Agenttrust as `ikenga-<agent id>`, so they can never collide with a
//! bot registered there directly. Agenttrust identifies each bot by a claimed secret; Ikenga
//! derives one per agent from `AGENTTRUST_SECRET` (or the owner key) with HMAC, so nothing
//! extra is stored and the same agent always gets the same secret.
//!
//! Like `liquidity.rs`, HTTP goes through the system `curl` — this build has no crates.io access.
//! Requests are fed to curl on stdin as a config file, so API keys and per-agent secrets never
//! appear on a command line where `ps` could show them.

use std::collections::HashMap;
use std::io::Write;
use std::process::{Command, Stdio};
use std::sync::Mutex;

use crate::json::{self, Json};

pub const DEFAULT_URL: &str = "https://agenttrust-production-381e.up.railway.app";
/// The Agenttrust identity of whoever proposed the outcome under dispute.
pub const RESOLVER_ID: &str = "ikenga-resolver";
/// How long a looked-up trust score is reused. Public lookups are rate-limited per IP on
/// Agenttrust's side, and a score only moves when a settlement happens.
const TRUST_CACHE_MS: i64 = 5 * 60 * 1000;
/// Minimum gap between two checks of the same pending verdict.
pub const POLL_EVERY_MS: i64 = 30 * 1000;

/// An open referral: the market is frozen and waiting on this Agenttrust agreement.
#[derive(Debug, Clone, PartialEq)]
pub struct Referral {
    pub agreement_id: String,
    pub last_polled_ms: i64,
}

/// What Agenttrust decided about a referred dispute.
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

pub struct AgentTrust {
    /// `None` when switched off with `AGENTTRUST_URL=off`.
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
}

/// The id an Ikenga agent has on Agenttrust.
pub fn agenttrust_id(agent_id: &str) -> String {
    let clean: String = agent_id
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
        .take(64)
        .collect();
    format!("ikenga-{clean}")
}

impl AgentTrust {
    pub fn from_env(owner_key: &[u8]) -> Self {
        let raw = std::env::var("AGENTTRUST_URL").unwrap_or_else(|_| DEFAULT_URL.to_string());
        let base_url = match raw.trim() {
            "" | "off" | "0" => None,
            u => Some(u.trim_end_matches('/').to_string()),
        };
        let nonempty = |k: &str| std::env::var(k).ok().filter(|v| !v.trim().is_empty());
        let secret_key = nonempty("AGENTTRUST_SECRET")
            .map(|s| s.into_bytes())
            .unwrap_or_else(|| owner_key.to_vec());
        Self::new(base_url, nonempty("AGENTTRUST_API_KEY"), nonempty("AGENTTRUST_ARBITER"), secret_key)
    }

    pub fn new(
        base_url: Option<String>,
        api_key: Option<String>,
        arbiter: Option<String>,
        secret_key: Vec<u8>,
    ) -> Self {
        AgentTrust {
            base_url,
            api_key,
            arbiter,
            secret_key,
            trust_cache: Mutex::new(HashMap::new()),
            referrals: Mutex::new(HashMap::new()),
            pending: Mutex::new(HashMap::new()),
        }
    }

    /// Whether disputes can be sent to Agenttrust. Opening an agreement needs a platform API key
    /// on the live service; without one, disputes stay with the operator as before.
    pub fn resolves_disputes(&self) -> bool {
        self.base_url.is_some() && self.api_key.is_some()
    }

    pub fn profile_url(&self, agent_id: &str) -> Option<String> {
        self.base_url.as_ref().map(|b| format!("{b}/trust/{}", agenttrust_id(agent_id)))
    }

    fn secret_for(&self, at_id: &str) -> String {
        crate::crypto::hex_encode(&crate::crypto::hmac_sha256(
            &self.secret_key,
            format!("agenttrust-secret:{at_id}").as_bytes(),
        ))
    }

    /// One HTTP call. Returns (status, parsed body).
    fn call(&self, method: &str, path: &str, body: Option<&Json>) -> Option<(u16, Json)> {
        let base = self.base_url.as_ref()?;
        let q = |s: &str| s.replace('\\', "\\\\").replace('"', "\\\"");
        let mut cfg = String::new();
        cfg.push_str(&format!("url = \"{}\"\n", q(&format!("{base}{path}"))));
        cfg.push_str(&format!("request = \"{method}\"\n"));
        cfg.push_str("silent\nshow-error\nmax-time = 10\n");
        cfg.push_str("header = \"Accept: application/json\"\n");
        if let Some(k) = &self.api_key {
            cfg.push_str(&format!("header = \"Authorization: Bearer {}\"\n", q(k)));
        }
        if let Some(b) = body {
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

    /// The agent's Agenttrust score and verdict. `None` if Agenttrust couldn't be reached.
    pub fn trust(&self, agent_id: &str, now_ms: i64) -> Option<TrustView> {
        let at_id = agenttrust_id(agent_id);
        if let Some((at, v)) = self.trust_cache.lock().unwrap().get(&at_id) {
            if now_ms - at < TRUST_CACHE_MS {
                return v.clone();
            }
        }
        let fetched = self.call("GET", &format!("/v1/trust/{at_id}"), None).and_then(|(code, j)| {
            match code {
                200 => Some(TrustView {
                    score: j.get("score").and_then(Json::as_f64),
                    verdict: j.get("trust_level").and_then(Json::as_str).map(str::to_string),
                }),
                // Never dealt on Agenttrust yet: a real answer, not an outage.
                404 => Some(TrustView { score: None, verdict: Some("unknown".into()) }),
                _ => None,
            }
        });
        // Cache only real answers, so a blip isn't remembered for five minutes.
        if fetched.is_some() {
            self.trust_cache.lock().unwrap().insert(at_id, (now_ms, fetched.clone()));
        }
        fetched
    }

    /// Opens the Agenttrust agreement for a disputed market and files both sides' reports.
    ///
    /// `outcomes` are the market's outcome names; Agenttrust gets one more, "void", for "this
    /// can't be determined". `proposed` is what the resolver said (`None` = void), `claimed` what
    /// the disputer says. Returns the agreement id.
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
        if !self.resolves_disputes() {
            return Err("Agenttrust dispute resolution is not configured".into());
        }
        let void_idx = outcomes.len();
        let mut labels: Vec<String> =
            outcomes.iter().map(|o| o.chars().take(64).collect::<String>()).collect();
        labels.push("void".into());
        let mut lower: Vec<String> = labels.iter().map(|l| l.trim().to_lowercase()).collect();
        lower.sort();
        lower.dedup();
        let outcomes_json = if lower.len() == labels.len() && labels.iter().all(|l| !l.trim().is_empty()) {
            Json::Array(labels.into_iter().map(Json::str).collect())
        } else {
            Json::num((void_idx + 1) as f64)
        };

        let disputer_id = agenttrust_id(disputer);
        let mut fields = vec![
            ("parties", Json::Array(vec![Json::str(RESOLVER_ID), Json::str(disputer_id.clone())])),
            ("outcomes", outcomes_json),
            ("stake", Json::num(stake_points.max(0.0))),
            ("asset", Json::str("IKENGA_POINTS")),
            ("domain", Json::str("wagering")),
            ("secret", Json::str(self.secret_for(RESOLVER_ID))),
        ];
        if let Some(a) = &self.arbiter {
            fields.push(("arbiter", Json::str(a.clone())));
        }
        let (code, created) = self
            .call("POST", "/v1/agreements", Some(&Json::obj(fields)))
            .ok_or("Agenttrust could not be reached")?;
        let agreement_id = created
            .get("agreement_id")
            .and_then(Json::as_str)
            .map(str::to_string)
            .ok_or_else(|| format!("Agenttrust refused the agreement ({code}): {}", created.to_string()))?;

        let report = |who: &str, outcome: usize, evidence: String| {
            self.call(
                "POST",
                &format!("/v1/agreements/{agreement_id}/report"),
                Some(&Json::obj(vec![
                    ("agent_id", Json::str(who)),
                    ("outcome", Json::num(outcome as f64)),
                    ("secret", Json::str(self.secret_for(who))),
                    ("evidence", Json::str(evidence)),
                ])),
            )
        };
        let short = |s: &str| s.chars().take(900).collect::<String>();
        report(
            RESOLVER_ID,
            proposed.unwrap_or(void_idx),
            short(&format!("Ikenga market {market_id}: \"{question}\". Resolver's evidence: {proposal_evidence}")),
        );
        report(
            &disputer_id,
            claimed.unwrap_or(void_idx),
            short(&format!("Disputed by Ikenga agent {disputer}: {reason}")),
        );
        Ok(agreement_id)
    }

    /// Asks Agenttrust whether the referred dispute has been decided.
    pub fn verdict(&self, agreement_id: &str, outcome_count: usize) -> Option<Verdict> {
        // Nudge Agenttrust's clocks (report deadlines, closed juries) forward. Harmless if it
        // has nothing to do.
        let _ = self.call("POST", "/v1/sweep", Some(&Json::obj(vec![])));
        let (code, j) = self.call("GET", &format!("/v1/agreements/{agreement_id}"), None)?;
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
    fn ids_are_namespaced_and_clean() {
        assert_eq!(agenttrust_id("agent_ABC"), "ikenga-agent_ABC");
        assert_eq!(agenttrust_id("a/b?c"), "ikenga-abc");
    }

    #[test]
    fn secrets_are_stable_per_agent_and_distinct() {
        let at = AgentTrust::new(None, None, None, b"k".to_vec());
        assert_eq!(at.secret_for("x"), at.secret_for("x"));
        assert_ne!(at.secret_for("x"), at.secret_for("y"));
        let other = AgentTrust::new(None, None, None, b"k2".to_vec());
        assert_ne!(at.secret_for("x"), other.secret_for("x"));
    }

    #[test]
    fn disputes_need_a_key_and_a_url() {
        assert!(!AgentTrust::new(None, Some("k".into()), None, vec![1]).resolves_disputes());
        assert!(!AgentTrust::new(Some("http://x".into()), None, None, vec![1]).resolves_disputes());
        assert!(AgentTrust::new(Some("http://x".into()), Some("k".into()), None, vec![1]).resolves_disputes());
        let off = AgentTrust::new(None, None, None, vec![1]);
        assert!(off.refer("m", "q", &["YES".into(), "NO".into()], 1.0, Some(0), "e", "a", None, "r").is_err());
    }

    /// A tiny stand-in for Agenttrust on localhost that records every request and answers from
    /// a fixed script, so the real curl path and parsing are exercised end to end.
    fn fake_agenttrust(
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
        let (url, seen) = fake_agenttrust(vec![
            ("/v1/agreements/agr_7/report", 200, "{\"result\":\"waiting\"}".into()),
            ("/v1/agreements", 201, "{\"agreement_id\":\"agr_7\"}".into()),
        ]);
        let at = AgentTrust::new(Some(url), Some("at_live_key".into()), Some("judge".into()), b"s".to_vec());
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
        assert_eq!(parties[1].as_str(), Some("ikenga-agent_B"));
        let outcomes = create.get("outcomes").and_then(Json::as_array).unwrap();
        assert_eq!(outcomes.len(), 3, "the market's outcomes plus void");
        let r1 = json::parse(&seen[1].1).unwrap();
        let r2 = json::parse(&seen[2].1).unwrap();
        assert_eq!(r1.get("agent_id").and_then(Json::as_str), Some(RESOLVER_ID));
        assert_eq!(r1.get("outcome").and_then(Json::as_f64), Some(0.0));
        assert_eq!(r2.get("agent_id").and_then(Json::as_str), Some("ikenga-agent_B"));
        assert_eq!(r2.get("outcome").and_then(Json::as_f64), Some(1.0));
        assert_ne!(r1.get("secret"), r2.get("secret"));
    }

    #[test]
    fn a_disputer_with_no_claim_argues_for_void() {
        let (url, seen) = fake_agenttrust(vec![
            ("/v1/agreements/agr_1/report", 200, "{}".into()),
            ("/v1/agreements", 201, "{\"agreement_id\":\"agr_1\"}".into()),
        ]);
        let at = AgentTrust::new(Some(url), Some("k".into()), None, b"s".to_vec());
        at.refer("m", "q", &["A".into(), "B".into(), "C".into()], 0.0, Some(2), "e", "x", None, "r").unwrap();
        let r2 = json::parse(&seen.lock().unwrap()[2].1).unwrap();
        assert_eq!(r2.get("outcome").and_then(Json::as_f64), Some(3.0), "index 3 is void");
    }

    #[test]
    fn a_refused_agreement_is_an_error_not_a_referral() {
        let (url, _) = fake_agenttrust(vec![("/v1/agreements", 401, "{\"error\":\"bad key\"}".into())]);
        let at = AgentTrust::new(Some(url), Some("k".into()), None, b"s".to_vec());
        let e = at.refer("m", "q", &["Y".into(), "N".into()], 0.0, Some(0), "e", "x", None, "r").unwrap_err();
        assert!(e.contains("401"), "{e}");
    }

    #[test]
    fn verdicts_map_onto_market_outcomes() {
        let (url, _) = fake_agenttrust(vec![
            ("/v1/sweep", 200, "{}".into()),
            ("/v1/agreements/paid", 200, "{\"settlement\":{\"instruction\":\"pay_out\",\"outcome\":1}}".into()),
            ("/v1/agreements/voidpick", 200, "{\"settlement\":{\"instruction\":\"pay_out\",\"outcome\":2}}".into()),
            ("/v1/agreements/refund", 200, "{\"settlement\":{\"instruction\":\"return_stakes\",\"outcome\":null}}".into()),
            ("/v1/agreements/open", 200, "{\"settlement\":{\"instruction\":\"wait\"}}".into()),
        ]);
        let at = AgentTrust::new(Some(url), Some("k".into()), None, b"s".to_vec());
        assert_eq!(at.verdict("paid", 2), Some(Verdict::Outcome(1)));
        assert_eq!(at.verdict("voidpick", 2), Some(Verdict::Void), "the extra outcome means void");
        assert_eq!(at.verdict("refund", 2), Some(Verdict::Void));
        assert_eq!(at.verdict("open", 2), Some(Verdict::Wait));
        assert_eq!(at.verdict("missing", 2), None);
    }

    #[test]
    fn trust_scores_are_read_and_cached() {
        let (url, seen) = fake_agenttrust(vec![
            ("/v1/trust/ikenga-good", 200, "{\"score\":412,\"trust_level\":\"fair\"}".into()),
        ]);
        let at = AgentTrust::new(Some(url), None, None, b"s".to_vec());
        let v = at.trust("good", 1_000).unwrap();
        assert_eq!(v.score, Some(412.0));
        assert_eq!(v.verdict.as_deref(), Some("fair"));
        at.trust("good", 2_000).unwrap();
        assert_eq!(seen.lock().unwrap().len(), 1, "second read came from the cache");
        let unknown = at.trust("nobody", 1_000).unwrap();
        assert_eq!(unknown.verdict.as_deref(), Some("unknown"));
    }

    #[test]
    fn an_unreachable_agenttrust_is_none_not_a_panic() {
        let at = AgentTrust::new(Some("http://127.0.0.1:1".into()), Some("k".into()), None, vec![1]);
        assert_eq!(at.trust("a", 0), None);
        assert_eq!(at.verdict("x", 2), None);
        assert!(at.refer("m", "q", &["Y".into(), "N".into()], 0.0, None, "e", "x", None, "r").is_err());
    }
}
