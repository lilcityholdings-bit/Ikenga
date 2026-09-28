"""Serve challenges to students and grade their responses.

serve(item, rng) returns (public_view, state). public_view goes to the browser
and never contains the answer; state (the shuffle order) stays on the server
until the student submits, then grade(item, state, response) scores it.

Scores are in [0, 1] so partial credit feeds the rating update smoothly.
"""


class BadResponse(ValueError):
    pass


def _perm(rng, n):
    p = list(range(n))
    rng.shuffle(p)
    return p


def serve(item, rng):
    kind = item["type"]
    base = {"id": item["id"], "type": kind, "prompt": item["prompt"],
            "level": item["level"], "skill": item["skill"]}
    if kind == "mc":
        p = _perm(rng, len(item["choices"]))
        return {**base, "choices": [item["choices"][i] for i in p]}, {"perm": p}
    if kind == "sort":
        p = _perm(rng, len(item["cards"]))
        return {**base, "bins": item["bins"], "cards": [item["cards"][i]["text"] for i in p]}, {"perm": p}
    if kind == "order":
        p = _perm(rng, len(item["steps"]))
        if p == sorted(p):  # never serve it already solved
            p.reverse()
        return {**base, "steps": [item["steps"][i] for i in p]}, {"perm": p}
    if kind == "spot":
        return {**base, "sentences": [s["text"] for s in item["sentences"]]}, {}
    if kind == "prompt":
        perms = [_perm(rng, len(s["options"])) for s in item["slots"]]
        slots = [{"label": s["label"], "options": [s["options"][i]["text"] for i in p]}
                 for s, p in zip(item["slots"], perms)]
        return {**base, "slots": slots}, {"perms": perms}
    if kind == "teach":
        p = _perm(rng, len(item["train"]))
        return {**base, "classes": item["classes"],
                "train": [item["train"][i]["text"] for i in p],
                "test": [t["text"] for t in item["test"]]}, {"perm": p}
    if kind == "tune":
        return {**base, "start": item["start"], "target": item["target"], "steps": item["steps"],
                "lr_min": 0.01, "lr_max": 1.2}, {}
    raise ValueError(f"unknown challenge type {kind}")


def _int_list(resp, n, hi):
    if not isinstance(resp, list) or len(resp) != n:
        raise BadResponse(f"expected a list of {n} numbers")
    out = []
    for v in resp:
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < hi:
            raise BadResponse("answer out of range")
        out.append(v)
    return out


def nearest_neighbor(train_x, train_y, x):
    """1-nearest-neighbour classifier (squared Euclidean). Ties go to the first example."""
    best, best_d = None, None
    for tx, ty in zip(train_x, train_y):
        d = sum((a - b) ** 2 for a, b in zip(tx, x))
        if best_d is None or d < best_d:
            best, best_d = ty, d
    return best


def gradient_descent(start, target, steps, lr):
    """Minimize (w - target)^2. Returns the path of w values."""
    w, path = start, [start]
    for _ in range(steps):
        w = w - lr * 2 * (w - target)
        if abs(w) > 1e6:  # diverged; stop before floats overflow
            path.append(w)
            break
        path.append(w)
    return path


def grade(item, state, response):
    """Returns (score in [0,1], feedback dict for the browser)."""
    kind = item["type"]

    if kind == "mc":
        p = state["perm"]
        if not isinstance(response, int) or isinstance(response, bool) or not 0 <= response < len(p):
            raise BadResponse("choose one option")
        correct_shown = p.index(item["answer"])
        ok = response == correct_shown
        return (1.0 if ok else 0.0), {"correct_choice": correct_shown}

    if kind == "sort":
        p = state["perm"]
        resp = _int_list(response, len(p), len(item["bins"]))
        truth = [item["cards"][i]["bin"] for i in p]
        right = [r == t for r, t in zip(resp, truth)]
        return sum(right) / len(right), {"truth": truth, "right": right}

    if kind == "order":
        p = state["perm"]
        n = len(p)
        # response: the shown-step indices in the order the student put them.
        resp = _int_list(response, n, n)
        if sorted(resp) != list(range(n)):
            raise BadResponse("use every step exactly once")
        ranks = [p[i] for i in resp]  # true position of each step as placed
        pairs = n * (n - 1) // 2
        good = sum(1 for a in range(n) for b in range(a + 1, n) if ranks[a] < ranks[b])
        return good / pairs, {"correct_order": [p.index(i) for i in range(n)]}

    if kind == "spot":
        n = len(item["sentences"])
        if not isinstance(response, list) or len(response) > n:
            raise BadResponse("send the list of sentences you marked false")
        marked = set(_int_list(response, len(response), n))
        right = [(i in marked) == (not s["true"]) for i, s in enumerate(item["sentences"])]
        return sum(right) / n, {"false_sentences": [i for i, s in enumerate(item["sentences"]) if not s["true"]],
                                "right": right}

    if kind == "prompt":
        perms = state["perms"]
        resp = _int_list(response, len(perms), 99)
        pts, best, tips = 0, 0, []
        for slot, p, r in zip(item["slots"], perms, resp):
            if r >= len(p):
                raise BadResponse("answer out of range")
            chosen = slot["options"][p[r]]
            top = max(slot["options"], key=lambda o: o["pts"])
            pts += chosen["pts"]
            best += top["pts"]
            tips.append({"label": slot["label"], "chosen": chosen["text"], "pts": chosen["pts"],
                         "best": top["text"]})
        return pts / best, {"slots": tips}

    if kind == "teach":
        p = state["perm"]
        resp = _int_list(response, len(p), len(item["classes"]))
        # resp[k] is the label for shown example k, i.e. item["train"][p[k]].
        train_x = [item["train"][i]["x"] for i in p]
        preds = [nearest_neighbor(train_x, resp, t["x"]) for t in item["test"]]
        right = [pr == t["y"] for pr, t in zip(preds, item["test"])]
        label_right = [r == item["train"][i]["y"] for r, i in zip(resp, p)]
        return sum(right) / len(right), {"predictions": preds, "truth": [t["y"] for t in item["test"]],
                                         "right": right, "labels_right": label_right}

    if kind == "tune":
        if not isinstance(response, (int, float)) or isinstance(response, bool) or not 0.01 <= response <= 1.2:
            raise BadResponse("learning rate must be between 0.01 and 1.2")
        path = gradient_descent(item["start"], item["target"], item["steps"], float(response))
        loss = (path[-1] - item["target"]) ** 2
        if loss < 0.01:
            score = 1.0
        elif loss < 0.1:
            score = 0.75
        elif loss < 1:
            score = 0.5
        elif loss < 10:
            score = 0.25
        else:
            score = 0.0
        return score, {"path": path, "final_loss": loss}

    raise ValueError(f"unknown challenge type {kind}")
