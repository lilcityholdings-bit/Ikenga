//! Ikenga Clear: a clearinghouse for bot-to-bot payments.
//!
//! # The problem
//!
//! Agents pay each other constantly and in tiny amounts: a data bot sells a quote to a trading
//! bot, the trading bot sells a forecast to a research bot, the research bot buys compute from
//! the data bot. Every one of those payments, settled on its own, pays the rail's fixed cost —
//! 30¢ on a card, a gas fee on-chain, a wire fee at a bank. For a 5¢ API call that cost is larger
//! than the payment, so the payment simply doesn't happen. That is the ceiling on the agent
//! economy today: not willingness to pay, but the cost of *moving* each payment.
//!
//! # The mechanism: multilateral netting
//!
//! Banks solved this a century ago. They don't settle every cheque; a clearinghouse adds up who
//! owes whom over a period and each bank pays or receives one net amount. This module does that
//! for agents:
//!
//! 1. **Record, don't pay.** An agent that owes another records an obligation here, signed with
//!    its own key (`POST /v1/clear/obligations`). No money moves. It costs nothing, so a
//!    one-tenth-of-a-cent payment is as cheap to record as a thousand-dollar one.
//! 2. **Net.** At the end of each cycle every agent's obligations are summed into a single net
//!    position per asset. A→B 10, B→C 10, C→A 10 is thirty dollars of payments and *zero*
//!    dollars of settlement.
//! 3. **Settle the minimum.** The net positions are turned into a short list of transfers — never
//!    more than one fewer than the number of agents with a non-zero position, however many
//!    payments went into it. Agents make those transfers themselves, from their own wallets.
//!
//! # Non-custodial
//!
//! This server never holds anyone's money. It holds *records*. Settlement instructions tell a
//! debtor whom to pay; the recipient confirms receipt. That is deliberate — see
//! `docs/CLEARING.md` for why it keeps the regulatory footprint small, and what it does not
//! remove.
//!
//! # How this earns: a share of what it saves, and nothing else
//!
//! Every cycle computes, per agent, what its payments *would* have cost settled one by one on the
//! configured rail (`counterfactual_cost`), and what its netted transfers actually cost
//! (`netted_cost`). The fee is a fixed share of the difference, per agent — capped at a small
//! fraction of what the agent sent, because against a card rail a micro-payment's "saving" can
//! exceed the payment itself. An agent that saved nothing pays nothing. The fee rides through the same netting — the
//! fee account is just one more creditor — so collecting it usually adds no transfer at all, and
//! the one transfer that pays it is sent net of that transfer's own rail cost, so it never costs
//! the payer anything extra.
//!
//! The guarantee, checked on every cycle and pinned by a randomized test: **no agent's fee
//! exceeds `share` of what the netting actually saved that agent**, measured against the final
//! transfer list including the fee itself. If a pathological cycle would break that, fees for
//! that asset are dropped for the cycle rather than charged.
//!
//! # Credit risk, stated plainly
//!
//! Netting means you may be paid by an agent you never dealt with: if A owes B and B owes C, the
//! plan may have A pay C directly. If A then doesn't pay, C is short. Three things bound that:
//!
//! - **Caps.** An agent's outstanding net debit can never exceed a limit set by its trust band.
//!   New agents get a small one. Gross volume is unlimited; only the *net* is capped, so a bot
//!   that both buys and sells can move far more than its cap.
//! - **Exclusion.** An agent with an overdue instruction can record no new obligations anywhere
//!   on the network until it settles. Being cut off from every counterparty at once is a much
//!   larger penalty than the cap it could have defaulted on.
//! - **A public credit check.** `GET /v1/clear/capacity/{agent}` tells a seller, before it
//!   delivers, whether a buyer can take on an obligation of that size and whether it is in good
//!   standing.
//!
//! None of that makes a default impossible — it bounds it. A loss-sharing fund is the obvious
//! next layer and is described, unbuilt, in `docs/CLEARING.md`.
//!
//! # Precision
//!
//! Amounts are integer micro-units (1e-6) end to end. Netting's whole claim is that positions sum
//! to exactly zero, and that is only checkable if arithmetic is exact.

use std::collections::{BTreeMap, HashMap};
use std::sync::Mutex;

use crate::json::Json;
use crate::trust::TrustBand;
use crate::wal::{Record, Wal};

pub const MICROS_PER_UNIT: i64 = 1_000_000;
/// The account fees are netted into. Settled by the operator, confirmed with the owner key.
pub const FEE_ACCOUNT: &str = "ikenga_clear";
/// Largest single obligation, in whole units. Bounds arithmetic, not business: nothing here is
/// sized for this and a cap would refuse it first anyway.
pub const MAX_OBLIGATION_UNITS: i64 = 10_000_000;
/// A backstop on memory between cycles. Past this the cycle should simply close more often.
pub const MAX_OPEN_OBLIGATIONS: usize = 500_000;
pub const MAX_MEMO_LEN: usize = 200;
pub const MAX_REF_LEN: usize = 64;

pub fn to_micros(units: f64) -> Option<i64> {
    if !units.is_finite() || units <= 0.0 {
        return None;
    }
    let m = (units * MICROS_PER_UNIT as f64).round();
    if m < 1.0 || m > (MAX_OBLIGATION_UNITS * MICROS_PER_UNIT) as f64 {
        return None;
    }
    Some(m as i64)
}

pub fn from_micros(m: i64) -> f64 {
    m as f64 / MICROS_PER_UNIT as f64
}

fn money(m: i64) -> Json {
    Json::num(from_micros(m))
}

/// What one transfer costs on the rail the agents would otherwise have used.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RailCost {
    pub fixed_micros: i64,
    /// Proportional cost in basis points (290 = 2.9%).
    pub bps: i64,
}

impl RailCost {
    pub fn cost(&self, amount_micros: i64) -> i64 {
        self.fixed_micros + ((amount_micros as i128 * self.bps as i128) / 10_000) as i64
    }
}

#[derive(Debug, Clone)]
pub struct ClearConfig {
    pub rail: RailCost,
    pub rail_label: String,
    /// Share of each agent's saving taken as the fee, in basis points (2000 = 20%).
    pub share_bps: i64,
    /// Ceiling on the fee as a share of what the agent sent, in basis points (100 = 1%).
    ///
    /// Without it the savings share is dishonest at the small end. Against a card rail, a 5¢
    /// payment "costs" 30¢ to send, so the measured saving is six times the payment and a 20%
    /// share of it is more than the payment was worth. Nobody was ever going to pay 30¢ to move
    /// 5¢ — they just wouldn't have transacted — so that saving is partly fictional. The fee is
    /// the lesser of the two: a share of real savings on large payments, a small fraction of
    /// volume on micro-payments.
    pub max_fee_bps_of_volume: i64,
    /// How long after a cycle closes its instructions must be settled.
    pub settle_window_ms: i64,
    /// Net-debit cap per trust band, New through Elite, in micros.
    pub caps_micros: [i64; 6],
    /// Seconds between automatic cycle closes. 0 disables them.
    pub cycle_secs: u64,
}

impl Default for ClearConfig {
    fn default() -> Self {
        ClearConfig {
            // The default is the rail most agent developers can actually reach today: a card
            // processor's standard pricing. On a cheap chain the fixed cost is lower and so is the
            // saving — the operator sets the rail their users would really have used.
            rail: RailCost { fixed_micros: 300_000, bps: 290 },
            rail_label: "card-style: 0.30 + 2.9% per transfer".to_string(),
            share_bps: 2_000,
            max_fee_bps_of_volume: 100,
            settle_window_ms: 24 * 60 * 60 * 1000,
            caps_micros: [25, 250, 2_500, 25_000, 100_000, 250_000].map(|u| u * MICROS_PER_UNIT),
            cycle_secs: 3_600,
        }
    }
}

