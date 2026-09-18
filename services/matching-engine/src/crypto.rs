//! Hand-rolled crypto primitives, used only because this build has no network access to pull
//! in a vetted crate (see the note at the top of Cargo.toml).
//!
//! WHAT'S HERE AND WHY:
//! - SHA-256 + HMAC-SHA256: used as the agent request-signing scheme in `auth.rs`, standing in
//!   for the spec's Ed25519. HMAC-SHA256 is symmetric (shared secret per agent) where the spec
//!   calls for asymmetric Ed25519 (agent holds a private key, server only ever sees the public
//!   key). That's a real difference — with HMAC, the server holding the secret is a target
//!   (leak the server, you can forge signatures for every agent it holds a secret for). Swap
//!   this for ed25519-dalek before this touches anything real.
//! - SHA-1 + base64: used only for the WebSocket handshake's Sec-WebSocket-Accept header
//!   (RFC 6455), which is a protocol-compatibility requirement, not a security control. SHA-1
//!   is fine for this specific use even though it's broken for general cryptographic use.
//!
//! Both hash functions are implemented from the standard published algorithms and checked
//! against known test vectors in the unit tests below.

pub fn sha256(input: &[u8]) -> [u8; 32] {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];

    let mut msg = input.to_vec();
    let bit_len = (input.len() as u64) * 8;
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    for chunk in msg.chunks(64) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes(chunk[i * 4..i * 4 + 4].try_into().unwrap());
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }

        let (mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);

        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(maj);

            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }

        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }

    let mut out = [0u8; 32];
    for i in 0..8 {
        out[i * 4..i * 4 + 4].copy_from_slice(&h[i].to_be_bytes());
    }
    out
}

pub fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    const BLOCK_SIZE: usize = 64;
    let mut key_block = [0u8; BLOCK_SIZE];
    if key.len() > BLOCK_SIZE {
        let hashed = sha256(key);
        key_block[..32].copy_from_slice(&hashed);
    } else {
        key_block[..key.len()].copy_from_slice(key);
    }

    let mut ipad = [0x36u8; BLOCK_SIZE];
    let mut opad = [0x5cu8; BLOCK_SIZE];
    for i in 0..BLOCK_SIZE {
        ipad[i] ^= key_block[i];
        opad[i] ^= key_block[i];
    }

    let mut inner = ipad.to_vec();
    inner.extend_from_slice(message);
    let inner_hash = sha256(&inner);

    let mut outer = opad.to_vec();
    outer.extend_from_slice(&inner_hash);
    sha256(&outer)
}

pub fn sha1(input: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];

    let mut msg = input.to_vec();
    let bit_len = (input.len() as u64) * 8;
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    for chunk in msg.chunks(64) {
        let mut w = [0u32; 80];
        for i in 0..16 {
            w[i] = u32::from_be_bytes(chunk[i * 4..i * 4 + 4].try_into().unwrap());
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }

        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);

        for (i, &wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A827999u32),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }

        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }

    let mut out = [0u8; 20];
    for i in 0..5 {
        out[i * 4..i * 4 + 4].copy_from_slice(&h[i].to_be_bytes());
    }
    out
}

const B64_ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn base64_encode(input: &[u8]) -> String {
    let mut out = String::with_capacity((input.len() + 2) / 3 * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0];
        let b1 = *chunk.get(1).unwrap_or(&0);
        let b2 = *chunk.get(2).unwrap_or(&0);

        out.push(B64_ALPHABET[(b0 >> 2) as usize] as char);
        out.push(B64_ALPHABET[(((b0 & 0x03) << 4) | (b1 >> 4)) as usize] as char);
        out.push(if chunk.len() > 1 {
            B64_ALPHABET[(((b1 & 0x0f) << 2) | (b2 >> 6)) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 { B64_ALPHABET[(b2 & 0x3f) as usize] as char } else { '=' });
    }
    out
}

pub fn hex_encode(input: &[u8]) -> String {
    input.iter().map(|b| format!("{:02x}", b)).collect()
}

/// Compares two byte slices in time independent of *where* they first differ (only their
/// lengths can be observed from timing, which is unavoidable and not sensitive here). Used
/// everywhere a secret is compared against attacker-controlled input (HMAC signatures, the
/// owner key) so a naive `==` can't leak the correct value one byte at a time via timing.
pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

pub fn hex_decode(input: &str) -> Option<Vec<u8>> {
    if input.len() % 2 != 0 {
        return None;
    }
    (0..input.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&input[i..i + 2], 16).ok())
        .collect()
}

