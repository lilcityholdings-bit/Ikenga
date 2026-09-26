"""
Central configuration.

SECURITY PRINCIPLE: nothing secret ever lives in this file or in source
control. All secrets come from environment variables (.env locally, or your
host's secret manager in production). If you ever see a real private key,
API key, or RPC URL with an embedded key committed to git, rotate it
immediately -- assume it is already compromised.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in your .env file or host's secret manager."
        )
    return val


class Settings:
    # --- Network: TESTNET ONLY until explicitly promoted ---
    # This is the single most important safety switch in the whole project.
    # It defaults to Sepolia and refuses to run against a mainnet chain ID
    # unless ALLOW_MAINNET=true is explicitly set, which we do NOT recommend
    # doing until you've had a security review and legal review done.
    ALLOW_MAINNET: bool = os.getenv("ALLOW_MAINNET", "false").lower() == "true"

    SEPOLIA_CHAIN_ID = 11155111
    RPC_URL: str = os.getenv("RPC_URL", "")  # e.g. Alchemy/Infura Sepolia URL

    # Verified from Uniswap's own deployment records / Sepolia Etherscan.
    # Re-verify at https://docs.uniswap.org/contracts/v3/reference/deployments
    # before relying on these for anything beyond testing -- Uniswap warns
    # addresses are NOT guaranteed identical across chains or over time.
    UNISWAP_V3_SWAP_ROUTER02_SEPOLIA = "0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E"
    UNISWAP_V2_ROUTER02_SEPOLIA = "0x86dcd3293C53Cf8EFd7303B57beb2a3F671dDE98"

    # --- App/API security ---
    API_KEY_PEPPER: str = os.getenv("API_KEY_PEPPER", "")  # used to hash stored keys
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    ENV: str = os.getenv("ENV", "development")

    # --- Swap safety limits (defense in depth against bugs/malicious agents) ---
    MAX_SLIPPAGE_BPS = int(os.getenv("MAX_SLIPPAGE_BPS", "300"))  # 3% hard ceiling
    DEFAULT_SLIPPAGE_BPS = int(os.getenv("DEFAULT_SLIPPAGE_BPS", "50"))  # 0.5%
    MAX_TX_VALUE_TEST_ETH = float(os.getenv("MAX_TX_VALUE_TEST_ETH", "0.05"))
    QUOTE_DEADLINE_SECONDS = int(os.getenv("QUOTE_DEADLINE_SECONDS", "120"))
    SWAP_DEADLINE_SECONDS = int(os.getenv("SWAP_DEADLINE_SECONDS", "180"))

    # --- Rate limiting (per API key) ---
    RATE_LIMIT_QUOTES = os.getenv("RATE_LIMIT_QUOTES", "30/minute")
    RATE_LIMIT_SWAPS = os.getenv("RATE_LIMIT_SWAPS", "6/minute")

    # --- One-time browser-based key creation (no terminal needed) ---
    # Set this to a long random value in your Render environment variables,
    # call /admin/create-key once with it, then DELETE this env variable
    # from Render afterward so the endpoint can no longer be used.
    ADMIN_BOOTSTRAP_SECRET: str = os.getenv("ADMIN_BOOTSTRAP_SECRET", "")

    def validate_chain_safety(self):
        if self.ALLOW_MAINNET:
            raise RuntimeError(
                "ALLOW_MAINNET is set to true. This build has not been "
                "security-audited or legally reviewed for real funds. "
                "Refusing to start. If you are certain, you'll need to "
                "remove this guard in code deliberately -- it should not "
                "be a config flag alone."
            )


settings = Settings()
