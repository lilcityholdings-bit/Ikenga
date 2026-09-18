//! A resumable feed of what just happened, for clients that are programs.
//!
//! # Why polling was the wrong answer
//!
//! Before this, the only way for a bot to notice that a market had opened, closed or paid out was
//! to fetch the whole market list again and diff it against what it saw last time. That is wrong
//! in three separate ways at once.
//!
//! It is expensive for the client: the interesting event is one line, and it has to download and
//! compare the entire board to find it. It is expensive for the server: every poll re-serialises
//! every market, and it happens once per client per interval forever. And it burns the caller's
//! rate limit on requests that mostly return "nothing changed" — a new agent gets ten requests a
//! second, and spending them on polling is how an integration hits 429s while doing nothing
//! useful.
//!
//! Worse, it is *lossy in the direction that matters*. A market that opens and closes between two
//! polls is invisible; a settlement missed while the bot was restarting is gone. A trading client
//! that cannot enumerate what happened while it was away cannot reconcile its own book.
//!
//! # Why a cursor and not a socket
//!
//! There is already a WebSocket here, and it would have been the obvious place. A cursor is
//! better for this audience:
//!
//! - **It survives disconnection.** The client stores one integer. Reconnect, ask for everything
//!   after it, and no event is missed — where a dropped socket loses whatever was sent while it
//!   was down, silently.
//! - **It needs no connection management.** Long-lived sockets need reconnect logic, backoff,
//!   heartbeats and a state machine. A GET with a number needs none of that, and gets the retry
//!   behaviour of ordinary HTTP for free.
//! - **It works everywhere.** Through proxies, serverless functions, cron jobs, and any HTTP
//!   client in any language — including one written by an LLM that has never heard of this API.
//!
//! The trade is latency: a poll cycle rather than a push. For markets that run in minutes, that is
//! not the constraint.
//!
//! # What it deliberately does not carry
//!
//! No agent identities, no individual stakes, no balances. The events describe the *venue* — a
//! market opened, an outcome was proposed, a pool paid out — not the people in it. The same rule
//! that governs the forecast feed governs this, and for the same reason: the moment a public
//! stream reveals who bet what, the informed money stops betting here.

use std::collections::VecDeque;
use std::sync::Mutex;

use crate::json::Json;

/// Something that happened to the venue, in the order it happened.
#[derive(Debug, Clone, PartialEq)]
pub struct Event {
    /// Monotonically increasing, starting at 1. This is the cursor.
    pub seq: u64,
    pub at_ms: i64,
    pub kind: EventKind,
    pub market_id: String,
    /// Human-readable, so a log of these is directly useful to a person too.
    pub detail: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventKind {
    MarketOpened,
    MarketClosed,
    OutcomeProposed,
    OutcomeDisputed,
    MarketResolved,
    MarketVoided,
}

impl EventKind {
    pub fn label(&self) -> &'static str {
        match self {
            EventKind::MarketOpened => "market_opened",
            EventKind::MarketClosed => "market_closed",
            EventKind::OutcomeProposed => "outcome_proposed",
            EventKind::OutcomeDisputed => "outcome_disputed",
            EventKind::MarketResolved => "market_resolved",
            EventKind::MarketVoided => "market_voided",
        }
    }
}

impl Event {
    pub fn to_json(&self) -> Json {
        Json::obj(vec![
            ("seq", Json::num(self.seq as f64)),
            ("at_ms", Json::num(self.at_ms as f64)),
            ("kind", Json::str(self.kind.label())),
            ("market_id", Json::str(self.market_id.clone())),
            ("detail", Json::str(self.detail.clone())),
        ])
    }
}

/// How many events are retained. Past this the oldest are dropped.
///
/// A ring rather than a growing log, for the same reason settled markets are trimmed: this thing
/// runs unattended for months and autopilot generates events on a timer. Two thousand covers a
/// client that has been away for a long time on any realistic venue, and a client that has been
/// away *longer* than that is told so explicitly rather than being handed a silently incomplete
/// history — see `since`.
pub const RETAINED: usize = 2_000;

#[derive(Default)]
pub struct EventLog {
    inner: Mutex<Inner>,
}

#[derive(Default)]
struct Inner {
    next_seq: u64,
    events: VecDeque<Event>,
}

/// The answer to a cursor query.
pub struct Page {
    pub events: Vec<Event>,
    /// What to pass as `since` next time.
    pub next_cursor: u64,
    /// True when the requested cursor is older than anything retained, so events were missed.
    ///
    /// This has to be surfaced rather than swallowed. A client that was away too long and is
    /// handed a partial history without being told would believe it had seen everything, and
    /// would reconcile its book against a gap it does not know exists.
    pub gap: bool,
}

