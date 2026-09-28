#!/usr/bin/env python3
"""AI Ladder server — classroom AI literacy, kindergarten through a bachelor's degree.

Standard library only (http.server + sqlite3): runs anywhere Python 3.9+ does,
with nothing to install, so a district IT team can host it on one small box.

Privacy by design (COPPA / FERPA): students are identified by a nickname and a
login code only. No email, birthday, or real name is ever asked for, and a
teacher can permanently delete a student's data from the dashboard.

  python3 server.py                 # http://localhost:8080
  PORT=9000 DB_PATH=/data/ladder.db python3 server.py
"""
import csv
import hashlib
import io
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import content
import grading
import rating

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
MAX_BODY = 16 * 1024
# No 0/O/1/I/L: codes get read aloud and copied off a board by young kids.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
RECENT_WINDOW = 20  # don't repeat a challenge within this many attempts
NICK_RE = re.compile(r"^[\w .'-]{1,24}$", re.UNICODE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS classes (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, band INTEGER NOT NULL,
  join_code TEXT UNIQUE NOT NULL, teacher_key_hash TEXT UNIQUE NOT NULL,
  leaderboard INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS students (
  id INTEGER PRIMARY KEY, class_id INTEGER REFERENCES classes(id) ON DELETE CASCADE,
  nickname TEXT NOT NULL, band INTEGER NOT NULL, login_code TEXT UNIQUE NOT NULL,
  created REAL NOT NULL, last_active REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS students_nick ON students(class_id, nickname COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS ratings (
  student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE, skill TEXT NOT NULL,
  rating REAL NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (student_id, skill));
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  item_id TEXT NOT NULL, skill TEXT NOT NULL, level INTEGER NOT NULL,
  score REAL NOT NULL, delta REAL NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_student ON attempts(student_id, id);
CREATE TABLE IF NOT EXISTS pending (
  student_id INTEGER PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
  item_id TEXT NOT NULL, state TEXT NOT NULL, issued REAL NOT NULL);
"""


def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


def code(n):
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


class Store:
    """All database access. One connection guarded by a lock: SQLite serializes
    writes anyway, and a classroom's traffic is tiny."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # ── accounts ──
    def create_class(self, name, band, leaderboard):
        key = "T-" + secrets.token_urlsafe(24)
        for _ in range(10):
            join = code(6)
            try:
                cur = self.db.execute(
                    "INSERT INTO classes(name, band, join_code, teacher_key_hash, leaderboard, created)"
                    " VALUES (?,?,?,?,?,?)", (name, band, join, sha256(key), int(leaderboard), time.time()))
                self.db.commit()
                return {"class_id": cur.lastrowid, "join_code": join, "teacher_key": key}
            except sqlite3.IntegrityError:
                continue
        raise ApiError(500, "could not allocate a class code")

    def class_by_key(self, key):
        row = self.db.execute("SELECT * FROM classes WHERE teacher_key_hash = ?", (sha256(key),)).fetchone()
        if not row:
            raise ApiError(401, "unknown teacher key")
        return row

    def create_student(self, class_id, nickname, band):
        now = time.time()
        for _ in range(10):
            login = code(4) + "-" + code(4)
            try:
                cur = self.db.execute(
                    "INSERT INTO students(class_id, nickname, band, login_code, created, last_active)"
                    " VALUES (?,?,?,?,?,?)", (class_id, nickname, band, login, now, now))
                break
            except sqlite3.IntegrityError as e:
                if "nickname" in str(e) or "students_nick" in str(e) or "students.class_id" in str(e):
                    raise ApiError(409, "that nickname is taken in this class — try another")
        else:
            raise ApiError(500, "could not allocate a login code")
        sid = cur.lastrowid
        start = rating.LEVEL_BASE[band]
        self.db.executemany("INSERT INTO ratings(student_id, skill, rating, n) VALUES (?,?,?,0)",
                            [(sid, s, start) for s in content.SKILL_IDS])
        self.db.commit()
        return self.db.execute("SELECT * FROM students WHERE id = ?", (sid,)).fetchone()

    def student_by_code(self, login):
        row = self.db.execute("SELECT * FROM students WHERE login_code = ?",
                              ((login or "").strip().upper(),)).fetchone()
        if not row:
            raise ApiError(401, "unknown login code")
        return row

    # ── profiles ──
    def ratings(self, sid):
        return {r["skill"]: {"rating": r["rating"], "n": r["n"]}
                for r in self.db.execute("SELECT * FROM ratings WHERE student_id = ?", (sid,))}

    def profile(self, st):
        rs = self.ratings(st["id"])
        overall = sum(r["rating"] for r in rs.values()) / len(rs)
        start = rating.LEVEL_BASE[st["band"]]
        stats = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(AVG(score), 0) avg FROM attempts WHERE student_id = ?",
            (st["id"],)).fetchone()
        lvl = rating.level_of(overall)
        cls = None
        if st["class_id"] is not None:
            c = self.db.execute("SELECT name, leaderboard FROM classes WHERE id = ?", (st["class_id"],)).fetchone()
            cls = {"name": c["name"], "leaderboard": bool(c["leaderboard"])}
        return {
            "id": st["id"], "nickname": st["nickname"], "login_code": st["login_code"], "band": st["band"],
            "class": cls, "overall": round(overall), "growth": round(overall - start),
            "level": lvl, "level_progress": round(rating.progress_in_level(overall), 3),
            "attempts": stats["n"], "avg_score": round(stats["avg"], 3),
            "skills": {s: {"rating": round(r["rating"]), "n": r["n"], "level": rating.level_of(r["rating"]),
                           "progress": round(rating.progress_in_level(r["rating"]), 3)}
                       for s, r in rs.items()},
        }

    # ── challenges ──
    def next_item(self, st, rng):
        sid = st["id"]
        pend = self.db.execute("SELECT * FROM pending WHERE student_id = ?", (sid,)).fetchone()
        if pend and pend["item_id"] in content.ITEMS_BY_ID:
            # Reloading the page returns the same challenge, so students can't
            # reroll until they get one they like.
            item = content.ITEMS_BY_ID[pend["item_id"]]
            return self._reapply(item, json.loads(pend["state"]))

        rs = self.ratings(sid)
        band = st["band"]
        lo, hi = max(0, band - 1), min(len(content.LEVELS) - 1, band + 2)
        recent = {r["item_id"] for r in self.db.execute(
            "SELECT item_id FROM attempts WHERE student_id = ? ORDER BY id DESC LIMIT ?", (sid, RECENT_WINDOW))}
        pool = [it for it in content.ITEMS if lo <= it["level"] <= hi]
        fresh = [it for it in pool if it["id"] not in recent] or pool
        # Cover every skill: practice the one with the fewest attempts.
        fewest = min(rs[s]["n"] for s in content.SKILL_IDS if any(it["skill"] == s for it in fresh))
        skills = [s for s in content.SKILL_IDS if rs[s]["n"] == fewest and any(it["skill"] == s for it in fresh)]
        skill = rng.choice(skills)
        cands = [it for it in fresh if it["skill"] == skill]
        target = rating.target_difficulty(rs[skill]["rating"])
        cands.sort(key=lambda it: abs(rating.difficulty(it) - target))
        item = rng.choice(cands[:3])
        view, state = grading.serve(item, rng)
        self.db.execute("INSERT OR REPLACE INTO pending(student_id, item_id, state, issued) VALUES (?,?,?,?)",
                        (sid, item["id"], json.dumps(state), time.time()))
        self.db.commit()
        return view

    @staticmethod
    def _reapply(item, state):
        """Rebuild the exact view a student was shown, from the stored shuffle."""

        class Fixed:
            def __init__(self, perms):
                self.perms = list(perms)

            def shuffle(self, lst):
                lst[:] = self.perms.pop(0)

        if "perm" in state:
            perms = [state["perm"]]
        elif "perms" in state:
            perms = state["perms"]
        else:
            perms = []
        view, _ = grading.serve(item, Fixed(perms))
        return view

    def answer(self, st, item_id, response):
        sid = st["id"]
        pend = self.db.execute("SELECT * FROM pending WHERE student_id = ?", (sid,)).fetchone()
        if not pend or pend["item_id"] != item_id:
            raise ApiError(409, "that challenge isn't active — load the next one")
        item = content.ITEMS_BY_ID[item_id]
        try:
            score, feedback = grading.grade(item, json.loads(pend["state"]), response)
        except grading.BadResponse as e:
            raise ApiError(400, str(e))
        r = self.db.execute("SELECT * FROM ratings WHERE student_id = ? AND skill = ?",
                            (sid, item["skill"])).fetchone()
        before = r["rating"]
        after = rating.update(before, r["n"], rating.difficulty(item), score)
        now = time.time()
        self.db.execute("UPDATE ratings SET rating = ?, n = n + 1 WHERE student_id = ? AND skill = ?",
                        (after, sid, item["skill"]))
        self.db.execute("INSERT INTO attempts(student_id, item_id, skill, level, score, delta, ts)"
                        " VALUES (?,?,?,?,?,?,?)", (sid, item_id, item["skill"], item["level"], score,
                                                    after - before, now))
        self.db.execute("DELETE FROM pending WHERE student_id = ?", (sid,))
        self.db.execute("UPDATE students SET last_active = ? WHERE id = ?", (now, sid))
        self.db.commit()
        return {"score": round(score, 3), "correct": score >= 0.999, "why": item["why"],
                "feedback": feedback, "skill": item["skill"],
                "rating_before": round(before), "rating_after": round(after),
                "level_before": rating.level_of(before), "level_after": rating.level_of(after)}

    # ── class views ──
    def roster(self, class_id):
        rows = self.db.execute("SELECT * FROM students WHERE class_id = ? ORDER BY nickname COLLATE NOCASE",
                               (class_id,)).fetchall()
        out = []
        for st in rows:
            p = self.profile(st)
            p["last_active"] = st["last_active"]
            out.append(p)
        return out

    def leaderboard(self, class_id):
        ros = self.roster(class_id)
        ros.sort(key=lambda p: -p["overall"])
        return [{"nickname": p["nickname"], "overall": p["overall"], "level": p["level"],
                 "growth": p["growth"], "attempts": p["attempts"]} for p in ros]


# ───────────────────────────── HTTP layer ─────────────────────────────

class RateLimiter:
    """Loose per-IP limit on account creation/login. Generous, because a whole
    school often shares one public IP."""

    def __init__(self, per_minute=240):
        self.per_minute, self.hits, self.lock = per_minute, {}, threading.Lock()

    def check(self, ip):
        now = time.time()
        with self.lock:
            q = [t for t in self.hits.get(ip, []) if now - t < 60]
            if len(q) >= self.per_minute:
                raise ApiError(429, "too many requests — wait a minute")
            q.append(now)
            self.hits[ip] = q
            if len(self.hits) > 10000:
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < 60}


CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; object-src 'none'; "
                               "base-uri 'none'; frame-ancestors 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def make_handler(store, limiter):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AILadder/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if os.environ.get("QUIET") != "1":
                super().log_message(fmt, *args)

        # ── plumbing ──
        def _send(self, status, body, ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError(413, "request too large")
            if n == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "invalid JSON")
            if not isinstance(data, dict):
                raise ApiError(400, "expected a JSON object")
            return data

        def _ip(self):
            return self.client_address[0]

        def _teacher(self):
            return store.class_by_key(self.headers.get("X-Teacher-Key") or "")

        def _student(self):
            return store.student_by_code(self.headers.get("X-Student-Code") or "")

        def _dispatch(self, method):
            path = urlparse(self.path).path
            try:
                if method in ("GET", "HEAD") and not path.startswith("/api/"):
                    return self._static(path)
                with store.lock:
                    status, body, *rest = self._route(method, path)
                self._send(status, body, *rest)
            except ApiError as e:
                self._send(e.status, {"error": e.msg})
            except Exception as e:  # never leak a stack trace to a browser
                self.log_error("internal error: %r", e)
                self._send(500, {"error": "internal error"})

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _static(self, path):
            if path == "/":
                path = "/index.html"
            full = os.path.realpath(os.path.join(STATIC, path.lstrip("/")))
            if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                data = f.read()
            ext = os.path.splitext(full)[1]
            self._send(200, data, CONTENT_TYPES.get(ext, "application/octet-stream"))

        # ── API ──
        def _route(self, method, path):
            if method == "GET" and path == "/api/health":
                return 200, {"ok": True}
            if method == "GET" and path == "/api/curriculum":
                return 200, {"levels": content.LEVELS, "skills": content.SKILLS,
                             "level_floor": rating.LEVEL_FLOOR,
                             "counts": {lv["id"]: sum(1 for it in content.ITEMS if it["level"] == lv["id"])
                                        for lv in content.LEVELS}}

            # Teachers
            if method == "POST" and path == "/api/classes":
                limiter.check(self._ip())
                b = self._body()
                name = str(b.get("name", "")).strip()[:60]
                band = b.get("band")
                if not name:
                    raise ApiError(400, "class name is required")
                if not isinstance(band, int) or not 0 <= band < len(content.LEVELS):
                    raise ApiError(400, "pick a grade band")
                # Public rankings default off for young kids; teachers can turn them on.
                lb = b.get("leaderboard", band >= 2)
                return 201, store.create_class(name, band, bool(lb))
            if path == "/api/teacher/class":
                c = self._teacher()
                if method == "PATCH":
                    b = self._body()
                    if "leaderboard" in b:
                        store.db.execute("UPDATE classes SET leaderboard = ? WHERE id = ?",
                                         (int(bool(b["leaderboard"])), c["id"]))
                    if "name" in b and str(b["name"]).strip():
                        store.db.execute("UPDATE classes SET name = ? WHERE id = ?",
                                         (str(b["name"]).strip()[:60], c["id"]))
                    store.db.commit()
                    c = self._teacher()
                elif method != "GET":
                    raise ApiError(405, "method not allowed")
                return 200, {"id": c["id"], "name": c["name"], "band": c["band"], "join_code": c["join_code"],
                             "leaderboard": bool(c["leaderboard"]), "students": store.roster(c["id"])}
            if method == "GET" and path == "/api/teacher/export.csv":
                c = self._teacher()
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["nickname", "login_code", "level", "overall_rating", "growth", "challenges",
                            "avg_score"] + [s["name"] for s in content.SKILLS])
                for p in store.roster(c["id"]):
                    w.writerow([_csv_safe(p["nickname"]), p["login_code"], content.LEVELS[p["level"]]["name"],
                                p["overall"], p["growth"], p["attempts"], p["avg_score"]]
                               + [p["skills"][s]["rating"] for s in content.SKILL_IDS])
                return 200, buf.getvalue(), "text/csv; charset=utf-8", {
                    "Content-Disposition": 'attachment; filename="ai-ladder-class.csv"'}
            m = re.fullmatch(r"/api/teacher/students/(\d+)", path)
            if m and method == "DELETE":
                c = self._teacher()
                cur = store.db.execute("DELETE FROM students WHERE id = ? AND class_id = ?",
                                       (int(m.group(1)), c["id"]))
                store.db.commit()
                if cur.rowcount == 0:
                    raise ApiError(404, "no such student in this class")
                return 200, {"deleted": True}

            # Students
            if method == "POST" and path == "/api/join":
                limiter.check(self._ip())
                b = self._body()
                nick = str(b.get("nickname", "")).strip()
                if not NICK_RE.match(nick):
                    raise ApiError(400, "nickname: 1-24 letters, numbers, or spaces")
                jc = str(b.get("join_code", "")).strip().upper()
                if jc:
                    c = store.db.execute("SELECT * FROM classes WHERE join_code = ?", (jc,)).fetchone()
                    if not c:
                        raise ApiError(404, "no class with that code — check with your teacher")
                    st = store.create_student(c["id"], nick, c["band"])
                else:
                    band = b.get("band")
                    if not isinstance(band, int) or not 0 <= band < len(content.LEVELS):
                        raise ApiError(400, "pick your grade band")
                    st = store.create_student(None, nick, band)
                return 201, store.profile(st)
            if method == "POST" and path == "/api/login":
                limiter.check(self._ip())
                return 200, store.profile(store.student_by_code(str(self._body().get("code", ""))))
            if method == "GET" and path == "/api/me":
                st = self._student()
                p = store.profile(st)
                p["recent"] = [dict(r) for r in store.db.execute(
                    "SELECT item_id, skill, level, score, delta, ts FROM attempts WHERE student_id = ?"
                    " ORDER BY id DESC LIMIT 10", (st["id"],))]
                return 200, p
            if method == "GET" and path == "/api/next":
                return 200, store.next_item(self._student(), random.Random(secrets.randbits(64)))
            if method == "POST" and path == "/api/answer":
                b = self._body()
                return 200, store.answer(self._student(), str(b.get("item_id", "")), b.get("response"))
            if method == "GET" and path == "/api/leaderboard":
                st = self._student()
                if st["class_id"] is None:
                    raise ApiError(404, "solo practice has no class leaderboard")
                c = store.db.execute("SELECT leaderboard FROM classes WHERE id = ?", (st["class_id"],)).fetchone()
                if not c["leaderboard"]:
                    raise ApiError(403, "your teacher has the leaderboard turned off")
                return 200, store.leaderboard(st["class_id"])
            raise ApiError(404, "not found")

    return Handler


def _csv_safe(s):
    # Stop spreadsheet formula injection via nicknames like "=HYPERLINK(...)".
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def make_server(host, port, db_path):
    store = Store(db_path)
    return ThreadingHTTPServer((host, port), make_handler(store, RateLimiter())), store


def main():
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    db_path = os.environ.get("DB_PATH", os.path.join(HERE, "data", "ladder.db"))
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
    srv, _ = make_server(host, port, db_path)
    print(f"AI Ladder running on http://{'localhost' if host == '0.0.0.0' else host}:{port}  (db: {db_path})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