impl ClearConfig {
    pub fn from_env() -> Self {
        let mut c = ClearConfig::default();
        let num = |k: &str| std::env::var(k).ok().and_then(|v| v.trim().parse::<f64>().ok());
        if let Some(f) = num("IKENGA_CLEAR_RAIL_FIXED") {
            if f.is_finite() && f >= 0.0 {
                c.rail.fixed_micros = (f * MICROS_PER_UNIT as f64).round() as i64;
            }
        }
        if let Some(b) = num("IKENGA_CLEAR_RAIL_BPS") {
            if b.is_finite() && (0.0..=10_000.0).contains(&b) {
                c.rail.bps = b.round() as i64;
            }
        }
        if num("IKENGA_CLEAR_RAIL_FIXED").is_some() || num("IKENGA_CLEAR_RAIL_BPS").is_some() {
            c.rail_label = format!(
                "{:.2} + {:.2}% per transfer",
                from_micros(c.rail.fixed_micros),
                c.rail.bps as f64 / 100.0
            );
        }
        if let Ok(l) = std::env::var("IKENGA_CLEAR_RAIL_LABEL") {
            if !l.trim().is_empty() {
                c.rail_label = l.trim().to_string();
            }
        }
        // Capped at half: a clearinghouse that keeps most of the saving has stopped being a
        // reason to use it.
        if let Some(s) = num("IKENGA_CLEAR_SHARE") {
            if s.is_finite() && (0.0..=0.5).contains(&s) {
                c.share_bps = (s * 10_000.0).round() as i64;
            }
        }
        if let Some(b) = num("IKENGA_CLEAR_MAX_FEE_BPS") {
            if b.is_finite() && (0.0..=1_000.0).contains(&b) {
                c.max_fee_bps_of_volume = b.round() as i64;
            }
        }
        if let Some(w) = num("IKENGA_CLEAR_SETTLE_SECS") {
            if w.is_finite() && w >= 1.0 {
                c.settle_window_ms = (w * 1000.0) as i64;
            }
        }
        if let Some(s) = num("IKENGA_CLEAR_CYCLE_SECS") {
            if s.is_finite() && s >= 0.0 {
                c.cycle_secs = s as u64;
            }
        }
        if let Ok(raw) = std::env::var("IKENGA_CLEAR_CAPS") {
            let parsed: Vec<i64> = raw
                .split(',')
                .filter_map(|p| p.trim().parse::<f64>().ok())
                .filter(|v| v.is_finite() && *v >= 0.0)
                .map(|v| (v * MICROS_PER_UNIT as f64).round() as i64)
                .collect();
            if parsed.len() == 6 {
                c.caps_micros.copy_from_slice(&parsed);
            } else {
                eprintln!("IKENGA_CLEAR_CAPS needs six comma-separated numbers (New..Elite); ignored");
            }
        }
        c
    }

    pub fn cap_for(&self, band: TrustBand) -> i64 {
        let i = match band {
            TrustBand::New => 0,
            TrustBand::Developing => 1,
            TrustBand::Established => 2,
            TrustBand::Trusted => 3,
            TrustBand::HighlyTrusted => 4,
            TrustBand::Elite => 5,
        };
        self.caps_micros[i]
    }
}

/// A promise to pay, recorded instead of a payment.
#[derive(Debug, Clone, PartialEq)]
pub struct Obligation {
    pub obligation_id: String,
    pub payer: String,
    pub payee: String,
    pub asset: String,
    pub amount_micros: i64,
    pub memo: String,
    /// The payer's own idempotency key. Recording the same reference twice in one cycle returns
    /// the first obligation instead of owing twice — a retrying bot must not double-pay.
    pub reference: Option<String>,
    pub created_at_ms: i64,
}

impl Obligation {
    pub fn to_json(&self) -> Json {
        Json::obj(vec![
            ("obligation_id", Json::str(self.obligation_id.clone())),
            ("payer", Json::str(self.payer.clone())),
            ("payee", Json::str(self.payee.clone())),
            ("asset", Json::str(self.asset.clone())),
            ("amount", money(self.amount_micros)),
            ("memo", Json::str(self.memo.clone())),
            ("ref", self.reference.clone().map(Json::str).unwrap_or(Json::Null)),
            ("created_at_ms", Json::num(self.created_at_ms as f64)),
        ])
    }

    fn from_json(j: &Json) -> Option<Obligation> {
        let s = |k: &str| j.get(k).and_then(Json::as_str).map(|v| v.to_string());
        Some(Obligation {
            obligation_id: s("obligation_id")?,
            payer: s("payer")?,
            payee: s("payee")?,
            asset: s("asset")?,
            amount_micros: (j.get("amount")?.as_f64()? * MICROS_PER_UNIT as f64).round() as i64,
            memo: s("memo")?,
            reference: s("ref"),
            created_at_ms: j.get("created_at_ms")?.as_f64()? as i64,
        })
    }

    /// A fee carried from an earlier cycle, not a payment. It is netted like any obligation but
    /// is not commerce: it earns no saving and is charged no fee.
    pub fn is_fee_carry(&self) -> bool {
        self.payee == FEE_ACCOUNT
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InstructionStatus {
    /// Waiting for the payer to send it.
    Pending,
    /// The payer says it sent it. Not settled until the recipient agrees.
    Paid,
    /// The recipient confirmed receipt. Final.
    Settled,
}

impl InstructionStatus {
    pub fn label(&self) -> &'static str {
        match self {
            InstructionStatus::Pending => "pending",
            InstructionStatus::Paid => "paid",
            InstructionStatus::Settled => "settled",
        }
    }
    fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "pending" => InstructionStatus::Pending,
            "paid" => InstructionStatus::Paid,
            "settled" => InstructionStatus::Settled,
            _ => return None,
        })
    }
}

/// One transfer a cycle asks for.
#[derive(Debug, Clone, PartialEq)]
pub struct Instruction {
    pub instruction_id: String,
    pub cycle_id: u64,
    pub from: String,
    pub to: String,
    pub asset: String,
    /// The position this transfer clears.
    pub amount_micros: i64,
    /// What the payer actually sends. Equal to `amount_micros` except on a transfer to the fee
    /// account, which is sent net of its own rail cost so collecting the fee never costs a payer
    /// a transfer fee on top.
    pub send_micros: i64,
    pub settle_by_ms: i64,
    pub status: InstructionStatus,
    pub tx_ref: Option<String>,
}

impl Instruction {
    pub fn is_fee(&self) -> bool {
        self.to == FEE_ACCOUNT
    }

    /// Overdue is derived from the clock, never stored: nothing has to run for an unpaid debt to
    /// become late, and nothing can forget that it did.
    pub fn is_overdue(&self, now_ms: i64) -> bool {
        self.status != InstructionStatus::Settled && now_ms > self.settle_by_ms
    }

    pub fn to_json(&self, now_ms: Option<i64>) -> Json {
        let mut pairs = vec![
            ("instruction_id", Json::str(self.instruction_id.clone())),
            ("cycle_id", Json::num(self.cycle_id as f64)),
            ("from", Json::str(self.from.clone())),
            ("to", Json::str(self.to.clone())),
            ("asset", Json::str(self.asset.clone())),
            ("amount", money(self.amount_micros)),
            ("send", money(self.send_micros)),
            ("settle_by_ms", Json::num(self.settle_by_ms as f64)),
            ("status", Json::str(self.status.label())),
            ("tx_ref", self.tx_ref.clone().map(Json::str).unwrap_or(Json::Null)),
            ("is_fee", Json::Bool(self.is_fee())),
        ];
        if let Some(now) = now_ms {
            pairs.push(("overdue", Json::Bool(self.is_overdue(now))));
        }
        Json::obj(pairs)
    }

