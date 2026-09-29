//! HTTP surface for the clearinghouse (`clearing.rs`). Everything under `/v1/clear`.

use crate::api::{authenticate, authenticate_owner, err_response, now_ms_pub};
use crate::clearing::{from_micros, to_micros, ClearError, Instruction, FEE_ACCOUNT};
use crate::http::{Request, Response};
use crate::json::Json;
use crate::state::AppState;
use crate::types::ApiError;

fn clear_err(e: ClearError) -> Response {
    let mut pairs = vec![("code", Json::str(e.code())), ("message", Json::str(e.message()))];
    if let ClearError::OverCap { cap_micros, available_micros } = &e {
        pairs.push(("cap", Json::num(from_micros(*cap_micros))));
        pairs.push(("available", Json::num(from_micros(*available_micros))));
    }
    Response::json(e.status(), &Json::obj(pairs))
}

fn parse_body(req: &Request) -> Result<Json, Response> {
    std::str::from_utf8(&req.body)
        .map_err(|_| ())
        .and_then(|s| crate::json::parse(if s.trim().is_empty() { "{}" } else { s }).map_err(|_| ()))
        .map_err(|_| err_response(400, ApiError::new("BAD_BODY", "body must be valid JSON")))
}

fn cap_for(state: &AppState, agent: &str) -> i64 {
    state.clearing.config.cap_for(state.trust.band(agent))
}

/// `GET /v1/clear` — what this is, the terms, and the running proof that it saves money.
pub fn overview(state: &AppState) -> Response {
    let cfg = &state.clearing.config;
    let life = state.clearing.lifetime();
    let (open_count, open_value) = state.clearing.open_stats();
    let caps = ["New", "Developing", "Established", "Trusted", "Highly Trusted", "Elite"]
        .iter()
        .zip(cfg.caps_micros.iter())
        .map(|(b, c)| Json::obj(vec![("band", Json::str(*b)), ("net_debit_cap", Json::num(from_micros(*c)))]))
        .collect();
    let recent = state.clearing.recent_cycles(5).iter().map(|c| c.summary_json()).collect();
    Response::json(
        200,
        &Json::obj(vec![
            ("service", Json::str("Ikenga Clear")),
            (
                "what",
                Json::str(
                    "A clearinghouse for bot-to-bot payments. Record what you owe instead of \
                     paying it; every cycle, everything owed is netted and each agent makes or \
                     receives the fewest transfers that settle it. You pay a share of what that \
                     saved you, and nothing if it saved you nothing.",
                ),
            ),
            ("custody", Json::str("none — this service holds records, never money. You settle instructions from your own wallet.")),
            (
                "pricing",
                Json::obj(vec![
                    ("model", Json::str("the lesser of a share of what netting saved you, or a small cap on what you sent")),
                    ("share_of_savings", Json::num(cfg.share_bps as f64 / 10_000.0)),
                    ("max_fee_percent_of_amount_sent", Json::num(cfg.max_fee_bps_of_volume as f64 / 100.0)),
                    ("savings_measured_against", Json::str(cfg.rail_label.clone())),
                    ("rail_fixed_per_transfer", Json::num(from_micros(cfg.rail.fixed_micros))),
                    ("rail_percent_per_transfer", Json::num(cfg.rail.bps as f64 / 100.0)),
                    (
                        "guarantee",
                        Json::str(
                            "No agent's fee exceeds the stated share of what netting saved that \
                             agent, measured against the final transfer list, nor the stated \
                             percent of what it sent. If a cycle cannot meet that, the fee is \
                             waived for that asset in that cycle.",
                        ),
                    ),
                ]),
            ),
            (
                "credit",
                Json::obj(vec![
                    ("rule", Json::str("only your NET debit is capped; gross volume is unlimited")),
                    ("caps_by_trust_band", Json::Array(caps)),
                    ("settle_within_secs", Json::num((cfg.settle_window_ms / 1000) as f64)),
                    (
                        "late",
                        Json::str(
                            "An agent with any overdue instruction can record no new obligations \
                             with anyone until it settles.",
                        ),
                    ),
                ]),
            ),
            (
                "cycle",
                Json::obj(vec![
                    ("auto_close_every_secs", Json::num(cfg.cycle_secs as f64)),
                    ("open_obligations", Json::num(open_count as f64)),
                    ("open_value", Json::num(from_micros(open_value))),
                ]),
            ),
            (
                "lifetime",
                Json::obj(vec![
                    ("cycles", Json::num(life.cycles as f64)),
                    ("payments_recorded", Json::num(life.payments as f64)),
                    ("transfers_required", Json::num(life.transfers as f64)),
                    ("gross_value", Json::num(from_micros(life.gross_micros))),
                    ("rail_cost_saved", Json::num(from_micros(life.saved_micros))),
                    ("fees_assessed", Json::num(from_micros(life.fees_micros))),
                ]),
            ),
            ("recent_cycles", Json::Array(recent)),
            (
                "endpoints",
                Json::Array(
                    [
                        ("POST", "/v1/clear/obligations", "signed", "Record that you owe another agent. Body: {payee, amount, asset?, memo?, ref?}. ref makes a retry safe."),
                        ("GET", "/v1/clear/position", "signed", "Your open obligations, net position, credit available and instructions to settle"),
                        ("GET", "/v1/clear/capacity/{agent_id}?asset=USDC&amount=5", "none", "Credit check before you deliver: can this agent take on this obligation?"),
                        ("GET", "/v1/clear/cycles/{id}", "optional", "A closed cycle's totals; signed callers also get their own savings receipt and instructions"),
                        ("POST", "/v1/clear/instructions/{id}/paid", "signed", "Payer: say you sent it, with an optional tx_ref"),
                        ("POST", "/v1/clear/instructions/{id}/confirm", "signed", "Recipient: confirm you received it. Only this settles an instruction."),
                        ("POST", "/v1/clear/cycles", "owner", "Close the open cycle now"),
                    ]
                    .iter()
                    .map(|(m, p, a, d)| {
                        Json::obj(vec![
                            ("method", Json::str(*m)),
                            ("path", Json::str(*p)),
                            ("auth", Json::str(*a)),
                            ("does", Json::str(*d)),
                        ])
                    })
                    .collect(),
                ),
            ),
        ]),
    )
}

