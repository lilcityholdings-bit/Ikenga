//! Liquidity source adapters for the router.
//!
//! `HttpSource` is the real one: it fetches a price from an external API and turns the response
//! into a `SourceQuote`. It shells out to `curl` for the same reason `ed25519.rs` shells out to
//! `openssl` — this build has no crates.io access, and the alternative to a system binary here
//! is hand-writing a TLS stack, which is not a thing anyone should do to save a dependency.
//!
//! # What is and isn't verified here
//!
//! The sandbox this was written in has no outbound network access, so no adapter here has ever
//! spoken to a real price API. What the tests below *do* verify is everything up to that line:
//! URL construction, the subprocess call, JSON parsing, path traversal into a nested response,
//! both quote shapes, and every failure mode — by running a real HTTP server on localhost and
//! pointing a real `HttpSource` at it. That covers the parts most likely to be wrong. What it
//! cannot cover is whether any particular vendor's response looks like you expect, which is a
//! thing to check against the actual API on the first deploy, not to assume.
//!
//! # Choosing sources
//!
//! The router needs at least two that answer independently, and "independently" is doing real
//! work in that sentence: two endpoints that both read the same underlying pool are one source
//! wearing two hats, and cross-checking them proves nothing. Prefer venues with genuinely
//! separate liquidity.

use std::process::Command;

use crate::json::{self, Json};
use crate::router::{LiquiditySource, SourceQuote};

fn now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64
}

/// What the number at `price_path` in the response actually means.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum QuoteShape {
    /// The response gives a price per one unit of the sell asset. Output = rate x sell_amount.
    /// Note this assumes no depth impact, which is fine for small size and wrong for large —
    /// see the warning in `HttpSource`'s docs.
    RatePerUnit,
    /// The response gives the total output for the requested amount. Preferred: it's the only
    /// shape that reflects the venue's actual depth at that size.
    OutputAmount,
}

/// Fetches a quote from an HTTP JSON API.
///
/// **Depth warning for `RatePerUnit`:** a spot price scaled by size is not a quote for that
/// size. It ignores slippage on the venue's own book, so it will over-promise on large orders.
/// Use an API that accepts an amount and returns an output (`OutputAmount`) wherever one exists;
/// `RatePerUnit` is for feeds that only publish a mid price.
pub struct HttpSource {
    name: String,
    /// `{sell}`, `{buy}` and `{amount}` are substituted before the request.
    url_template: String,
    /// Where the number lives in the response, e.g. `["data", "price"]`.
    price_path: Vec<String>,
    shape: QuoteShape,
    timeout_secs: u64,
}

impl HttpSource {
    pub fn new(
        name: impl Into<String>,
        url_template: impl Into<String>,
        price_path: Vec<String>,
        shape: QuoteShape,
    ) -> Self {
        HttpSource {
            name: name.into(),
            url_template: url_template.into(),
            price_path,
            shape,
            timeout_secs: 3,
        }
    }

    pub fn with_timeout(mut self, secs: u64) -> Self {
        self.timeout_secs = secs;
        self
    }

    /// Asset symbols go straight into a URL, so anything that isn't a plain symbol is refused.
    /// There's no shell here to inject into (the request is built with argv, not a command
    /// string), but a symbol with a `&` or a `/` in it would silently produce a URL that means
    /// something other than intended, and a quote from the wrong pair is worse than no quote.
    fn is_safe_symbol(s: &str) -> bool {
        !s.is_empty()
            && s.len() <= 32
            && s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
    }

    fn build_url(&self, sell: &str, buy: &str, amount: f64) -> String {
        // Lowercase variants because a good number of venues route on `btcusd` and 404 on
        // `BTCUSD`. Without them those APIs simply cannot be configured, which in practice means
        // the operator has fewer sources, which means weaker cross-checking on the number that
        // decides who gets paid.
        self.url_template
            .replace("{sell_lower}", &sell.to_ascii_lowercase())
            .replace("{buy_lower}", &buy.to_ascii_lowercase())
            .replace("{sell}", sell)
            .replace("{buy}", buy)
            .replace("{amount}", &format!("{amount}"))
    }