    fn from_json(j: &Json) -> Option<Instruction> {
        let s = |k: &str| j.get(k).and_then(Json::as_str).map(|v| v.to_string());
        let m = |k: &str| j.get(k).and_then(Json::as_f64).map(|v| (v * MICROS_PER_UNIT as f64).round() as i64);
        Some(Instruction {
            instruction_id: s("instruction_id")?,
            cycle_id: j.get("cycle_id")?.as_f64()? as u64,
            from: s("from")?,
            to: s("to")?,
            asset: s("asset")?,
            amount_micros: m("amount")?,
            send_micros: m("send")?,
            settle_by_ms: j.get("settle_by_ms")?.as_f64()? as i64,
            status: InstructionStatus::parse(&s("status")?)?,
            tx_ref: s("tx_ref"),
        })
    }
}

/// One agent's result for one asset in one cycle — its savings receipt.
#[derive(Debug, Clone, PartialEq)]
pub struct AgentLine {
    pub agent: String,
    pub asset: String,
    pub payments_made: u64,
    pub gross_out_micros: i64,
    pub gross_in_micros: i64,
    /// Net position after netting, before the fee. Positive: receives.
    pub net_micros: i64,
    /// Its payments settled one at a time on the rail.
    pub counterfactual_cost_micros: i64,
    /// The rail cost of the transfers it is actually asked to make.
    pub netted_cost_micros: i64,
    pub fee_micros: i64,
}

impl AgentLine {
    pub fn saved_micros(&self) -> i64 {
        self.counterfactual_cost_micros - self.netted_cost_micros
    }

    pub fn to_json(&self) -> Json {
        Json::obj(vec![
            ("agent", Json::str(self.agent.clone())),
            ("asset", Json::str(self.asset.clone())),
            ("payments_made", Json::num(self.payments_made as f64)),
            ("gross_out", money(self.gross_out_micros)),
            ("gross_in", money(self.gross_in_micros)),
            ("net", money(self.net_micros)),
            ("cost_if_paid_one_by_one", money(self.counterfactual_cost_micros)),
            ("cost_netted", money(self.netted_cost_micros)),
            ("saved", money(self.saved_micros())),
            ("fee", money(self.fee_micros)),
            ("kept", money(self.saved_micros() - self.fee_micros)),
        ])
    }

    fn from_json(j: &Json) -> Option<AgentLine> {
        let s = |k: &str| j.get(k).and_then(Json::as_str).map(|v| v.to_string());
        let m = |k: &str| j.get(k).and_then(Json::as_f64).map(|v| (v * MICROS_PER_UNIT as f64).round() as i64);
        Some(AgentLine {
            agent: s("agent")?,
            asset: s("asset")?,
            payments_made: j.get("payments_made")?.as_f64()? as u64,
            gross_out_micros: m("gross_out")?,
            gross_in_micros: m("gross_in")?,
            net_micros: m("net")?,
            counterfactual_cost_micros: m("cost_if_paid_one_by_one")?,
            netted_cost_micros: m("cost_netted")?,
            fee_micros: m("fee")?,
        })
    }
}

/// Per-asset totals for a cycle.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct AssetTotals {
    pub asset: String,
    pub payments: u64,
    pub gross_micros: i64,
    pub transfers: u64,
    pub settled_value_micros: i64,
    pub cost_one_by_one_micros: i64,
    pub cost_netted_micros: i64,
    pub fees_micros: i64,
    /// Set when the fee guarantee could not be met for this asset and fees were dropped.
    pub fees_waived: bool,
}

impl AssetTotals {
    fn to_json(&self) -> Json {
        let compression = if self.gross_micros > 0 {
            1.0 - self.settled_value_micros as f64 / self.gross_micros as f64
        } else {
            0.0
        };
        Json::obj(vec![
            ("asset", Json::str(self.asset.clone())),
            ("payments", Json::num(self.payments as f64)),
            ("gross_value", money(self.gross_micros)),
            ("transfers", Json::num(self.transfers as f64)),
            ("settled_value", money(self.settled_value_micros)),
            ("value_compression", Json::num((compression * 10_000.0).round() / 10_000.0)),
            ("cost_if_paid_one_by_one", money(self.cost_one_by_one_micros)),
            ("cost_netted", money(self.cost_netted_micros)),
            ("saved", money(self.cost_one_by_one_micros - self.cost_netted_micros)),
            ("fees", money(self.fees_micros)),
            ("fees_waived", Json::Bool(self.fees_waived)),
        ])
    }

    fn from_json(j: &Json) -> Option<AssetTotals> {
        let m = |k: &str| j.get(k).and_then(Json::as_f64).map(|v| (v * MICROS_PER_UNIT as f64).round() as i64);
        let n = |k: &str| j.get(k).and_then(Json::as_f64).map(|v| v as u64);
        Some(AssetTotals {
            asset: j.get("asset")?.as_str()?.to_string(),
            payments: n("payments")?,
            gross_micros: m("gross_value")?,
            transfers: n("transfers")?,
            settled_value_micros: m("settled_value")?,
            cost_one_by_one_micros: m("cost_if_paid_one_by_one")?,
            cost_netted_micros: m("cost_netted")?,
            fees_micros: m("fees")?,
            fees_waived: matches!(j.get("fees_waived"), Some(Json::Bool(true))),
        })
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Cycle {
    pub cycle_id: u64,
    pub closed_at_ms: i64,
    pub settle_by_ms: i64,
    pub totals: Vec<AssetTotals>,
    pub lines: Vec<AgentLine>,
    pub instruction_ids: Vec<String>,
    /// Fees too small to be worth a transfer this cycle, carried into the next as obligations
    /// to the fee account. See `worth_sending`.
    pub carried: Vec<Obligation>,
    /// SHA-256 over every obligation and instruction in the cycle, in canonical form. Published
    /// so any agent holding its own records can check it was netted on the same facts as
    /// everyone else.
    pub digest: String,
}

impl Cycle {
    pub fn summary_json(&self) -> Json {
        Json::obj(vec![
            ("cycle_id", Json::num(self.cycle_id as f64)),
            ("closed_at_ms", Json::num(self.closed_at_ms as f64)),
            ("settle_by_ms", Json::num(self.settle_by_ms as f64)),
            ("assets", Json::Array(self.totals.iter().map(AssetTotals::to_json).collect())),
            ("participants", Json::num(self.lines.len() as f64)),
            ("digest", Json::str(self.digest.clone())),
        ])
    }

    /// The durable form: everything needed to rebuild the cycle without re-running the netting.
    fn to_log_json(&self, instructions: &[Instruction]) -> Json {
        Json::obj(vec![
            ("cycle_id", Json::num(self.cycle_id as f64)),
            ("closed_at_ms", Json::num(self.closed_at_ms as f64)),
            ("settle_by_ms", Json::num(self.settle_by_ms as f64)),
            ("totals", Json::Array(self.totals.iter().map(AssetTotals::to_json).collect())),
            ("lines", Json::Array(self.lines.iter().map(AgentLine::to_json).collect())),
            ("instructions", Json::Array(instructions.iter().map(|i| i.to_json(None)).collect())),
            ("carried", Json::Array(self.carried.iter().map(Obligation::to_json).collect())),
            ("digest", Json::str(self.digest.clone())),
        ])
    }

    fn from_log_json(j: &Json) -> Option<(Cycle, Vec<Instruction>)> {
        let instructions: Vec<Instruction> = j
            .get("instructions")?
            .as_array()?
            .iter()
            .map(Instruction::from_json)
            .collect::<Option<_>>()?;
        let cycle = Cycle {
            cycle_id: j.get("cycle_id")?.as_f64()? as u64,
            closed_at_ms: j.get("closed_at_ms")?.as_f64()? as i64,
            settle_by_ms: j.get("settle_by_ms")?.as_f64()? as i64,
            totals: j.get("totals")?.as_array()?.iter().map(AssetTotals::from_json).collect::<Option<_>>()?,
            lines: j.get("lines")?.as_array()?.iter().map(AgentLine::from_json).collect::<Option<_>>()?,
            instruction_ids: instructions.iter().map(|i| i.instruction_id.clone()).collect(),
            carried: j.get("carried")?.as_array()?.iter().map(Obligation::from_json).collect::<Option<_>>()?,
            digest: j.get("digest")?.as_str()?.to_string(),
        };
        Some((cycle, instructions))
    }
}

// ---------------------------------------------------------------------------------------------
// The netting itself: pure functions, no locks, no clock, no randomness.
// ---------------------------------------------------------------------------------------------

/// Turns net positions (positive: is owed) into transfers.
///
/// Two passes. First, any debtor and creditor whose positions are *exactly* equal are paired —
/// one transfer clears both, which the greedy pass can miss. Then the largest remaining debtor
/// pays the largest remaining creditor, repeatedly. Every transfer zeroes at least one side, so
/// the result has at most (non-zero positions − 1) transfers however many payments went in.
///
/// Finding the true minimum number of transfers is NP-hard (it is subset-sum in disguise); this
/// is the standard bound, and deterministic — ties break on agent id — so replaying the same
/// positions always yields the same plan.
pub fn plan_transfers(positions: &BTreeMap<String, i64>) -> Vec<(String, String, i64)> {
    debug_assert_eq!(positions.values().sum::<i64>(), 0, "positions must net to zero");
    let mut debtors: Vec<(String, i64)> =
        positions.iter().filter(|(_, v)| **v < 0).map(|(k, v)| (k.clone(), -v)).collect();
    let mut creditors: Vec<(String, i64)> =
        positions.iter().filter(|(_, v)| **v > 0).map(|(k, v)| (k.clone(), *v)).collect();
    let mut out = Vec::new();

    // Exact matches first.
    let mut by_amount: BTreeMap<i64, Vec<usize>> = BTreeMap::new();
    for (i, (_, amt)) in creditors.iter().enumerate() {
        by_amount.entry(*amt).or_default().push(i);
    }
    for list in by_amount.values_mut() {
        list.reverse(); // pop() then yields the lowest index, i.e. the lowest agent id
    }
    for d in debtors.iter_mut() {
        if let Some(list) = by_amount.get_mut(&d.1) {
            if let Some(ci) = list.pop() {
                out.push((d.0.clone(), creditors[ci].0.clone(), d.1));
                creditors[ci].1 = 0;
                d.1 = 0;
            }
        }
    }
    debtors.retain(|d| d.1 > 0);
    creditors.retain(|c| c.1 > 0);

    // Largest against largest.
    let order = |a: &(String, i64), b: &(String, i64)| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0));
    debtors.sort_by(order);
    creditors.sort_by(order);
    let (mut di, mut ci) = (0, 0);
    while di < debtors.len() && ci < creditors.len() {
        let amt = debtors[di].1.min(creditors[ci].1);
        out.push((debtors[di].0.clone(), creditors[ci].0.clone(), amt));
        debtors[di].1 -= amt;
        creditors[ci].1 -= amt;
        if debtors[di].1 == 0 {
            di += 1;
        }
        if creditors[ci].1 == 0 {
            ci += 1;
        }
    }
    out
}

