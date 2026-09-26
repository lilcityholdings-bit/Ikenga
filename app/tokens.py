"""
Token allow-list.

IMPORTANT: Sepolia does not have real liquid markets for most of the "top
20" mainnet tokens. For a genuine testnet swap demo you have two honest
options:
  1. Use Sepolia's canonical WETH + a couple of official Uniswap test
     tokens (small liquidity, but real on-chain swaps).
  2. Fork mainnet locally (e.g. with Anvil/Hardhat) so real mainnet
     token contracts and pools exist in your test environment.

The addresses below are placeholders for the *shape* of the allow-list.
Fill in verified addresses before relying on this -- do not guess or
copy addresses from an untrusted source, since a wrong/malicious
contract address here is exactly how "fake token" scams work.
"""

# symbol -> Sepolia contract address (VERIFY before use)
TOP_20_TESTNET_TOKENS: dict[str, str] = {
    "WETH": "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14",  # Sepolia canonical WETH
    # "USDC": "0x...",   # TODO: verify official Sepolia USDC (Circle's faucet token)
    # "DAI":  "0x...",   # TODO
    # "WBTC": "0x...",   # TODO
    # Add remaining top-20 only after verifying each contract address
    # against an official source (project docs, not a random block explorer
    # search result).
}

ALLOWED_TOKEN_ADDRESSES: set[str] = set(TOP_20_TESTNET_TOKENS.values())
