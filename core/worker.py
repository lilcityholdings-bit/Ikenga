"""
The worker. One process, one loop, every running bot each cycle.

Bots run concurrently because their time is almost entirely spent waiting on
network calls. SQLite is safe here thanks to WAL plus a 60 second busy timeout,
but do not push MAX_PARALLEL_BOTS much past 6 or you trade speed for lock
contention.
"""
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from agents.bot import Bot
from config.settings import (
    MAX_ARTICLES_PER_BOT_PER_HOUR, MAX_PARALLEL_BOTS, MAX_PENDING_QUEUE,
    TRADING_LOOP_INTERVAL_SECONDS, TRADING_MODE, WORKER_INTERVAL_SECONDS,
)
from core import db

ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT = ROOT / "data" / "heartbeat.txt"
PIDFILE = ROOT / "data" / "worker.pid"


def beat(status="ok"):
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(
        f"{datetime.utcnow().isoformat()}\n{status}\n{WORKER_INTERVAL_SECONDS}\n",
        encoding="utf-8",
    )


def read_beat():
    if not HEARTBEAT.exists():
        return {"alive": False, "age": None, "status": "never started"}
    try:
        parts = HEARTBEAT.read_text(encoding="utf-8").strip().split("\n")
        age = (datetime.utcnow() - datetime.fromisoformat(parts[0])).total_seconds()
        interval = int(parts[2]) if len(parts) > 2 else WORKER_INTERVAL_SECONDS
        # Allow two full cycles plus slack. A flat window equal to the sleep
        # length makes the worker look dead right before every cycle.
        return {"alive": age < (interval * 2 + 60), "age": age,
                "status": parts[1] if len(parts) > 1 else ""}
    except Exception:
        return {"alive": False, "age": None, "status": "unreadable"}


def _run(bot_id):
    try:
        return bool(Bot(bot_id).run_cycle())
    except Exception as e:
        db.log(bot_id, "error", str(e)[:400])
        return False


LOCKFILE = ROOT / "data" / "cycle.lock"
LOCK_STALE_SECONDS = 900


def _acquire_lock():
    """
    Stop the background worker and the dashboard's "Run one cycle now" button
    from running the same bot at the same time. They are separate processes,
    so without this you get duplicate articles and double the API spend.
    """
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        if LOCKFILE.exists():
            age = time.time() - LOCKFILE.stat().st_mtime
            if age < LOCK_STALE_SECONDS:
                return False
            LOCKFILE.unlink()          # previous run died mid-cycle
    except Exception:
        pass
    try:
        # O_EXCL makes creation atomic: whoever wins creates it, the other fails.
        fd = os.open(str(LOCKFILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return True                    # never let locking block real work


def _release_lock():
    try:
        LOCKFILE.unlink()
    except Exception:
        pass


def run_once():
    """One full pass: ranking if due, all bots, then autopilot review."""
    if not _acquire_lock():
        beat("skipped: another cycle already running")
        return {"ran": 0, "blocked": 0, "busy": True,
                "auto": {"published": 0, "rejected": 0, "escalated": 0}}
    try:
        return _run_once_locked()
    finally:
        _release_lock()


def _run_once_locked():
    try:
        from core.controller import Controller
        ranked = Controller().maybe_rank()
        if ranked:
            db.log(0, "ranking", f"removed={ranked.get('removed')} bred={ranked.get('bred')}")
    except Exception as e:
        db.log(0, "ranking_error", str(e)[:300])

    running = [b["id"] for b in db.get_bots() if b["status"] == "running"]

    # Backpressure: writing more when a dozen drafts are already unreviewed
    # just buries you. Autopilot still runs below, so the queue can drain.
    pending = db.kpis().get("needs_review", 0)
    if pending >= MAX_PENDING_QUEUE:
        beat(f"paused: {pending} drafts awaiting review")
        try:
            from core.autopilot import review_pending
            auto = review_pending()
            beat(f"paused, queue {pending}, autopilot cleared "
                 f"{auto.get('published', 0) + auto.get('rejected', 0)}")
        except Exception:
            pass
        return {"ran": 0, "blocked": len(running), "paused": True,
                "auto": {"published": 0, "rejected": 0, "escalated": 0}}

    blocked = db.rate_limited_bots(running, MAX_ARTICLES_PER_BOT_PER_HOUR)
    todo = [b for b in running if b not in blocked]

    ran = 0
    if todo:
        workers = max(1, min(MAX_PARALLEL_BOTS, len(todo)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for f in as_completed([pool.submit(_run, b) for b in todo]):
                if f.result():
                    ran += 1

    auto = {"published": 0, "rejected": 0, "escalated": 0}
    try:
        from core.autopilot import review_pending
        auto = review_pending()
    except Exception as e:
        db.log(0, "autopilot_error", str(e)[:300])

    _maybe_run_trading_tick()

    beat(f"ran={ran} limited={len(blocked)} "
         f"auto_pub={auto.get('published', 0)} auto_rej={auto.get('rejected', 0)} "
         f"escalated={auto.get('escalated', 0)}")
    return {"ran": ran, "blocked": len(blocked), "auto": auto}


def _maybe_run_trading_tick():
    """Crypto trading is a separate subsystem from the content bots above —
    own tables, own money, own safety gates (see core/trading/). Only runs
    in TRADING_MODE=poll; in TRADING_MODE=realtime, trading_stream.py owns
    it via its own persistent process, and this must stay out of its way
    entirely so the same account is never traded by both at once."""
    if TRADING_MODE != "poll":
        return
    try:
        last = db.get_setting("trading_last_tick")
        now = datetime.now(timezone.utc)
        if last:
            elapsed = (now - datetime.fromisoformat(last)).total_seconds()
            if elapsed < TRADING_LOOP_INTERVAL_SECONDS:
                return
        db.set_setting("trading_last_tick", now.isoformat())
        from core.trading.controller import run_trading_tick
        run_trading_tick()
    except Exception as e:
        db.log_trading_activity("error", f"tick failed: {str(e)[:300]}")


def run_forever():
    db.init()
    try:
        from core.controller import Controller
        Controller().seed()
    except Exception:
        pass
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    beat("starting")

    while True:
        started = time.time()
        try:
            run_once()
        except Exception:
            beat("error")
            logf = ROOT / "logs" / "worker.log"
            logf.parent.mkdir(parents=True, exist_ok=True)
            with logf.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.utcnow().isoformat()}\n{traceback.format_exc()}\n")
        # Sleep the remainder of the interval, not a flat delay on top of work.
        time.sleep(max(5, WORKER_INTERVAL_SECONDS - (time.time() - started)))


if __name__ == "__main__":
    run_forever()
