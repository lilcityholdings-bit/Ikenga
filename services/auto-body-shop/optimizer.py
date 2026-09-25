"""Pluggable optimizers: given the live instruction and a batch's metrics, propose a new one.

Selected with ABS_OPTIMIZER:

    none     (default) jobs are recorded as SKIPPED — nothing is staged automatically.
    command  runs ABS_OPTIMIZER_CMD (split shell-style, no shell), writes the job context as JSON
             to its stdin, and takes its stdout as the candidate instruction. Lets you plug in any
             optimizer — an eval harness, a different model, a script — without touching this file.
    claude   asks Claude via the official `anthropic` SDK (pip install anthropic; credentials from
             ANTHROPIC_API_KEY or an `ant auth login` profile). Model: ABS_CLAUDE_MODEL.

Whatever runs, its output is only ever STAGED. See the note at the top of server.py on why
promotion stays a separate, explicit step.
"""
import json
import os
import shlex
import subprocess

DEFAULT_CLAUDE_MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You revise system instructions for production AI agents.

You receive an agent's current system instruction and aggregate metrics from its most recent
batch of live runs: success rate and latency. You do not see the individual inputs or failures,
so do not invent specific failure modes. Make targeted, conservative edits that plausibly improve
reliability (clearer output contract, explicit handling of ambiguous or empty input, fewer
conflicting requirements) or reduce latency (less unnecessary verbosity asked of the model).
Preserve the instruction's task, scope and any required output format exactly.

If the metrics are already healthy and you see no clear improvement, return the current
instruction unchanged. Reply with the revised system instruction only: no preamble, no
explanation, no surrounding quotes or code fences."""


def _command_optimizer(cmd):
    argv = shlex.split(cmd)
    timeout = float(os.environ.get("ABS_OPTIMIZER_TIMEOUT", "300"))

    def optimize(ctx):
        proc = subprocess.run(argv, input=json.dumps(ctx).encode(), capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"exit {proc.returncode}: {proc.stderr.decode(errors='replace')[-500:]}")
        return proc.stdout.decode()

    return optimize


def _claude_optimizer(model):
    import anthropic  # optional dependency: only needed when ABS_OPTIMIZER=claude

    client = anthropic.Anthropic()

    def optimize(ctx):
        user = ("Current system instruction:\n<instruction>\n" + ctx["system_instruction"] +
                "\n</instruction>\n\nMetrics for the last batch of runs against version " +
                ctx["version"] + ":\n" + json.dumps(ctx["batch"], indent=2))
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            output_config={"effort": "high"},
            # Server-side refusal fallback: a declined request is retried on a fallback model
            # within the same call instead of failing the job.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model declined the request")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("response truncated at max_tokens")
        return "".join(b.text for b in response.content if b.type == "text")

    return optimize


def from_env():
    kind = os.environ.get("ABS_OPTIMIZER", "none")
    if kind == "none":
        return None
    if kind == "command":
        cmd = os.environ.get("ABS_OPTIMIZER_CMD")
        if not cmd:
            raise SystemExit("FATAL: ABS_OPTIMIZER=command requires ABS_OPTIMIZER_CMD")
        return _command_optimizer(cmd)
    if kind == "claude":
        try:
            return _claude_optimizer(os.environ.get("ABS_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL))
        except ImportError:
            raise SystemExit("FATAL: ABS_OPTIMIZER=claude requires `pip install anthropic`")
    raise SystemExit(f"FATAL: unknown ABS_OPTIMIZER={kind!r} (expected none, command or claude)")


def describe():
    kind = os.environ.get("ABS_OPTIMIZER", "none")
    if kind == "claude":
        return f"claude ({os.environ.get('ABS_CLAUDE_MODEL', DEFAULT_CLAUDE_MODEL)})"
    return kind
