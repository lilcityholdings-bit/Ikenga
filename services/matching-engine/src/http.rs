//! Minimal hand-rolled HTTP/1.1 request parsing + response writing over `std::net::TcpStream`.
//! Stands in for axum/hyper (see the dependency note in Cargo.toml). Only supports what this
//! project needs: request-line + headers + a Content-Length body, and writing back a status
//! line + headers + body. Supports HTTP/1.1 keep-alive (many sequential requests per
//! connection). No chunked transfer-encoding, and no *pipelining* — a client must read each
//! response before sending the next request on the same connection.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;

use crate::json::Json;

pub struct Request {
    pub method: String,
    pub path: String,
    pub query: HashMap<String, String>,
    pub headers: HashMap<String, String>, // lowercased keys
    pub body: Vec<u8>,
    /// The request target exactly as it came off the wire, query string included and nothing
    /// decoded. Endpoints whose parameters live in the query string sign against this rather
    /// than `path`, so those parameters are actually covered by the signature — signing a
    /// stripped path would leave `?amount=...` free for anyone in the middle to rewrite.
    pub raw_target: String,
    /// Who the connection is from, as the OS reports it. Used only to rate-limit the endpoints
    /// that have no agent to attribute a call to (registration, trial quotes).
    ///
    /// Behind a proxy — which is the recommended deployment, see docs/DEPLOY.md — every request
    /// arrives from the proxy's address, so this collapses to a single bucket and the limits
    /// below stop distinguishing callers. `X-Forwarded-For` is deliberately NOT trusted here:
    /// it's a client-supplied header, so honouring it unconditionally would let anyone mint a
    /// fresh identity per request and defeat the limit entirely. Doing this properly means
    /// trusting that header only from known proxy addresses, which needs configuration this
    /// build doesn't have yet.
    pub peer_ip: String,
}

impl Request {
    /// The address to attribute this request to for rate limiting.
    ///
    /// Normally the socket's own address. When the socket address is a *configured* trusted proxy,
    /// the real client is taken from `X-Forwarded-For` instead.
    ///
    /// # Why this had to exist before the HTTPS advice was any good
    ///
    /// Every recommended way to put this venue on the internet — a Cloudflare tunnel, Caddy, nginx
    /// — terminates the connection itself, so every visitor arrives from one address. With the
    /// per-IP registration cap that meant the whole world shared a single bucket: the sixth person
    /// to open the betting page in an hour was refused, and so was everyone after them, forever.
    /// A limit meant to stop one farm was instead stopping every real user, and it would have
    /// looked like the venue was simply broken.
    ///
    /// # Why the header is not just trusted
    ///
    /// `X-Forwarded-For` is client-supplied. Honouring it unconditionally would let anyone mint a
    /// fresh bucket per request by inventing an address, which is strictly worse than having no
    /// limit at all, because it would look like one was working.
    ///
    /// So it is honoured only when the connection itself came from an address the operator listed
    /// in `IKENGA_TRUSTED_PROXIES`. The list is walked from the right — the end a trusted proxy
    /// appends to — skipping entries that are themselves trusted, and the first untrusted address
    /// found is the client. Anything a client prepended sits to the left of that and is ignored.
    pub fn client_ip(&self) -> String {
        if !proxy_is_trusted(&self.peer_ip) {
            return self.peer_ip.clone();
        }
        let Some(xff) = self.header("x-forwarded-for") else {
            return self.peer_ip.clone();
        };
        for hop in xff.rsplit(',').map(str::trim) {
            // Note `listed_proxy`, not `proxy_is_trusted`: the `all` setting means "whoever
            // connected to us is a proxy", never "every address in this header is a proxy". The
            // first version used the same predicate for both, so with `all` every hop looked like
            // a proxy, all of them were skipped, and it silently fell back to the tunnel's own
            // address — which is precisely the bug it was written to fix, still present and now
            // invisible.
            if hop.is_empty() || listed_proxy(hop) {
                continue;
            }
            // Bounded and filtered: this string becomes a rate-limiter key, and a caller that can
            // make it arbitrarily long or arbitrarily varied can exhaust the limiter's memory.
            if hop.len() <= 45 && hop.chars().all(|c| c.is_ascii_hexdigit() || ".:".contains(c)) {
                return hop.to_string();
            }
        }
        self.peer_ip.clone()
    }

    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.get(&name.to_ascii_lowercase()).map(|s| s.as_str())
    }

    pub fn query_param(&self, name: &str) -> Option<&str> {
        self.query.get(name).map(|s| s.as_str())
    }

    /// What a client must sign for this request: path plus query string, byte for byte.
    pub fn signing_path(&self) -> &str {
        &self.raw_target
    }
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                if let Ok(byte) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                    out.push(byte);
                    i += 3;
                    continue;
                }
                out.push(bytes[i]);
                i += 1;
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