impl EventLog {
    pub fn record(&self, kind: EventKind, market_id: &str, detail: impl Into<String>, at_ms: i64) {
        let mut inner = self.inner.lock().unwrap();
        inner.next_seq += 1;
        let event = Event {
            seq: inner.next_seq,
            at_ms,
            kind,
            market_id: market_id.to_string(),
            detail: detail.into(),
        };
        inner.events.push_back(event);
        while inner.events.len() > RETAINED {
            inner.events.pop_front();
        }
    }

    /// Everything after `since`, capped at `limit`.
    pub fn since(&self, since: u64, limit: usize) -> Page {
        let inner = self.inner.lock().unwrap();
        let oldest = inner.events.front().map(|e| e.seq);

        // A gap means the caller asked for events that have already been dropped. Asking from 0
        // (a first-time client) is not a gap — it is a client that has no history to miss.
        let gap = match oldest {
            Some(first) => since > 0 && since + 1 < first,
            None => false,
        };

        let events: Vec<Event> = inner
            .events
            .iter()
            .filter(|e| e.seq > since)
            .take(limit.max(1))
            .cloned()
            .collect();

        let next_cursor = events.last().map(|e| e.seq).unwrap_or_else(|| {
            // Nothing new. Hand back a cursor that is still valid rather than resetting the
            // client to zero, which would make it replay everything on every empty poll.
            since.max(inner.events.back().map(|e| e.seq).unwrap_or(0)).min(inner.next_seq)
        });

        Page { events, next_cursor, gap }
    }

    pub fn latest_seq(&self) -> u64 {
        self.inner.lock().unwrap().next_seq
    }

    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().events.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn log_with(n: u64) -> EventLog {
        let log = EventLog::default();
        for i in 0..n {
            log.record(EventKind::MarketOpened, &format!("m{i}"), "opened", 1_000 + i as i64);
        }
        log
    }

    #[test]
    fn a_client_can_resume_exactly_where_it_stopped() {
        let log = log_with(5);
        let first = log.since(0, 100);
        assert_eq!(first.events.len(), 5);
        assert_eq!(first.next_cursor, 5);
        assert!(!first.gap);

        log.record(EventKind::MarketResolved, "m9", "paid out", 2_000);
        let second = log.since(first.next_cursor, 100);
        assert_eq!(second.events.len(), 1, "must return only what is new");
        assert_eq!(second.events[0].kind, EventKind::MarketResolved);
        assert_eq!(second.next_cursor, 6);
    }

    #[test]
    fn an_empty_poll_does_not_rewind_the_client() {
        // The bug this guards against: returning cursor 0 when there is nothing new, which makes
        // the next poll replay the entire history, every time, forever.
        let log = log_with(3);
        let p = log.since(3, 100);
        assert!(p.events.is_empty());
        assert_eq!(p.next_cursor, 3, "an empty page must hand back the same position");
    }

    #[test]
    fn the_log_does_not_grow_without_bound() {
        let log = log_with(RETAINED as u64 + 500);
        assert_eq!(log.len(), RETAINED);
        assert_eq!(log.latest_seq(), RETAINED as u64 + 500, "sequence numbers never restart");
    }

    #[test]
    fn a_client_that_was_away_too_long_is_told_so() {
        let log = log_with(RETAINED as u64 + 100);
        // Cursor 1 is long gone.
        let p = log.since(1, 100);
        assert!(p.gap, "silently handing back a partial history is how books drift");
        assert!(!p.events.is_empty(), "it should still get what remains");

        // A brand-new client asking from zero has missed nothing by definition.
        let fresh = log.since(0, 10);
        assert!(!fresh.gap, "a first-time client has no history to miss");
    }

    #[test]
    fn paging_walks_the_whole_log_without_skipping_or_repeating() {
        let log = log_with(250);
        let mut cursor = 0;
        let mut seen = Vec::new();
        loop {
            let page = log.since(cursor, 40);
            if page.events.is_empty() {
                break;
            }
            seen.extend(page.events.iter().map(|e| e.seq));
            cursor = page.next_cursor;
        }
        assert_eq!(seen.len(), 250, "every event exactly once");
        let mut sorted = seen.clone();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(sorted.len(), 250, "no duplicates");
        assert_eq!(seen, sorted, "and in order");
    }

    #[test]
    fn events_carry_nothing_about_who_was_involved() {
        let log = EventLog::default();
        log.record(EventKind::MarketResolved, "m1", "outcome 0; pool 500; rake 2.60", 1);
        let blob = log.since(0, 10).events[0].to_json().to_string();
        for leak in ["agent", "stake_id", "balance", "address", "pubkey"] {
            assert!(!blob.contains(leak), "event exposed '{leak}': {blob}");
        }
    }
}