/// Buffered CSPRNG for the request hot path.
///
/// `secure_random_bytes` opens, reads, and closes `/dev/urandom` on every call — measured at
/// ~2.6us, which is more than the order book spends actually matching a crossing order (~2.4us).
/// A single order needs at least three of these (order_id, trade_id, counterparty alias), so
/// entropy collection was costing more than the trading engine.
///
/// This reads a page at a time and hands out slices, which removes the syscall from all but
/// every-few-hundredth call. The bytes are still the OS CSPRNG's output — buffering changes
/// *when* they were generated, not how they were generated.
///
/// CONSTRAINT: not fork-safe. A forked child would inherit the parent's unused buffer and hand
/// out the same bytes. This process never forks (threads only); if that ever changes, this pool
/// must be reset in the child, or the buffer dropped entirely on the fork path.
struct RandomPool {
    file: Option<std::fs::File>,
    buf: Vec<u8>,
    pos: usize,
}

const RANDOM_POOL_BYTES: usize = 4096;

impl RandomPool {
    fn new() -> Self {
        RandomPool { file: std::fs::File::open("/dev/urandom").ok(), buf: Vec::new(), pos: 0 }
    }

    fn take(&mut self, n: usize) -> Vec<u8> {
        // Anything approaching the buffer size goes straight to the device rather than
        // thrashing the pool — long-lived key material shouldn't come from here anyway.
        if n >= RANDOM_POOL_BYTES / 2 {
            return secure_random_bytes(n);
        }
        if self.pos + n > self.buf.len() {
            use std::io::Read as _;
            let mut fresh = vec![0u8; RANDOM_POOL_BYTES];
            let refilled = match self.file.as_mut() {
                Some(f) => f.read_exact(&mut fresh).is_ok(),
                None => false,
            };
            if !refilled {
                // Device unavailable mid-run: fall back to the same unbuffered path (which
                // warns loudly and degrades to the weak PRNG) rather than serving stale bytes.
                return secure_random_bytes(n);
            }
            self.buf = fresh;
            self.pos = 0;
        }
        let out = self.buf[self.pos..self.pos + n].to_vec();
        self.pos += n;
        out
    }
}

static RANDOM_POOL: std::sync::OnceLock<std::sync::Mutex<RandomPool>> = std::sync::OnceLock::new();

/// Hot-path random bytes. Use for per-request identifiers. For long-lived secrets (agent keys,
/// the owner key) prefer `secure_random_bytes`, which talks to the device directly.
pub fn random_bytes_pooled(n: usize) -> Vec<u8> {
    let pool = RANDOM_POOL.get_or_init(|| std::sync::Mutex::new(RandomPool::new()));
    match pool.lock() {
        Ok(mut p) => p.take(n),
        Err(_) => secure_random_bytes(n),
    }
}

/// Cryptographically secure random bytes, read straight from the OS CSPRNG (`/dev/urandom` on
/// Linux/macOS) via plain `std::fs` — no `rand`/`getrandom` crate needed, since the OS device
/// node itself *is* the CSPRNG. This is what agent secrets and the owner key are generated
/// with (see main.rs). Falls back to `weak_random_bytes` (with a loud warning) only if the
/// device can't be opened, e.g. a non-Unix target — a fallback that's fine for a local demo
/// binary but should be a hard error in anything meant to run unattended in production.
pub fn secure_random_bytes(n: usize) -> Vec<u8> {
    use std::fs::File;
    use std::io::Read as _;
    match File::open("/dev/urandom").and_then(|mut f| {
        let mut buf = vec![0u8; n];
        f.read_exact(&mut buf)?;
        Ok(buf)
    }) {
        Ok(buf) => buf,
        Err(e) => {
            eprintln!(
                "WARNING: could not read /dev/urandom ({e}) — falling back to a non-cryptographic \
                 PRNG for random bytes. Fine for a local demo, not for anything real."
            );
            weak_random_bytes(n)
        }
    }
}

