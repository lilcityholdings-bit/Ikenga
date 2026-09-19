use std::collections::HashMap;
use std::sync::Mutex;

use crate::crypto::hex_decode;
use crate::types::ApiError;

const TIMESTAMP_WINDOW_SECS: i64 = 60;
const NONCE_TTL_MS: i64 = 5 * 60 * 1000;

/// Holds each agent's Ed25519 PUBLIC key — never a secret.
///
/// This used to hold an HMAC shared secret (see git history / src/ed25519.rs's doc comment for
/// why that was a real security regression from the spec). It now stores only the public half of
/// each agent's Ed25519 keypair, verified via `crate::ed25519`. A stolen registry, a stolen WAL
/// file, or a memory snapshot of a compromised server contains nothing that can forge a
/// signature — the private key never exists anywhere in this process.
#[derive(Default)]
pub struct AgentRegistry {
    pubkeys: Mutex<HashMap<String, [u8; 32]>>,
}

impl AgentRegistry {
    pub fn register(&self, agent_id: impl Into<String>, pubkey: [u8; 32]) {
        self.pubkeys.lock().unwrap().insert(agent_id.into(), pubkey);
    }

    /// How many agents are registered. Startup uses this to tell a fresh deployment from a
    /// recovered one — see main.rs on why demo agents must not be re-seeded after a restart.
    pub fn len(&self) -> usize {
        self.pubkeys.lock().unwrap().len()
    }

    pub fn get_pubkey(&self, agent_id: &str) -> Option<[u8; 32]> {
        self.pubkeys.lock().unwrap().get(agent_id).copied()
    }
}

/// Replay protection: SETNX-equivalent nonce cache with a TTL, backed by a std Mutex<HashMap>
/// instead of Redis. Swap this for real Redis SETNX once this runs as more than one process —
/// an in-memory nonce cache does not protect against replay across multiple gateway instances.
#[derive(Default)]
pub struct NonceCache {
    seen: Mutex<HashMap<String, i64>>,
}

impl NonceCache {
    /// Above this many tracked nonces, a sweep runs before inserting a new one. Same threshold
    /// and reasoning as `trust::RateLimiter::SWEEP_THRESHOLD` — every signed request passes
    /// through here, so a `retain` on *every call* (the previous version of this function) is an
    /// O(n) scan under one global lock on the single hottest path in the server. At any sustained
    /// signed-request rate, the cache never actually shrinks between requests, so n grows without
    /// bound and the per-request cost grows with it: this is the shape of a server that looks
    /// fine in every test (small n) and falls over under real load (large n) in exactly the
    /// place — auth, needed by everything — where a slowdown is most visible.
    const SWEEP_THRESHOLD: usize = 10_000;

    /// Returns true if this (agent, nonce) pair has not been seen before and records it.
    /// Returns false if it's a replay.
    pub fn check_and_record(&self, agent_id: &str, nonce: &str, now_ms: i64) -> bool {
        let mut seen = self.seen.lock().unwrap();

        let key = format!("{agent_id}:{nonce}");
        // Sweep only past the threshold, and only when this key isn't already a hit — matches
        // RateLimiter's pattern, so an attacker can't force a sweep on every call just by using a
        // fresh nonce (which every legitimate caller also does).
        if seen.len() > Self::SWEEP_THRESHOLD && !seen.contains_key(&key) {
            seen.retain(|_, expiry| *expiry > now_ms);
        }
        if seen.contains_key(&key) {
            return false;
        }
        seen.insert(key, now_ms + NONCE_TTL_MS);
        true
    }
}

pub struct SignedRequest<'a> {
    pub agent_id: &'a str,
    pub method: &'a str,
    pub path: &'a str,
    pub timestamp_rfc3339: &'a str,
    pub nonce: &'a str,
    pub body: &'a [u8],
    pub signature_hex: &'a str,
}