    fn fetch(&self, url: &str) -> Option<String> {
        let out = Command::new("curl")
            .args([
                "-sS",
                "--fail",
                "--max-time",
                &self.timeout_secs.to_string(),
                "-H",
                "Accept: application/json",
                url,
            ])
            .output()
            .ok()?;
        if !out.status.success() {
            return None;
        }
        String::from_utf8(out.stdout).ok()
    }

    fn extract(&self, body: &str) -> Option<f64> {
        let parsed = json::parse(body).ok()?;
        let mut cursor = &parsed;
        for key in &self.price_path {
            cursor = match cursor {
                // A numeric segment indexes an array. Ticker endpoints very commonly answer with
                // a one-element list (`{"data":[{"last":"..."}]}`), and without this the whole
                // family of them is unconfigurable.
                Json::Array(items) => {
                    let idx: usize = key.parse().ok()?;
                    items.get(idx)?
                }
                // `*` means "the only member of this object", for vendors that key the answer by
                // their own internal name for the pair. Kraken returns
                // `{"result":{"XXBTZUSD":{...}}}` — that key is not derivable from BTC-USD by any
                // rule worth writing, and hardcoding it would make the source work for exactly one
                // pair. With `result.*.c.0` it works for all of them.
                //
                // Deliberately refuses when there is more than one member: silently picking the
                // first would make the price depend on the vendor's map ordering, which is a
                // wrong number arriving quietly rather than an error arriving loudly.
                Json::Object(fields) if key == "*" => {
                    if fields.len() != 1 {
                        return None;
                    }
                    &fields[0].1
                }
                _ => cursor.get(key)?,
            };
        }
        match cursor {
            Json::Number(n) if n.is_finite() => Some(*n),
            // Plenty of price APIs return numbers as strings. Accepting both is not laxness,
            // it's the difference between working against half the vendors and none of them.
            Json::String(s) => s.parse::<f64>().ok().filter(|n| n.is_finite()),
            _ => None,
        }
    }
}

impl LiquiditySource for HttpSource {
    fn name(&self) -> &str {
        &self.name
    }

    fn quote(&self, sell_asset: &str, buy_asset: &str, sell_amount: f64) -> Option<SourceQuote> {
        if !Self::is_safe_symbol(sell_asset) || !Self::is_safe_symbol(buy_asset) {
            return None;
        }
        let url = self.build_url(sell_asset, buy_asset, sell_amount);
        let body = self.fetch(&url)?;
        let value = self.extract(&body)?;
        if !(value > 0.0) {
            return None;
        }
        let buy_amount = match self.shape {
            QuoteShape::RatePerUnit => value * sell_amount,
            QuoteShape::OutputAmount => value,
        };
        Some(SourceQuote {
            source: self.name.clone(),
            sell_amount,
            buy_amount,
            // The quote is as of the moment it was fetched. The router's staleness check is what
            // makes this matter: a source that takes three seconds to answer produces a quote
            // that is already three seconds old by the time anyone can act on it.
            as_of_ms: now_ms(),
        })
    }
}

/// A fixed-rate source. **Demo and tests only** — it invents a price with no market behind it.
///
/// Wired into a live deployment this would be actively harmful: it always answers, always looks
/// fresh, and always agrees with itself, which is exactly the profile the router's outlier check
/// cannot protect anyone from. `main.rs` refuses to install it when `IKENGA_ENV=production`.
pub struct FixedRateSource {
    name: String,
    rate: f64,
}

impl FixedRateSource {
    pub fn new(name: impl Into<String>, rate: f64) -> Self {
        FixedRateSource { name: name.into(), rate }
    }
}

impl LiquiditySource for FixedRateSource {
    fn name(&self) -> &str {
        &self.name
    }

    fn quote(&self, _sell: &str, _buy: &str, sell_amount: f64) -> Option<SourceQuote> {
        Some(SourceQuote {
            source: self.name.clone(),
            sell_amount,
            buy_amount: self.rate * sell_amount,
            as_of_ms: now_ms(),
        })
    }
}