/// What a transfer costs its payer. A transfer to the fee account is sent net of its own rail
/// cost, so to the payer it costs nothing beyond the fee itself.
fn transfer_cost(rail: &RailCost, to: &str, amount: i64) -> i64 {
    if to == FEE_ACCOUNT {
        0
    } else {
        rail.cost(amount)
    }
}

/// Whether a fee is worth its own transfer: the rail may take at most a tenth of it. Anything
/// smaller carries forward and accumulates. On a card rail that is about 4.20; on a cheap chain,
/// cents.
pub fn worth_sending(rail: &RailCost, amount: i64) -> bool {
    rail.cost(amount) as i128 * 10 <= amount as i128
}

fn costs_by_payer(rail: &RailCost, plan: &[(String, String, i64)]) -> HashMap<String, i64> {
    let mut m = HashMap::new();
    for (from, to, amt) in plan {
        *m.entry(from.clone()).or_insert(0) += transfer_cost(rail, to, *amt);
    }
    m
}

/// The fee for one agent: a share of what it saved, never more than a small fraction of what
/// it sent.
pub fn fee_for(cfg: &ClearConfig, saved: i64, gross_out: i64) -> i64 {
    let of_savings = saved.max(0) as i128 * cfg.share_bps as i128 / 10_000;
    let of_volume = gross_out.max(0) as i128 * cfg.max_fee_bps_of_volume as i128 / 10_000;
    of_savings.min(of_volume) as i64
}

/// The result of netting one asset.
#[derive(Debug, Clone)]
pub struct AssetResult {
    pub totals: AssetTotals,
    pub lines: Vec<AgentLine>,
    pub plan: Vec<(String, String, i64)>,
}

/// Nets one asset's obligations, prices the saving, and charges the fee.
///
/// The fee is computed, the plan rebuilt with the fee account as a creditor, and the guarantee
/// re-checked against that *final* plan — because adding a creditor can change who pays whom.
/// Any agent whose fee would exceed `share` of its real saving has its fee lowered to fit, and
/// the check repeats. If it hasn't settled after a few rounds, every fee for this asset is
/// waived for the cycle. Undercharging is always the fallback; overcharging never is.
pub fn net_asset(asset: &str, obligations: &[&Obligation], cfg: &ClearConfig) -> AssetResult {
    let rail = &cfg.rail;
    let mut positions: BTreeMap<String, i64> = BTreeMap::new();
    let mut lines: BTreeMap<String, AgentLine> = BTreeMap::new();
    let mut totals = AssetTotals { asset: asset.to_string(), ..Default::default() };
    let blank = |agent: &str| AgentLine {
        agent: agent.to_string(),
        asset: asset.to_string(),
        payments_made: 0,
        gross_out_micros: 0,
        gross_in_micros: 0,
        net_micros: 0,
        counterfactual_cost_micros: 0,
        netted_cost_micros: 0,
        fee_micros: 0,
    };

    for o in obligations {
        *positions.entry(o.payer.clone()).or_insert(0) -= o.amount_micros;
        *positions.entry(o.payee.clone()).or_insert(0) += o.amount_micros;
        if o.is_fee_carry() {
            lines.entry(o.payer.clone()).or_insert_with(|| blank(&o.payer));
            continue;
        }
        let p = lines.entry(o.payer.clone()).or_insert_with(|| blank(&o.payer));
        p.payments_made += 1;
        p.gross_out_micros += o.amount_micros;
        p.counterfactual_cost_micros += rail.cost(o.amount_micros);
        lines.entry(o.payee.clone()).or_insert_with(|| blank(&o.payee)).gross_in_micros += o.amount_micros;
        totals.payments += 1;
        totals.gross_micros += o.amount_micros;
        totals.cost_one_by_one_micros += rail.cost(o.amount_micros);
    }
    for (agent, line) in lines.iter_mut() {
        line.net_micros = positions[agent];
    }
    positions.retain(|_, v| *v != 0);

    let base_plan = plan_transfers(&positions);
    let base_costs = costs_by_payer(rail, &base_plan);

    // Opening fees from the plan without them.
    let mut fees: BTreeMap<String, i64> = lines
        .iter()
        .map(|(a, l)| {
            let saved = l.counterfactual_cost_micros - base_costs.get(a).copied().unwrap_or(0);
            (a.clone(), fee_for(cfg, saved, l.gross_out_micros))
        })
        .collect();

    let build = |fees: &BTreeMap<String, i64>| {
        let mut pos = positions.clone();
        let mut total = 0;
        for (a, f) in fees {
            if *f > 0 {
                *pos.entry(a.clone()).or_insert(0) -= f;
                total += f;
            }
        }
        if total > 0 {
            *pos.entry(FEE_ACCOUNT.to_string()).or_insert(0) += total;
        }
        pos.retain(|_, v| *v != 0);
        plan_transfers(&pos)
    };

    let mut plan = build(&fees);
    let mut ok = false;
    for _ in 0..4 {
        let costs = costs_by_payer(rail, &plan);
        let mut adjusted = false;
        for (a, f) in fees.iter_mut() {
            let saved = lines[a].counterfactual_cost_micros - costs.get(a).copied().unwrap_or(0);
            let allowed = fee_for(cfg, saved, lines[a].gross_out_micros);
            if *f > allowed {
                *f = allowed;
                adjusted = true;
            }
        }
        if !adjusted {
            ok = true;
            break;
        }
        plan = build(&fees);
    }
    if !ok {
        for f in fees.values_mut() {
            *f = 0;
        }
        plan = base_plan;
        totals.fees_waived = true;
    }

    let costs = costs_by_payer(rail, &plan);
    for (a, line) in lines.iter_mut() {
        line.netted_cost_micros = costs.get(a).copied().unwrap_or(0);
        line.fee_micros = fees.get(a).copied().unwrap_or(0);
    }
    totals.transfers = plan.len() as u64;
    totals.settled_value_micros = plan.iter().filter(|(_, to, _)| to != FEE_ACCOUNT).map(|p| p.2).sum();
    totals.cost_netted_micros = costs.values().sum();
    totals.fees_micros = fees.values().sum();

    AssetResult { totals, lines: lines.into_values().collect(), plan }
}

