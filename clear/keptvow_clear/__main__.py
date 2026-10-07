"""Run Clear:  cd clear && python3 -m keptvow_clear   (test mode only)"""
import os

from . import build
from .server import background, make_server


def main():
    clear = build()
    host = os.environ.get("CLEAR_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", os.environ.get("CLEAR_PORT", "8400")))
    srv = make_server(clear, host, port)
    if clear.cfg.sync_secs:
        background(clear, clear.cfg.sync_secs)
    print(f"Keptvow Clear (test mode) on http://{host}:{port}/  "
          f"Stripe: {'test key set' if clear.stripe else 'off'}  "
          f"Keptvow reports: {'on' if clear.cfg.keptvow_source_secret else 'off'}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
