use std::collections::{BTreeMap, VecDeque};

use crate::types::{Fill, Order, OrderStatus, Side};

/// What one `submit` produced: the fills, plus any of the submitter's own resting orders that
/// had to be pulled to avoid a self-trade.
///
/// SELF-TRADE PREVENTION exists because without it an agent could match against its own resting
/// order. That is wash trading: it manufactures volume and price prints out of nothing, at no
/// cost beyond fees paid to yourself. It is the single cheapest way to fake an exchange's
/// activity, it is illegal in regulated markets, and every venue that got caught doing it wore
/// it badly. It also silently corrupts the trust score, the fee ledger and the public tick feed.
///
/// Policy here is **cancel-resting** (a.k.a. cancel-oldest), which is what Coinbase and Kraken
/// do: when an incoming order would hit the submitter's own resting order, the resting one is
/// cancelled and matching continues against the next maker. The alternative — rejecting the
/// incoming order — punishes an agent for having liquidity on the book, which discourages
/// exactly the market making this venue needs.
#[derive(Debug, Clone, Default)]
pub struct MatchResult {
    pub fills: Vec<Fill>,
    /// Order IDs the submitter had resting that were cancelled to prevent a self-trade. The
    /// caller must mark these Cancelled in its own order map — see api.rs::submit_order.
    pub self_cancelled: Vec<String>,
}

/// Price-time-priority order book for a single symbol, held entirely in memory.
///
/// NOT crash-safe: there is no write-ahead log here. A process restart loses the book.
/// See docs/ROADMAP.md phase 1 before this is trusted with real funds.
#[derive(Default)]
pub struct OrderBook {
    pub symbol: String,
    /// price_ticks -> FIFO queue of resting buy orders at that price
    pub bids: BTreeMap<i64, VecDeque<Order>>,
    /// price_ticks -> FIFO queue of resting sell orders at that price
    pub asks: BTreeMap<i64, VecDeque<Order>>,
}

impl OrderBook {
    pub fn new(symbol: impl Into<String>) -> Self {
        Self { symbol: symbol.into(), bids: BTreeMap::new(), asks: BTreeMap::new() }
    }

    pub fn best_bid(&self) -> Option<i64> {
        self.bids.keys().next_back().copied()
    }

    pub fn best_ask(&self) -> Option<i64> {
        self.asks.keys().next().copied()
    }