fn digest_of(obligations: &[Obligation], instructions: &[Instruction]) -> String {
    let mut canon = String::new();
    for o in obligations {
        canon.push_str(&format!(
            "O|{}|{}|{}|{}|{}\n",
            o.obligation_id, o.payer, o.payee, o.asset, o.amount_micros
        ));
    }
    for i in instructions {
        canon.push_str(&format!(
            "I|{}|{}|{}|{}|{}|{}\n",
            i.instruction_id, i.from, i.to, i.asset, i.amount_micros, i.send_micros
        ));
    }
    crate::crypto::hex_encode(&crate::crypto::sha256(canon.as_bytes()))
}

// ---------------------------------------------------------------------------------------------
// The clearinghouse: state, limits, durability.
// ---------------------------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub enum ClearError {
    SelfPayment,
    UnknownPayee,
    BadAsset,
    BadAmount,
    MemoTooLong,
    BadReference,
    /// Has an unsettled instruction past its deadline.
    Frozen { overdue: usize },
    OverCap { cap_micros: i64, available_micros: i64 },
    Full,
    NoSuchInstruction,
    NotYours,
    AlreadySettled,
    NoSuchCycle,
}

impl ClearError {
    pub fn status(&self) -> u16 {
        match self {
            ClearError::Frozen { .. } | ClearError::OverCap { .. } => 402,
            ClearError::NoSuchInstruction | ClearError::NoSuchCycle | ClearError::UnknownPayee => 404,
            ClearError::NotYours => 403,
            ClearError::AlreadySettled => 409,
            ClearError::Full => 503,
            _ => 400,
        }
    }

    pub fn code(&self) -> &'static str {
        match self {
            ClearError::SelfPayment => "SELF_PAYMENT",
            ClearError::UnknownPayee => "UNKNOWN_PAYEE",
            ClearError::BadAsset => "BAD_ASSET",
            ClearError::BadAmount => "BAD_AMOUNT",
            ClearError::MemoTooLong => "MEMO_TOO_LONG",
            ClearError::BadReference => "BAD_REFERENCE",
            ClearError::Frozen { .. } => "OVERDUE_SETTLEMENT",
            ClearError::OverCap { .. } => "OVER_CREDIT_CAP",
            ClearError::Full => "CYCLE_FULL",
            ClearError::NoSuchInstruction => "NO_SUCH_INSTRUCTION",
            ClearError::NotYours => "NOT_YOUR_INSTRUCTION",
            ClearError::AlreadySettled => "ALREADY_SETTLED",
            ClearError::NoSuchCycle => "NO_SUCH_CYCLE",
        }
    }

    pub fn message(&self) -> String {
        match self {
            ClearError::SelfPayment => "an agent cannot owe itself".into(),
            ClearError::UnknownPayee => "payee is not a registered agent".into(),
            ClearError::BadAsset => "asset must be 2-10 uppercase letters or digits, e.g. USDC".into(),
            ClearError::BadAmount => format!(
                "amount must be positive, at least 0.000001 and at most {MAX_OBLIGATION_UNITS}"
            ),
            ClearError::MemoTooLong => format!("memo is limited to {MAX_MEMO_LEN} characters"),
            ClearError::BadReference => format!(
                "ref must be 1-{MAX_REF_LEN} characters of letters, digits, '-', '_' or '.'"
            ),
            ClearError::Frozen { overdue } => format!(
                "you have {overdue} settlement instruction(s) past their deadline. No new \
                 obligations anywhere on the network until they are settled — see \
                 GET /v1/clear/position"
            ),
            ClearError::OverCap { cap_micros, available_micros } => format!(
                "this would take your net debit past your cap of {} (available: {}). Gross \
                 volume is unlimited; only what you owe net of what you are owed is capped. \
                 Settle outstanding instructions, wait for payments in, or raise your trust band.",
                from_micros(*cap_micros),
                from_micros(*available_micros)
            ),
            ClearError::Full => "too many open obligations; the cycle must close first".into(),
            ClearError::NoSuchInstruction => "no such instruction".into(),
            ClearError::NotYours => "only this instruction's payer may mark it paid, and only its \
                                     recipient may confirm it"
                .into(),
            ClearError::AlreadySettled => "this instruction is already settled".into(),
            ClearError::NoSuchCycle => "no such cycle".into(),
        }
    }
}

pub fn valid_asset(a: &str) -> bool {
    (2..=10).contains(&a.len()) && a.bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit())
}

fn valid_ref(r: &str) -> bool {
    (1..=MAX_REF_LEN).contains(&r.len())
        && r.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
}

#[derive(Default)]
struct Inner {
    open: Vec<Obligation>,
    /// (payer, reference) → obligation id, for the open cycle.
    refs: HashMap<(String, String), String>,
    /// (agent, asset) → net position in the open cycle.
    open_net: HashMap<(String, String), i64>,
    cycles: Vec<Cycle>,
    instructions: BTreeMap<String, Instruction>,
    next_cycle: u64,
}

impl Inner {
    fn apply_obligation(&mut self, o: Obligation) {
        *self.open_net.entry((o.payer.clone(), o.asset.clone())).or_insert(0) -= o.amount_micros;
        *self.open_net.entry((o.payee.clone(), o.asset.clone())).or_insert(0) += o.amount_micros;
        if let Some(r) = &o.reference {
            self.refs.insert((o.payer.clone(), r.clone()), o.obligation_id.clone());
        }
        self.open.push(o);
    }

    fn apply_cycle(&mut self, cycle: Cycle, instructions: Vec<Instruction>) {
        self.open.clear();
        self.refs.clear();
        self.open_net.clear();
        self.next_cycle = self.next_cycle.max(cycle.cycle_id + 1);
        for i in instructions {
            self.instructions.insert(i.instruction_id.clone(), i);
        }
        for o in &cycle.carried {
            self.apply_obligation(o.clone());
        }
        self.cycles.push(cycle);
    }

    /// Unsettled debit from closed cycles.
    fn outstanding(&self, agent: &str, asset: &str) -> i64 {
        self.instructions
            .values()
            .filter(|i| i.from == agent && i.asset == asset && i.status != InstructionStatus::Settled)
            .map(|i| i.amount_micros)
            .sum()
    }

    fn overdue_count(&self, agent: &str, now_ms: i64) -> usize {
        self.instructions.values().filter(|i| i.from == agent && i.is_overdue(now_ms)).count()
    }

    fn exposure_after(&self, agent: &str, asset: &str, extra_debit: i64) -> i64 {
        let open = self.open_net.get(&(agent.to_string(), asset.to_string())).copied().unwrap_or(0);
        self.outstanding(agent, asset) + (-(open - extra_debit)).max(0)
    }
}