fn parse_query(qs: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for pair in qs.split('&') {
        if pair.is_empty() {
            continue;
        }
        let mut parts = pair.splitn(2, '=');
        let k = percent_decode(parts.next().unwrap_or(""));
        let v = percent_decode(parts.next().unwrap_or(""));
        map.insert(k, v);
    }
    map
}

/// Reads and parses one HTTP request from a *persistent* reader. Returns Ok(None) on a clean
/// EOF before any bytes were read (idle connection closed).
///
/// Takes the `BufReader` by reference rather than constructing one per call, which is what
/// makes keep-alive possible at all: a fresh `BufReader` per request would discard any bytes of
/// the *next* pipelined request already sitting in the buffer.
pub fn read_request(reader: &mut BufReader<&TcpStream>) -> std::io::Result<Option<Request>> {
    read_request_from(reader, String::new())
}

/// As `read_request`, but records the peer address the connection came from.
/// Largest request body accepted, in bytes.
///
/// Without a ceiling here, `vec![0u8; content_length]` sizes an allocation directly from an
/// attacker-supplied header. `Content-Length: 1152921504606846976` on an unauthenticated POST
/// asks for an exabyte; the allocator refuses, and a failed allocation in Rust is an **abort**,
/// not an error you can catch. Eighty bytes, no body, no credentials, and the whole process is
/// gone — every other connection with it. This is the single cheapest way to kill the service,
/// so the limit is checked before a single byte is reserved.
pub const MAX_BODY_BYTES: usize = 1 << 20; // 1 MiB

/// Longest request line or header line accepted. `read_line` grows a String until it meets a
/// newline, so without this a client that never sends one is an unbounded memory sink that the
/// idle timeout does not save you from — the bytes keep arriving, they just never terminate.
pub const MAX_LINE_BYTES: u64 = 16 * 1024;

/// Most header lines accepted on one request. The header loop otherwise ends only at a blank
/// line or EOF, so a client can stream distinct headers into a HashMap indefinitely.
pub const MAX_HEADERS: usize = 100;

/// Reads one line, refusing to grow past `MAX_LINE_BYTES`.
///
/// Returns `Ok(None)` at EOF. An over-length line is an error rather than a truncation: silently
/// truncating a request line would change which route is matched.
fn read_line_capped(
    reader: &mut BufReader<&TcpStream>,
    out: &mut String,
) -> std::io::Result<Option<usize>> {
    use std::io::Read as _;
    let n = reader.by_ref().take(MAX_LINE_BYTES).read_line(out)?;
    if n == 0 {
        return Ok(None);
    }
    if !out.ends_with('\n') && n as u64 == MAX_LINE_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "header line too long",
        ));
    }
    Ok(Some(n))
}

