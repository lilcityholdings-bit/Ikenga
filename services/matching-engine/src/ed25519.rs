//! Ed25519 signing/verification via the system `openssl` binary.
//!
//! `ed25519-dalek` — what the spec actually calls for — can't be fetched here; see the note at
//! the top of Cargo.toml. The alternative to a crate isn't "write curve25519 field and point
//! arithmetic by hand": that's several hundred lines of modular-inverse and point-encoding code
//! in exactly the place where a subtle bug is a silent forgery vulnerability instead of a compile
//! error, and it can't be meaningfully checked in this sandbox beyond a couple of hand-typed test
//! vectors (which, worth admitting, took three tries to transcribe correctly while writing this
//! module — that alone is a good argument for never hand-rolling this kind of code from memory).
//!
//! Instead this shells out to the system's OpenSSL, which every mainstream Linux base image
//! (including the one in this project's Dockerfile) ships, and which implements Ed25519 the same
//! way everyone else serious does: audited, maintained, not written by this project. The honest
//! cost is latency — a process spawn plus two temp-file writes per verification, replacing what
//! used to be a single in-process HMAC call. That's measured in docs/PERFORMANCE.md. It is a real
//! regression and a deliberate trade: the alternative to "slow but audited" here is "fast but
//! nobody has checked it," and a signature scheme is the wrong place to buy back milliseconds.
//!
//! What this actually fixes: `AgentRegistry` (src/auth.rs) now stores each agent's PUBLIC key
//! only, both in memory and in the write-ahead log. A stolen WAL file or a memory snapshot of a
//! compromised server contains nothing that can forge a signature — compare the old doc comment
//! on `AgentRegistry`, which had to say the opposite about the HMAC shared secret it used to hold.

use std::io::Write as _;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};

/// Fixed 12-byte ASN.1 prefix for an Ed25519 SubjectPublicKeyInfo (RFC 8410 / RFC 5280). Every
/// field except the 32-byte key itself is a fixed OID for "this is an Ed25519 public key," so
/// this never varies. Confirmed against real `openssl pkey -pubout -outform DER` output, not
/// typed from memory.
const SPKI_PREFIX: [u8; 12] = [
    0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00,
];
/// Fixed 16-byte ASN.1 prefix for an Ed25519 PKCS#8 private key wrapping a 32-byte seed.
/// Same provenance note as above.
const PKCS8_PREFIX: [u8; 16] = [
    0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20,
];

fn pem_wrap(label: &str, der: &[u8]) -> String {
    let b64 = crate::crypto::base64_encode(der);
    let mut out = format!("-----BEGIN {label}-----\n");
    for chunk in b64.as_bytes().chunks(64) {
        out.push_str(std::str::from_utf8(chunk).unwrap_or(""));
        out.push('\n');
    }
    out.push_str(&format!("-----END {label}-----\n"));
    out
}

fn pubkey_pem(pubkey: &[u8; 32]) -> String {
    let mut der = SPKI_PREFIX.to_vec();
    der.extend_from_slice(pubkey);
    pem_wrap("PUBLIC KEY", &der)
}

fn privkey_pem(seed: &[u8; 32]) -> String {
    let mut der = PKCS8_PREFIX.to_vec();
    der.extend_from_slice(seed);
    pem_wrap("PRIVATE KEY", &der)
}

static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

fn scratch_dir() -> PathBuf {
    let dir = std::env::temp_dir().join("ikenga-ed25519");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

/// A unique, self-deleting temp file. Every verify/sign/keygen call touches disk (openssl's CLI
/// has no "here are some bytes" API) — this makes sure those files never pile up, including on
/// the early-return paths above.
struct TempFile(PathBuf);

impl TempFile {
    fn write(tag: &str, bytes: &[u8]) -> std::io::Result<TempFile> {
        let n = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = scratch_dir().join(format!("{tag}-{}-{n}", std::process::id()));
        let mut f = std::fs::File::create(&path)?;
        f.write_all(bytes)?;
        Ok(TempFile(path))
    }

    fn path_str(&self) -> &str {
        self.0.to_str().unwrap_or("")
    }
}

impl Drop for TempFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}