/// Lifetime numbers, published as the proof the service does what it says.
#[derive(Debug, Clone, Default)]
pub struct Lifetime {
    pub cycles: u64,
    pub payments: u64,
    pub transfers: u64,
    pub gross_micros: i64,
    pub saved_micros: i64,
    pub fees_micros: i64,
}

pub struct ClearingHouse {
    pub config: ClearConfig,
    inner: Mutex<Inner>,
}

impl Default for ClearingHouse {
    fn default() -> Self {
        ClearingHouse::new(ClearConfig::default())
    }
}

pub struct Capacity {
    pub cap_micros: i64,
    pub exposure_micros: i64,
    pub overdue: usize,
}

impl ClearingHouse {
    pub fn new(config: ClearConfig) -> Self {
        ClearingHouse { config, inner: Mutex::new(Inner { next_cycle: 1, ..Default::default() }) }
    }

    /// Records a promise to pay. The WAL append happens under the same lock as the change, so
    /// log order is exactly the order obligations and cycle closes happened in — which is what
    /// lets replay assign every obligation to the right cycle without storing the assignment.
    #[allow(clippy::too_many_arguments)]
    pub fn record(
        &self,
        wal: &Wal,
        payer: &str,
        payee: &str,
        asset: &str,
        amount_micros: i64,
        memo: &str,
        reference: Option<&str>,
        cap_micros: i64,
        now_ms: i64,
    ) -> Result<(Obligation, bool), ClearError> {
        if payer == payee {
            return Err(ClearError::SelfPayment);
        }
        if payee == FEE_ACCOUNT {
            return Err(ClearError::UnknownPayee);
        }
        if !valid_asset(asset) {
            return Err(ClearError::BadAsset);
        }
        if amount_micros < 1 || amount_micros > MAX_OBLIGATION_UNITS * MICROS_PER_UNIT {
            return Err(ClearError::BadAmount);
        }
        if memo.chars().count() > MAX_MEMO_LEN {
            return Err(ClearError::MemoTooLong);
        }
        if let Some(r) = reference {
            if !valid_ref(r) {
                return Err(ClearError::BadReference);
            }
        }

        let mut inner = self.inner.lock().unwrap();
        if let Some(r) = reference {
            if let Some(id) = inner.refs.get(&(payer.to_string(), r.to_string())) {
                let existing = inner.open.iter().find(|o| &o.obligation_id == id).cloned();
                if let Some(o) = existing {
                    return Ok((o, false));
                }
            }
        }
        let overdue = inner.overdue_count(payer, now_ms);
        if overdue > 0 {
            return Err(ClearError::Frozen { overdue });
        }
        let exposure = inner.exposure_after(payer, asset, amount_micros);
        if exposure > cap_micros {
            let now_exposure = inner.exposure_after(payer, asset, 0);
            return Err(ClearError::OverCap {
                cap_micros,
                available_micros: (cap_micros - now_exposure).max(0),
            });
        }
        if inner.open.len() >= MAX_OPEN_OBLIGATIONS {
            return Err(ClearError::Full);
        }

        let o = Obligation {
            obligation_id: crate::privacy::random_id("obl_"),
            payer: payer.to_string(),
            payee: payee.to_string(),
            asset: asset.to_string(),
            amount_micros,
            memo: memo.to_string(),
            reference: reference.map(str::to_string),
            created_at_ms: now_ms,
        };
        wal.append(&[Record::ClearObligation {
            obligation_id: o.obligation_id.clone(),
            payer: o.payer.clone(),
            payee: o.payee.clone(),
            asset: o.asset.clone(),
            amount_micros: o.amount_micros,
            memo: o.memo.clone(),
            reference: o.reference.clone(),
            created_at_ms: o.created_at_ms,
        }]);
        inner.apply_obligation(o.clone());
        Ok((o, true))
    }

    /// Closes the open cycle: nets every asset, prices the saving, issues instructions.
    /// Returns None when there was nothing to net.
    pub fn close_cycle(&self, wal: &Wal, now_ms: i64) -> Option<Cycle> {
        let mut inner = self.inner.lock().unwrap();
        // Carried fees alone are not a reason to close: they would only carry again, and an
        // hourly timer would fill the history with empty cycles.
        if inner.open.iter().all(Obligation::is_fee_carry) {
            return None;
        }
        let cycle_id = inner.next_cycle;
        let settle_by_ms = now_ms + self.config.settle_window_ms;

        let mut by_asset: BTreeMap<String, Vec<&Obligation>> = BTreeMap::new();
        for o in &inner.open {
            by_asset.entry(o.asset.clone()).or_default().push(o);
        }
        let mut totals = Vec::new();
        let mut lines = Vec::new();
        let mut instructions = Vec::new();
        let mut carried = Vec::new();
        for (asset, obs) in &by_asset {
            let r = net_asset(asset, obs, &self.config);
            for (from, to, amt) in r.plan {
                let send = if to == FEE_ACCOUNT {
                    if !worth_sending(&self.config.rail, amt) {
                        // Not worth a transfer yet. Owed next cycle instead, where it nets
                        // against whatever this agent is owed and grows until it is.
                        carried.push(Obligation {
                            obligation_id: crate::privacy::random_id("obl_"),
                            payer: from,
                            payee: to,
                            asset: asset.clone(),
                            amount_micros: amt,
                            memo: format!("fee carried from cycle {cycle_id}"),
                            reference: None,
                            created_at_ms: now_ms,
                        });
                        continue;
                    }
                    amt - self.config.rail.cost(amt)
                } else {
                    amt
                };
                instructions.push(Instruction {
                    instruction_id: crate::privacy::random_id("ins_"),
                    cycle_id,
                    from,
                    to,
                    asset: asset.clone(),
                    amount_micros: amt,
                    send_micros: send,
                    settle_by_ms,
                    status: InstructionStatus::Pending,
                    tx_ref: None,
                });
            }
            totals.push(r.totals);
            lines.extend(r.lines);
        }
        let digest = digest_of(&inner.open, &instructions);
        let cycle = Cycle {
            cycle_id,
            closed_at_ms: now_ms,
            settle_by_ms,
            totals,
            lines,
            instruction_ids: instructions.iter().map(|i| i.instruction_id.clone()).collect(),
            carried,
            digest,
        };
        wal.append(&[Record::ClearCycle { snapshot: cycle.to_log_json(&instructions) }]);
        inner.apply_cycle(cycle.clone(), instructions);
        Some(cycle)
    }

    /// The payer says it sent the money. Informational until the recipient confirms.
    pub fn mark_paid(
        &self,
        wal: &Wal,
        instruction_id: &str,
        agent: &str,
        tx_ref: Option<&str>,
        now_ms: i64,
    ) -> Result<Instruction, ClearError> {
        let mut inner = self.inner.lock().unwrap();
        let i = inner.instructions.get_mut(instruction_id).ok_or(ClearError::NoSuchInstruction)?;
        if i.from != agent {
            return Err(ClearError::NotYours);
        }
        if i.status == InstructionStatus::Settled {
            return Err(ClearError::AlreadySettled);
        }
        wal.append(&[Record::ClearStatus {
            instruction_id: instruction_id.to_string(),
            status: "paid".into(),
            tx_ref: tx_ref.map(str::to_string),
            at_ms: now_ms,
        }]);
        i.status = InstructionStatus::Paid;
        i.tx_ref = tx_ref.map(str::to_string);
        Ok(i.clone())
    }

