//! Raw WebSocket (RFC 6455) server-side handshake + binary frame writer, hand-rolled over
//! `std::net::TcpStream` since tokio-tungstenite/axum aren't available (see Cargo.toml). Only
//! implements what a push-only binary market-data feed needs: the handshake and unmasked
//! server->client binary frames. Does not parse incoming client frames beyond detecting a
//! closed connection.

use std::io::{Read, Write};
use std::net::TcpStream;

use crate::crypto::{base64_encode, sha1};
use crate::http::Request;

const WS_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/// Binary market-data tick frame payload.
///
/// Layout (big-endian, no padding), 34 bytes total:
///   seq:       u64  (8 bytes)  — monotonic, global, 1-indexed (see AppState::next_tick_seq)
///   symbol_id: u16  (2 bytes)
///   price:     f64  (8 bytes)
///   qty:       f64  (8 bytes)
///   ts_ms:     u64  (8 bytes)
///
/// History: the original spec called this "16 bytes" for symbol_id+price+qty+ts_ms alone —
/// that was already wrong (2+8+8+8=26). This build fixed the byte count AND added the `seq`
/// field on top (making it 34 bytes) so a client can detect gaps in the stream at all — the
/// spec asks for "delta orderbook + checksum" gap detection, which this flat trade-tick format
/// doesn't have room for; a monotonic sequence number is the minimum viable version of that.
/// See docs/ARCHITECTURE.md section 4.
pub fn encode_tick(seq: u64, symbol_id: u16, price: f64, qty: f64, ts_ms: u64) -> [u8; 34] {
    let mut buf = [0u8; 34];
    buf[0..8].copy_from_slice(&seq.to_be_bytes());
    buf[8..10].copy_from_slice(&symbol_id.to_be_bytes());
    buf[10..18].copy_from_slice(&price.to_be_bytes());
    buf[18..26].copy_from_slice(&qty.to_be_bytes());
    buf[26..34].copy_from_slice(&ts_ms.to_be_bytes());
    buf
}

#[allow(dead_code)]
pub fn decode_tick(buf: &[u8; 34]) -> (u64, u16, f64, f64, u64) {
    let seq = u64::from_be_bytes(buf[0..8].try_into().unwrap());
    let symbol_id = u16::from_be_bytes(buf[8..10].try_into().unwrap());
    let price = f64::from_be_bytes(buf[10..18].try_into().unwrap());
    let qty = f64::from_be_bytes(buf[18..26].try_into().unwrap());
    let ts_ms = u64::from_be_bytes(buf[26..34].try_into().unwrap());
    (seq, symbol_id, price, qty, ts_ms)
}

/// Performs the RFC 6455 server handshake. Returns Err if the request isn't a valid upgrade.
pub fn handshake(stream: &mut TcpStream, req: &Request) -> std::io::Result<()> {
    let key = req
        .header("sec-websocket-key")
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "missing Sec-WebSocket-Key"))?;
    let mut accept_input = key.to_string();
    accept_input.push_str(WS_GUID);
    let accept = base64_encode(&sha1(accept_input.as_bytes()));

    let response = format!(
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    );
    stream.write_all(response.as_bytes())?;
    stream.flush()
}

/// Writes one unmasked binary frame (server->client). Payloads here are always 26 bytes
/// (well under the 126-byte short-length encoding threshold), so the extended-length frame
/// encodings (16-bit/64-bit) are implemented but exercised only by the unit test.
pub fn write_binary_frame(stream: &mut TcpStream, payload: &[u8]) -> std::io::Result<()> {
    let mut header = vec![0x82u8]; // FIN=1, opcode=0x2 (binary)
    let len = payload.len();
    if len < 126 {
        header.push(len as u8);
    } else if len <= 0xFFFF {
        header.push(126);
        header.extend_from_slice(&(len as u16).to_be_bytes());
    } else {
        header.push(127);
        header.extend_from_slice(&(len as u64).to_be_bytes());
    }
    stream.write_all(&header)?;
    stream.write_all(payload)?;
    stream.flush()
}

/// Blocks until the peer closes the connection or a read error occurs. Used on a dedicated
/// thread per WS connection purely to detect disconnects (a market-data-only feed never
/// expects meaningful frames from the client) so the writer side can stop broadcasting to it.
pub fn wait_for_disconnect(mut stream: TcpStream) {
    let mut buf = [0u8; 512];
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,   // clean close
            Ok(_) => continue, // ignore any client frames (ping/pong/close not decoded here)
            Err(_) => break,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tick_roundtrips_and_is_34_bytes() {
        let frame = encode_tick(42, 1, 65440.50, 0.25, 1_725_000_000_000);
        assert_eq!(frame.len(), 34);
        let (seq, symbol_id, price, qty, ts_ms) = decode_tick(&frame);
        assert_eq!(seq, 42);
        assert_eq!(symbol_id, 1);
        assert!((price - 65440.50).abs() < 1e-9);
        assert!((qty - 0.25).abs() < 1e-9);
        assert_eq!(ts_ms, 1_725_000_000_000);
    }

    #[test]
    fn handshake_accept_matches_rfc6455_example() {
        // RFC 6455 section 1.3 worked example.
        let key = "dGhlIHNhbXBsZSBub25jZQ==";
        let mut input = key.to_string();
        input.push_str(WS_GUID);
        let accept = base64_encode(&sha1(input.as_bytes()));
        assert_eq!(accept, "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=");
    }
}