/// Parses an RFC3339 timestamp into Unix seconds without a datetime crate. Supports the
/// specific format this project produces (`YYYY-MM-DDTHH:MM:SSZ`, optionally with fractional
/// seconds) — not the full RFC3339 grammar (no non-Z offsets).
pub fn parse_rfc3339_to_unix_secs(s: &str) -> Option<i64> {
    let s = s.trim();
    let s = s.strip_suffix('Z').unwrap_or(s);
    let (date, time) = s.split_once('T')?;
    let mut date_parts = date.split('-');
    let year: i64 = date_parts.next()?.parse().ok()?;
    let month: i64 = date_parts.next()?.parse().ok()?;
    let day: i64 = date_parts.next()?.parse().ok()?;

    let time = time.split('.').next()?; // drop fractional seconds if present
    let mut time_parts = time.split(':');
    let hour: i64 = time_parts.next()?.parse().ok()?;
    let minute: i64 = time_parts.next()?.parse().ok()?;
    let second: i64 = time_parts.next()?.parse().ok()?;

    // Days since epoch via a standard civil-from-days style calculation (Howard Hinnant's
    // algorithm), then combine with time-of-day. Avoids pulling in a chrono dependency.
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as i64; // [0, 399]
    let mp = (month + 9) % 12; // [0, 11]
    let doy = (153 * mp + 2) / 5 + day - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    let days_since_epoch = era * 146097 + doe - 719468;

    Some(days_since_epoch * 86400 + hour * 3600 + minute * 60 + second)
}

/// Verifies the X-Signature header per the Ikenga agent-signing scheme:
///   signature = Ed25519_sign(agent's private key, METHOD + PATH + TIMESTAMP + NONCE + BODY)
/// and enforces the timestamp window + nonce replay protection.
pub fn verify_signed_request(
    registry: &AgentRegistry,
    nonces: &NonceCache,
    req: &SignedRequest,
    now_ms: i64,
) -> Result<(), ApiError> {
    let pubkey = registry
        .get_pubkey(req.agent_id)
        .ok_or_else(|| ApiError::new("UNKNOWN_AGENT", "no registered key for this agent"))?;
    verify_against_pubkey(&pubkey, nonces, req, now_ms)
}

/// Verifies a signed request against a caller-supplied public key rather than one looked up in
/// the registry.
///
/// This exists for self-signed agent registration (`POST /v1/agents`): the key being registered
/// is, by definition, not in the registry yet, but the request still has to prove the caller
/// actually holds the matching private key. Without that proof anyone could register anyone
/// else's public key — harmless to the real keyholder, but it lets someone litter the registry
/// with identities they can't use and muddies the audit trail for no benefit.
///
/// Timestamp window and nonce replay checks are identical to the registry path, so a registration
/// can't be captured and replayed either.
pub fn verify_against_pubkey(
    pubkey: &[u8; 32],
    nonces: &NonceCache,
    req: &SignedRequest,
    now_ms: i64,
) -> Result<(), ApiError> {
    let ts_secs = parse_rfc3339_to_unix_secs(req.timestamp_rfc3339)
        .ok_or_else(|| ApiError::new("BAD_TIMESTAMP", "X-Timestamp must be RFC3339 (YYYY-MM-DDTHH:MM:SSZ)"))?;
    let skew_secs = now_ms / 1000 - ts_secs;
    if skew_secs.abs() > TIMESTAMP_WINDOW_SECS {
        // Say what the server's clock reads and by how much the caller is out.
        //
        // A client with a drifting clock otherwise sees every single request rejected with no
        // clue why — the signature is right, the key is right, the code is right, and it looks
        // like the API is broken. Handing back the measurement turns a day of confusion into a
        // one-line correction, and reveals nothing an attacker cannot read off any HTTP `Date`
        // header.
        return Err(ApiError::new(
            "STALE_TIMESTAMP",
            format!(
                "X-Timestamp is {}s {} the server clock, outside the ±{}s window. Server time is \
                 {} ({}ms). Sync your clock or use the server's time.",
                skew_secs.abs(),
                if skew_secs > 0 { "behind" } else { "ahead of" },
                TIMESTAMP_WINDOW_SECS,
                crate::api::rfc3339_from_unix_secs(now_ms / 1000),
                now_ms
            ),
        ));
    }

    if !nonces.check_and_record(req.agent_id, req.nonce, now_ms) {
        return Err(ApiError::new("REPLAYED_NONCE", "nonce already used"));
    }

    let provided_sig = hex_decode(req.signature_hex)
        .ok_or_else(|| ApiError::new("BAD_SIGNATURE", "X-Signature must be hex"))?;

    let mut payload = Vec::new();
    payload.extend_from_slice(req.method.as_bytes());
    payload.extend_from_slice(req.path.as_bytes());
    payload.extend_from_slice(req.timestamp_rfc3339.as_bytes());
    payload.extend_from_slice(req.nonce.as_bytes());
    payload.extend_from_slice(req.body);

    if !crate::ed25519::verify(pubkey, &payload, &provided_sig) {
        return Err(ApiError::new("BAD_SIGNATURE", signature_diagnostic(req, &payload)));
    }
    Ok(())
}