    /// The recipient confirms receipt. `as_owner` lets the operator confirm transfers to the fee
    /// account, which no agent key controls.
    pub fn confirm(
        &self,
        wal: &Wal,
        instruction_id: &str,
        agent: Option<&str>,
        as_owner: bool,
        now_ms: i64,
    ) -> Result<Instruction, ClearError> {
        let mut inner = self.inner.lock().unwrap();
        let i = inner.instructions.get_mut(instruction_id).ok_or(ClearError::NoSuchInstruction)?;
        let allowed = match agent {
            Some(a) => i.to == a,
            None => as_owner && i.to == FEE_ACCOUNT,
        };
        if !allowed {
            return Err(ClearError::NotYours);
        }
        if i.status == InstructionStatus::Settled {
            return Err(ClearError::AlreadySettled);
        }
        wal.append(&[Record::ClearStatus {
            instruction_id: instruction_id.to_string(),
            status: "settled".into(),
            tx_ref: i.tx_ref.clone(),
            at_ms: now_ms,
        }]);
        i.status = InstructionStatus::Settled;
        Ok(i.clone())
    }

    // ---- replay ----

    pub fn replay_obligation(&self, o: Obligation) {
        self.inner.lock().unwrap().apply_obligation(o);
    }

    pub fn replay_cycle(&self, snapshot: &Json) -> bool {
        match Cycle::from_log_json(snapshot) {
            Some((c, ins)) => {
                self.inner.lock().unwrap().apply_cycle(c, ins);
                true
            }
            None => false,
        }
    }

    pub fn replay_status(&self, instruction_id: &str, status: &str, tx_ref: Option<String>) {
        let mut inner = self.inner.lock().unwrap();
        if let (Some(i), Some(st)) =
            (inner.instructions.get_mut(instruction_id), InstructionStatus::parse(status))
        {
            i.status = st;
            i.tx_ref = tx_ref;
        }
    }

    // ---- reads ----

    pub fn capacity(&self, agent: &str, asset: &str, cap_micros: i64, now_ms: i64) -> Capacity {
        let inner = self.inner.lock().unwrap();
        Capacity {
            cap_micros,
            exposure_micros: inner.exposure_after(agent, asset, 0),
            overdue: inner.overdue_count(agent, now_ms),
        }
    }

    pub fn lifetime(&self) -> Lifetime {
        let inner = self.inner.lock().unwrap();
        let mut l = Lifetime { cycles: inner.cycles.len() as u64, ..Default::default() };
        for c in &inner.cycles {
            for t in &c.totals {
                l.payments += t.payments;
                l.transfers += t.transfers;
                l.gross_micros += t.gross_micros;
                l.saved_micros += t.cost_one_by_one_micros - t.cost_netted_micros;
                l.fees_micros += t.fees_micros;
            }
        }
        l
    }

    pub fn open_stats(&self) -> (usize, i64) {
        let inner = self.inner.lock().unwrap();
        (inner.open.len(), inner.open.iter().map(|o| o.amount_micros).sum())
    }

    pub fn recent_cycles(&self, n: usize) -> Vec<Cycle> {
        let inner = self.inner.lock().unwrap();
        inner.cycles.iter().rev().take(n).cloned().collect()
    }

    pub fn cycle(&self, cycle_id: u64) -> Option<(Cycle, Vec<Instruction>)> {
        let inner = self.inner.lock().unwrap();
        let c = inner.cycles.iter().find(|c| c.cycle_id == cycle_id)?.clone();
        let ins = c.instruction_ids.iter().filter_map(|id| inner.instructions.get(id).cloned()).collect();
        Some((c, ins))
    }

    pub fn instruction(&self, id: &str) -> Option<Instruction> {
        self.inner.lock().unwrap().instructions.get(id).cloned()
    }

