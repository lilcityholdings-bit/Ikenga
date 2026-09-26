"""
Swap execution -- NON-CUSTODIAL.

This service NEVER holds agent private keys and NEVER signs or broadcasts
a transaction on an agent's behalf. It builds an unsigned transaction and
returns it; the agent signs it locally with its own key and either
broadcasts it themselves or posts the signed raw tx back to /broadcast.

Why this matters: if this API held private keys, a single server
compromise (or a bug, or a malicious insider) could drain every
connected agent's wallet. Keeping signing entirely client-side means
compromising this API only exposes price/quote logic, not funds.
"""
import time
from fastapi import HTTPException

from app.chain import get_web3
from app.config import settings
from app.tokens import ALLOWED_TOKEN_ADDRESSES

ROUTER02_MIN_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "internalType": "struct IV3SwapRouter.ExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    }
]


def build_unsigned_swap_tx(
    *,
    agent_wallet_address: str,
    token_in: str,
    token_out: str,
    amount_in_wei: int,
    amount_out_minimum_wei: int,
    fee: int = 3000,
) -> dict:
    if amount_in_wei <= 0:
        raise HTTPException(status_code=400, detail="amount_in must be positive")

    if token_in.lower() not in {a.lower() for a in ALLOWED_TOKEN_ADDRESSES}:
        raise HTTPException(status_code=400, detail=f"Token not allow-listed: {token_in}")
    if token_out.lower() not in {a.lower() for a in ALLOWED_TOKEN_ADDRESSES}:
        raise HTTPException(status_code=400, detail=f"Token not allow-listed: {token_out}")

    w3 = get_web3()
    router = w3.eth.contract(
        address=w3.to_checksum_address(settings.UNISWAP_V3_SWAP_ROUTER02_SEPOLIA),
        abi=ROUTER02_MIN_ABI,
    )

    checksum_agent = w3.to_checksum_address(agent_wallet_address)

    try:
        tx = router.functions.exactInputSingle(
            (
                w3.to_checksum_address(token_in),
                w3.to_checksum_address(token_out),
                fee,
                checksum_agent,
                amount_in_wei,
                amount_out_minimum_wei,
                0,
            )
        ).build_transaction(
            {
                "from": checksum_agent,
                "nonce": w3.eth.get_transaction_count(checksum_agent),
                "chainId": settings.SEPOLIA_CHAIN_ID,
                # Agent's own wallet/library should fill in gas price + gas
                # limit at signing time based on current network conditions;
                # we provide a conservative estimate here as a starting point.
            }
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to build swap tx: {e}")

    return {
        "unsigned_tx": tx,
        "note": (
            "Sign this transaction with your own private key (client-side) "
            "and either broadcast it yourself or POST the signed raw tx to "
            "/broadcast. This API never sees or holds your private key."
        ),
        "expires_at": int(time.time()) + settings.SWAP_DEADLINE_SECONDS,
    }


def broadcast_signed_tx(signed_raw_tx_hex: str) -> str:
    w3 = get_web3()
    try:
        tx_hash = w3.eth.send_raw_transaction(bytes.fromhex(signed_raw_tx_hex.replace("0x", "")))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Broadcast failed: {e}")
    return tx_hash.hex()
