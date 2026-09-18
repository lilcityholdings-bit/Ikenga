use crate::json::Json;

/// Fixed-point price representation. Prices are stored as integer "ticks" rather than f64
/// so that BTreeMap ordering and equality are exact — comparing raw f64 prices in a matching
/// engine is a real bug waiting to happen (NaN, rounding, -0.0 vs 0.0, etc).
pub const PRICE_SCALE: f64 = 1_00_000_000.0; // 1e8, i.e. 8 decimal places of precision

pub fn price_to_ticks(price: f64) -> i64 {
    (price * PRICE_SCALE).round() as i64
}

pub fn ticks_to_price(ticks: i64) -> f64 {
    ticks as f64 / PRICE_SCALE
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Buy,
    Sell,
}

impl Side {
    pub fn parse(s: &str) -> Option<Side> {
        match s {
            "Buy" | "buy" => Some(Side::Buy),
            "Sell" | "sell" => Some(Side::Sell),
            _ => None,
        }
    }
    pub fn as_str(&self) -> &'static str {
        match self {
            Side::Buy => "Buy",
            Side::Sell => "Sell",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderType {
    Limit,
    Market,
}

impl OrderType {
    pub fn parse(s: &str) -> Option<OrderType> {
        match s {
            "Limit" | "limit" => Some(OrderType::Limit),
            "Market" | "market" => Some(OrderType::Market),
            _ => None,
        }
    }
    pub fn as_str(&self) -> &'static str {
        match self {
            OrderType::Limit => "Limit",
            OrderType::Market => "Market",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderStatus {
    Open,
    PartiallyFilled,
    Filled,
    Cancelled,
}

impl OrderStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            OrderStatus::Open => "Open",
            OrderStatus::PartiallyFilled => "PartiallyFilled",
            OrderStatus::Filled => "Filled",
            OrderStatus::Cancelled => "Cancelled",
        }
    }
}

#[derive(Debug, Clone)]
pub struct NewOrderRequest {
    pub symbol: String,
    pub side: Side,
    pub order_type: OrderType,
    pub price: Option<f64>,
    pub qty: f64,
    pub client_order_id: Option<String>,
}

impl NewOrderRequest {
    pub fn from_json(json: &Json) -> Result<Self, String> {
        let symbol =
            json.get("symbol").and_then(Json::as_str).ok_or("missing symbol")?.to_string();
        let side_str = json.get("side").and_then(Json::as_str).ok_or("missing side")?;
        let side = Side::parse(side_str).ok_or("invalid side")?;
        let type_str =
            json.get("order_type").and_then(Json::as_str).ok_or("missing order_type")?;
        let order_type = OrderType::parse(type_str).ok_or("invalid order_type")?;
        let price = json.get("price").and_then(Json::as_f64);
        let qty = json.get("qty").and_then(Json::as_f64).ok_or("missing qty")?;
        let client_order_id =
            json.get("client_order_id").and_then(Json::as_str).map(|s| s.to_string());
        Ok(Self { symbol, side, order_type, price, qty, client_order_id })
    }
}

#[derive(Debug, Clone)]
pub struct Order {
    pub order_id: String,
    pub agent_id: String,
    pub symbol: String,
    pub side: Side,
    pub order_type: OrderType,
    pub price_ticks: i64,
    pub qty: f64,
    pub filled_qty: f64,
    pub status: OrderStatus,
    pub created_at_ms: i64,
}

impl Order {
    pub fn remaining(&self) -> f64 {
        (self.qty - self.filled_qty).max(0.0)
    }

