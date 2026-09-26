from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from app.config import settings
from app.security import (
    limiter,
    require_api_key,
    check_and_consume_nonce,
    enforce_slippage_ceiling,
    register_api_key,
)
from app.prices import get_quote
from app.swap import build_unsigned_swap_tx, broadcast_signed_tx
from app.tokens import TOP_20_TESTNET_TOKENS

settings.validate_chain_safety()  # refuses to start if ALLOW_MAINNET is set

app = FastAPI(title="Agent DEX Aggregator (Sepolia Testnet)", version="0.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.get("/health")
def health():
    return {"status": "ok", "network": "sepolia-testnet", "env": settings.ENV}


@app.get("/tokens")
def tokens():
    return {"supported_tokens": TOP_20_TESTNET_TOKENS}


@app.get("/admin/create-key")
def create_key(agent_name: str, secret: str):
    """
    One-time, browser-friendly way to create your first API key without a
    terminal. Visit:
      https://YOUR-APP-URL/admin/create-key?agent_name=my_agent&secret=YOUR_ADMIN_BOOTSTRAP_SECRET
    in your phone's browser. Copy the returned key immediately -- it is
    never shown again. Then go delete ADMIN_BOOTSTRAP_SECRET from your
    Render environment variables so this endpoint stops working.
    """
    if not settings.ADMIN_BOOTSTRAP_SECRET:
        raise HTTPException(status_code=404, detail="Bootstrap disabled (no secret configured)")
    if secret != settings.ADMIN_BOOTSTRAP_SECRET:
        raise HTTPException(status_code=401, detail="Wrong secret")
    raw_key = register_api_key(agent_name)
    return {
        "agent_name": agent_name,
        "api_key": raw_key,
        "warning": "Save this now -- it will never be shown again. "
        "Then remove ADMIN_BOOTSTRAP_SECRET from your environment variables.",
    }


class QuoteRequest(BaseModel):
    token_in: str
    token_out: str
    amount_in_wei: int = Field(gt=0)
    fee: int = 3000


@app.post("/quote")
@limiter.limit(settings.RATE_LIMIT_QUOTES)
def quote(request: QuoteRequest, agent_id: str = Depends(require_api_key)):
    return get_quote(request.token_in, request.token_out, request.amount_in_wei, request.fee)


class SwapRequest(BaseModel):
    nonce: str  # unique per request, prevents replay
    agent_wallet_address: str
    token_in: str
    token_out: str
    amount_in_wei: int = Field(gt=0)
    amount_out_minimum_wei: int = Field(ge=0)
    slippage_bps: int = settings.DEFAULT_SLIPPAGE_BPS
    fee: int = 3000


@app.post("/swap/build")
@limiter.limit(settings.RATE_LIMIT_SWAPS)
def build_swap(request: SwapRequest, agent_id: str = Depends(require_api_key)):
    check_and_consume_nonce(request.nonce)
    enforce_slippage_ceiling(request.slippage_bps)

    return build_unsigned_swap_tx(
        agent_wallet_address=request.agent_wallet_address,
        token_in=request.token_in,
        token_out=request.token_out,
        amount_in_wei=request.amount_in_wei,
        amount_out_minimum_wei=request.amount_out_minimum_wei,
        fee=request.fee,
    )


class BroadcastRequest(BaseModel):
    nonce: str
    signed_raw_tx_hex: str


@app.post("/swap/broadcast")
@limiter.limit(settings.RATE_LIMIT_SWAPS)
def broadcast(request: BroadcastRequest, agent_id: str = Depends(require_api_key)):
    check_and_consume_nonce(request.nonce)
    tx_hash = broadcast_signed_tx(request.signed_raw_tx_hex)
    return {"tx_hash": tx_hash, "explorer_url": f"https://sepolia.etherscan.io/tx/{tx_hash}"}