    /// Everything one agent needs: its open obligations both ways, its net per asset, and every
    /// unsettled instruction it is party to.
    pub fn position(&self, agent: &str) -> (Vec<Obligation>, Vec<(String, i64)>, Vec<Instruction>) {
        let inner = self.inner.lock().unwrap();
        let open: Vec<Obligation> = inner
            .open
            .iter()
            .filter(|o| o.payer == agent || o.payee == agent)
            .cloned()
            .collect();
        let mut net: Vec<(String, i64)> = inner
            .open_net
            .iter()
            .filter(|((a, _), v)| a == agent && **v != 0)
            .map(|((_, asset), v)| (asset.clone(), *v))
            .collect();
        net.sort();
        let ins = inner
            .instructions
            .values()
            .filter(|i| (i.from == agent || i.to == agent) && i.status != InstructionStatus::Settled)
            .cloned()
            .collect();
        (open, net, ins)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ob(payer: &str, payee: &str, units: f64) -> Obligation {
        Obligation {
            obligation_id: format!("o_{payer}_{payee}_{units}"),
            payer: payer.into(),
            payee: payee.into(),
            asset: "USDC".into(),
            amount_micros: to_micros(units).unwrap(),
            memo: String::new(),
            reference: None,
            created_at_ms: 0,
        }
    }

    fn apply(plan: &[(String, String, i64)]) -> BTreeMap<String, i64> {
        let mut m = BTreeMap::new();
        for (f, t, a) in plan {
            *m.entry(f.clone()).or_insert(0) -= a;
            *m.entry(t.clone()).or_insert(0) += a;
        }
        m.retain(|_, v| *v != 0);
        m
    }

    #[test]
    fn a_perfect_circle_settles_with_no_transfers_at_all() {
        let obs = [ob("a", "b", 10.0), ob("b", "c", 10.0), ob("c", "a", 10.0)];
        let refs: Vec<&Obligation> = obs.iter().collect();
        let r = net_asset("USDC", &refs, &ClearConfig::default());
        // Thirty dollars of payments, zero dollars of settlement between agents. The only
        // transfers left are each agent's fee, which is sent net of its own rail cost.
        assert!(r.plan.iter().all(|(_, to, _)| to == FEE_ACCOUNT));
        assert_eq!(r.totals.settled_value_micros, 0);
        assert_eq!(r.totals.cost_netted_micros, 0);
        // Three card payments of 10: 3 × (0.30 + 0.29) = 1.77 that would have been spent.
        assert_eq!(r.totals.cost_one_by_one_micros, 1_770_000);
        // 20% of each agent's 0.59 saving would be 0.118; 1% of the 10 it sent is 0.10, and the
        // lesser applies.
        assert!(r.lines.iter().all(|l| l.fee_micros == 100_000));
    }

    #[test]
    fn the_plan_reproduces_every_net_position_exactly() {
        let obs = [
            ob("a", "b", 3.0),
            ob("a", "c", 7.25),
            ob("b", "c", 1.0),
            ob("d", "a", 2.0),
            ob("c", "d", 0.5),
        ];
        let mut pos = BTreeMap::new();
        for o in &obs {
            *pos.entry(o.payer.clone()).or_insert(0) -= o.amount_micros;
            *pos.entry(o.payee.clone()).or_insert(0) += o.amount_micros;
        }
        pos.retain(|_, v| *v != 0);
        let plan = plan_transfers(&pos);
        assert_eq!(apply(&plan), pos);
        assert!(plan.len() < pos.len());
    }

    #[test]
    fn exact_matches_are_paired_before_the_greedy_pass() {
        // Largest-against-largest alone: 5→4, 1→3, 3→2, 1→1 — four transfers. Pairing the two
        // 3s first leaves 5→4, 1→1 — three.
        let mut pos = BTreeMap::new();
        pos.insert("d1".to_string(), -5);
        pos.insert("d2".to_string(), -3);
        pos.insert("c1".to_string(), 4);
        pos.insert("c2".to_string(), 3);
        pos.insert("c3".to_string(), 1);
        let plan = plan_transfers(&pos);
        assert_eq!(plan.len(), 3);
        assert_eq!(apply(&plan), pos);
    }

    /// Deterministic xorshift so the property test is reproducible without a crate.
    struct Rng(u64);
    impl Rng {
        fn next(&mut self) -> u64 {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            self.0
        }
    }

    #[test]
    fn no_agent_ever_pays_more_than_its_share_of_what_it_saved() {
        let mut rng = Rng(0x9E37_79B9_7F4A_7C15);
        let cfgs = [
            ClearConfig::default(),
            ClearConfig { rail: RailCost { fixed_micros: 10_000, bps: 0 }, ..Default::default() },
            ClearConfig { rail: RailCost { fixed_micros: 0, bps: 100 }, share_bps: 5_000, max_fee_bps_of_volume: 1_000, ..Default::default() },
        ];
        for round in 0..400 {
            let cfg = &cfgs[round % cfgs.len()];
            let agents = 2 + (rng.next() % 12) as usize;
            let n = 1 + (rng.next() % 60) as usize;
            let mut obs = Vec::new();
            for k in 0..n {
                let p = (rng.next() % agents as u64) as usize;
                let mut q = (rng.next() % agents as u64) as usize;
                if q == p {
                    q = (q + 1) % agents;
                }
                let micros = 1 + (rng.next() % 50_000_000) as i64;
                obs.push(Obligation {
                    obligation_id: format!("o{k}"),
                    payer: format!("ag{p}"),
                    payee: format!("ag{q}"),
                    asset: "USDC".into(),
                    amount_micros: micros,
                    memo: String::new(),
                    reference: None,
                    created_at_ms: 0,
                });
            }
            let refs: Vec<&Obligation> = obs.iter().collect();
            let r = net_asset("USDC", &refs, cfg);

            // 1. The plan clears every position, with the fee account taking exactly the fees.
            let mut expect: BTreeMap<String, i64> = BTreeMap::new();
            for l in &r.lines {
                expect.insert(l.agent.clone(), l.net_micros - l.fee_micros);
            }
            if r.totals.fees_micros > 0 {
                expect.insert(FEE_ACCOUNT.into(), r.totals.fees_micros);
            }
            expect.retain(|_, v| *v != 0);
            assert_eq!(apply(&r.plan), expect, "round {round}");

            // 2. At most one transfer fewer than the number of non-zero positions.
            assert!(r.plan.len() <= expect.len().saturating_sub(1), "round {round}");

            // 3. The guarantee, per agent, against the final plan.
            for l in &r.lines {
                let allowed = (l.saved_micros().max(0) as i128 * cfg.share_bps as i128 / 10_000) as i64;
                assert!(l.fee_micros <= allowed, "round {round}: {l:?}");
                let vol = (l.gross_out_micros as i128 * cfg.max_fee_bps_of_volume as i128 / 10_000) as i64;
                assert!(l.fee_micros <= vol, "round {round}: fee past volume ceiling {l:?}");
                assert!(l.fee_micros >= 0);
            }
        }
    }

    #[test]
    fn caps_limit_the_net_not_the_gross() {
        let house = ClearingHouse::default();
        let wal = Wal::disabled();
        let cap = 10 * MICROS_PER_UNIT;
        // a owes b 10: at the cap.
        house.record(&wal, "a", "b", "USDC", cap, "", None, cap, 0).unwrap();
        // One more unit is refused...
        let e = house.record(&wal, "a", "b", "USDC", MICROS_PER_UNIT, "", None, cap, 0).unwrap_err();
        assert!(matches!(e, ClearError::OverCap { available_micros: 0, .. }));
        // ...until b owes a something back, which frees exactly that much.
        house.record(&wal, "b", "a", "USDC", 4 * MICROS_PER_UNIT, "", None, cap, 0).unwrap();
        house.record(&wal, "a", "b", "USDC", 4 * MICROS_PER_UNIT, "", None, cap, 0).unwrap();
        let e = house.record(&wal, "a", "b", "USDC", 1, "", None, cap, 0).unwrap_err();
        assert!(matches!(e, ClearError::OverCap { .. }));
    }

    #[test]
    fn a_reference_makes_a_retry_safe() {
        let house = ClearingHouse::default();
        let wal = Wal::disabled();
        let cap = 100 * MICROS_PER_UNIT;
        let (a, created) = house.record(&wal, "a", "b", "USDC", 5, "", Some("call-1"), cap, 0).unwrap();
        assert!(created);
        let (b, created) = house.record(&wal, "a", "b", "USDC", 5, "", Some("call-1"), cap, 0).unwrap();
        assert!(!created);
        assert_eq!(a, b);
        assert_eq!(house.open_stats().0, 1);
    }

    #[test]
    fn an_overdue_payer_is_frozen_until_it_settles() {
        let house = ClearingHouse::new(ClearConfig { settle_window_ms: 1_000, ..Default::default() });
        let wal = Wal::disabled();
        let cap = 100 * MICROS_PER_UNIT;
        house.record(&wal, "a", "b", "USDC", 10 * MICROS_PER_UNIT, "", None, cap, 0).unwrap();
        let c = house.close_cycle(&wal, 0).unwrap();
        let (_, ins) = house.cycle(c.cycle_id).unwrap();
        let to_b = ins.iter().find(|i| i.to == "b").unwrap().clone();

        let e = house.record(&wal, "a", "c", "USDC", 1, "", None, cap, 5_000).unwrap_err();
        assert_eq!(e, ClearError::Frozen { overdue: 1 });

        // The payer can't settle its own debt; only the recipient can confirm.
        assert_eq!(house.confirm(&wal, &to_b.instruction_id, Some("a"), false, 5_000).unwrap_err(), ClearError::NotYours);
        house.mark_paid(&wal, &to_b.instruction_id, "a", Some("0xabc"), 5_000).unwrap();
        house.confirm(&wal, &to_b.instruction_id, Some("b"), false, 5_000).unwrap();
        house.record(&wal, "a", "c", "USDC", 1, "", None, cap, 5_000).unwrap();
    }

    #[test]
    fn a_fee_too_small_to_send_carries_forward_until_it_is_worth_a_transfer() {
        let house = ClearingHouse::default();
        let wal = Wal::disabled();
        let cap = 1_000 * MICROS_PER_UNIT;
        let circle = |house: &ClearingHouse| {
            for (p, q) in [("a", "b"), ("b", "c"), ("c", "a")] {
                house.record(&wal, p, q, "USDC", 10 * MICROS_PER_UNIT, "", None, cap, 0).unwrap();
            }
        };
        circle(&house);
        let c1 = house.close_cycle(&wal, 0).unwrap();
        // Each fee is 0.10 — a card rail would take 0.30 of it. Nothing is sent...
        assert!(house.cycle(c1.cycle_id).unwrap().1.is_empty());
        assert_eq!(c1.carried.len(), 3);
        // ...and nothing is forgiven: it is owed in the next cycle.
        let (open, _, _) = house.position("a");
        assert_eq!(open.len(), 1);
        assert!(open[0].is_fee_carry());
        // Carries alone don't open a cycle.
        assert!(house.close_cycle(&wal, 1).is_none());
        // Enough rounds and it is worth collecting.
        let mut sent = 0;
        for k in 0..60 {
            circle(&house);
            let c = house.close_cycle(&wal, 2 + k).unwrap();
            sent += house.cycle(c.cycle_id).unwrap().1.iter().filter(|i| i.is_fee()).count();
            if sent > 0 {
                break;
            }
        }
        assert!(sent > 0, "fees never became worth a transfer");
    }

    #[test]
    fn a_cycle_survives_the_log_exactly() {
        let house = ClearingHouse::default();
        let wal = Wal::disabled();
        let cap = 1_000 * MICROS_PER_UNIT;
        for (p, q, u) in [("a", "b", 3.1), ("b", "c", 2.2), ("c", "a", 0.7), ("d", "b", 9.0)] {
            house.record(&wal, p, q, "USDC", to_micros(u).unwrap(), "x", None, cap, 0).unwrap();
        }
        let c = house.close_cycle(&wal, 42).unwrap();
        let (_, ins) = house.cycle(c.cycle_id).unwrap();
        let text = c.to_log_json(&ins).to_string();
        let parsed = crate::json::parse(&text).unwrap();
        let (c2, ins2) = Cycle::from_log_json(&parsed).unwrap();
        assert_eq!(c, c2);
        assert_eq!(ins, ins2);
    }
}