    /// Matches an incoming (taker) order against resting liquidity, then rests any remainder
    /// on the book (limit orders only — unfilled market order remainder is dropped, since a
    /// market order has no price to rest at).
    /// `trade_id`s are random, not sequential. They used to be `t_{now_ms}_{counter}` off a
    /// global counter, which told anyone holding two trade IDs exactly how many trades the
    /// whole platform had done in between. See privacy.rs.
    pub fn submit(&mut self, mut incoming: Order, now_ms: i64) -> MatchResult {
        let mut fills = Vec::new();
        let mut self_cancelled = Vec::new();

        match incoming.side {
            Side::Buy => {
                loop {
                    if incoming.remaining() <= 0.0 {
                        break;
                    }
                    let Some((&ask_price, _)) = self.asks.iter().next() else { break };
                    if incoming.order_type == crate::types::OrderType::Limit
                        && ask_price > incoming.price_ticks
                    {
                        break; // best ask is above our limit — no match possible
                    }
                    let queue = self.asks.get_mut(&ask_price).unwrap();
                    let Some(maker) = queue.front_mut() else {
                        self.asks.remove(&ask_price);
                        continue;
                    };
                    // SELF-TRADE PREVENTION: cancel-resting. See the note on MatchResult.
                    if maker.agent_id == incoming.agent_id {
                        let mut pulled = queue.pop_front().expect("front_mut just succeeded");
                        pulled.status = OrderStatus::Cancelled;
                        self_cancelled.push(pulled.order_id);
                        if queue.is_empty() {
                            self.asks.remove(&ask_price);
                        }
                        continue;
                    }
                    let trade_qty = incoming.remaining().min(maker.remaining());
                    fills.push(Fill {
                        trade_id: crate::privacy::random_id("t_"),
                        symbol: self.symbol.clone(),
                        price_ticks: ask_price,
                        qty: trade_qty,
                        taker_order_id: incoming.order_id.clone(),
                        maker_order_id: maker.order_id.clone(),
                        taker_agent_id: incoming.agent_id.clone(),
                        maker_agent_id: maker.agent_id.clone(),
                        ts_ms: now_ms,
                    });
                    incoming.filled_qty += trade_qty;
                    maker.filled_qty += trade_qty;
                    if maker.remaining() <= 0.0 {
                        maker.status = OrderStatus::Filled;
                        queue.pop_front();
                    } else {
                        maker.status = OrderStatus::PartiallyFilled;
                    }
                    if queue.is_empty() {
                        self.asks.remove(&ask_price);
                    }
                }
            }
            Side::Sell => {
                loop {
                    if incoming.remaining() <= 0.0 {
                        break;
                    }
                    let Some((&bid_price, _)) = self.bids.iter().next_back() else { break };
                    if incoming.order_type == crate::types::OrderType::Limit
                        && bid_price < incoming.price_ticks
                    {
                        break; // best bid is below our limit — no match possible
                    }
                    let queue = self.bids.get_mut(&bid_price).unwrap();
                    let Some(maker) = queue.front_mut() else {
                        self.bids.remove(&bid_price);
                        continue;
                    };
                    // SELF-TRADE PREVENTION: cancel-resting. See the note on MatchResult.
                    if maker.agent_id == incoming.agent_id {
                        let mut pulled = queue.pop_front().expect("front_mut just succeeded");
                        pulled.status = OrderStatus::Cancelled;
                        self_cancelled.push(pulled.order_id);
                        if queue.is_empty() {
                            self.bids.remove(&bid_price);
                        }
                        continue;
                    }
                    let trade_qty = incoming.remaining().min(maker.remaining());
                    fills.push(Fill {
                        trade_id: crate::privacy::random_id("t_"),
                        symbol: self.symbol.clone(),
                        price_ticks: bid_price,
                        qty: trade_qty,
                        taker_order_id: incoming.order_id.clone(),
                        maker_order_id: maker.order_id.clone(),
                        taker_agent_id: incoming.agent_id.clone(),
                        maker_agent_id: maker.agent_id.clone(),
                        ts_ms: now_ms,
                    });
                    incoming.filled_qty += trade_qty;
                    maker.filled_qty += trade_qty;
                    if maker.remaining() <= 0.0 {
                        maker.status = OrderStatus::Filled;
                        queue.pop_front();
                    } else {
                        maker.status = OrderStatus::PartiallyFilled;
                    }
                    if queue.is_empty() {
                        self.bids.remove(&bid_price);
                    }
                }
            }
        }

        incoming.status = if incoming.remaining() <= 0.0 {
            OrderStatus::Filled
        } else if incoming.filled_qty > 0.0 {
            OrderStatus::PartiallyFilled
        } else {
            OrderStatus::Open
        };

        // Rest any remainder, limit orders only.
        if incoming.remaining() > 0.0 && incoming.order_type == crate::types::OrderType::Limit {
            let book_side = match incoming.side {
                Side::Buy => &mut self.bids,
                Side::Sell => &mut self.asks,
            };
            book_side.entry(incoming.price_ticks).or_default().push_back(incoming);
        }

        MatchResult { fills, self_cancelled }
    }

