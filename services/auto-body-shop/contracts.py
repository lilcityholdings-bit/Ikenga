"""Output contracts and graders: how pass/fail is decided without trusting the agent.

An agent's `success: true` is a claim, and the failures that matter most (well-formed but wrong
output, a confident hallucination) are exactly the ones an agent reports as success. So the shop
decides for itself where it can:

- A *contract* is declared once per agent (a JSON Schema subset, or a regex). Every trace's
  output is checked against it server-side.
- A *grader* is attached to each replayable case. It is the contract check, plus optionally an
  expected value supplied by someone other than the agent.

Supported JSON Schema keywords: type, properties, required, additionalProperties (bool),
items, enum, const, minLength, maxLength, pattern, minimum, maximum, minItems, maxItems.
Anything else is rejected when the contract is set, not silently ignored at check time.
"""
import json
import re

try:
    import re._parser as sre_parse  # Python 3.11+
except ImportError:  # pragma: no cover
    import sre_parse

from db import ApiError

SCHEMA_KEYS = {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
               "minLength", "maxLength", "pattern", "minimum", "maximum", "minItems", "maxItems",
               "description", "title"}
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
GRADERS = ("contract", "equals", "json_equals", "contains", "regex")
# Graders anyone other than the agent's owner may attach. No regex: see safe_regex.
CONSUMER_GRADERS = ("contract", "equals", "json_equals", "contains")
MAX_PATTERN = 256
MAX_REGEX_SUBJECT = 20_000
_REPEATS = {sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT, getattr(sre_parse, "POSSESSIVE_REPEAT", None)}


def safe_regex(pattern, where):
    """Refuse patterns that can backtrack catastrophically (ReDoS).

    Python's `re` has no timeout and holds the interpreter lock while matching, so one bad
    pattern like (a+)+$ freezes the whole server, for every customer, on an input of a few
    dozen characters. Rejected: nested unbounded repetition (a repeat inside a repeat, the
    classic exponential shape), backreferences, and patterns over 256 characters. Subjects are
    also capped (MAX_REGEX_SUBJECT) wherever a pattern is run. This is a structural check, not
    a proof of linear time, which is why it's paired with the subject cap."""
    if not isinstance(pattern, str) or len(pattern) > MAX_PATTERN:
        raise ApiError(422, f"{where}: pattern must be a string of at most {MAX_PATTERN} characters")
    try:
        parsed = sre_parse.parse(pattern)
    except re.error as e:
        raise ApiError(422, f"{where}: bad pattern: {e}")

    def walk(items, inside_repeat):
        for op, arg in items:
            if op in (sre_parse.GROUPREF, getattr(sre_parse, "GROUPREF_EXISTS", None)):
                raise ApiError(422, f"{where}: backreferences are not allowed")
            if op in _REPEATS:
                lo, hi, sub = arg
                unbounded = hi == sre_parse.MAXREPEAT or hi > 100
                if inside_repeat and unbounded:
                    raise ApiError(422, f"{where}: nested repetition like (a+)+ is not allowed (it can backtrack "
                                        f"catastrophically); use a JSON schema, or a pattern with no repeat inside a repeat")
                walk(sub, inside_repeat or unbounded)
            elif op == sre_parse.SUBPATTERN:
                walk(arg[-1], inside_repeat)
            elif op == sre_parse.BRANCH:
                for branch in arg[1]:
                    walk(branch, inside_repeat)
            elif op in (sre_parse.ASSERT, sre_parse.ASSERT_NOT):
                walk(arg[1], inside_repeat)
    walk(parsed, False)
    return pattern


def _bounded_search(pattern, text, full=False):
    if text is None or len(text) > MAX_REGEX_SUBJECT:
        return False
    return (re.fullmatch(pattern, text, re.S) if full else re.search(pattern, text)) is not None