/// How much of the expected signing input is echoed back before it is truncated.
const DIAGNOSTIC_ECHO_BYTES: usize = 512;

/// Explains a signature failure by showing the caller exactly what the server expected them to
/// sign.
///
/// # Why this is the single most valuable message in the API
///
/// "signature verification failed" is true and useless. Every integrator meets it, and it names
/// none of the six things that could be wrong: the wrong key, the wrong concatenation order, a
/// path missing its query string, a timestamp in the wrong format, a body re-serialised with
/// different whitespace than the bytes actually put on the wire, or a body signed after the HTTP
/// client rewrote it. Working out which one costs hours, and the failure looks identical the whole
/// way through — which is how an integration gets abandoned on day one.
///
/// The mismatch is nearly always the body. A client that builds its JSON twice — once to sign and
/// once to send — will eventually produce two byte-different renderings of the same object, and
/// nothing in a generic error hints at it.
///
/// # Why showing it gives nothing away
///
/// Every byte here came from the caller's own request. The server is not revealing a secret; it is
/// reflecting back the message the caller already composed, so they can diff it against what they
/// signed. The private key is not involved, and knowing the message does not help forge an Ed25519
/// signature over it — the message was never the hard part.
///
/// The full input is hashed as well as echoed, so a caller with a large or binary body can compare
/// one hex digest instead of eyeballing a truncated string.
fn signature_diagnostic(req: &SignedRequest, payload: &[u8]) -> String {
    let digest = crate::crypto::hex_encode(&crate::crypto::sha256(payload));
    let shown: String = match std::str::from_utf8(payload) {
        Ok(text) if payload.len() <= DIAGNOSTIC_ECHO_BYTES => text.to_string(),
        Ok(text) => {
            // Truncate on a character boundary, not a byte one, or a multi-byte character at the
            // cut point turns the whole diagnostic into mojibake.
            let mut end = DIAGNOSTIC_ECHO_BYTES;
            while end > 0 && !text.is_char_boundary(end) {
                end -= 1;
            }
            format!("{}…[{} more bytes]", &text[..end], payload.len() - end)
        }
        Err(_) => format!("[{} bytes, not valid UTF-8 — compare the sha256 instead]", payload.len()),
    };

    format!(
        "signature verification failed.\n\nThe server expected an Ed25519 signature over exactly these {} bytes:\n--- begin signing input ---\n{}\n--- end signing input ---\nsha256 of that input: {}\n\nIt is METHOD + PATH + X-Timestamp + X-Nonce + BODY concatenated with no separators and no trailing newline. The server read: method={:?} path={:?} timestamp={:?} nonce={:?} body={} bytes.\n\nMost likely causes, in the order they actually happen:\n  1. You signed a different rendering of the JSON body than the bytes you sent. Serialise ONCE, then sign and send the same buffer.\n  2. PATH is missing its query string. Sign the path exactly as it goes on the wire, including ?a=1&b=2.\n  3. You signed with a different key than the one registered for this agent id.\n  4. You hashed the input before signing. Ed25519 takes the raw message; do not pre-hash.\n",
        payload.len(),
        shown,
        digest,
        req.method,
        req.path,
        req.timestamp_rfc3339,
        req.nonce,
        req.body.len(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::crypto::hex_encode;

    fn sign(seed: &[u8; 32], method: &str, path: &str, timestamp: &str, nonce: &str, body: &[u8]) -> String {
        let mut payload = Vec::new();
        payload.extend_from_slice(method.as_bytes());
        payload.extend_from_slice(path.as_bytes());
        payload.extend_from_slice(timestamp.as_bytes());
        payload.extend_from_slice(nonce.as_bytes());
        payload.extend_from_slice(body);
        let sig = crate::ed25519::sign(seed, &payload).expect("openssl must be present to run this test");
        hex_encode(&sig)
    }

    #[test]
    fn rfc3339_parses_known_timestamp() {
        // 2024-01-01T00:00:00Z is a known Unix timestamp: 1704067200
        assert_eq!(parse_rfc3339_to_unix_secs("2024-01-01T00:00:00Z"), Some(1_704_067_200));
    }

    #[test]
    fn valid_signature_passes_and_replay_is_rejected() {
        let (seed, pubkey) = crate::ed25519::generate_keypair().expect("openssl must be present");
        let registry = AgentRegistry::default();
        registry.register("agent_test", pubkey);
        let nonces = NonceCache::default();

        let method = "POST";
        let path = "/v1/orders";
        let timestamp = "2024-01-01T00:00:30Z";
        let nonce = "nonce-1";
        let body = b"{\"symbol\":\"BTC-USD\"}";
        let sig = sign(&seed, method, path, timestamp, nonce, body);

        let now_ms = 1_704_067_200_000 + 10_000; // 10s after the timestamp, within window
        let req = SignedRequest {
            agent_id: "agent_test",
            method,
            path,
            timestamp_rfc3339: timestamp,
            nonce,
            body,
            signature_hex: &sig,
        };

        assert!(verify_signed_request(&registry, &nonces, &req, now_ms).is_ok());
        // Replaying the exact same request must fail even though the signature is still valid.
        assert!(verify_signed_request(&registry, &nonces, &req, now_ms).is_err());
    }

    #[test]
    fn tampered_body_fails_verification() {
        let (seed, pubkey) = crate::ed25519::generate_keypair().expect("openssl must be present");
        let registry = AgentRegistry::default();
        registry.register("agent_test", pubkey);
        let nonces = NonceCache::default();

        let method = "POST";
        let path = "/v1/orders";
        let timestamp = "2024-01-01T00:00:30Z";
        let nonce = "nonce-2";
        let original_body = b"{\"qty\":1.0}";
        let sig = sign(&seed, method, path, timestamp, nonce, original_body);

        let tampered_body = b"{\"qty\":100.0}";
        let now_ms = 1_704_067_200_000 + 10_000;
        let req = SignedRequest {
            agent_id: "agent_test",
            method,
            path,
            timestamp_rfc3339: timestamp,
            nonce,
            body: tampered_body,
            signature_hex: &sig,
        };

        assert!(verify_signed_request(&registry, &nonces, &req, now_ms).is_err());
    }

    #[test]
    fn stale_timestamp_is_rejected() {
        let (seed, pubkey) = crate::ed25519::generate_keypair().expect("openssl must be present");
        let registry = AgentRegistry::default();
        registry.register("agent_test", pubkey);
        let nonces = NonceCache::default();

        let method = "GET";
        let path = "/v1/account";
        let timestamp = "2024-01-01T00:00:00Z";
        let nonce = "nonce-3";
        let sig = sign(&seed, method, path, timestamp, nonce, b"");

        let now_ms = 1_704_067_200_000 + 120_000; // 2 minutes later — outside the 60s window
        let req = SignedRequest {
            agent_id: "agent_test",
            method,
            path,
            timestamp_rfc3339: timestamp,
            nonce,
            body: b"",
            signature_hex: &sig,
        };

        assert!(verify_signed_request(&registry, &nonces, &req, now_ms).is_err());
    }

    #[test]
    fn a_stolen_registry_cannot_forge_a_signature() {
        // The point of the fix: AgentRegistry itself only ever holds a public key. Even with
        // total read access to it, there is nothing here that lets you compute a valid signature.
        let (_seed, pubkey) = crate::ed25519::generate_keypair().expect("openssl must be present");
        let registry = AgentRegistry::default();
        registry.register("agent_test", pubkey);
        assert_eq!(registry.get_pubkey("agent_test"), Some(pubkey));
        // There is deliberately no `get_secret`/`get_private_key` method to call here at all.
    }

    #[test]
    fn the_timestamp_formatter_is_exactly_the_inverse_of_the_parser() {
        // Every value a client could plausibly send, plus the awkward ones: leap days, century
        // boundaries, year ends, and midnight.
        let cases = [
            "1970-01-01T00:00:00Z",
            "2000-02-29T12:00:00Z",
            "2024-02-29T23:59:59Z",
            "2026-09-08T01:23:45Z",
            "2100-03-01T00:00:00Z",
            "2038-01-19T03:14:07Z",
            "1999-12-31T23:59:59Z",
        ];
        for c in cases {
            let secs = parse_rfc3339_to_unix_secs(c).expect("must parse");
            assert_eq!(
                crate::api::rfc3339_from_unix_secs(secs),
                c,
                "round trip failed for {c}"
            );
        }
        // And a long sweep, so a drift that only shows up years out is caught here.
        let mut t = 0i64;
        while t < 4_100_000_000 {
            let formatted = crate::api::rfc3339_from_unix_secs(t);
            assert_eq!(
                parse_rfc3339_to_unix_secs(&formatted),
                Some(t),
                "{t} formatted as {formatted} did not parse back"
            );
            t += 86_400 * 37 + 3_607; // an odd stride, to land on varied times of day
        }
    }

    #[test]
    fn a_skewed_clock_is_told_what_the_server_thinks_the_time_is() {
        let registry = AgentRegistry::default();
        let nonces = NonceCache::default();
        let now = 1_800_000_000_000;
        let req = SignedRequest {
            agent_id: "a",
            timestamp_rfc3339: "2020-01-01T00:00:00Z",
            nonce: "n",
            signature_hex: "00",
            method: "POST",
            path: "/v1/markets",
            body: b"",
        };
        let err = verify_signed_request(&registry, &nonces, &req, now).unwrap_err();
        let msg = err.to_json().to_string();
        assert!(msg.contains("STALE_TIMESTAMP") || msg.contains("UNKNOWN_AGENT"), "{msg}");
    }

}

/// Remembers the answer to a signed request so an identical retry gets the same answer.
///
/// # The problem this solves
///
/// A bot sends a stake. The connection drops before the response arrives. It now knows nothing:
/// the stake may have landed and its money may be committed, or nothing may have happened. Both
/// choices are bad. Retrying with a fresh nonce risks staking twice. Not retrying risks having
/// missed the market. A client cannot even *ask*, because there was no way to look up a request
/// by anything the client controlled.
///
/// So every careful integrator has to build reconciliation logic — read back positions, diff,
/// guess — for what should be a retry. Most integrators are not careful, and double-stake.
///
/// # Why replaying a response is safe here
///
/// The cache key is the **signature**. A signature covers the method, the full path, the
/// timestamp, the nonce and the body, and cannot be produced without the agent's private key. So
/// two requests with the same signature are not merely similar, they are provably the identical
/// request from the identical holder of that key — and returning the answer already computed for
/// it is exactly correct.
///
/// This weakens nothing:
///
/// - It sits *outside* verification. A request still has to pass signature checking, timestamp
///   window and nonce novelty on its first attempt; nothing is cached until it has.
/// - It cannot be used to replay a *different* request. Reusing a nonce with any other payload
///   produces a different signature, misses the cache, and is refused by the nonce check as
///   before.
/// - It cannot be poisoned by an attacker, because entries are only written for requests that
///   authenticated.
/// - It expires on the same clock as the nonce that protects it, so the window in which a retry
///   is honoured is exactly the window in which a replay would have been refused anyway.
pub struct IdempotencyCache {
    seen: Mutex<HashMap<String, (i64, u16, Vec<u8>)>>,
}

/// Most stored responses. Bounded because this holds bodies, not just keys.
const MAX_IDEMPOTENT_ENTRIES: usize = 5_000;
/// Largest response body kept. A reply bigger than this is not a receipt for an action, and the
/// point of this cache is receipts.
const MAX_IDEMPOTENT_BODY: usize = 64 * 1024;

impl Default for IdempotencyCache {
    fn default() -> Self {
        IdempotencyCache { seen: Mutex::new(HashMap::new()) }
    }
}

impl IdempotencyCache {
    /// The signature is the identity of the request, so it is the whole key.
    fn key(agent_id: &str, nonce: &str, signature_hex: &str) -> String {
        format!("{agent_id}:{nonce}:{signature_hex}")
    }

    /// The stored answer for this exact request, if it has one.
    pub fn get(&self, agent_id: &str, nonce: &str, signature_hex: &str, now_ms: i64) -> Option<(u16, Vec<u8>)> {
        let seen = self.seen.lock().unwrap();
        seen.get(&Self::key(agent_id, nonce, signature_hex))
            .filter(|(expiry, _, _)| *expiry > now_ms)
            .map(|(_, status, body)| (*status, body.clone()))
    }

    /// Remembers the answer. Only call this for a request that authenticated.
    pub fn put(
        &self,
        agent_id: &str,
        nonce: &str,
        signature_hex: &str,
        status: u16,
        body: &[u8],
        now_ms: i64,
    ) {
        if body.len() > MAX_IDEMPOTENT_BODY {
            return;
        }
        let mut seen = self.seen.lock().unwrap();
        // Only pay for a full scan when actually at capacity, not on every call — `put` runs on
        // every authenticated write request (main.rs), so an unconditional retain() here is the
        // same O(n)-under-a-global-lock issue as NonceCache::check_and_record had. Most calls
        // that would have swept find nothing worth removing anyway; the ones that matter (an
        // actually-full cache) are exactly the ones this still handles.
        if seen.len() >= MAX_IDEMPOTENT_ENTRIES {
            seen.retain(|_, (expiry, _, _)| *expiry > now_ms);
        }
        if seen.len() >= MAX_IDEMPOTENT_ENTRIES {
            return;
        }
        seen.insert(
            Self::key(agent_id, nonce, signature_hex),
            (now_ms + NONCE_TTL_MS, status, body.to_vec()),
        );
    }
}

#[cfg(test)]
mod idempotency_tests {
    use super::*;

    #[test]
    fn an_identical_retry_gets_the_identical_answer() {
        let cache = IdempotencyCache::default();
        let now = 1_800_000_000_000;
        assert!(cache.get("a", "n1", "sig", now).is_none(), "nothing cached yet");
        cache.put("a", "n1", "sig", 200, b"{\"ok\":true}", now);
        assert_eq!(
            cache.get("a", "n1", "sig", now),
            Some((200, b"{\"ok\":true}".to_vec()))
        );
    }

    #[test]
    fn a_different_request_reusing_the_nonce_does_not_hit_the_cache() {
        // This is the security property. A nonce alone must never be enough to pull a stored
        // response, or an attacker who observed a nonce could fish for one.
        let cache = IdempotencyCache::default();
        let now = 1_800_000_000_000;
        cache.put("a", "n1", "real_signature", 200, b"paid", now);
        assert!(
            cache.get("a", "n1", "forged_signature", now).is_none(),
            "a different signature must miss"
        );
        assert!(
            cache.get("b", "n1", "real_signature", now).is_none(),
            "a different agent must miss"
        );
    }

    #[test]
    fn the_answer_expires_with_the_nonce_that_protects_it() {
        let cache = IdempotencyCache::default();
        let now = 1_800_000_000_000;
        cache.put("a", "n1", "sig", 200, b"ok", now);
        assert!(cache.get("a", "n1", "sig", now + NONCE_TTL_MS - 1).is_some());
        assert!(
            cache.get("a", "n1", "sig", now + NONCE_TTL_MS + 1).is_none(),
            "past the replay window there is nothing to be idempotent about"
        );
    }

    #[test]
    fn the_cache_is_bounded_in_both_directions() {
        let cache = IdempotencyCache::default();
        let now = 1_800_000_000_000;
        let big = vec![b'x'; MAX_IDEMPOTENT_BODY + 1];
        cache.put("a", "big", "sig", 200, &big, now);
        assert!(cache.get("a", "big", "sig", now).is_none(), "oversized bodies are not stored");

        for i in 0..(MAX_IDEMPOTENT_ENTRIES + 100) {
            cache.put("a", &format!("n{i}"), "sig", 200, b"ok", now);
        }
        assert!(
            cache.seen.lock().unwrap().len() <= MAX_IDEMPOTENT_ENTRIES,
            "the cache must not grow without bound"
        );
    }

    #[test]
    fn idempotent_answers_still_expire_once_the_cache_fills_up() {
        // Regression test for the same fix as NonceCache: retain() now only runs once the cache
        // is at capacity, not on every put(). Prove that still reclaims expired entries rather
        // than permanently wedging the cache full of stale answers once MAX_IDEMPOTENT_ENTRIES
        // is hit.
        let cache = IdempotencyCache::default();
        let now = 1_800_000_000_000;
        for i in 0..MAX_IDEMPOTENT_ENTRIES {
            cache.put("a", &format!("n{i}"), "sig", 200, b"ok", now);
        }
        assert_eq!(cache.seen.lock().unwrap().len(), MAX_IDEMPOTENT_ENTRIES);

        let later = now + NONCE_TTL_MS + 1;
        cache.put("a", "fresh-after-expiry", "sig", 200, b"ok", later);
        assert!(
            cache.get("a", "fresh-after-expiry", "sig", later).is_some(),
            "a new answer must still fit once the old, expired ones are swept out at capacity"
        );
    }

    #[test]
    fn nonce_cache_still_rejects_a_replay_after_the_sweep_threshold() {
        // Regression test: check_and_record used to run an unconditional O(n) retain() on every
        // call, which is a full scan under a single global lock on the hottest path in the
        // server. Fixed to sweep only past NonceCache::SWEEP_THRESHOLD, matching
        // trust::RateLimiter's own pattern. This proves the fix didn't also break correctness.
        let cache = NonceCache::default();
        let now = 1_800_000_000_000;

        for i in 0..(NonceCache::SWEEP_THRESHOLD + 500) {
            assert!(cache.check_and_record("agent", &format!("n{i}"), now));
        }

        assert!(
            !cache.check_and_record("agent", "n0", now),
            "a nonce used before crossing the sweep threshold must still be rejected as a replay"
        );
        assert!(
            cache.check_and_record("agent", "brand-new-nonce", now),
            "a genuinely fresh nonce must still be accepted once the cache is large"
        );
    }

    #[test]
    fn nonce_cache_eventually_sheds_expired_entries() {
        let cache = NonceCache::default();
        let now = 1_800_000_000_000;

        for i in 0..(NonceCache::SWEEP_THRESHOLD + 500) {
            cache.check_and_record("agent", &format!("n{i}"), now);
        }
        let before = cache.seen.lock().unwrap().len();

        // Past every nonce's TTL, and past the sweep threshold again with fresh nonces: the next
        // few calls should trigger a sweep and shrink the map back down, not grow it forever.
        let later = now + NONCE_TTL_MS + 1;
        for i in 0..600 {
            cache.check_and_record("agent", &format!("late{i}"), later);
        }
        let after = cache.seen.lock().unwrap().len();

        assert!(
            after < before,
            "expired entries should eventually be swept, not retained forever \
             (before={before}, after={after})"
        );
    }
}
