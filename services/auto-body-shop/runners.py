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
"""
import json
import os
import shlex
import subprocess


def _command_runner(cmd, timeout):
    argv = shlex.split(cmd)

    def run(config, input_text):
        proc = subprocess.run(argv, input=json.dumps({"config": config, "input": input_text}).encode(),
                              capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"runner exit {proc.returncode}: {proc.stderr.decode(errors='replace')[-300:]}")
        return proc.stdout.decode()

    return run


def _claude_runner():
    import anthropic  # optional dependency: only needed when ABS_RUNNER=claude

    client = anthropic.Anthropic()

    def run(config, input_text):
        params = config.get("params") or {}
        messages = []
        for ex in config.get("examples") or []:
            messages += [{"role": "user", "content": str(ex["input"])},
                         {"role": "assistant", "content": str(ex["output"])}]
        messages.append({"role": "user", "content": input_text})
        kwargs = {"model": config["model"], "max_tokens": int(params.get("max_tokens", 16000)),
                  "system": config["system_instruction"], "messages": messages}
        if params.get("effort"):
            kwargs["output_config"] = {"effort": params["effort"]}
        if config.get("tools"):
            kwargs["tools"] = config["tools"]
        response = client.messages.create(**kwargs)
        if response.stop_reason == "refusal":
            return None  # graded as a failure, which is what a refusal is to the agent's caller
        return "".join(b.text for b in response.content if b.type == "text")

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