def check_schema_supported(schema, path="$"):
    if not isinstance(schema, dict):
        raise ApiError(422, f"Schema at {path} must be an object")
    unknown = set(schema) - SCHEMA_KEYS
    if unknown:
        raise ApiError(422, f"Unsupported schema keyword(s) at {path}: {', '.join(sorted(unknown))}")
    for name, sub in (schema.get("properties") or {}).items():
        check_schema_supported(sub, f"{path}.{name}")
    if "items" in schema:
        check_schema_supported(schema["items"], f"{path}[]")
    if "pattern" in schema:
        safe_regex(schema["pattern"], f"schema {path}")


def _is_type(value, t):
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, TYPES[t])


def validate(schema, value, path="$"):
    """Returns the first violation as a string, or None."""
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(value, x) for x in types):
            return f"{path}: expected {t}"
    if "const" in schema and value != schema["const"]:
        return f"{path}: must equal {schema['const']!r}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: not one of {schema['enum']!r}"
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return f"{path}: shorter than {schema['minLength']}"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path}: longer than {schema['maxLength']}"
        if "pattern" in schema and not _bounded_search(schema["pattern"], value):
            return f"{path}: does not match {schema['pattern']!r}"
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path}: below {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path}: above {schema['maximum']}"
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            return f"{path}: fewer than {schema['minItems']} items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path}: more than {schema['maxItems']} items"
        for i, item in enumerate(value):
            err = "items" in schema and validate(schema["items"], item, f"{path}[{i}]")
            if err:
                return err
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                return f"{path}: missing required '{name}'"
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                return f"{path}: unexpected {sorted(extra)}"
        for name, sub in props.items():
            if name in value:
                err = validate(sub, value[name], f"{path}.{name}")
                if err:
                    return err
    return None


def normalize_contract(contract):
    if contract is None:
        return {"type": "none"}
    if not isinstance(contract, dict) or contract.get("type") not in ("none", "json_schema", "regex"):
        raise ApiError(422, "contract.type must be none, json_schema or regex")
    if contract["type"] == "json_schema":
        check_schema_supported(contract.get("schema"))
    if contract["type"] == "regex":
        safe_regex(contract.get("pattern"), "contract")
    return contract


def check_contract(contract, output):
    """(ok, reason). ok is None when there's no contract to check against."""
    kind = contract.get("type", "none")
    if kind == "none":
        return None, "no contract"
    if output is None:
        return False, "no output"
    if kind == "regex":
        return (True, "ok") if _bounded_search(contract["pattern"], output, full=True) else (False, "output does not match contract pattern")
    try:
        value = json.loads(output)
    except ValueError:
        return False, "output is not valid JSON"
    err = validate(contract["schema"], value)
    return (False, err) if err else (True, "ok")


def make_grader(body, allowed=GRADERS):
    grader = body.get("grader") or ("json_equals" if "expected" in body and not isinstance(body["expected"], str)
                                    else "equals" if "expected" in body else "contract")
    if grader not in allowed:
        raise ApiError(422, f"grader must be one of {', '.join(allowed)}")
    if "expected" in body and len(json.dumps(body["expected"])) > 65536:
        raise ApiError(413, "expected is too large")
    if grader != "contract" and "expected" not in body:
        raise ApiError(422, f"grader '{grader}' needs 'expected'")
    g = {"type": grader}
    if grader != "contract":
        g["expected"] = body["expected"]
        if grader == "regex":
            safe_regex(body["expected"], "grader")
    return g


def grade(grader, contract, output):
    """A case passes when the output satisfies the agent's contract (if it has one) AND the
    case's own grader."""
    ok, _ = check_contract(contract, output)
    if ok is False:
        return False
    kind = grader["type"]
    if kind == "contract":
        return ok is True or (ok is None and output is not None)
    exp = grader["expected"]
    if output is None:
        return False
    if kind == "equals":
        return output.strip() == str(exp).strip()
    if kind == "contains":
        return str(exp) in output
    if kind == "regex":
        return _bounded_search(exp, output)
    if kind == "json_equals":
        try:
            return json.loads(output) == (json.loads(exp) if isinstance(exp, str) else exp)
        except ValueError:
            return False
    return False