    /// NOTE: deliberately does NOT include `agent_id`. An agent only ever sees its own orders,
    /// so including the owner was redundant — and it made this a footgun, since any new endpoint
    /// that serialized an `Order` would silently leak ownership. See privacy.rs.
    pub fn to_json(&self) -> Json {
        Json::obj(vec![
            ("order_id", Json::str(self.order_id.clone())),
            ("symbol", Json::str(self.symbol.clone())),
            ("side", Json::str(self.side.as_str())),
            ("order_type", Json::str(self.order_type.as_str())),
            ("price", Json::num(ticks_to_price(self.price_ticks))),
            ("qty", Json::num(self.qty)),
            ("filled_qty", Json::num(self.filled_qty)),
            ("status", Json::str(self.status.as_str())),
            ("created_at_ms", Json::num(self.created_at_ms as f64)),
        ])
    }
}

#[derive(Debug, Clone)]
pub struct Fill {
    pub trade_id: String,
    pub symbol: String,
    pub price_ticks: i64,
    pub qty: f64,
    pub taker_order_id: String,
    pub maker_order_id: String,
    pub taker_agent_id: String,
    pub maker_agent_id: String,
    pub ts_ms: i64,
}

impl Fill {
    /// Serializes a fill *from one participant's point of view*.
    ///
    /// The counterparty is reduced to a single-use alias (see privacy.rs) and their order_id is
    /// dropped entirely. There is deliberately no `to_json()` that serializes both sides: the
    /// old one did, which meant every fill response handed the taker the maker's permanent
    /// `agent_id`. Requiring a viewer here makes that leak impossible to reintroduce by accident
    /// — you cannot serialize a fill without saying who is allowed to read it.
    pub fn to_json_for(&self, viewer_agent_id: &str, aliases: &crate::privacy::AliasRegistry) -> Json {
        let viewer_is_taker = self.taker_agent_id == viewer_agent_id;
        let (your_side, your_order_id, counterparty_agent_id) = if viewer_is_taker {
            ("Taker", &self.taker_order_id, &self.maker_agent_id)
        } else {
            ("Maker", &self.maker_order_id, &self.taker_agent_id)
        };

        Json::obj(vec![
            ("trade_id", Json::str(self.trade_id.clone())),
            ("symbol", Json::str(self.symbol.clone())),
            ("price", Json::num(ticks_to_price(self.price_ticks))),
            ("qty", Json::num(self.qty)),
            ("your_side", Json::str(your_side)),
            ("your_order_id", Json::str(your_order_id.clone())),
            ("counterparty_alias", Json::str(aliases.issue(counterparty_agent_id))),
            ("ts_ms", Json::num(self.ts_ms as f64)),
        ])
    }
}

#[derive(Debug, Clone)]
pub struct OrderAck {
    pub order_id: String,
    pub status: OrderStatus,
    pub filled_qty: f64,
    pub avg_fill_price: Option<f64>,
    pub fills: Vec<Fill>,
}

impl OrderAck {
    /// Takes the viewer for the same reason `Fill::to_json_for` does — an ack carries fills, and
    /// fills can't be serialized without deciding whose eyes they're for.
    pub fn to_json_for(
        &self,
        viewer_agent_id: &str,
        aliases: &crate::privacy::AliasRegistry,
    ) -> Json {
        Json::obj(vec![
            ("order_id", Json::str(self.order_id.clone())),
            ("status", Json::str(self.status.as_str())),
            ("filled_qty", Json::num(self.filled_qty)),
            (
                "avg_fill_price",
                match self.avg_fill_price {
                    Some(p) => Json::num(p),
                    None => Json::Null,
                },
            ),
            (
                "fills",
                Json::Array(
                    self.fills.iter().map(|f| f.to_json_for(viewer_agent_id, aliases)).collect(),
                ),
            ),
        ])
    }
}

#[derive(Debug, Clone)]
pub struct ApiError {
    pub code: &'static str,
    pub message: String,
}

impl ApiError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self { code, message: message.into() }
    }

    pub fn to_json(&self) -> Json {
        Json::obj(vec![
            ("code", Json::str(self.code)),
            ("message", Json::str(self.message.clone())),
        ])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::privacy::AliasRegistry;

    fn sample_fill() -> Fill {
        Fill {
            trade_id: "t_abc".to_string(),
            symbol: "BTC-USD".to_string(),
            price_ticks: price_to_ticks(65000.0),
            qty: 0.5,
            taker_order_id: "o_taker".to_string(),
            maker_order_id: "o_maker".to_string(),
            taker_agent_id: "agent_TAKER".to_string(),
            maker_agent_id: "agent_MAKER".to_string(),
            ts_ms: 1_700_000_000_000,
        }
    }

    /// The regression test for the leak this whole privacy layer exists to close: a fill used to
    /// be serialized with BOTH agent IDs, so the taker's own order ack named the maker outright.
    #[test]
    fn fill_never_reveals_the_counterpartys_real_identity() {
        let aliases = AliasRegistry::default();
        let fill = sample_fill();

        let as_taker = fill.to_json_for("agent_TAKER", &aliases).to_string();
        assert!(!as_taker.contains("agent_MAKER"), "taker's view leaked the maker: {as_taker}");
        assert!(!as_taker.contains("o_maker"), "taker's view leaked the maker's order id");
        assert!(as_taker.contains("\"your_side\":\"Taker\""));
        assert!(as_taker.contains("o_taker"), "taker should still see their own order id");

        let as_maker = fill.to_json_for("agent_MAKER", &aliases).to_string();
        assert!(!as_maker.contains("agent_TAKER"), "maker's view leaked the taker: {as_maker}");
        assert!(!as_maker.contains("o_taker"), "maker's view leaked the taker's order id");
        assert!(as_maker.contains("\"your_side\":\"Maker\""));
    }

    /// Two views of the *same* trade must not hand out the same alias, or the two participants
    /// could compare notes and correlate.
    #[test]
    fn counterparty_aliases_are_single_use() {
        let aliases = AliasRegistry::default();
        let fill = sample_fill();
        let first = fill.to_json_for("agent_TAKER", &aliases).to_string();
        let second = fill.to_json_for("agent_TAKER", &aliases).to_string();
        assert_ne!(first, second, "the same counterparty produced a repeatable alias");
        // Both must still resolve internally, or compliance can't do its job.
        assert_eq!(aliases.issued_count(), 2);
    }

    #[test]
    fn order_json_does_not_include_its_owner() {
        let order = Order {
            order_id: "o_1".to_string(),
            agent_id: "agent_SECRET".to_string(),
            symbol: "BTC-USD".to_string(),
            side: Side::Buy,
            order_type: OrderType::Limit,
            price_ticks: price_to_ticks(100.0),
            qty: 1.0,
            filled_qty: 0.0,
            status: OrderStatus::Open,
            created_at_ms: 0,
        };
        assert!(!order.to_json().to_string().contains("agent_SECRET"));
    }
}