/// Builds sources from an env-var spec, so a deployment can add venues without a rebuild.
///
/// Format — semicolon-separated sources, pipe-separated fields:
///   `name|url_template|json.path.to.number|rate|out`
/// where the last field is `rate` (price per unit) or `out` (total output). Example:
///   `IKENGA_ROUTE_SOURCES=venue_a|https://a.test/q?f={sell}&t={buy}&a={amount}|data.out|out;\
///    venue_b|https://b.test/price?p={sell}{buy}|price|rate`
///
/// Returns the sources it could build plus a human-readable problem for each one it couldn't.
/// A malformed entry is skipped and reported rather than failing startup: losing one venue
/// should degrade routing (fewer sources to cross-check), not take the whole service down.

/// Ready-made source definitions for public price APIs that need no account and no key.
///
/// # Why these exist
///
/// The pipe-delimited form is fully general and genuinely hard to get right by hand — and the
/// thing it configures is the oracle that decides who gets paid. A typo in a JSON path does not
/// fail loudly; it produces a source that never answers, which quietly halves your
/// cross-checking and leaves the median resting on one venue. Presets turn the most common cases
/// into a name.
///
/// Every entry here is keyless and public. None of them are endorsed or guaranteed — an API can
/// change shape or start refusing traffic at any time, which is exactly why the router demands
/// agreement between at least two of them and why `--check-sources` exists to tell you which
/// ones are actually answering today.
pub const PRESETS: &[(&str, &str)] = &[
    // {"data":{"amount":"64090.00","base":"BTC","currency":"USD"}}
    (
        "coinbase",
        "coinbase|https://api.coinbase.com/v2/prices/{sell}-{buy}/spot|data.amount|rate",
    ),
    // {"symbol":"BTCUSDT","price":"64090.00"} — quote is usually USDT, so pair as BTC-USDT.
    (
        "binance",
        "binance|https://api.binance.com/api/v3/ticker/price?symbol={sell}{buy}|price|rate",
    ),
    // {"last":"64090","high":...} — lowercase pair in the path.
    (
        "bitstamp",
        "bitstamp|https://www.bitstamp.net/api/v2/ticker/{sell_lower}{buy_lower}/|last|rate",
    ),
    // {"code":"0","data":[{"instId":"BTC-USDT","last":"64090",...}]}
    (
        "okx",
        "okx|https://www.okx.com/api/v5/market/ticker?instId={sell}-{buy}|data.0.last|rate",
    ),
    // {"symbol":"BTC-USD","price":"64090.00",...}
    (
        "coinbase_exchange",
        "coinbase_exchange|https://api.exchange.coinbase.com/products/{sell}-{buy}/ticker|price|rate",
    ),
    // {"bid":"78506.70","ask":"78506.71","last":"78531.37","volume":{...}} — verified live.
    (
        "gemini",
        "gemini|https://api.gemini.com/v1/pubticker/{sell_lower}{buy_lower}|last|rate",
    ),
    // A bare array; index 6 is the last traded price. Verified against a live response:
    // [bid, bid_size, ask, ask_size, chg, chg_pct, LAST, volume, high, low].
    (
        "bitfinex",
        "bitfinex|https://api-pub.bitfinex.com/v2/ticker/t{sell}{buy}|6|rate",
    ),
    // {"error":[],"result":{"XXBTZUSD":{"c":["78754.00","0.0007"],...}}} — the pair key is
    // Kraken's own name for the market, hence the `*` segment. `c` is the last trade; `c.0` is
    // its price. Verified live.
    (
        "kraken",
        "kraken|https://api.kraken.com/0/public/Ticker?pair={sell}{buy}|result.*.c.0|rate",
    ),
];