/// NOT cryptographically secure. A xorshift64 PRNG seeded from wall-clock time + a process-wide
/// counter. Only used as `secure_random_bytes`'s fallback when `/dev/urandom` can't be opened —
/// do not call this directly for anything beyond that.
pub fn weak_random_bytes(n: usize) -> Vec<u8> {
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};
    static COUNTER: AtomicU64 = AtomicU64::new(0x9e3779b97f4a7c15);

    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos() as u64;
    let mut state = nanos ^ COUNTER.fetch_add(0x2545F4914F6CDD1D, Ordering::Relaxed);
    if state == 0 {
        state = 0xdead_beef_dead_beef;
    }

    let mut out = Vec::with_capacity(n);
    while out.len() < n {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        out.extend_from_slice(&state.to_le_bytes());
    }
    out.truncate(n);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constant_time_eq_matches_naive_equality() {
        assert!(constant_time_eq(b"same", b"same"));
        assert!(!constant_time_eq(b"same", b"diff"));
        assert!(!constant_time_eq(b"short", b"longer-string"));
        assert!(constant_time_eq(b"", b""));
    }

    #[test]
    fn secure_random_bytes_has_right_length_and_varies() {
        let a = secure_random_bytes(32);
        let b = secure_random_bytes(32);
        assert_eq!(a.len(), 32);
        assert_eq!(b.len(), 32);
        assert_ne!(a, b, "two independent draws should not collide");
    }

    #[test]
    fn sha256_matches_known_vector() {
        // NIST test vector: SHA256("abc")
        let digest = sha256(b"abc");
        assert_eq!(
            hex_encode(&digest),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn sha256_matches_empty_string_vector() {
        let digest = sha256(b"");
        assert_eq!(
            hex_encode(&digest),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn sha1_matches_known_vector() {
        // NIST test vector: SHA1("abc")
        let digest = sha1(b"abc");
        assert_eq!(hex_encode(&digest), "a9993e364706816aba3e25717850c26c9cd0d89d");
    }

    #[test]
    fn hmac_sha256_matches_rfc4231_test_case_1() {
        // RFC 4231 test case 1: key = 0x0b * 20, data = "Hi There"
        let key = [0x0bu8; 20];
        let digest = hmac_sha256(&key, b"Hi There");
        assert_eq!(
            hex_encode(&digest),
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
        );
    }

    #[test]
    fn base64_matches_known_vector() {
        assert_eq!(base64_encode(b"any carnal pleasure."), "YW55IGNhcm5hbCBwbGVhc3VyZS4=");
        assert_eq!(base64_encode(b""), "");
    }

    #[test]
    fn hex_roundtrips() {
        let bytes = [0xde, 0xad, 0xbe, 0xef];
        let hex = hex_encode(&bytes);
        assert_eq!(hex, "deadbeef");
        assert_eq!(hex_decode(&hex).unwrap(), bytes);
    }
}

#[cfg(test)]
mod pool_tests {
    use super::*;

    #[test]
    fn pooled_random_returns_requested_length_and_varies() {
        let a = random_bytes_pooled(16);
        let b = random_bytes_pooled(16);
        assert_eq!(a.len(), 16);
        assert_eq!(b.len(), 16);
        assert_ne!(a, b);
    }

    #[test]
    fn pooled_random_survives_crossing_the_buffer_boundary() {
        // More than RANDOM_POOL_BYTES worth of 16-byte draws forces at least one refill; every
        // draw must still be distinct across that boundary.
        let draws: Vec<Vec<u8>> = (0..(RANDOM_POOL_BYTES / 16) + 50)
            .map(|_| random_bytes_pooled(16))
            .collect();
        let mut sorted = draws.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), draws.len(), "pool repeated bytes across a refill");
    }

    #[test]
    fn large_requests_bypass_the_pool_and_still_work() {
        let big = random_bytes_pooled(RANDOM_POOL_BYTES);
        assert_eq!(big.len(), RANDOM_POOL_BYTES);
    }
}
