//! Anonymity layer: what one market participant can and cannot learn about another.
//!
//! # Threat model — read this before changing anything in here
//!
//! **What this protects against:** one trader (human or agent) profiling another. Before this
//! module existed, every fill response named the counterparty's permanent `agent_id`. A single
//! trade told you exactly who you traded with, and every later trade with them was linkable to
//! that same identity — enough to reverse-engineer another agent's strategy, typical size, and
//! daily schedule. Order and trade IDs were also built from a wall-clock nanosecond timestamp
//! plus a global monotonic counter, so any two IDs revealed exactly how many orders the whole
//! platform had processed in between. That's a free volume oracle for a competitor.
//!
//! **What this does NOT protect against, and cannot:**
//!
//! - *The operator.* The server sees every order, balance, and identity in plaintext. That is
//!   inherent to a central matching engine that also keeps the balance ledger. Operator-blind
//!   trading requires on-chain settlement or zero-knowledge proofs — a different architecture,
//!   not a module you can bolt on. Do not describe this build as "the operator can't see you."
//! - *Network-level identification.* The server sees the connecting IP on every request. Fixing
//!   that is a deployment concern (onion service, proxy layer), not application code.
//! - *Timing and size correlation.* Someone watching the public tick feed while timing their own
//!   orders against it can still infer a lot. Hidden-size (iceberg) orders would narrow that gap
//!   and are not implemented.
//!
//! # Why not full anonymity
//!
//! The trust system, the rate limiter, and any future abuse detection all need to know who is
//! acting. An unaccountable market is not a safe one — it's one where wash trading and sybil
//! attacks are free. So the design here is deliberately *not* "nobody knows anything":
//!
//!   unlinkable in public → resolvable internally → every resolution permanently logged
//!
//! Identity still exists server-side; what changes is that it never leaves the server, and when
//! an operator does look behind an alias, that lookup becomes its own permanent record (see
//! `DisclosureLog`). Accountability for the accountability.

use std::collections::HashMap;
use std::sync::Mutex;

/// Generates an unpredictable public identifier with the given prefix.
///
/// Every ID an outside party can see goes through here. The point is that IDs carry *no*
/// information: not when they were created, not how many came before them, not who made them.
/// The previous scheme (`{nanos:x}{counter:x}`) leaked all three.
pub fn random_id(prefix: &str) -> String {
    format!("{prefix}{}", crate::crypto::hex_encode(&crate::crypto::random_bytes_pooled(16)))
}

/// Maps single-use public aliases back to the agent behind them.
///
/// A fresh alias is issued **per trade**, not per agent. Two fills against the same maker get
/// two unrelated aliases, so a counterparty can't be tracked across trades. The tradeoff is
/// real and deliberate: a trader also loses the ability to notice "I keep getting filled by the
/// same desk," which is information they might legitimately want for their own risk management.
/// Unlinkability wins here because the alternative leaks by default and can't be opted out of.
#[derive(Default)]
pub struct AliasRegistry {
    forward: Mutex<HashMap<String, String>>,
}

impl AliasRegistry {
    /// Issues a fresh, unlinkable alias for `agent_id`. Never returns the same alias twice.
    pub fn issue(&self, agent_id: &str) -> String {
        let alias = random_id("anon_");
        self.forward.lock().unwrap().insert(alias.clone(), agent_id.to_string());
        alias
    }

    /// Internal-only lookup. Callers must be owner-authenticated AND must record the lookup in
    /// the `DisclosureLog` — `api.rs::resolve_alias` is the only intended caller.
    pub fn resolve(&self, alias: &str) -> Option<String> {
        self.forward.lock().unwrap().get(alias).cloned()
    }

    pub fn issued_count(&self) -> usize {
        self.forward.lock().unwrap().len()
    }
}

#[derive(Debug, Clone)]
pub struct Disclosure {
    pub alias: String,
    pub agent_id: String,
    pub reason: String,
    pub at_ms: i64,
}

impl Disclosure {
    pub fn to_json(&self) -> crate::json::Json {
        crate::json::Json::obj(vec![
            ("alias", crate::json::Json::str(self.alias.clone())),
            ("agent_id", crate::json::Json::str(self.agent_id.clone())),
            ("reason", crate::json::Json::str(self.reason.clone())),
            ("at_ms", crate::json::Json::num(self.at_ms as f64)),
        ])
    }
}

/// Append-only record of every time an operator de-anonymized someone.
///
/// This exists so that "the operator can look" is not the same as "the operator can look without
/// anyone ever knowing." Every resolution requires a stated reason and lands here permanently.
///
/// GAP: append-only *by convention* only — it's a `Vec` in memory, so it dies with the process
/// and an operator with code access could edit it. A real version needs the same durable store
/// as everything else in ROADMAP phase 1, ideally write-once (append-only table with no UPDATE
/// or DELETE grant) and hash-chained so tampering is detectable rather than merely discouraged.
#[derive(Default)]
pub struct DisclosureLog {
    entries: Mutex<Vec<Disclosure>>,
}

impl DisclosureLog {
    pub fn record(&self, alias: &str, agent_id: &str, reason: &str, at_ms: i64) {
        self.entries.lock().unwrap().push(Disclosure {
            alias: alias.to_string(),
            agent_id: agent_id.to_string(),
            reason: reason.to_string(),
            at_ms,
        });
    }

    pub fn snapshot(&self) -> Vec<Disclosure> {
        self.entries.lock().unwrap().clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_are_unpredictable_and_carry_no_counter() {
        let a = random_id("o_");
        let b = random_id("o_");
        assert_ne!(a, b);
        assert!(a.starts_with("o_") && b.starts_with("o_"));
        // 16 random bytes -> 32 hex chars, plus the prefix.
        assert_eq!(a.len(), 2 + 32);
        // The old scheme embedded a monotonic counter, so consecutive IDs sorted in creation
        // order. These must not: sort order should be meaningless.
        let ids: Vec<String> = (0..64).map(|_| random_id("o_")).collect();
        let mut sorted = ids.clone();
        sorted.sort();
        assert_ne!(ids, sorted, "IDs still encode creation order — that leaks volume/timing");
    }

    #[test]
    fn each_trade_gets_an_unlinkable_alias_for_the_same_agent() {
        let reg = AliasRegistry::default();
        let first = reg.issue("agent_A");
        let second = reg.issue("agent_A");
        assert_ne!(first, second, "same agent got a reusable alias — that's linkable across trades");
        assert_eq!(reg.resolve(&first).as_deref(), Some("agent_A"));
        assert_eq!(reg.resolve(&second).as_deref(), Some("agent_A"));
    }

    #[test]
    fn unknown_alias_resolves_to_nothing() {
        let reg = AliasRegistry::default();
        reg.issue("agent_A");
        assert_eq!(reg.resolve("anon_deadbeef"), None);
    }

    #[test]
    fn disclosures_are_recorded_with_a_reason() {
        let log = DisclosureLog::default();
        log.record("anon_1", "agent_A", "sanctions screening", 1000);
        log.record("anon_2", "agent_B", "wash trade investigation", 2000);
        let snap = log.snapshot();
        assert_eq!(snap.len(), 2);
        assert_eq!(snap[0].agent_id, "agent_A");
        assert_eq!(snap[0].reason, "sanctions screening");
        assert_eq!(snap[1].at_ms, 2000);
    }
}