/// What `IKENGA_ROUTE_SOURCES` uses when the operator has not chosen.
///
/// # Why not the obvious two
///
/// The first default here was `coinbase; binance`, which is the pair anyone would name — and it
/// is broken on the most likely deployment there is. Binance.com does not serve the United
/// States, so on a US server one of the two returns nothing, the router is left with a single
/// usable quote, `min_sources` is 2, and **no market ever settles**. Everything else works
/// perfectly: markets open, bets are accepted, and then nothing resolves, which is the worst
/// possible failure for a venue that exists to resolve things.
///
/// These four are all reachable from the US and from most of Europe, and they are four rather
/// than two so that one being down, rate-limited or geo-blocked still leaves the two the router
/// needs to cross-check. Run `./ikenga sources` on the actual machine before trusting it — the
/// only opinion that counts is that server's.
pub const DEFAULT_SOURCES: &str = "coinbase; kraken; gemini; bitstamp";

/// Expands preset names in a spec, leaving full pipe-delimited entries untouched.
///
/// So `coinbase;okx` and the two full definitions are the same configuration, and the two forms
/// can be mixed — a preset for the easy venues, a hand-written entry for one that isn't covered.
pub fn expand_presets(spec: &str) -> (String, Vec<String>) {
    let mut out: Vec<String> = Vec::new();
    let mut problems = Vec::new();
    for entry in spec.split(';').map(str::trim).filter(|e| !e.is_empty()) {
        if entry.contains('|') {
            out.push(entry.to_string());
            continue;
        }
        let key = entry.to_ascii_lowercase();
        match PRESETS.iter().find(|(name, _)| *name == key) {
            Some((_, definition)) => out.push((*definition).to_string()),
            None => problems.push(format!(
                "'{entry}' is not a known source. Presets: {}. Or write the full \
                 name|url|json.path|rate form.",
                PRESETS.iter().map(|(n, _)| *n).collect::<Vec<_>>().join(", ")
            )),
        }
    }
    (out.join(";"), problems)
}

