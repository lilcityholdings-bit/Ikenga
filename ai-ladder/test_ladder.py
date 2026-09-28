#!/usr/bin/env python3
"""Tests for AI Ladder: content integrity, grading, ratings, and the HTTP API.

  python3 test_ladder.py
"""
import json
import os
import random
import threading
import unittest
import urllib.error
import urllib.request

os.environ["QUIET"] = "1"

import content
import grading
import rating
import server


def perfect_response(item, view, state):
    """What a student who knows everything would submit for this served view."""
    k = item["type"]
    if k == "mc":
        return state["perm"].index(item["answer"])
    if k == "sort":
        return [item["cards"][i]["bin"] for i in state["perm"]]
    if k == "order":
        return [state["perm"].index(i) for i in range(len(item["steps"]))]
    if k == "spot":
        return [i for i, s in enumerate(item["sentences"]) if not s["true"]]
    if k == "prompt":
        out = []
        for slot, p in zip(item["slots"], state["perms"]):
            best = max(range(len(slot["options"])), key=lambda j: slot["options"][j]["pts"])
            out.append(p.index(best))
        return out
    if k == "teach":
        return [item["train"][i]["y"] for i in state["perm"]]
    if k == "tune":
        return 0.5
    raise AssertionError(k)


class ContentTest(unittest.TestCase):
    def test_ids_unique(self):
        ids = [it["id"] for it in content.ITEMS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_level_covers_every_skill(self):
        for lv in content.LEVELS:
            for sk in content.SKILL_IDS:
                n = sum(1 for it in content.ITEMS if it["level"] == lv["id"] and it["skill"] == sk)
                self.assertGreaterEqual(n, 2, f"level {lv['id']} skill {sk} has only {n} challenges")

    def test_items_well_formed(self):
        for it in content.ITEMS:
            with self.subTest(it["id"]):
                self.assertIn(it["skill"], content.SKILL_IDS)
                self.assertIn(it["tier"], (1, 2, 3))
                self.assertTrue(it["prompt"] and it["why"])
                if it["type"] == "mc":
                    self.assertLess(it["answer"], len(it["choices"]))
                    self.assertEqual(len(set(it["choices"])), len(it["choices"]))
                if it["type"] == "sort":
                    for c in it["cards"]:
                        self.assertLess(c["bin"], len(it["bins"]))
                if it["type"] == "spot":
                    self.assertTrue(any(not s["true"] for s in it["sentences"]))
                if it["type"] == "prompt":
                    for s in it["slots"]:
                        self.assertEqual(max(o["pts"] for o in s["options"]), 2)

    def test_perfect_answers_score_full_marks(self):
        rng = random.Random(1)
        for it in content.ITEMS:
            with self.subTest(it["id"]):
                view, state = grading.serve(it, rng)
                score, _ = grading.grade(it, state, perfect_response(it, view, state))
                self.assertEqual(score, 1.0)

    def test_served_view_never_leaks_answers(self):
        rng = random.Random(2)
        for it in content.ITEMS:
            view, _ = grading.serve(it, rng)
            blob = json.dumps(view)
            for leak in ('"answer"', '"bin"', '"true"', '"pts"', '"x"', '"y"'):
                self.assertNotIn(leak, blob, f"{it['id']} leaks {leak}")


class GradingTest(unittest.TestCase):
    def test_teach_learns_from_wrong_labels(self):
        it = content.ITEMS_BY_ID["l0a"]
        _, state = grading.serve(it, random.Random(3))
        # Flip every label: the robot learns the opposite and fails the test.
        wrong = [1 - it["train"][i]["y"] for i in state["perm"]]
        score, fb = grading.grade(it, state, wrong)
        self.assertEqual(score, 0.0)
        self.assertFalse(any(fb["labels_right"]))

    def test_tune_diverges_above_one(self):
        it = content.ITEMS_BY_ID["l4a"]
        self.assertEqual(grading.grade(it, {}, 1.2)[0], 0.0)
        self.assertEqual(grading.grade(it, {}, 0.5)[0], 1.0)
        self.assertLess(grading.grade(it, {}, 0.02)[0], 0.5)

    def test_order_partial_credit(self):
        it = content.ITEMS_BY_ID["r3c"]
        state = {"perm": list(range(6))}
        # Swap two adjacent steps: 14 of 15 pairs still in order.
        score, _ = grading.grade(it, state, [0, 1, 3, 2, 4, 5])
        self.assertAlmostEqual(score, 14 / 15)

    def test_bad_responses_rejected(self):
        it = content.ITEMS_BY_ID["p0a"]
        _, state = grading.serve(it, random.Random(4))
        for bad in (None, -1, 99, True, "0", [0]):
            with self.assertRaises(grading.BadResponse):
                grading.grade(it, state, bad)
        with self.assertRaises(grading.BadResponse):
            grading.grade(content.ITEMS_BY_ID["r0b"], {"perm": [3, 2, 1, 0]}, [0, 0, 1, 2])


class RatingTest(unittest.TestCase):
    def test_levels(self):
        for i, base in enumerate(rating.LEVEL_BASE):
            self.assertEqual(rating.level_of(base), i)

    def test_update_direction(self):
        self.assertGreater(rating.update(1000, 0, 1000, 1.0), 1000)
        self.assertLess(rating.update(1000, 0, 1000, 0.0), 1000)
        # Beating a harder challenge earns more than beating an easier one.
        self.assertGreater(rating.update(1000, 0, 1300, 1.0), rating.update(1000, 0, 700, 1.0))

    def test_strong_student_climbs(self):
        r = rating.LEVEL_BASE[0]
        for _ in range(200):
            r = rating.update(r, 50, rating.target_difficulty(r) + 100, 1.0)
        self.assertEqual(rating.level_of(r), 5)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.store = server.make_server("127.0.0.1", 0, ":memory:")
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, method, path, body=None, headers=None, raw=False):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as r:
                txt = r.read().decode()
                return r.status, (txt if raw else json.loads(txt))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def perfect(self, code, view):
        item = content.ITEMS_BY_ID[view["id"]]
        with self.store.lock:
            row = self.store.db.execute("SELECT state FROM pending WHERE student_id ="
                                        " (SELECT id FROM students WHERE login_code = ?)", (code,)).fetchone()
        return perfect_response(item, view, json.loads(row["state"]))

    def test_classroom_flow(self):
        st, cls = self.call("POST", "/api/classes", {"name": "Room 12", "band": 1})
        self.assertEqual(st, 201)
        self.assertFalse(self.call("GET", "/api/teacher/class", headers={"X-Teacher-Key": cls["teacher_key"]})[1]["leaderboard"])
        tk = {"X-Teacher-Key": cls["teacher_key"]}

        st, me = self.call("POST", "/api/join", {"join_code": cls["join_code"].lower(), "nickname": "Rocket"})
        self.assertEqual(st, 201)
        self.assertEqual(me["level"], 1)
        sk = {"X-Student-Code": me["login_code"]}
        self.assertEqual(self.call("POST", "/api/join", {"join_code": cls["join_code"], "nickname": "rocket"})[0], 409)

        # Reloading returns the same challenge with the same shuffle.
        v1 = self.call("GET", "/api/next", headers=sk)[1]
        v2 = self.call("GET", "/api/next", headers=sk)[1]
        self.assertEqual(v1, v2)

        # A perfect streak raises the rating and covers every skill.
        skills = set()
        view = v1
        for _ in range(18):
            st, res = self.call("POST", "/api/answer", {"item_id": view["id"], "response": self.perfect(me["login_code"], view)}, sk)
            self.assertEqual(st, 200, res)
            self.assertTrue(res["correct"])
            skills.add(res["skill"])
            view = self.call("GET", "/api/next", headers=sk)[1]
        self.assertEqual(skills, set(content.SKILL_IDS))
        prof = self.call("GET", "/api/me", headers=sk)[1]
        self.assertGreater(prof["growth"], 50)  # ~85-120 in practice; depends on which items were drawn

        # Answering a challenge that isn't active is refused (no rating farming).
        self.assertEqual(self.call("POST", "/api/answer", {"item_id": "p0a", "response": 0}, sk)[0], 409)

        # Leaderboard is off for this band until the teacher turns it on.
        self.assertEqual(self.call("GET", "/api/leaderboard", headers=sk)[0], 403)
        self.call("PATCH", "/api/teacher/class", {"leaderboard": True}, tk)
        st, board = self.call("GET", "/api/leaderboard", headers=sk)
        self.assertEqual((st, board[0]["nickname"]), (200, "Rocket"))
        self.assertNotIn("login_code", board[0])

        dash = self.call("GET", "/api/teacher/class", headers=tk)[1]
        self.assertEqual(len(dash["students"]), 1)
        st, csv_txt = self.call("GET", "/api/teacher/export.csv", headers=tk, raw=True)
        self.assertIn("Rocket", csv_txt)

        # Another teacher cannot delete this student; the right teacher can.
        other = self.call("POST", "/api/classes", {"name": "Other", "band": 3})[1]
        self.assertEqual(self.call("DELETE", f"/api/teacher/students/{me['id']}",
                                   headers={"X-Teacher-Key": other["teacher_key"]})[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/teacher/students/{me['id']}", headers=tk)[0], 200)
        self.assertEqual(self.call("GET", "/api/me", headers=sk)[0], 401)

    def test_solo_and_validation(self):
        self.assertEqual(self.call("POST", "/api/join", {"nickname": "Ada", "band": 5})[0], 201)
        self.assertEqual(self.call("POST", "/api/join", {"nickname": "<script>", "band": 0})[0], 400)
        self.assertEqual(self.call("POST", "/api/join", {"nickname": "Ada", "band": 9})[0], 400)
        self.assertEqual(self.call("POST", "/api/join", {"nickname": "Ada", "join_code": "ZZZZZZ"})[0], 404)
        self.assertEqual(self.call("GET", "/api/me", headers={"X-Student-Code": "nope"})[0], 401)
        self.assertEqual(self.call("GET", "/api/teacher/class", headers={"X-Teacher-Key": "nope"})[0], 401)

    def test_static_and_traversal(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"AI Ladder", r.read())
            self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(self.call("GET", "/../server.py")[0], 404)
        self.assertEqual(self.call("GET", "/%2e%2e/server.py")[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=1)
