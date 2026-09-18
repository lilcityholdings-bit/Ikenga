//! Minimal hand-rolled JSON value, parser and serializer — a stand-in for serde_json (see the
//! dependency note at the top of Cargo.toml). Supports the subset of JSON this project actually
//! needs: objects, arrays, strings, numbers, bools, null. Basic escape sequences only — no
//! `\uXXXX` unicode escape decoding. Good enough for a request/response API; not a general-
//! purpose JSON library.

use std::fmt::Write as _;

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Number(f64),
    String(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
}

impl Json {
    pub fn obj(pairs: Vec<(&str, Json)>) -> Json {
        Json::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }

    pub fn str(s: impl Into<String>) -> Json {
        Json::String(s.into())
    }

    pub fn num(n: f64) -> Json {
        Json::Number(n)
    }

    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Object(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::String(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Json::Number(n) => Some(*n),
            _ => None,
        }
    }

    #[allow(dead_code)]
    pub fn as_array(&self) -> Option<&Vec<Json>> {
        match self {
            Json::Array(a) => Some(a),
            _ => None,
        }
    }

    pub fn to_string(&self) -> String {
        let mut out = String::new();
        self.write(&mut out);
        out
    }

    fn write(&self, out: &mut String) {
        match self {
            Json::Null => out.push_str("null"),
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Number(n) => {
                // NaN and infinity have no JSON representation. Writing them bare produces a
                // document nothing can read back — including this service's own write-ahead log
                // on the next restart. Null is wrong, but it is *parseable* wrong, which keeps a
                // bad number from taking the whole log with it.
                if !n.is_finite() {
                    out.push_str("null");
                } else if n.fract() == 0.0 && n.abs() < 1e15 {
                    // `-0.0 as i64` is 0, which drops the sign and makes the value that comes
                    // back out of the write-ahead log a different one from the value that went
                    // in. Numerically it does not matter for a balance, but a serialiser that
                    // quietly alters what it is given is the wrong foundation for a ledger.
                    if *n == 0.0 && n.is_sign_negative() {
                        out.push_str("-0");
                    } else {
                        let _ = write!(out, "{}", *n as i64);
                    }
                } else {
                    let _ = write!(out, "{}", n);
                }
            }
            Json::String(s) => write_json_string(s, out),
            Json::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    item.write(out);
                }
                out.push(']');
            }
            Json::Object(pairs) => {
                out.push('{');
                for (i, (k, v)) in pairs.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_json_string(k, out);
                    out.push(':');
                    v.write(out);
                }
                out.push('}');
            }
        }
    }
}

fn write_json_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// How deeply nested a parsed document may be.
///
/// The parser is recursive, and a Rust stack overflow is not a catchable error — it aborts the
/// entire process. `[[[[[…` two hundred thousand deep, posted unauthenticated to `/v1/agents`
/// (which has to parse the body to find the key it will verify the signature against), took the
/// whole server down. Nothing legitimate here nests past a handful of levels.
pub const MAX_DEPTH: usize = 64;

pub fn parse(input: &str) -> Result<Json, String> {
    let chars: Vec<char> = input.chars().collect();
    let mut pos = 0;
    let value = parse_value(&chars, &mut pos, 0)?;
    // Trailing content means the document was not what the sender claimed. Accepting it lets
    // two parties disagree about what a signed body said.
    skip_ws(&chars, &mut pos);
    if pos < chars.len() {
        return Err(format!("unexpected trailing content at byte {pos}"));
    }
    Ok(value)
}

fn skip_ws(chars: &[char], pos: &mut usize) {
    while *pos < chars.len() && chars[*pos].is_whitespace() {
        *pos += 1;
    }
}

fn parse_value(chars: &[char], pos: &mut usize, depth: usize) -> Result<Json, String> {
    if depth > MAX_DEPTH {
        return Err(format!("nested deeper than {MAX_DEPTH} levels"));
    }
    skip_ws(chars, pos);
    if *pos >= chars.len() {
        return Err("unexpected end of input".to_string());
    }
    match chars[*pos] {
        '{' => parse_object(chars, pos, depth + 1),
        '[' => parse_array(chars, pos, depth + 1),
        '"' => Ok(Json::String(parse_string(chars, pos)?)),
        't' => {
            expect_literal(chars, pos, "true")?;
            Ok(Json::Bool(true))
        }
        'f' => {
            expect_literal(chars, pos, "false")?;
            Ok(Json::Bool(false))
        }
        'n' => {
            expect_literal(chars, pos, "null")?;
            Ok(Json::Null)
        }
        _ => parse_number(chars, pos),
    }
}

fn expect_literal(chars: &[char], pos: &mut usize, lit: &str) -> Result<(), String> {
    let lit_chars: Vec<char> = lit.chars().collect();
    if *pos + lit_chars.len() > chars.len() || chars[*pos..*pos + lit_chars.len()] != lit_chars[..]
    {
        return Err(format!("expected literal {lit}"));
    }
    *pos += lit_chars.len();
    Ok(())
}