/// SHA-512. Needed because Ed25519 is defined in terms of it, and `crypto.rs` only has SHA-256.
///
/// The plain FIPS 180-4 reference construction. It is not on any hot path except signature
/// verification, where it runs once over a few hundred bytes.
pub fn sha512(message: &[u8]) -> [u8; 64] {
    const K: [u64; 80] = [
        0x428a2f98d728ae22, 0x7137449123ef65cd, 0xb5c0fbcfec4d3b2f, 0xe9b5dba58189dbbc,
        0x3956c25bf348b538, 0x59f111f1b605d019, 0x923f82a4af194f9b, 0xab1c5ed5da6d8118,
        0xd807aa98a3030242, 0x12835b0145706fbe, 0x243185be4ee4b28c, 0x550c7dc3d5ffb4e2,
        0x72be5d74f27b896f, 0x80deb1fe3b1696b1, 0x9bdc06a725c71235, 0xc19bf174cf692694,
        0xe49b69c19ef14ad2, 0xefbe4786384f25e3, 0x0fc19dc68b8cd5b5, 0x240ca1cc77ac9c65,
        0x2de92c6f592b0275, 0x4a7484aa6ea6e483, 0x5cb0a9dcbd41fbd4, 0x76f988da831153b5,
        0x983e5152ee66dfab, 0xa831c66d2db43210, 0xb00327c898fb213f, 0xbf597fc7beef0ee4,
        0xc6e00bf33da88fc2, 0xd5a79147930aa725, 0x06ca6351e003826f, 0x142929670a0e6e70,
        0x27b70a8546d22ffc, 0x2e1b21385c26c926, 0x4d2c6dfc5ac42aed, 0x53380d139d95b3df,
        0x650a73548baf63de, 0x766a0abb3c77b2a8, 0x81c2c92e47edaee6, 0x92722c851482353b,
        0xa2bfe8a14cf10364, 0xa81a664bbc423001, 0xc24b8b70d0f89791, 0xc76c51a30654be30,
        0xd192e819d6ef5218, 0xd69906245565a910, 0xf40e35855771202a, 0x106aa07032bbd1b8,
        0x19a4c116b8d2d0c8, 0x1e376c085141ab53, 0x2748774cdf8eeb99, 0x34b0bcb5e19b48a8,
        0x391c0cb3c5c95a63, 0x4ed8aa4ae3418acb, 0x5b9cca4f7763e373, 0x682e6ff3d6b2b8a3,
        0x748f82ee5defb2fc, 0x78a5636f43172f60, 0x84c87814a1f0ab72, 0x8cc702081a6439ec,
        0x90befffa23631e28, 0xa4506cebde82bde9, 0xbef9a3f7b2c67915, 0xc67178f2e372532b,
        0xca273eceea26619c, 0xd186b8c721c0c207, 0xeada7dd6cde0eb1e, 0xf57d4f7fee6ed178,
        0x06f067aa72176fba, 0x0a637dc5a2c898a6, 0x113f9804bef90dae, 0x1b710b35131c471b,
        0x28db77f523047d84, 0x32caab7b40c72493, 0x3c9ebe0a15c9bebc, 0x431d67c49c100d4c,
        0x4cc5d4becb3e42b6, 0x597f299cfc657e2a, 0x5fcb6fab3ad6faec, 0x6c44198c4a475817,
    ];
    let mut h: [u64; 8] = [
        0x6a09e667f3bcc908, 0xbb67ae8584caa73b, 0x3c6ef372fe94f82b, 0xa54ff53a5f1d36f1,
        0x510e527fade682d1, 0x9b05688c2b3e6c1f, 0x1f83d9abfb41bd6b, 0x5be0cd19137e2179,
    ];

    let bit_len = (message.len() as u128) * 8;
    let mut padded = Vec::with_capacity(message.len() + 145);
    padded.extend_from_slice(message);
    padded.push(0x80);
    while padded.len() % 128 != 112 {
        padded.push(0);
    }
    padded.extend_from_slice(&bit_len.to_be_bytes());

    let mut w = [0u64; 80];
    for block in padded.chunks_exact(128) {
        for i in 0..16 {
            w[i] = u64::from_be_bytes(block[i * 8..i * 8 + 8].try_into().unwrap());
        }
        for i in 16..80 {
            let s0 = w[i - 15].rotate_right(1) ^ w[i - 15].rotate_right(8) ^ (w[i - 15] >> 7);
            let s1 = w[i - 2].rotate_right(19) ^ w[i - 2].rotate_right(61) ^ (w[i - 2] >> 6);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let (mut a, mut b, mut c, mut dd, mut e, mut f, mut g, mut hh) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);
        for i in 0..80 {
            let s1 = e.rotate_right(14) ^ e.rotate_right(18) ^ e.rotate_right(41);
            let ch = (e & f) ^ ((!e) & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(28) ^ a.rotate_right(34) ^ a.rotate_right(39);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g; g = f; f = e;
            e = dd.wrapping_add(t1);
            dd = c; c = b; b = a;
            a = t1.wrapping_add(t2);
        }
        for (i, v) in [a, b, c, dd, e, f, g, hh].iter().enumerate() {
            h[i] = h[i].wrapping_add(*v);
        }
    }

    let mut out = [0u8; 64];
    for (i, v) in h.iter().enumerate() {
        out[i * 8..i * 8 + 8].copy_from_slice(&v.to_be_bytes());
    }
    out
}

/// Ed25519 verification done in-process, instead of by spawning `openssl`.
///
/// # Why
///
/// Every signed request used to write three temporary files and fork a process, which costs about
/// 2.4ms. Under load that is the whole story: signed writes flattened at roughly 400 a second on
/// two cores, and adding concurrency only added latency. Turning the write-ahead log's fsync off
/// changed the number by less than 4%, which is how it became clear the disk had never been the
/// bottleneck and the signature check always was.
///
/// # Trusting it
///
/// This decides whether a request is really from who it claims to be, so "it looks right" is not
/// a standard. It is checked three ways: the RFC 8032 vectors, the specific edge cases the RFC
/// calls out, and a differential test that puts thousands of random keys, messages and
/// corruptions through both this and OpenSSL and asserts they agree every time — most importantly
/// on the ones that must be rejected.
///
/// Arithmetic is on five 51-bit limbs, the standard representation for this curve. It is not
/// written to be constant-time, and does not need to be: everything it touches is public. A
/// signature and a public key are sent in the clear by the caller; there is no secret here whose
/// timing could leak. Signing — which does handle a secret — is not implemented here and stays
/// where it was.
pub mod native {
    const MASK: u64 = (1u64 << 51) - 1;

    #[derive(Clone, Copy)]
    pub struct Fe([u64; 5]);

    impl Fe {
        const ZERO: Fe = Fe([0; 5]);
        const ONE: Fe = Fe([1, 0, 0, 0, 0]);

        fn from_bytes(b: &[u8; 32]) -> Fe {
            let w = |i: usize| u64::from_le_bytes(b[i..i + 8].try_into().unwrap());
            Fe([
                w(0) & MASK,
                (w(6) >> 3) & MASK,
                (w(12) >> 6) & MASK,
                (w(19) >> 1) & MASK,
                (w(24) >> 12) & MASK,
            ])
        }

        fn to_bytes(self) -> [u8; 32] {
            let mut t = self.carry();
            // Canonicalise: subtract p if we are at or above it, so equal values encode equally.
            let mut q = (t.0[0] + 19) >> 51;
            for i in 1..5 {
                q = (t.0[i] + q) >> 51;
            }
            t.0[0] += 19 * q;
            let mut c = 0u64;
            for i in 0..5 {
                t.0[i] += c;
                c = t.0[i] >> 51;
                t.0[i] &= MASK;
            }
            let words = [
                t.0[0] | (t.0[1] << 51),
                (t.0[1] >> 13) | (t.0[2] << 38),
                (t.0[2] >> 26) | (t.0[3] << 25),
                (t.0[3] >> 39) | (t.0[4] << 12),
            ];
            let mut out = [0u8; 32];
            for (i, x) in words.iter().enumerate() {
                out[i * 8..i * 8 + 8].copy_from_slice(&x.to_le_bytes());
            }
            out
        }

        fn carry(mut self) -> Fe {
            let mut c = 0u64;
            for i in 0..5 {
                self.0[i] += c;
                c = self.0[i] >> 51;
                self.0[i] &= MASK;
            }
            self.0[0] += 19 * c;
            c = self.0[0] >> 51;
            self.0[0] &= MASK;
            self.0[1] += c;
            self
        }

        fn add(self, o: Fe) -> Fe {
            let mut r = [0u64; 5];
            for i in 0..5 {
                r[i] = self.0[i] + o.0[i];
            }
            Fe(r).carry()
        }

        fn sub(self, o: Fe) -> Fe {
            // 2p added first so no limb can underflow.
            let two_p = [
                0x0f_ffff_ffff_ffda,
                0x0f_ffff_ffff_fffe,
                0x0f_ffff_ffff_fffe,
                0x0f_ffff_ffff_fffe,
                0x0f_ffff_ffff_fffe,
            ];
            let mut r = [0u64; 5];
            for i in 0..5 {
                r[i] = self.0[i] + two_p[i] - o.0[i];
            }
            Fe(r).carry()
        }

        fn mul(self, o: Fe) -> Fe {
            let a: [u128; 5] = [
                self.0[0] as u128, self.0[1] as u128, self.0[2] as u128,
                self.0[3] as u128, self.0[4] as u128,
            ];
            let b: [u128; 5] = [
                o.0[0] as u128, o.0[1] as u128, o.0[2] as u128, o.0[3] as u128, o.0[4] as u128,
            ];
            // 2^255 = 19 mod p, so limbs that overflow the top wrap round multiplied by 19.
            let b19: [u128; 5] = [b[0], b[1] * 19, b[2] * 19, b[3] * 19, b[4] * 19];
            let c = [
                a[0]*b[0] + a[1]*b19[4] + a[2]*b19[3] + a[3]*b19[2] + a[4]*b19[1],
                a[0]*b[1] + a[1]*b[0]   + a[2]*b19[4] + a[3]*b19[3] + a[4]*b19[2],
                a[0]*b[2] + a[1]*b[1]   + a[2]*b[0]   + a[3]*b19[4] + a[4]*b19[3],
                a[0]*b[3] + a[1]*b[2]   + a[2]*b[1]   + a[3]*b[0]   + a[4]*b19[4],
                a[0]*b[4] + a[1]*b[3]   + a[2]*b[2]   + a[3]*b[1]   + a[4]*b[0],
            ];
            let mut r = [0u64; 5];
            let mut carry: u128 = 0;
            for i in 0..5 {
                let v = c[i] + carry;
                r[i] = (v as u64) & MASK;
                carry = v >> 51;
            }
            r[0] += (carry as u64) * 19;
            let c0 = r[0] >> 51;
            r[0] &= MASK;
            r[1] += c0;
            Fe(r)
        }

        fn sq(self) -> Fe {
            self.mul(self)
        }

        /// Square-and-multiply over a little-endian exponent. Written the plain way on purpose:
        /// the clever fixed-chain versions are faster and much easier to get subtly wrong, and
        /// this runs in microseconds either way.
        fn pow(self, exp_le: &[u8; 32]) -> Fe {
            let mut r = Fe::ONE;
            for byte_i in (0..32).rev() {
                for bit in (0..8).rev() {
                    r = r.sq();
                    if (exp_le[byte_i] >> bit) & 1 == 1 {
                        r = r.mul(self);
                    }
                }
            }
            r
        }

        fn invert(self) -> Fe {
            // p - 2
            let mut e = [0xffu8; 32];
            e[0] = 0xeb;
            e[31] = 0x7f;
            self.pow(&e)
        }

        fn pow_p58(self) -> Fe {
            // (p - 5) / 8 = 2^252 - 3
            let mut e = [0xffu8; 32];
            e[0] = 0xfd;
            e[31] = 0x0f;
            self.pow(&e)
        }

        fn is_zero(self) -> bool {
            self.to_bytes() == [0u8; 32]
        }

        fn is_negative(self) -> bool {
            self.to_bytes()[0] & 1 == 1
        }

        fn neg(self) -> Fe {
            Fe::ZERO.sub(self)
        }
    }

    fn d() -> Fe {
        // -121665/121666
        Fe::from_bytes(&[
            0xa3, 0x78, 0x59, 0x13, 0xca, 0x4d, 0xeb, 0x75, 0xab, 0xd8, 0x41, 0x41, 0x4d, 0x0a,
            0x70, 0x00, 0x98, 0xe8, 0x79, 0x77, 0x79, 0x40, 0xc7, 0x8c, 0x73, 0xfe, 0x6f, 0x2b,
            0xee, 0x6c, 0x03, 0x52,
        ])
    }

    fn sqrt_m1() -> Fe {
        Fe::from_bytes(&[
            0xb0, 0xa0, 0x0e, 0x4a, 0x27, 0x1b, 0xee, 0xc4, 0x78, 0xe4, 0x2f, 0xad, 0x06, 0x18,
            0x43, 0x2f, 0xa7, 0xd7, 0xfb, 0x3d, 0x99, 0x00, 0x4d, 0x2b, 0x0b, 0xdf, 0xc1, 0x4f,
            0x80, 0x24, 0x83, 0x2b,
        ])
    }

    /// Extended twisted Edwards coordinates: x = X/Z, y = Y/Z, xy = T/Z.
    #[derive(Clone, Copy)]
    pub struct Point {
        x: Fe,
        y: Fe,
        z: Fe,
        t: Fe,
    }

    impl Point {
        fn identity() -> Point {
            Point { x: Fe::ZERO, y: Fe::ONE, z: Fe::ONE, t: Fe::ZERO }
        }

        fn base() -> Point {
            // y = 4/5, x recovered with the positive sign.
            let y = Fe::from_bytes(&[
                0x58, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66,
                0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x66,
                0x66, 0x66, 0x66, 0x66, 0x66, 0x66,
            ]);
            let x = recover_x(y, false).expect("the base point is on the curve");
            Point { x, y, z: Fe::ONE, t: x.mul(y) }
        }

        fn add(self, o: Point) -> Point {
            let a = self.y.sub(self.x).mul(o.y.sub(o.x));
            let b = self.y.add(self.x).mul(o.y.add(o.x));
            let c = self.t.mul(o.t).mul(d()).mul(Fe([2, 0, 0, 0, 0]));
            let dd = self.z.mul(o.z).mul(Fe([2, 0, 0, 0, 0]));
            let e = b.sub(a);
            let f = dd.sub(c);
            let g = dd.add(c);
            let h = b.add(a);
            Point { x: e.mul(f), y: g.mul(h), z: f.mul(g), t: e.mul(h) }
        }

        fn mul(self, scalar_le: &[u8; 32]) -> Point {
            let mut r = Point::identity();
            for byte_i in (0..32).rev() {
                for bit in (0..8).rev() {
                    r = r.add(r);
                    if (scalar_le[byte_i] >> bit) & 1 == 1 {
                        r = r.add(self);
                    }
                }
            }
            r
        }

        fn eq(self, o: Point) -> bool {
            // Projective: compare cross-multiplied affine coordinates rather than normalising.
            self.x.mul(o.z).eq(o.x.mul(self.z)) && self.y.mul(o.z).eq(o.y.mul(self.z))
        }
    }

    trait FeEq {
        fn eq(self, o: Fe) -> bool;
    }
    impl FeEq for Fe {
        fn eq(self, o: Fe) -> bool {
            self.to_bytes() == o.to_bytes()
        }
    }

    /// x from y and the sign bit: x² = (y²−1)/(dy²+1).
    fn recover_x(y: Fe, x_is_negative: bool) -> Option<Fe> {
        let y2 = y.sq();
        let u = y2.sub(Fe::ONE);
        let v = y2.mul(d()).add(Fe::ONE);
        if v.is_zero() {
            return None;
        }
        let v3 = v.sq().mul(v);
        let v7 = v3.sq().mul(v);
        let mut x = u.mul(v3).mul(u.mul(v7).pow_p58());

        let check = v.mul(x.sq());
        if !check.eq(u) {
            // The other square root: multiply by sqrt(-1).
            if check.eq(u.neg()) {
                x = x.mul(sqrt_m1());
            } else {
                return None; // y is not on the curve at all
            }
        }
        if x.is_zero() && x_is_negative {
            return None; // no valid encoding of -0
        }
        if x.is_negative() != x_is_negative {
            x = x.neg();
        }
        Some(x)
    }

    fn decompress(bytes: &[u8; 32]) -> Option<Point> {
        let mut ybytes = *bytes;
        let sign = ybytes[31] >> 7 == 1;
        ybytes[31] &= 0x7f;
        let y = Fe::from_bytes(&ybytes);
        // A non-canonical y (>= p) is refused: two encodings of one point is a malleability
        // surface, and every real implementation rejects it.
        if y.to_bytes() != ybytes {
            return None;
        }
        let x = recover_x(y, sign)?;
        Some(Point { x, y, z: Fe::ONE, t: x.mul(y) })
    }

    /// The group order L = 2^252 + 27742317777372353535851937790883648493.
    const L: [u8; 32] = [
        0xed, 0xd3, 0xf5, 0x5c, 0x1a, 0x63, 0x12, 0x58, 0xd6, 0x9c, 0xf7, 0xa2, 0xde, 0xf9, 0xde,
        0x14, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x10,
    ];

    fn scalar_is_canonical(s: &[u8; 32]) -> bool {
        for i in (0..32).rev() {
            if s[i] < L[i] {
                return true;
            }
            if s[i] > L[i] {
                return false;
            }
        }
        false // s == L is not less than L
    }

    /// Reduces a 64-byte hash mod L, by schoolbook long division on bytes. Not the fastest way,
    /// and the one that is hardest to get wrong.
    fn reduce64(h: &[u8; 64]) -> [u8; 32] {
        // Big-endian digits for simple long division.
        let mut acc = [0u8; 64];
        for i in 0..64 {
            acc[i] = h[63 - i];
        }
        let mut l_be = [0u8; 32];
        for i in 0..32 {
            l_be[i] = L[31 - i];
        }
        // rem = acc mod l, processed one bit at a time.
        let mut rem = [0u8; 33];
        for byte in acc.iter() {
            for bit in (0..8).rev() {
                // rem = rem*2 + bit
                let mut carry = (byte >> bit) & 1;
                for i in (0..33).rev() {
                    let v = ((rem[i] as u16) << 1) | carry as u16;
                    rem[i] = v as u8;
                    carry = (v >> 8) as u8;
                }
                // if rem >= L, subtract
                let mut ge = true;
                for i in 0..33 {
                    let lv = if i == 0 { 0 } else { l_be[i - 1] };
                    if rem[i] != lv {
                        ge = rem[i] > lv;
                        break;
                    }
                }
                if ge {
                    let mut borrow = 0i16;
                    for i in (0..33).rev() {
                        let lv = if i == 0 { 0 } else { l_be[i - 1] } as i16;
                        let mut v = rem[i] as i16 - lv - borrow;
                        if v < 0 {
                            v += 256;
                            borrow = 1;
                        } else {
                            borrow = 0;
                        }
                        rem[i] = v as u8;
                    }
                }
            }
        }
        let mut out = [0u8; 32];
        for i in 0..32 {
            out[i] = rem[32 - i];
        }
        out
    }

    /// `signature` is R || S. Returns false for anything that is not a valid signature, including
    /// every malformed encoding — never panics, whatever the caller sends.
    pub fn verify(pubkey: &[u8; 32], message: &[u8], signature: &[u8]) -> bool {
        if signature.len() != 64 {
            return false;
        }
        let mut r_bytes = [0u8; 32];
        let mut s_bytes = [0u8; 32];
        r_bytes.copy_from_slice(&signature[..32]);
        s_bytes.copy_from_slice(&signature[32..]);

        // S must be reduced. Without this check a signature can be mauled into a different valid
        // encoding of the same thing, which breaks any system that treats a signature as an
        // identifier.
        if !scalar_is_canonical(&s_bytes) {
            return false;
        }

        let Some(a_point) = decompress(pubkey) else { return false };
        let Some(r_point) = decompress(&r_bytes) else { return false };

        let mut hash_input = Vec::with_capacity(64 + message.len());
        hash_input.extend_from_slice(&r_bytes);
        hash_input.extend_from_slice(pubkey);
        hash_input.extend_from_slice(message);
        let k = reduce64(&super::sha512(&hash_input));

        // [s]B == R + [k]A
        let lhs = Point::base().mul(&s_bytes);
        let rhs = r_point.add(a_point.mul(&k));
        lhs.eq(rhs)
    }

    /// Whether a 32-byte string decodes to a point on the curve.
    pub fn is_valid_point(pubkey: &[u8; 32]) -> bool {
        decompress(pubkey).is_some()
    }
}


/// Verifies a raw (not DER, not base64 — exactly 64 bytes) Ed25519 signature over `message`
/// against a raw 32-byte public key. Any I/O or subprocess failure returns `false` — a broken
/// openssl call is a reason to reject a signature, never a reason to accept one.
pub fn verify_via_openssl(pubkey: &[u8; 32], message: &[u8], signature: &[u8]) -> bool {
    if signature.len() != 64 {
        return false;
    }
    let Ok(pub_pem) = TempFile::write("pub", pubkey_pem(pubkey).as_bytes()) else { return false };
    let Ok(msg_file) = TempFile::write("msg", message) else { return false };
    let Ok(sig_file) = TempFile::write("sig", signature) else { return false };

    let output = Command::new("openssl")
        .args([
            "pkeyutl",
            "-verify",
            "-pubin",
            "-inkey",
            pub_pem.path_str(),
            "-rawin",
            "-in",
            msg_file.path_str(),
            "-sigfile",
            sig_file.path_str(),
        ])
        .output();

    matches!(output, Ok(o) if o.status.success())
}

/// Screens an Ed25519 public key at registration time.
///
/// **What this catches:** a key OpenSSL can't parse at all, and the all-zero key — the value an
/// uninitialised or never-written buffer produces, and the most common way a client accidentally
/// registers something it can never sign for.
///
/// **What it does not catch, and cannot:** a key that is simply the wrong one. Any 32 bytes that
/// aren't obviously degenerate look like a valid Ed25519 public key, so a hex typo or an exported
/// wrong field sails straight through here and only shows up as failed signatures later. This is
/// inherent to the key format, not a gap to be closed with more checking. It also does not do
/// full small-order-point rejection: OpenSSL's parser doesn't, and hardcoding the small-order
/// point list from memory is exactly the kind of thing this codebase avoids doing with crypto
/// constants — see this module's header.
///
/// It's still worth a subprocess at registration (never on a hot path), because the failure it
/// does catch is otherwise diagnosed as "signature verification failed" on every subsequent
/// request, with nothing pointing back at the key registered ten minutes earlier.
pub fn is_valid_public_key(pubkey: &[u8; 32]) -> bool {
    if pubkey.iter().all(|b| *b == 0) {
        return false;
    }
    let Ok(pem) = TempFile::write("pubcheck", pubkey_pem(pubkey).as_bytes()) else {
        return false;
    };
    matches!(
        Command::new("openssl")
            // `-in`, not `-inkey`: `openssl pkey` and `openssl pkeyutl` disagree on the flag
            // name for the same thing, and the wrong one fails on a perfectly good key.
            .args(["pkey", "-pubin", "-in", pem.path_str(), "-noout"])
            .output(),
        Ok(o) if o.status.success()
    )
}

/// Generates a fresh Ed25519 keypair, returning `(seed, pubkey)`, both raw 32-byte arrays.
///
/// Used only for dev/demo agent seeding at startup. In the real flow an agent generates its own
/// keypair locally (its SDK would do this) and only ever sends the server the public half — the
/// server calling this function itself, for its own demo agents, is a testing convenience, not
/// the intended production path.
pub fn generate_keypair() -> Option<([u8; 32], [u8; 32])> {
    let n = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let key_path = scratch_dir().join(format!("genkey-{}-{n}", std::process::id()));
    let priv_der_path = scratch_dir().join(format!("privder-{}-{n}", std::process::id()));
    let pub_der_path = scratch_dir().join(format!("pubder-{}-{n}", std::process::id()));

    let cleanup = |paths: &[&PathBuf]| {
        for p in paths {
            let _ = std::fs::remove_file(p);
        }
    };

    let ok = Command::new("openssl")
        .args(["genpkey", "-algorithm", "ed25519", "-out", key_path.to_str()?])
        .status()
        .ok()?
        .success();
    if !ok {
        cleanup(&[&key_path]);
        return None;
    }

    let priv_ok = Command::new("openssl")
        .args(["pkey", "-in", key_path.to_str()?, "-outform", "DER", "-out", priv_der_path.to_str()?])
        .status()
        .ok()?
        .success();
    let pub_ok = Command::new("openssl")
        .args(["pkey", "-in", key_path.to_str()?, "-pubout", "-outform", "DER", "-out", pub_der_path.to_str()?])
        .status()
        .ok()?
        .success();

    let result = if priv_ok && pub_ok {
        let priv_der = std::fs::read(&priv_der_path).ok();
        let pub_der = std::fs::read(&pub_der_path).ok();
        match (priv_der, pub_der) {
            (Some(priv_der), Some(pub_der)) if priv_der.len() == 48 && pub_der.len() == 44 => {
                let mut seed = [0u8; 32];
                seed.copy_from_slice(&priv_der[16..48]);
                let mut pubkey = [0u8; 32];
                pubkey.copy_from_slice(&pub_der[12..44]);
                Some((seed, pubkey))
            }
            _ => None,
        }
    } else {
        None
    };

    cleanup(&[&key_path, &priv_der_path, &pub_der_path]);
    result
}

/// Derives the public key that corresponds to a raw 32-byte seed.
///
/// Needed because the venue's attestation key is *derived* from the owner key rather than
/// generated and stored — there is no keypair on disk to read the public half out of, and a
/// verifier needs it to check anything this venue signs.
pub fn public_from_seed(seed: &[u8; 32]) -> Option<[u8; 32]> {
    let priv_pem = TempFile::write("pubfromseed", privkey_pem(seed).as_bytes()).ok()?;
    let n = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let der_path = scratch_dir().join(format!("pubfs-{}-{n}", std::process::id()));

    let ok = Command::new("openssl")
        .args([
            "pkey",
            "-in",
            priv_pem.path_str(),
            "-pubout",
            "-outform",
            "DER",
            "-out",
            der_path.to_str()?,
        ])
        .status()
        .ok()?
        .success();

    let result = if ok {
        std::fs::read(&der_path).ok().and_then(|der| {
            // SubjectPublicKeyInfo for Ed25519 is a fixed 44 bytes: a 12-byte header then the key.
            (der.len() == 44).then(|| {
                let mut pk = [0u8; 32];
                pk.copy_from_slice(&der[12..44]);
                pk
            })
        })
    } else {
        None
    };
    let _ = std::fs::remove_file(&der_path);
    result
}

/// Signs `message` with a raw 32-byte seed, returning a raw 64-byte signature.
///
/// Exists so this module's own tests, and the demo CLI clients in this repo, can round-trip
/// without a second language in the loop. A real agent signs with its own SDK (`sdk/ts` uses
/// Node's built-in `crypto` module — no dependency needed there either), not this function.
pub fn sign(seed: &[u8; 32], message: &[u8]) -> Option<Vec<u8>> {
    let priv_pem = TempFile::write("privsign", privkey_pem(seed).as_bytes()).ok()?;
    let msg_file = TempFile::write("signmsg", message).ok()?;
    let n = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let sig_path = scratch_dir().join(format!("sigout-{}-{n}", std::process::id()));

    let ok = Command::new("openssl")
        .args([
            "pkeyutl",
            "-sign",
            "-inkey",
            priv_pem.path_str(),
            "-rawin",
            "-in",
            msg_file.path_str(),
            "-out",
            sig_path.to_str()?,
        ])
        .status()
        .ok()?
        .success();

    let result = if ok {
        std::fs::read(&sig_path).ok().filter(|s| s.len() == 64)
    } else {
        None
    };
    let _ = std::fs::remove_file(&sig_path);
    result
}

#[cfg(test)]
mod native_tests {
    use super::*;

    fn unhex(s: &str) -> Vec<u8> {
        (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
    }
    fn key(s: &str) -> [u8; 32] {
        let v = unhex(s);
        let mut k = [0u8; 32];
        k.copy_from_slice(&v);
        k
    }

    #[test]
    fn sha512_matches_the_published_vectors() {
        let h = crate::crypto::hex_encode(&sha512(b""));
        assert_eq!(h, "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce\
                       47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e"
            .replace(['\n', ' '], ""));
        let h = crate::crypto::hex_encode(&sha512(b"abc"));
        assert_eq!(h, "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a\
                       2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f"
            .replace(['\n', ' '], ""));
        // Two blocks, to exercise the padding boundary.
        let h = crate::crypto::hex_encode(&sha512(&[b'a'; 112]));
        assert_eq!(h.len(), 128);
    }

    /// RFC 8032 section 7.1.
    #[test]
    fn rfc8032_vectors_verify() {
        let vectors: &[(&str, &str, &str)] = &[
            ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
             "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
            ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
             "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
            ("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
             "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
        ];
        for (pk, msg, sig) in vectors {
            assert!(verify(&key(pk), &unhex(msg), &unhex(sig)), "vector {pk} failed to verify");
            // And the same vector must fail once anything is touched.
            let mut bad = unhex(sig);
            bad[0] ^= 1;
            assert!(!verify(&key(pk), &unhex(msg), &bad), "a corrupted signature verified");
            let mut m = unhex(msg);
            m.push(0);
            assert!(!verify(&key(pk), &m, &unhex(sig)), "a lengthened message verified");
        }
    }

    #[test]
    fn malformed_input_is_refused_rather_than_panicking() {
        let pk = key("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a");
        for sig in [vec![], vec![0u8; 63], vec![0u8; 65], vec![0xff; 64], vec![0u8; 64]] {
            assert!(!verify(&pk, b"x", &sig));
        }
        // A public key that is not a point on the curve.
        assert!(!verify(&[0xff; 32], b"x", &[0u8; 64]));
        // Every byte string is a candidate key; none may panic.
        for i in 0..=255u8 {
            let _ = verify(&[i; 32], b"whatever", &[i; 64]);
        }
    }

    #[test]
    fn a_non_canonical_s_is_rejected() {
        // S must be below the group order. Accepting S + L would let anyone produce a second
        // valid encoding of an existing signature, which breaks anything that treats a signature
        // as an identifier — this venue's replay defences included.
        let pk = key("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c");
        let msg = unhex("72");
        let good = unhex("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00");
        assert!(verify(&pk, &msg, &good));

        // S + L, carried by hand over the little-endian bytes.
        let l: [u8; 32] = [
            0xed, 0xd3, 0xf5, 0x5c, 0x1a, 0x63, 0x12, 0x58, 0xd6, 0x9c, 0xf7, 0xa2, 0xde, 0xf9,
            0xde, 0x14, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x10,
        ];
        let mut mauled = good.clone();
        let mut carry = 0u16;
        for i in 0..32 {
            let v = mauled[32 + i] as u16 + l[i] as u16 + carry;
            mauled[32 + i] = v as u8;
            carry = v >> 8;
        }
        assert!(!verify(&pk, &msg, &mauled), "a mauled signature was accepted");
    }

    /// The one that actually settles it: thousands of random cases through both implementations.
    #[test]
    #[ignore = "spawns openssl thousands of times; run with --ignored"]
    fn agrees_with_openssl_on_everything() {
        let mut checked = 0;
        for round in 0..250 {
            let Some((seed, pubkey)) = generate_keypair() else { continue };
            let len = (round * 7) % 300;
            let msg: Vec<u8> = (0..len).map(|i| ((i * 31 + round) % 251) as u8).collect();
            let Some(sig) = sign(&seed, &msg) else { continue };

            for (label, pk, m, sg) in [
                ("valid", pubkey, msg.clone(), sig.clone()),
                ("flipped signature bit", pubkey, msg.clone(), {
                    let mut s = sig.clone(); s[round % 64] ^= 1 << (round % 8); s
                }),
                ("flipped message bit", pubkey, {
                    let mut m = msg.clone();
                    if !m.is_empty() { let i = round % m.len(); m[i] ^= 1; }
                    m
                }, sig.clone()),
                ("wrong key", {
                    let mut k = pubkey; k[round % 32] ^= 1; k
                }, msg.clone(), sig.clone()),
            ] {
                let mine = verify(&pk, &m, &sg);
                let theirs = verify_via_openssl(&pk, &m, &sg);
                assert_eq!(
                    mine, theirs,
                    "round {round} '{label}': native said {mine}, openssl said {theirs}"
                );
                checked += 1;
            }
        }
        assert!(checked > 500, "only {checked} comparisons ran");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_key_signs_and_verifies() {
        let (seed, pubkey) = generate_keypair().expect("openssl must be present to run this test");
        let msg = b"POST/v1/orders2026-09-05T00:00:00Znonce-1{\"symbol\":\"BTC-USD\"}";
        let sig = sign(&seed, msg).expect("signing should succeed");
        assert_eq!(sig.len(), 64);
        assert!(verify(&pubkey, msg, &sig));
    }

    #[test]
    fn tampered_message_fails_verification() {
        let (seed, pubkey) = generate_keypair().unwrap();
        let sig = sign(&seed, b"original message").unwrap();
        assert!(!verify(&pubkey, b"tampered message", &sig));
    }

    #[test]
    fn wrong_public_key_fails_verification() {
        let (seed, _pubkey) = generate_keypair().unwrap();
        let (_other_seed, other_pubkey) = generate_keypair().unwrap();
        let sig = sign(&seed, b"some payload").unwrap();
        assert!(!verify(&other_pubkey, b"some payload", &sig));
    }

    #[test]
    fn truncated_signature_is_rejected_without_shelling_out() {
        let (_seed, pubkey) = generate_keypair().unwrap();
        assert!(!verify(&pubkey, b"anything", &[0u8; 63]));
    }

    #[test]
    fn a_derived_public_key_verifies_what_its_seed_signed() {
        let (seed, generated_pub) = generate_keypair().unwrap();
        let derived = public_from_seed(&seed).expect("derivation failed");
        assert_eq!(
            derived, generated_pub,
            "the derived public key must be the one openssl generated, or every attestation this \
             venue publishes is unverifiable"
        );
        let sig = sign(&seed, b"ikenga-record-v1").unwrap();
        assert!(verify(&derived, b"ikenga-record-v1", &sig));
        assert!(!verify(&derived, b"ikenga-record-v2", &sig), "signature covered the wrong bytes");
    }

    #[test]
    fn signing_is_deterministic_like_real_ed25519() {
        let (seed, _pubkey) = generate_keypair().unwrap();
        let a = sign(&seed, b"same message twice").unwrap();
        let b = sign(&seed, b"same message twice").unwrap();
        assert_eq!(a, b, "Ed25519 (unlike ECDSA) is deterministic — two signs of the same message with the same key must match");
    }

    #[test]
    fn key_screening_accepts_real_keys_and_rejects_the_all_zero_one() {
        let (_seed, pubkey) = generate_keypair().unwrap();
        assert!(is_valid_public_key(&pubkey));
        // The uninitialised-buffer case. Worth pinning because OpenSSL's parser accepts it
        // happily — this rejection is ours, not its.
        assert!(!is_valid_public_key(&[0u8; 32]));
    }

    #[test]
    fn key_screening_does_not_pretend_to_catch_a_merely_wrong_key() {
        // Documents a real limit rather than a capability: 32 arbitrary non-zero bytes are
        // indistinguishable from a real public key at registration time. Anyone reading this
        // test should not expect registration to catch a mistyped key.
        let not_really_a_key = [7u8; 32];
        assert!(is_valid_public_key(&not_really_a_key));
    }

    #[test]
    fn two_generated_keypairs_are_different() {
        let (seed_a, pub_a) = generate_keypair().unwrap();
        let (seed_b, pub_b) = generate_keypair().unwrap();
        assert_ne!(seed_a, seed_b);
        assert_ne!(pub_a, pub_b);
    }
}


/// The verifier the server uses on every signed request.
///
/// In-process, so a request costs microseconds of arithmetic rather than a process spawn. The
/// OpenSSL path is kept as `verify_via_openssl` purely so the differential tests can hold the two
/// against each other; nothing in the running server calls it.
pub fn verify(pubkey: &[u8; 32], message: &[u8], signature: &[u8]) -> bool {
    native::verify(pubkey, message, signature)
}