    pub fn cancel(&mut self, order_id: &str) -> Option<Order> {
        for side_map in [&mut self.bids, &mut self.asks] {
            let target_price = side_map
                .iter()
                .find(|(_, queue)| queue.iter().any(|o| o.order_id == order_id))
                .map(|(&price, _)| price);

            let Some(price) = target_price else { continue };
            let queue = side_map.get_mut(&price).unwrap();
            let pos = queue.iter().position(|o| o.order_id == order_id).unwrap();
            let mut order = queue.remove(pos).unwrap();
            order.status = OrderStatus::Cancelled;
            // Prune the now-empty price level so best_bid()/best_ask() don't return a stale,
            // empty level (the bug this replaces: cancel used to leave an empty VecDeque
            // behind at that price, which best_bid()/best_ask() happily returned as if it
            // still had resting liquidity).
            if queue.is_empty() {
                side_map.remove(&price);
            }
            return Some(order);
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{price_to_ticks, OrderType};

    fn mk_order(id: &str, agent: &str, side: Side, price: f64, qty: f64) -> Order {
        Order {
            order_id: id.to_string(),
            agent_id: agent.to_string(),
            symbol: "BTC-USD".to_string(),
            side,
            order_type: OrderType::Limit,
            price_ticks: price_to_ticks(price),
            qty,
            filled_qty: 0.0,
            status: OrderStatus::Open,
            created_at_ms: 0,
        }
    }

    #[test]
    fn resting_order_with_no_match_just_rests() {
        let mut book = OrderBook::new("BTC-USD");
        let fills = book.submit(mk_order("o1", "agent_A", Side::Buy, 100.0, 1.0), 1).fills;
        assert!(fills.is_empty());
        assert_eq!(book.best_bid(), Some(price_to_ticks(100.0)));
    }

    #[test]
    fn crossing_orders_match_at_maker_price() {
        let mut book = OrderBook::new("BTC-USD");
        book.submit(mk_order("maker1", "agent_A", Side::Sell, 100.0, 1.0), 1);
        let fills = book.submit(mk_order("taker1", "agent_B", Side::Buy, 101.0, 1.0), 2).fills;
        assert_eq!(fills.len(), 1);
        assert_eq!(fills[0].price_ticks, price_to_ticks(100.0)); // fills at maker's price, not taker's
        assert_eq!(fills[0].qty, 1.0);
        assert!(book.best_ask().is_none());
        assert!(book.best_bid().is_none());
    }

    #[test]
    fn partial_fill_leaves_remainder_resting() {
        let mut book = OrderBook::new("BTC-USD");
        book.submit(mk_order("maker1", "agent_A", Side::Sell, 100.0, 0.4), 1);
        let fills = book.submit(mk_order("taker1", "agent_B", Side::Buy, 100.0, 1.0), 2).fills;
        assert_eq!(fills.len(), 1);
        assert_eq!(fills[0].qty, 0.4);
        // remaining 0.6 of the taker order should now rest as a bid
        assert_eq!(book.best_bid(), Some(price_to_ticks(100.0)));
    }

    #[test]
    fn price_time_priority_fills_earlier_order_first() {
        let mut book = OrderBook::new("BTC-USD");
        book.submit(mk_order("maker1", "agent_A", Side::Sell, 100.0, 1.0), 1);
        book.submit(mk_order("maker2", "agent_C", Side::Sell, 100.0, 1.0), 2);
        let fills = book.submit(mk_order("taker1", "agent_B", Side::Buy, 100.0, 1.0), 3).fills;
        assert_eq!(fills[0].maker_order_id, "maker1"); // FIFO at same price level
    }

    #[test]
    fn cancel_removes_resting_order() {
        let mut book = OrderBook::new("BTC-USD");
        book.submit(mk_order("o1", "agent_A", Side::Buy, 100.0, 1.0), 1);
        let cancelled = book.cancel("o1");
        assert!(cancelled.is_some());
        assert!(book.best_bid().is_none());
    }

    #[test]
    fn an_agent_cannot_trade_against_itself() {
        let mut book = OrderBook::new("BTC-USD");
        // Same agent on both sides at a crossing price — the classic wash trade.
        book.submit(mk_order("resting", "agent_WASH", Side::Sell, 100.0, 1.0), 1);
        let result = book.submit(mk_order("crossing", "agent_WASH", Side::Buy, 100.0, 1.0), 2);

        assert!(result.fills.is_empty(), "agent wash-traded with itself: {:?}", result.fills);
        assert_eq!(result.self_cancelled, vec!["resting".to_string()]);
        // The resting order is gone and the incoming one is now the resting bid.
        assert_eq!(book.best_ask(), None);
        assert_eq!(book.best_bid(), Some(price_to_ticks(100.0)));
    }

    #[test]
    fn self_match_prevention_does_not_block_trading_with_others() {
        let mut book = OrderBook::new("BTC-USD");
        // Own order sits in front (earlier), a genuine counterparty behind it at the same price.
        book.submit(mk_order("mine", "agent_A", Side::Sell, 100.0, 1.0), 1);
        book.submit(mk_order("theirs", "agent_B", Side::Sell, 100.0, 1.0), 2);

        let result = book.submit(mk_order("taker", "agent_A", Side::Buy, 100.0, 1.0), 3);
        assert_eq!(result.self_cancelled, vec!["mine".to_string()]);
        assert_eq!(result.fills.len(), 1, "should still fill against agent_B");
        assert_eq!(result.fills[0].maker_agent_id, "agent_B");
        assert_eq!(result.fills[0].qty, 1.0);
    }
}
