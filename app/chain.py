"""
Blockchain connection layer.

CRITICAL DESIGN DECISION: this service is non-custodial. It never holds,
generates, or has access to agent private keys. Agents sign their own
transactions locally (or via their own wallet infra) using unsigned
transaction data this API returns -- we build transactions, we don't
execute them on anyone's behalf with keys we hold. That single decision
removes an entire category of catastrophic hack (us leaking/losing
agent private keys).
"""
from web3 import Web3
from fastapi import HTTPException

from app.config import settings


def get_web3() -> Web3:
    if not settings.RPC_URL:
        raise HTTPException(
            status_code=500,
            detail="RPC_URL not configured. Set a Sepolia RPC URL (e.g. from "
            "Alchemy or Infura) in your environment.",
        )
    w3 = Web3(Web3.HTTPProvider(settings.RPC_URL))
    if not w3.is_connected():
        raise HTTPException(status_code=503, detail="Could not connect to RPC node")

    chain_id = w3.eth.chain_id
    if chain_id != settings.SEPOLIA_CHAIN_ID:
        # Hard stop. This is intentional and should NOT be bypassed by
        # a config flag alone -- see config.py validate_chain_safety().
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to operate on chain_id={chain_id}. "
            f"This build only operates on Sepolia testnet "
            f"({settings.SEPOLIA_CHAIN_ID}) until it has been "
            f"security-audited and legally reviewed for mainnet/real funds.",
        )
    return w3
