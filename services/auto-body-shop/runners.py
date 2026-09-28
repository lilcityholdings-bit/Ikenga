"""Runners execute an agent configuration on one input, so the referee can replay cases.

Selected with ABS_RUNNER:

    command  runs ABS_RUNNER_CMD (split shell-style, no shell) with {"config": ..., "input": ...}
             as JSON on stdin; stdout is the output. Use it for any model or framework, including
             the agent's real code path. This is the most faithful option.
    claude   calls Claude with the official `anthropic` SDK (pip install anthropic; credentials
             from ANTHROPIC_API_KEY). The config's `model` is used as-is, with no fallback to a
             different model, because an eval must run what production runs. Tools are sent as
             definitions but not executed: a tool-using agent should use `command`.

Anything else (the default) means the referee can't run cases, and bounties refuse to open.
That's a clear failure, not a silent one.

Metering: a runner returns (output, cost) where cost is in atomic units (1e-6 USD), or None if it
can't tell. The claude runner prices each call from the response's token usage. The command runner
reports cost if ABS_RUNNER_REPORTS_COST=1, in which case stdout must be {"output": ..., "cost": units}.
Unmetered runs are charged a flat ABS_UNMETERED_COST_PER_RUN.
"""
import json
import os
import shlex
import subprocess

# USD per million tokens (input, output), from Anthropic's published price list. Because 1 atomic
# unit = 1e-6 USD, cost in units = input_tokens * in_price + output_tokens * out_price.
# Override or extend with ABS_MODEL_PRICES='{"model": [in, out]}'.
MODEL_PRICES = {
    "claude-fable-5-1": (10, 50), "claude-fable-5": (10, 50), "claude-opus-5-5": (4, 20), "claude-opus-5": (5, 25),
    "claude-opus-4-8": (5, 25), "claude-opus-4-7": (5, 25), "claude-opus-4-6": (5, 25), "claude-sonnet-5": (2, 10),
    "claude-sonnet-4-6": (3, 15), "claude-haiku-4-5": (1, 5),
}


def _command_runner(cmd, timeout):
    argv = shlex.split(cmd)
    reports_cost = os.environ.get("ABS_RUNNER_REPORTS_COST") == "1"

    def run(config, input_text):
        proc = subprocess.run(argv, input=json.dumps({"config": config, "input": input_text}).encode(),
                              capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"runner exit {proc.returncode}: {proc.stderr.decode(errors='replace')[-300:]}")
        if not reports_cost:
            return proc.stdout.decode(), None
        out = json.loads(proc.stdout)
        return out.get("output"), int(out["cost"])

    return run


def _claude_runner():
    import anthropic  # optional dependency: only needed when ABS_RUNNER=claude

    client = anthropic.Anthropic()
    prices = dict(MODEL_PRICES, **{k: tuple(v) for k, v in json.loads(os.environ.get("ABS_MODEL_PRICES", "{}")).items()})
    cap = int(os.environ.get("ABS_MAX_COST_PER_RUN", "20000"))

    def run(config, input_text):
        params = config.get("params") or {}
        messages = []
        for ex in config.get("examples") or []:
            messages += [{"role": "user", "content": str(ex["input"])},
                         {"role": "assistant", "content": str(ex["output"])}]
        messages.append({"role": "user", "content": input_text})
        price = prices.get(config["model"])
        if price is None:
            # Unpriced model: we couldn't cap what the call costs, so we don't make it.
            raise RuntimeError(f"no price known for model {config['model']!r}; add it to ABS_MODEL_PRICES")
        # The per-run cost cap is what callers are charged at most, so it must also bound what the
        # call can really cost. Estimate input at ~3 characters per token and give the output
        # whatever budget is left. Without this, a long max_tokens bills the operator far more
        # than it can charge.
        est_in = (len(config["system_instruction"]) + sum(len(m["content"]) for m in messages)) // 3 + 1
        budget_out = (cap - est_in * price[0]) // price[1]
        if budget_out < 16:
            raise RuntimeError("input alone exceeds the per-run cost cap (ABS_MAX_COST_PER_RUN)")
        kwargs = {"model": config["model"], "max_tokens": int(min(int(params.get("max_tokens", 16000)), budget_out)),
                  "system": config["system_instruction"], "messages": messages}
        if params.get("effort"):
            kwargs["output_config"] = {"effort": params["effort"]}
        if config.get("tools"):
            kwargs["tools"] = config["tools"]
        response = client.messages.create(**kwargs)
        cost = response.usage.input_tokens * price[0] + response.usage.output_tokens * price[1]
        if response.stop_reason == "refusal":
            return None, cost  # graded as a failure, which is what a refusal is to the agent's caller
        return "".join(b.text for b in response.content if b.type == "text"), cost

    return run


def from_env():
    kind = os.environ.get("ABS_RUNNER", "none")
    if kind == "command":
        cmd = os.environ.get("ABS_RUNNER_CMD")
        if not cmd:
            raise SystemExit("FATAL: ABS_RUNNER=command requires ABS_RUNNER_CMD")
        return _command_runner(cmd, float(os.environ.get("ABS_RUNNER_TIMEOUT", "120")))
    if kind == "claude":
        try:
            return _claude_runner()
        except ImportError:
            raise SystemExit("FATAL: ABS_RUNNER=claude requires `pip install anthropic`")
    if kind != "none":
        raise SystemExit(f"FATAL: unknown ABS_RUNNER={kind!r} (expected command or claude)")
    return None
