"""Settings, all from environment variables. Nothing secret is ever written in code."""
import os
import secrets
import sys
from dataclasses import dataclass


def _env(name, default=""):
    return os.environ.get(name, default).strip()


@dataclass
class Config:
    mode: str
    db_path: str
    admin_secret: str
    token_secret: bytes
    stripe_key: str  # sk_test_... only; empty means "don't send invoices"
    stripe_api: str
    keptvow_url: str
    keptvow_source: str  # Clear's name as a Keptvow reporting source
    keptvow_source_secret: str  # empty means "don't report to Keptvow"
    days_until_due: int
    grace_days: int  # days past due before an unpaid bill is reported to Keptvow
    included_settlements: int  # paid bills per month included in the plan, before the $0.01 rate
    sync_secs: int  # 0 = no background loop

    @staticmethod
    def from_env():
        admin = _env("CLEAR_ADMIN_SECRET")
        if not admin:
            admin = secrets.token_urlsafe(24)
            print(f"CLEAR_ADMIN_SECRET not set; using a one-time secret for this run: {admin}", file=sys.stderr)
        token = _env("CLEAR_TOKEN_SECRET") or secrets.token_hex(32)
        return Config(
            mode=_env("CLEAR_MODE", "test"),
            db_path=_env("CLEAR_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "clear.db")),
            admin_secret=admin,
            token_secret=token.encode(),
            stripe_key=_env("STRIPE_SECRET_KEY"),
            stripe_api=_env("STRIPE_API_BASE", "https://api.stripe.com").rstrip("/"),
            keptvow_url=_env("KEPTVOW_URL", "https://keptvow.com").rstrip("/"),
            keptvow_source=_env("KEPTVOW_SOURCE", "clear"),
            keptvow_source_secret=_env("KEPTVOW_SOURCE_SECRET"),
            days_until_due=int(_env("CLEAR_DAYS_UNTIL_DUE", "14")),
            grace_days=int(_env("CLEAR_GRACE_DAYS", "30")),
            included_settlements=int(_env("CLEAR_INCLUDED_SETTLEMENTS", "0")),
            sync_secs=int(_env("CLEAR_SYNC_SECS", "0")),
        )

    def check_test_mode(self):
        """Refuse to run unless this is plainly test mode. There is no live mode yet."""
        if self.mode != "test":
            raise SystemExit(f"CLEAR_MODE={self.mode!r}: only test mode exists. Live mode is not built.")
        if self.stripe_key and not self.stripe_key.startswith(("sk_test_", "rk_test_")):
            raise SystemExit("STRIPE_SECRET_KEY must be a Stripe *test* key (sk_test_... or rk_test_...).")
        return self