/// `POST /v1/clear/obligations` — signed by the payer. Only the payer can put itself in debt.
pub fn record_obligation(state: &AppState, req: &Request) -> Response {
    let payer = match authenticate(state, req, "POST", "/v1/clear/obligations") {
        Ok(id) => id,
        Err(r) => return r,
    };
    let body = match parse_body(req) {
        Ok(b) => b,
        Err(r) => return r,
    };
    let text = |k: &str| body.get(k).and_then(Json::as_str);
    let Some(payee) = text("payee") else {
        return err_response(400, ApiError::new("MISSING_FIELD", "payee is required"));
    };
    let Some(amount) = body.get("amount").and_then(Json::as_f64).and_then(to_micros) else {
        return clear_err(ClearError::BadAmount);
    };
    if state.agent_registry.get_pubkey(payee).is_none() {
        return clear_err(ClearError::UnknownPayee);
    }
    let asset = text("asset").unwrap_or("USDC");
    let now = now_ms_pub();
    match state.clearing.record(
        &state.wal,
        &payer,
        payee,
        asset,
        amount,
        text("memo").unwrap_or(""),
        text("ref"),
        cap_for(state, &payer),
        now,
    ) {
        Ok((o, created)) => {
            let cap = state.clearing.capacity(&payer, asset, cap_for(state, &payer), now);
            let mut j = o.to_json();
            if let Json::Object(pairs) = &mut j {
                pairs.push(("created".into(), Json::Bool(created)));
                pairs.push((
                    "credit_available".into(),
                    Json::num(from_micros((cap.cap_micros - cap.exposure_micros).max(0))),
                ));
                pairs.push((
                    "note".into(),
                    Json::str("No money moved. This is netted at the next cycle close; see GET /v1/clear/position."),
                ));
            }
            Response::json(if created { 201 } else { 200 }, &j)
        }
        Err(e) => clear_err(e),
    }
}