fn parse_object(chars: &[char], pos: &mut usize, depth: usize) -> Result<Json, String> {
    *pos += 1; // consume '{'
    let mut pairs = Vec::new();
    skip_ws(chars, pos);
    if *pos < chars.len() && chars[*pos] == '}' {
        *pos += 1;
        return Ok(Json::Object(pairs));
    }
    loop {
        skip_ws(chars, pos);
        let key = parse_string(chars, pos)?;
        skip_ws(chars, pos);
        if *pos >= chars.len() || chars[*pos] != ':' {
            return Err("expected ':' in object".to_string());
        }
        *pos += 1;
        let value = parse_value(chars, pos, depth + 1)?;
        pairs.push((key, value));
        skip_ws(chars, pos);
        if *pos >= chars.len() {
            return Err("unterminated object".to_string());
        }
        match chars[*pos] {
            ',' => {
                *pos += 1;
            }
            '}' => {
                *pos += 1;
                break;
            }
            _ => return Err("expected ',' or '}' in object".to_string()),
        }
    }
    Ok(Json::Object(pairs))
}

fn parse_array(chars: &[char], pos: &mut usize, depth: usize) -> Result<Json, String> {
    *pos += 1; // consume '['
    let mut items = Vec::new();
    skip_ws(chars, pos);
    if *pos < chars.len() && chars[*pos] == ']' {
        *pos += 1;
        return Ok(Json::Array(items));
    }
    loop {
        let value = parse_value(chars, pos, depth + 1)?;
        items.push(value);
        skip_ws(chars, pos);
        if *pos >= chars.len() {
            return Err("unterminated array".to_string());
        }
        match chars[*pos] {
            ',' => {
                *pos += 1;
            }
            ']' => {
                *pos += 1;
                break;
            }
            _ => return Err("expected ',' or ']' in array".to_string()),
        }
    }
    Ok(Json::Array(items))
}

fn parse_string(chars: &[char], pos: &mut usize) -> Result<String, String> {
    skip_ws(chars, pos);
    if *pos >= chars.len() || chars[*pos] != '"' {
        return Err("expected string".to_string());
    }
    *pos += 1;
    let mut s = String::new();
    while *pos < chars.len() && chars[*pos] != '"' {
        if chars[*pos] == '\\' {
            *pos += 1;
            if *pos >= chars.len() {
                return Err("unterminated escape".to_string());
            }
            match chars[*pos] {
                '"' => s.push('"'),
                '\\' => s.push('\\'),
                '/' => s.push('/'),
                'n' => s.push('\n'),
                'r' => s.push('\r'),
                't' => s.push('\t'),
                'b' => s.push('\u{08}'),
                'f' => s.push('\u{0c}'),
                other => s.push(other), // not fully spec-compliant (no \uXXXX) — fine for this API
            }
            *pos += 1;
        } else {
            s.push(chars[*pos]);
            *pos += 1;
        }
    }
    if *pos >= chars.len() {
        return Err("unterminated string".to_string());
    }
    *pos += 1; // consume closing quote
    Ok(s)
}

fn parse_number(chars: &[char], pos: &mut usize) -> Result<Json, String> {
    let start = *pos;
    if *pos < chars.len() && (chars[*pos] == '-' || chars[*pos] == '+') {
        *pos += 1;
    }
    while *pos < chars.len()
        && (chars[*pos].is_ascii_digit()
            || chars[*pos] == '.'
            || chars[*pos] == 'e'
            || chars[*pos] == 'E'
            || chars[*pos] == '-'
            || chars[*pos] == '+')
    {
        *pos += 1;
    }
    let s: String = chars[start..*pos].iter().collect();
    s.parse::<f64>().map(Json::Number).map_err(|_| format!("invalid number: {s}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrips_object_with_mixed_types() {
        let json = Json::obj(vec![
            ("name", Json::str("agent_A")),
            ("score", Json::num(250.0)),
            ("active", Json::Bool(true)),
            ("tags", Json::Array(vec![Json::str("a"), Json::str("b")])),
        ]);
        let s = json.to_string();
        let parsed = parse(&s).unwrap();
        assert_eq!(parsed.get("name").unwrap().as_str(), Some("agent_A"));
        assert_eq!(parsed.get("score").unwrap().as_f64(), Some(250.0));
        assert_eq!(parsed.get("active").unwrap(), &Json::Bool(true));
        assert_eq!(parsed.get("tags").unwrap().as_array().unwrap().len(), 2);
    }

    #[test]
    fn parses_nested_and_negative_numbers() {
        let parsed = parse(r#"{"a": -1.5, "b": {"c": 3}}"#).unwrap();
        assert_eq!(parsed.get("a").unwrap().as_f64(), Some(-1.5));
        assert_eq!(parsed.get("b").unwrap().get("c").unwrap().as_f64(), Some(3.0));
    }

    #[test]
    fn parses_escaped_string() {
        let parsed = parse(r#"{"msg": "hello \"world\"\n"}"#).unwrap();
        assert_eq!(parsed.get("msg").unwrap().as_str(), Some("hello \"world\"\n"));
    }

    #[test]
    fn negative_zero_keeps_its_sign() {
        let round = |v: f64| match parse(&Json::Number(v).to_string()).unwrap() {
            Json::Number(n) => n,
            other => panic!("expected a number, got {other:?}"),
        };
        assert!(round(-0.0).is_sign_negative(), "-0.0 must not come back as +0.0");
        assert!(round(0.0).is_sign_positive());
        assert_eq!(round(-0.0), 0.0, "it is still numerically zero");
    }
}
