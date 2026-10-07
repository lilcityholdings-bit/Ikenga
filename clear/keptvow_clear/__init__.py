"""Keptvow Clear: usage billing for MCP servers and APIs that AI agents call."""
from .billing import Clear
from .config import Config
from .store import Store
from .stripe_api import Stripe
from .trust import Keptvow


def build(cfg=None, store=None):
    cfg = (cfg or Config.from_env()).check_test_mode()
    store = store or Store(cfg.db_path)
    stripe = Stripe(cfg.stripe_key, cfg.stripe_api) if cfg.stripe_key else None
    return Clear(cfg, store, Keptvow(cfg.keptvow_url, cfg.keptvow_source, cfg.keptvow_source_secret), stripe)