/// `GET /v1/clear/position` — signed.
pub fn position(state: &AppState, req: &Request) -> Response {
    let agent = match authenticate(state, req, "GET", "/v1/clear/position") {
        Ok(id) => id,
        Err(r) => return r,
    };
    let now = now_ms_pub();
    let (open, net, ins) = state.clearing.position(&agent);
    let cap = cap_for(state, &agent);
    let mut assets: Vec<String> = net.iter().map(|(a, _)| a.clone()).collect();
    assets.extend(ins.iter().map(|i| i.asset.clone()));
    if assets.is_empty() {
        assets.push("USDC".into());
    }
    assets.sort();
    assets.dedup();
    let credit = assets
        .iter()
        .map(|a| {
            let c = state.clearing.capacity(&agent, a, cap, now);
            Json::obj(vec![
                ("asset", Json::str(a.clone())),
                ("cap", Json::num(from_micros(c.cap_micros))),
                ("exposure", Json::num(from_micros(c.exposure_micros))),
                ("available", Json::num(from_micros((c.cap_micros - c.exposure_micros).max(0)))),
            ])
        })
        .collect();
    let overdue = ins.iter().filter(|i| i.from == agent && i.is_overdue(now)).count();
    Response::json(
        200,
        &Json::obj(vec![
            ("agent_id", Json::str(agent.clone())),
            ("trust_band", Json::str(state.trust.band(&agent).label())),
            ("frozen", Json::Bool(overdue > 0)),
            ("overdue_instructions", Json::num(overdue as f64)),
            (
                "open_net",
                Json::Array(
                    net.iter()
                        .map(|(a, v)| Json::obj(vec![("asset", Json::str(a.clone())), ("net", Json::num(from_micros(*v)))]))
                        .collect(),
                ),
            ),
            ("credit", Json::Array(credit)),
            ("open_obligations", Json::Array(open.iter().map(|o| o.to_json()).collect())),
            (
                "to_pay",
                Json::Array(ins.iter().filter(|i| i.from == agent).map(|i| i.to_json(Some(now))).collect()),
            ),
            (
                "to_receive",
                Json::Array(ins.iter().filter(|i| i.to == agent).map(|i| i.to_json(Some(now))).collect()),
            ),
        ]),
    )
}

/// `GET /v1/clear/capacity/{agent}?asset=USDC&amount=5` — public credit check.
///
/// Answers "can this agent take on this obligation, and is it in good standing?" without
/// publishing its positions: the reply is a yes/no and a standing, not a balance sheet.
pub fn capacity(state: &AppState, req: &Request, agent: &str) -> Response {
    if state.agent_registry.get_pubkey(agent).is_none() {
        return err_response(404, ApiError::new("UNKNOWN_AGENT", "no such agent"));
    }
    let asset = req.query_param("asset").unwrap_or("USDC");
    if !crate::clearing::valid_asset(asset) {
        return clear_err(ClearError::BadAsset);
    }
    let amount = match req.query_param("amount") {
        None => 0,
        Some(raw) => match raw.parse::<f64>().ok().and_then(to_micros) {
            Some(m) => m,
            None => return clear_err(ClearError::BadAmount),
        },
    };
    let now = now_ms_pub();
    let c = state.clearing.capacity(agent, asset, cap_for(state, agent), now);
    let can = c.overdue == 0 && c.exposure_micros + amount <= c.cap_micros;
    Response::json(
        200,
        &Json::obj(vec![
            ("agent_id", Json::str(agent)),
            ("asset", Json::str(asset)),
            ("amount", Json::num(from_micros(amount))),
            ("can_owe", Json::Bool(can)),
            ("in_good_standing", Json::Bool(c.overdue == 0)),
            ("trust_band", Json::str(state.trust.band(agent).label())),
            (
                "meaning",
                Json::str(
                    "can_owe: recording this obligation would be accepted right now. It is a \
                     bound on exposure, not a guarantee of payment — see GET /v1/clear.",
                ),
            ),
        ]),
    )
}