pub fn sources_from_spec(spec: &str) -> (Vec<Box<dyn LiquiditySource>>, Vec<String>) {
    let mut sources: Vec<Box<dyn LiquiditySource>> = Vec::new();
    let (spec, mut problems) = expand_presets(spec);
    let spec = spec.as_str();

    for (i, entry) in spec.split(';').map(str::trim).filter(|e| !e.is_empty()).enumerate() {
        let fields: Vec<&str> = entry.split('|').map(str::trim).collect();
        if fields.len() != 4 {
            problems.push(format!(
                "source #{}: expected 4 pipe-separated fields (name|url|json.path|rate\\|out), got {}",
                i + 1,
                fields.len()
            ));
            continue;
        }
        let (name, url, path, shape_raw) = (fields[0], fields[1], fields[2], fields[3]);
        if name.is_empty() || url.is_empty() || path.is_empty() {
            problems.push(format!("source #{}: name, url and json path must all be non-empty", i + 1));
            continue;
        }
        // Either casing counts. Checking only the upper-case form silently rejected every
        // lower-case-pair venue — including one of the presets shipped right here, which is how
        // this was found.
        let has_sell = url.contains("{sell}") || url.contains("{sell_lower}");
        let has_buy = url.contains("{buy}") || url.contains("{buy_lower}");
        if !has_sell || !has_buy {
            problems.push(format!(
                "source #{name}: url must contain {{sell}} and {{buy}} placeholders, or every \
                 pair would be quoted with the same URL"
            ));
            continue;
        }
        let shape = match shape_raw {
            "rate" => QuoteShape::RatePerUnit,
            "out" => QuoteShape::OutputAmount,
            other => {
                problems.push(format!("source #{name}: shape must be 'rate' or 'out', got '{other}'"));
                continue;
            }
        };
        let json_path: Vec<String> = path.split('.').map(|s| s.to_string()).collect();
        sources.push(Box::new(HttpSource::new(name, url, json_path, shape)));
    }

    (sources, problems)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Real bodies, captured from the live endpoints on 2026-09-09. Parsing a shape someone
    /// remembered is how a source silently returns nothing on the day it matters; these are what
    /// the vendors actually sent.
    fn preset(name: &str) -> HttpSource {
        let (_, def) = PRESETS.iter().find(|(n, _)| *n == name).expect("preset exists");
        let f: Vec<&str> = def.split('|').collect();
        HttpSource {
            name: f[0].to_string(),
            url_template: f[1].to_string(),
            price_path: f[2].split('.').map(|x| x.to_string()).collect(),
            shape: QuoteShape::RatePerUnit,
            timeout_secs: 5,
        }
    }

    #[test]
    fn gemini_live_body_parses() {
        let body = r#"{"bid":"78506.70000","ask":"78506.71000","last":"78531.37000","volume":{"BTC":"76.83286898","USD":"6033790.4620299026","timestamp":1788856560000}}"#;
        assert_eq!(preset("gemini").extract(body), Some(78531.37));
    }

    #[test]
    fn bitfinex_live_body_parses() {
        // Bare array; index 6 is the last traded price, not the bid at index 0.
        let body = "[78995,3.49078624,79026,3.1115484,-764,-0.00957513,79026,1114.8989299,80010,78845,1358182043000]";
        assert_eq!(preset("bitfinex").extract(body), Some(79026.0));
    }

    #[test]
    fn kraken_live_body_parses_through_the_wildcard() {
        let body = r#"{"error":[],"result":{"XXBTZUSD":{"a":["78754.00000","1","1.000"],"b":["78753.90000","1","1.000"],"c":["78754.00000","0.00076909"],"v":["45.57085330","2047.52799798"],"o":"78449.60000"}}}"#;
        assert_eq!(preset("kraken").extract(body), Some(78754.0));
    }

    #[test]
    fn a_wildcard_refuses_an_ambiguous_object() {
        // Two markets in one response: picking either would make the price depend on the
        // vendor's key ordering. Returning nothing is the only honest answer.
        let two = r#"{"error":[],"result":{"XXBTZUSD":{"c":["1","2"]},"XETHZUSD":{"c":["3","4"]}}}"#;
        assert_eq!(preset("kraken").extract(two), None);
        let none = r#"{"error":["EQuery:Unknown asset pair"],"result":{}}"#;
        assert_eq!(preset("kraken").extract(none), None);
    }

    #[test]
    fn every_preset_builds_a_url_that_names_the_pair() {
        for (name, _) in PRESETS {
            let src = preset(name);
            let url = src.build_url("BTC", "USD", 1.0);
            assert!(!url.contains('{'), "{name} left a placeholder unfilled: {url}");
            assert!(
                url.to_ascii_lowercase().contains("btc"),
                "{name} built a url with no pair in it: {url}"
            );
            assert!(url.starts_with("https://"), "{name} is not https: {url}");
        }
    }

    #[test]
    fn the_default_sources_all_resolve_and_are_enough_to_cross_check() {
        let (sources, problems) = sources_from_spec(DEFAULT_SOURCES);
        assert!(problems.is_empty(), "default sources do not parse: {problems:?}");
        assert!(
            sources.len() >= crate::router::RouterConfig::default().min_sources + 1,
            "the defaults leave no margin: one source down and nothing settles"
        );
        // The specific failure this guards: a default that cannot be reached from the US.
        let names: Vec<&str> = sources.iter().map(|s| s.name()).collect();
        assert!(
            !names.contains(&"binance") && !names.contains(&"okx"),
            "default sources include one that does not serve the US: {names:?}"
        );
    }
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    /// Serves one canned response on a free port and returns its URL base. Real HTTP over a real
    /// socket, so the adapter's curl invocation and parsing are genuinely exercised — the only
    /// thing missing versus a vendor call is the vendor.
    fn serve_once(body: &'static str, status_line: &'static str) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind a port");
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut buf = [0u8; 2048];
                let _ = stream.read(&mut buf); // drain the request line/headers
                let resp = format!(
                    "{status_line}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
                     Connection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(resp.as_bytes());
            }
        });
        format!("http://127.0.0.1:{port}")
    }

    #[test]
    fn parses_a_nested_output_amount_over_real_http() {
        let base = serve_once(r#"{"data":{"out":"63950.25"},"ok":true}"#, "HTTP/1.1 200 OK");
        let src = HttpSource::new(
            "test_venue",
            format!("{base}/quote?from={{sell}}&to={{buy}}&amount={{amount}}"),
            vec!["data".into(), "out".into()],
            QuoteShape::OutputAmount,
        );
        let q = src.quote("BTC", "USD", 1.0).expect("should parse a quote");
        assert_eq!(q.source, "test_venue");
        assert!((q.buy_amount - 63_950.25).abs() < 1e-6);
        assert_eq!(q.sell_amount, 1.0);
    }

    #[test]
    fn rate_per_unit_is_multiplied_by_size() {
        let base = serve_once(r#"{"price":2000.0}"#, "HTTP/1.1 200 OK");
        let src = HttpSource::new(
            "rate_venue",
            format!("{base}/p?s={{sell}}&b={{buy}}&a={{amount}}"),
            vec!["price".into()],
            QuoteShape::RatePerUnit,
        );
        let q = src.quote("ETH", "USD", 3.0).expect("should parse a quote");
        assert!((q.buy_amount - 6_000.0).abs() < 1e-6);
    }

    #[test]
    fn an_http_error_is_no_quote_rather_than_a_zero() {
        let base = serve_once(r#"{"error":"rate limited"}"#, "HTTP/1.1 429 Too Many Requests");
        let src = HttpSource::new(
            "flaky",
            format!("{base}/q?s={{sell}}&b={{buy}}&a={{amount}}"),
            vec!["price".into()],
            QuoteShape::OutputAmount,
        );
        assert!(src.quote("BTC", "USD", 1.0).is_none());
    }

    #[test]
    fn a_missing_field_is_no_quote() {
        let base = serve_once(r#"{"something_else":1}"#, "HTTP/1.1 200 OK");
        let src = HttpSource::new(
            "wrong_shape",
            format!("{base}/q?s={{sell}}&b={{buy}}&a={{amount}}"),
            vec!["data".into(), "price".into()],
            QuoteShape::OutputAmount,
        );
        assert!(src.quote("BTC", "USD", 1.0).is_none());
    }

    #[test]
    fn a_zero_or_negative_price_is_no_quote() {
        let base = serve_once(r#"{"price":0}"#, "HTTP/1.1 200 OK");
        let src = HttpSource::new(
            "zero",
            format!("{base}/q?s={{sell}}&b={{buy}}&a={{amount}}"),
            vec!["price".into()],
            QuoteShape::OutputAmount,
        );
        assert!(src.quote("BTC", "USD", 1.0).is_none());
    }

    #[test]
    fn unreachable_host_is_no_quote_not_a_panic() {
        // Nothing is listening on this port. The adapter must degrade to "one fewer source".
        let src = HttpSource::new(
            "dead",
            "http://127.0.0.1:1/q?s={sell}&b={buy}&a={amount}",
            vec!["price".into()],
            QuoteShape::OutputAmount,
        )
        .with_timeout(1);
        assert!(src.quote("BTC", "USD", 1.0).is_none());
    }

    #[test]
    fn odd_symbols_are_refused_before_a_request_is_made() {
        let src = HttpSource::new(
            "guarded",
            "http://127.0.0.1:1/q?s={sell}&b={buy}&a={amount}",
            vec!["price".into()],
            QuoteShape::OutputAmount,
        );
        assert!(src.quote("BTC&evil=1", "USD", 1.0).is_none());
        assert!(src.quote("BTC", "../../etc", 1.0).is_none());
        assert!(src.quote("", "USD", 1.0).is_none());
    }

    #[test]
    fn spec_parser_builds_good_sources_and_reports_bad_ones() {
        let (sources, problems) = sources_from_spec(
            "good_a|https://a.test/q?f={sell}&t={buy}&a={amount}|data.out|out;\
             good_b|https://b.test/p?f={sell}&t={buy}|price|rate;\
             missing_fields|https://c.test|price;\
             no_placeholders|https://d.test/fixed|price|out;\
             bad_shape|https://e.test/q?f={sell}&t={buy}|price|sideways",
        );
        assert_eq!(sources.len(), 2);
        assert_eq!(sources[0].name(), "good_a");
        assert_eq!(sources[1].name(), "good_b");
        assert_eq!(problems.len(), 3, "each malformed entry should be reported: {problems:?}");
        assert!(problems.iter().any(|p| p.contains("no_placeholders")));
        assert!(problems.iter().any(|p| p.contains("bad_shape")));
    }

    #[test]
    fn an_empty_spec_yields_no_sources_and_no_complaints() {
        let (sources, problems) = sources_from_spec("");
        assert!(sources.is_empty());
        assert!(problems.is_empty());
    }

    #[test]
    fn url_template_substitutes_all_three_placeholders() {
        let src = HttpSource::new(
            "t",
            "https://x.test/q?s={sell}&b={buy}&a={amount}",
            vec!["p".into()],
            QuoteShape::OutputAmount,
        );
        assert_eq!(src.build_url("BTC", "USD", 0.5), "https://x.test/q?s=BTC&b=USD&a=0.5");
    }

    #[test]
    fn lowercase_pair_urls_are_accepted_and_substituted() {
        let (sources, problems) = sources_from_spec(
            "lo|https://x.test/{sell_lower}{buy_lower}/|last|rate",
        );
        assert!(problems.is_empty(), "{problems:?}");
        assert_eq!(sources.len(), 1);
        let src = HttpSource {
            name: "lo".into(),
            url_template: "https://x.test/t/{sell_lower}{buy_lower}/?u={sell}".into(),
            price_path: vec!["last".into()],
            shape: QuoteShape::RatePerUnit,
            timeout_secs: 5,
        };
        assert_eq!(src.build_url("BTC", "USD", 1.0), "https://x.test/t/btcusd/?u=BTC");
    }

    #[test]
    fn a_url_with_no_pair_placeholder_at_all_is_still_refused() {
        // The check was loosened to accept lower-case forms; it must not have been loosened into
        // accepting a fixed URL that would quote every pair identically.
        let (_, problems) = sources_from_spec("fixed|https://x.test/always|price|rate");
        assert_eq!(problems.len(), 1, "a placeholder-free url must still be rejected");
    }

    #[test]
    fn numeric_path_segments_index_into_arrays() {
        let src = HttpSource {
            name: "okx".into(),
            url_template: "https://x.test?i={sell}-{buy}".into(),
            price_path: vec!["data".into(), "0".into(), "last".into()],
            shape: QuoteShape::RatePerUnit,
            timeout_secs: 5,
        };
        assert_eq!(
            src.extract(r#"{"code":"0","data":[{"last":"64080.25"}]}"#),
            Some(64080.25)
        );
        // Out of range is a miss, not a panic.
        assert_eq!(src.extract(r#"{"data":[]}"#), None);
        // A non-numeric segment against an array is a miss, not a panic.
        let bad = HttpSource {
            price_path: vec!["data".into(), "nope".into()],
            ..src
        };
        assert_eq!(bad.extract(r#"{"data":[{"last":"1"}]}"#), None);
    }

    #[test]
    fn every_shipped_preset_parses_into_a_usable_source() {
        // A preset that does not parse is worse than no preset: the operator believes they
        // configured a source and the router silently has one fewer.
        for (name, _) in PRESETS {
            let (sources, problems) = sources_from_spec(name);
            assert!(problems.is_empty(), "preset {name}: {problems:?}");
            assert_eq!(sources.len(), 1, "preset {name} produced {} sources", sources.len());
            assert_eq!(sources[0].name(), *name);
        }
    }

    #[test]
    fn presets_and_hand_written_entries_mix() {
        let (sources, problems) =
            sources_from_spec("coinbase;mine|https://x.test/q?f={sell}&t={buy}|price|rate;okx");
        assert!(problems.is_empty(), "{problems:?}");
        assert_eq!(sources.len(), 3);
    }

    #[test]
    fn an_unknown_preset_name_says_what_is_available() {
        let (sources, problems) = sources_from_spec("coinbase;nosuchvenue");
        assert_eq!(sources.len(), 1, "the good one must survive");
        assert_eq!(problems.len(), 1);
        assert!(problems[0].contains("coinbase"), "should list the presets: {}", problems[0]);
    }
}