pub fn read_request_from(
    reader: &mut BufReader<&TcpStream>,
    peer_ip: String,
) -> std::io::Result<Option<Request>> {
    let mut request_line = String::new();
    let bytes_read = match read_line_capped(reader, &mut request_line)? {
        Some(n) => n,
        None => return Ok(None),
    };
    if bytes_read == 0 {
        return Ok(None);
    }
    let request_line = request_line.trim_end();
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let target = parts.next().unwrap_or("").to_string();
    if method.is_empty() || target.is_empty() {
        return Ok(None);
    }

    let raw_target = target.clone();
    let (path, query) = match target.split_once('?') {
        Some((p, q)) => (p.to_string(), parse_query(q)),
        None => (target, HashMap::new()),
    };

    let mut headers = HashMap::new();
    let mut header_count = 0usize;
    loop {
        let mut line = String::new();
        let n = match read_line_capped(reader, &mut line)? {
            Some(n) => n,
            None => break,
        };
        if n == 0 {
            break;
        }
        let line = line.trim_end();
        if line.is_empty() {
            break;
        }
        header_count += 1;
        if header_count > MAX_HEADERS {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "too many headers",
            ));
        }
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
        }
    }

    // A body this server will not read is a body it must not size an allocation from. Note the
    // order: the length is validated *before* `vec![0u8; ..]`, because the allocation is the
    // thing that kills the process.
    let declared = headers.get("content-length").and_then(|v| v.parse::<u64>().ok()).unwrap_or(0);
    if declared > MAX_BODY_BYTES as u64 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "request body too large",
        ));
    }
    let content_length = declared as usize;
    let mut body = vec![0u8; content_length];
    if content_length > 0 {
        reader.read_exact(&mut body)?;
    }

    Ok(Some(Request { method, path: percent_decode(&path), query, headers, body, raw_target, peer_ip }))
}

/// Whether an address is a proxy the operator told us to believe.
///
/// `IKENGA_TRUSTED_PROXIES=all` is the right setting when the only way in is through one tunnel or
/// load balancer, which is the common case; anything else is a comma-separated list of addresses.
/// Unset means trust nothing, which is correct for a venue exposed directly.
fn proxy_is_trusted(addr: &str) -> bool {
    matches!(std::env::var("IKENGA_TRUSTED_PROXIES"), Ok(ref v) if v.trim().eq_ignore_ascii_case("all"))
        || listed_proxy(addr)
}

/// Whether this address appears explicitly in the trusted-proxy list. Never true merely because
/// the list says `all` — see the note in `client_ip`.
fn listed_proxy(addr: &str) -> bool {
    match std::env::var("IKENGA_TRUSTED_PROXIES") {
        Ok(v) => {
            let v = v.trim();
            if v.eq_ignore_ascii_case("all") {
                return false;
            }
            v.split(',').map(str::trim).any(|p| !p.is_empty() && p == addr)
        }
        Err(_) => false,
    }
}

pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        409 => "Conflict",
        422 => "Unprocessable Entity",
        413 => "Payload Too Large",
        425 => "Too Early",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        503 => "Service Unavailable",
        101 => "Switching Protocols",
        _ => "Unknown",
    }
}

impl Response {
    /// Serves an already-serialised JSON body. Used by the feed cache, which stores the
    /// rendered string rather than re-walking the object graph on every hit.
    pub fn json_str(status: u16, body: String) -> Response {
        Response {
            status,
            headers: vec![("Content-Type".to_string(), "application/json".to_string())],
            body: body.into_bytes(),
        }
    }

    pub fn json(status: u16, json: &Json) -> Response {
        let body = json.to_string().into_bytes();
        Response {
            status,
            headers: vec![("Content-Type".to_string(), "application/json".to_string())],
            body,
        }
    }

    /// Serves a UTF-8 HTML page. `nosniff` plus an explicit charset because the only HTML this
    /// service serves is the operator console, and a mis-sniffed console is a console that can be
    /// made to run something else.
    pub fn html(status: u16, body: &str) -> Response {
        Response {
            status,
            headers: vec![
                ("Content-Type".to_string(), "text/html; charset=utf-8".to_string()),
                ("X-Content-Type-Options".to_string(), "nosniff".to_string()),
                ("Referrer-Policy".to_string(), "no-referrer".to_string()),
                (
                    "Content-Security-Policy".to_string(),
                    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; \
                     connect-src 'self'"
                        .to_string(),
                ),
            ],
            body: body.as_bytes().to_vec(),
        }
    }