/// `POST /v1/clear/cycles` — owner-only: close the open cycle now.
pub fn close_cycle(state: &AppState, req: &Request) -> Response {
    if let Err(r) = authenticate_owner(state, req) {
        return r;
    }
    match state.clearing.close_cycle(&state.wal, now_ms_pub()) {
        Some(c) => Response::json(201, &c.summary_json()),
        None => Response::json(
            200,
            &Json::obj(vec![("closed", Json::Bool(false)), ("message", Json::str("nothing to net"))]),
        ),
    }
}

/// `GET /v1/clear/cycles/{id}` — totals for everyone; signed callers also get their own lines
/// and instructions; the owner gets everything.
pub fn get_cycle(state: &AppState, req: &Request, id: &str) -> Response {
    let Ok(cycle_id) = id.parse::<u64>() else {
        return clear_err(ClearError::NoSuchCycle);
    };
    let viewer = if req.header("x-agent-id").is_some() {
        match authenticate(state, req, "GET", req.signing_path()) {
            Ok(a) => Some(a),
            Err(r) => return r,
        }
    } else {
        None
    };
    let owner = req.header("x-owner-key").is_some() && authenticate_owner(state, req).is_ok();
    let Some((c, ins)) = state.clearing.cycle(cycle_id) else {
        return clear_err(ClearError::NoSuchCycle);
    };
    let now = now_ms_pub();
    let mut j = c.summary_json();
    let visible = |who: &str| owner || viewer.as_deref() == Some(who);
    if let Json::Object(pairs) = &mut j {
        if owner || viewer.is_some() {
            pairs.push((
                "your_savings".into(),
                Json::Array(c.lines.iter().filter(|l| visible(&l.agent)).map(|l| l.to_json()).collect()),
            ));
            pairs.push((
                "your_instructions".into(),
                Json::Array(
                    ins.iter()
                        .filter(|i| visible(&i.from) || visible(&i.to))
                        .map(|i| i.to_json(Some(now)))
                        .collect(),
                ),
            ));
        }
    }
    Response::json(200, &j)
}

fn instruction_reply(i: Instruction) -> Response {
    Response::json(200, &i.to_json(Some(now_ms_pub())))
}

/// `POST /v1/clear/instructions/{id}/paid` — signed by the payer.
pub fn mark_paid(state: &AppState, req: &Request, id: &str) -> Response {
    let path = format!("/v1/clear/instructions/{id}/paid");
    let agent = match authenticate(state, req, "POST", &path) {
        Ok(a) => a,
        Err(r) => return r,
    };
    let body = match parse_body(req) {
        Ok(b) => b,
        Err(r) => return r,
    };
    let tx = body.get("tx_ref").and_then(Json::as_str).map(|s| s.chars().take(200).collect::<String>());
    match state.clearing.mark_paid(&state.wal, id, &agent, tx.as_deref(), now_ms_pub()) {
        Ok(i) => instruction_reply(i),
        Err(e) => clear_err(e),
    }
}

/// `POST /v1/clear/instructions/{id}/confirm` — signed by the recipient, or the owner key for
/// transfers to the fee account.
pub fn confirm(state: &AppState, req: &Request, id: &str) -> Response {
    let now = now_ms_pub();
    let result = if req.header("x-agent-id").is_some() {
        let path = format!("/v1/clear/instructions/{id}/confirm");
        let agent = match authenticate(state, req, "POST", &path) {
            Ok(a) => a,
            Err(r) => return r,
        };
        state.clearing.confirm(&state.wal, id, Some(&agent), false, now)
    } else {
        if let Err(r) = authenticate_owner(state, req) {
            return r;
        }
        if state.clearing.instruction(id).map(|i| i.to != FEE_ACCOUNT).unwrap_or(false) {
            return clear_err(ClearError::NotYours);
        }
        state.clearing.confirm(&state.wal, id, None, true, now)
    };
    match result {
        Ok(i) => instruction_reply(i),
        Err(e) => clear_err(e),
    }
}
