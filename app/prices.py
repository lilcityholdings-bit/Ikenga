"""
Price quoting against the real Uniswap V3 Quoter contract on Sepolia.
Replaces the old placeholder/fake price data with a genuine on-chain read.
"""
import time
from fastapi import HTTPException

from app.chain import get_web3
from app.config import settings

QUOTER_V2_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "internalType": "struct IQuoterV2.QuoteExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceX96After", "type": "uint160"},
            {"internalType": "uint32", "name": "initializedTicksCrossed", "type": "uint32"},
            {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# QuoterV2 on Sepolia -- verify at https://docs.uniswap.org before trusting
QUOTER_V2_ADDRESS_SEPOLIA = "0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3"


def get_quote(token_in: str, token_out: str, amount_in_wei: int, fee: int = 3000) -> dict:
    w3 = get_web3()
    quoter = w3.eth.contract(
        address=w3.to_checksum_address(QUOTER_V2_ADDRESS_SEPOLIA), abi=QUOTER_V2_ABI
    )
    try:
        result = quoter.functions.quoteExactInputSingle(
            (
                w3.to_checksum_address(token_in),
                w3.to_checksum_address(token_out),
                amount_in_wei,
                fee,
                0,
            )
        ).call()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Quote failed: {e}")

    amount_out = result[0]
    expires_at = int(time.time()) + settings.QUOTE_DEADLINE_SECONDS
    return {
        "amount_in_wei": amount_in_wei,
        "amount_out_wei": amount_out,
        "fee_tier": fee,
        "expires_at": expires_at,
    }