    /// Writes the response. `keep_alive` controls whether the connection is announced as
    /// reusable — every response used to say `Connection: close`, which forced a fresh TCP
    /// handshake *and* a fresh server thread for every single order. For an agent submitting
    /// thousands of orders that was the dominant cost in the whole request path.
    /// Takes `&TcpStream`, not `&mut` — std implements `Write` for `&TcpStream`, which lets the
    /// read side hold a `BufReader<&TcpStream>` over the same socket at the same time. The
    /// alternative (`try_clone`) costs a dup(2) syscall on every connection.
    pub fn write_to(&self, mut stream: &TcpStream, keep_alive: bool) -> std::io::Result<()> {
        let mut head = format!(
            "HTTP/1.1 {} {}\r\n",
            self.status,
            reason_phrase(self.status)
        );
        for (k, v) in &self.headers {
            head.push_str(&format!("{k}: {v}\r\n"));
        }
        head.push_str(&format!("Content-Length: {}\r\n", self.body.len()));
        head.push_str(if keep_alive {
            "Connection: keep-alive\r\n\r\n"
        } else {
            "Connection: close\r\n\r\n"
        });
        // One write syscall instead of two. At small body sizes the header and body are each
        // well under a segment, so writing them separately meant two syscalls (and, without
        // TCP_NODELAY, potentially two packets) per response.
        let mut out = Vec::with_capacity(head.len() + self.body.len());
        out.extend_from_slice(head.as_bytes());
        out.extend_from_slice(&self.body);
        stream.write_all(&out)?;
        stream.flush()
    }
}
#[cfg(test)]
mod proxy_tests {
    use super::*;
    use std::collections::HashMap;

    fn req(peer: &str, xff: Option<&str>) -> Request {
        let mut headers = HashMap::new();
        if let Some(v) = xff {
            headers.insert("x-forwarded-for".to_string(), v.to_string());
        }
        Request {
            method: "GET".into(),
            path: "/".into(),
            query: HashMap::new(),
            headers,
            body: Vec::new(),
            raw_target: "/".into(),
            peer_ip: peer.to_string(),
        }
    }

    /// Env is process-global, so these run as one test rather than racing each other.
    #[test]
    fn forwarded_addresses_are_believed_only_from_a_configured_proxy() {
        std::env::remove_var("IKENGA_TRUSTED_PROXIES");
        assert_eq!(
            req("203.0.113.9", Some("1.2.3.4")).client_ip(),
            "203.0.113.9",
            "an unconfigured venue must ignore the header — believing it lets anyone mint a \
             fresh rate-limit bucket per request"
        );

        std::env::set_var("IKENGA_TRUSTED_PROXIES", "all");
        assert_eq!(
            req("10.0.0.1", Some("198.51.100.7")).client_ip(),
            "198.51.100.7",
            "behind a tunnel every visitor otherwise shares one bucket and the sixth is refused"
        );
        assert_eq!(
            req("10.0.0.1", Some("1.2.3.4, 5.6.7.8, 198.51.100.7")).client_ip(),
            "198.51.100.7",
            "the client's own prepended hops sit to the left and must be ignored"
        );
        assert_eq!(
            req("10.0.0.1", None).client_ip(),
            "10.0.0.1",
            "no header, no change"
        );
        assert_eq!(
            req("10.0.0.1", Some("not an ip; drop table")).client_ip(),
            "10.0.0.1",
            "a junk hop must not become a rate-limiter key"
        );
        assert_eq!(
            req("10.0.0.1", Some(&"9".repeat(500))).client_ip(),
            "10.0.0.1",
            "an unbounded hop must not become a rate-limiter key either"
        );

        std::env::set_var("IKENGA_TRUSTED_PROXIES", "10.0.0.1");
        assert_eq!(
            req("10.0.0.1", Some("198.51.100.7")).client_ip(),
            "198.51.100.7",
            "an explicitly listed proxy is believed"
        );
        assert_eq!(
            req("10.0.0.2", Some("198.51.100.7")).client_ip(),
            "10.0.0.2",
            "a proxy that is not on the list is not"
        );
        // Chained proxies: the last untrusted hop is the client.
        std::env::set_var("IKENGA_TRUSTED_PROXIES", "10.0.0.1,10.0.0.2");
        assert_eq!(
            req("10.0.0.1", Some("198.51.100.7, 10.0.0.2")).client_ip(),
            "198.51.100.7"
        );
        std::env::remove_var("IKENGA_TRUSTED_PROXIES");
    }
}

