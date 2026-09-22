"""
Shared lessons. Bots compete on results but pool what they learn.

Capped at a fixed number of lines so the file cannot grow without bound and
cannot quietly eat the prompt budget.
"""
import json
from datetime import datetime
from pathlib import Path

HIVE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
HIVE_FILE = HIVE_DIR / "hive.jsonl"
MAX_LINES = 300

HIVE_DIR.mkdir(parents=True, exist_ok=True)


def contribute(bot_id, kind, content, score=0.0):
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "bot_id": bot_id,
        "kind": kind,
        "content": (content or "")[:400],
        "score": float(score or 0),
    }
    try:
        with HIVE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        lines = HIVE_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_LINES:
            HIVE_FILE.write_text("\n".join(lines[-MAX_LINES:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def read(limit=8):
    if not HIVE_FILE.exists():
        return []
    items = []
    try:
        for line in HIVE_FILE.read_text(encoding="utf-8").splitlines()[-100:]:
            try:
                items.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        return []
    items.sort(key=lambda x: (float(x.get("score") or 0), x.get("ts", "")), reverse=True)
    return items[:limit]


def text(limit=8):
    items = read(limit)
    if not items:
        return "(nothing shared yet)"
    return "\n".join(
        f"- [{i.get('kind')}] {i.get('content','')[:180]}" for i in items
    )
