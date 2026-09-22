#!/usr/bin/env python3
"""
Start the worker and the dashboard together.

The worker is supervised: if it crashes, it is restarted. Previously a dead
worker stayed dead until the whole service was redeployed, and the only sign
was a "Down" badge on the dashboard.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_stop = threading.Event()


def supervise_worker():
    """Keep one worker alive. Backs off so a hard-failing worker cannot spin."""
    delay = 5
    while not _stop.is_set():
        proc = subprocess.Popen([sys.executable, "-m", "core.worker"], cwd=str(ROOT))
        print(f"[supervisor] worker started (pid {proc.pid})", flush=True)
        while not _stop.is_set():
            if proc.poll() is not None:
                break
            time.sleep(2)
        if _stop.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            return
        print(f"[supervisor] worker exited ({proc.returncode}); "
              f"restarting in {delay}s", flush=True)
        # A stale lock from the crashed cycle would block the new worker.
        try:
            (ROOT / "data" / "cycle.lock").unlink()
        except Exception:
            pass
        if _stop.wait(delay):
            return
        delay = min(delay * 2, 120)


def main():
    print("Revenue Bots starting...", flush=True)

    from core import db
    db.init()
    print("Database ready", flush=True)

    t = threading.Thread(target=supervise_worker, daemon=True)
    t.start()
    time.sleep(1)

    port = os.getenv("PORT", "8501")
    try:
        subprocess.run([
            sys.executable, "-m", "streamlit", "run",
            str(ROOT / "dashboard" / "app.py"),
            "--server.port", str(port),
            "--server.address", "0.0.0.0",
            "--server.headless", "true",
            "--browser.gatherUsageStats", "false",
        ], cwd=str(ROOT))
    finally:
        _stop.set()
        t.join(timeout=10)
        print("Worker stopped", flush=True)


if __name__ == "__main__":
    main()
