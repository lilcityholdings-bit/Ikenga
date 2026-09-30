#!/usr/bin/env python3
"""Measure each challenge's real difficulty from how students actually did.

Every difficulty in content.py starts as the author's guess. After a pilot, run
this to see which guesses were wrong, and --apply to make the rating system use
the measured values instead. Only challenges with enough attempts are changed.

  python3 calibrate.py                       # report only
  python3 calibrate.py --apply --min-n 30    # save measured difficulties
  python3 calibrate.py --db /data/ladder.db

It also lists the challenges students reported as wrong or confusing, which is
the content-review queue.
"""
import argparse
import os
import sqlite3
import time

import content
import rating

HERE = os.path.dirname(os.path.abspath(__file__))


def measure(db, min_n):
    rows = db.execute("SELECT item_id, rating_before, score FROM attempts WHERE rating_before IS NOT NULL").fetchall()
    by_item = {}
    for item_id, r, score in rows:
        by_item.setdefault(item_id, ([], []))
        by_item[item_id][0].append(r)
        by_item[item_id][1].append(score)
    out = []
    for item_id, (ratings, scores) in sorted(by_item.items()):
        it = content.ITEMS_BY_ID.get(item_id)
        if not it or len(scores) < min_n:
            continue
        guess = rating.authored_difficulty(it)
        measured = rating.fit_difficulty(ratings, scores)
        out.append({"item_id": item_id, "level": it["level"], "n": len(scores),
                    "avg_score": sum(scores) / len(scores), "guess": guess, "measured": measured})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("DB_PATH", os.path.join(HERE, "data", "ladder.db")))
    ap.add_argument("--min-n", type=int, default=30, help="attempts needed before trusting the data (default 30)")
    ap.add_argument("--apply", action="store_true", help="save measured difficulties for the server to use")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    results = measure(db, args.min_n)
    if not results:
        print(f"No challenge has {args.min_n}+ attempts yet. Keep piloting.")
    else:
        print(f"{'challenge':<10}{'level':>6}{'tries':>7}{'avg':>7}{'guess':>8}{'measured':>10}{'off by':>8}")
        for r in sorted(results, key=lambda r: -abs(r["measured"] - r["guess"])):
            print(f"{r['item_id']:<10}{r['level']:>6}{r['n']:>7}{r['avg_score']:>7.0%}{r['guess']:>8}"
                  f"{r['measured']:>10.0f}{r['measured'] - r['guess']:>+8.0f}")
        if args.apply:
            now = time.time()
            db.executemany("INSERT OR REPLACE INTO calibration(item_id, difficulty, n, updated) VALUES (?,?,?,?)",
                           [(r["item_id"], r["measured"], r["n"], now) for r in results])
            db.commit()
            print(f"\nSaved {len(results)} measured difficulties. Restart the server to use them.")

    flagged = db.execute("SELECT item_id, reason, COUNT(*) FROM reports GROUP BY item_id, reason"
                         " ORDER BY COUNT(*) DESC").fetchall()
    if flagged:
        print("\nReported by students (review these first):")
        for item_id, reason, n in flagged:
            it = content.ITEMS_BY_ID.get(item_id)
            print(f"  {item_id:<6} x{n:<3} {content.REPORT_REASONS.get(reason, reason):<32} "
                  f"{it['prompt'][:60] if it else '(removed)'}")


if __name__ == "__main__":
    main()
