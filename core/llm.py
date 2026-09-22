"""
Flexible LLM helper with provider failover.

Changes from the old version:
  1. Pooled HTTP session instead of a new connection per call.
  2. Remembers which providers just failed and skips them for 5 minutes,
     instead of burning 15 seconds on a dead key every single cycle.
  3. Key presence is read once, not on every call.
"""
import os
import time
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

TIMEOUT = 20
COOLDOWN = 300  # seconds to skip a provider after it fails

_session = requests.Session()
_cooldowns = {}


def _key(name: str) -> str:
    try:
        from core.db import get_secret
        return get_secret(name)
    except Exception:
        return os.getenv(name, "").strip()


def _skip(provider: str) -> bool:
    until = _cooldowns.get(provider, 0)
    return time.time() < until


def _fail(provider: str):
    _cooldowns[provider] = time.time() + COOLDOWN


def _ok(provider: str):
    _cooldowns.pop(provider, None)


def _try_openai_style(provider, url, key, model, messages, max_tokens):
    r = _session.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages,
              "max_tokens": max_tokens, "temperature": 0.7},
        timeout=TIMEOUT,
    )
    if r.status_code == 200:
        _ok(provider)
        return r.json()["choices"][0]["message"]["content"].strip()
    # 429 and 5xx mean "come back later"; 401 means the key is wrong.
    _fail(provider)
    return None


def call_llm(prompt: str, system: str = "", max_tokens: int = 800) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    providers = [
        ("groq", "GROQ_API_KEY",
         "https://api.groq.com/openai/v1/chat/completions",
         "llama-3.3-70b-versatile"),
        ("openrouter", "OPENROUTER_API_KEY",
         "https://openrouter.ai/api/v1/chat/completions",
         "meta-llama/llama-3.1-8b-instruct:free"),
        ("together", "TOGETHER_API_KEY",
         "https://api.together.xyz/v1/chat/completions",
         "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"),
    ]

    for name, env, url, model in providers:
        key = _key(env)
        if not key or _skip(name):
            continue
        try:
            out = _try_openai_style(name, url, key, model, messages, max_tokens)
            if out:
                return out
        except Exception:
            _fail(name)

    gemini = _key("GEMINI_API_KEY")
    if gemini and not _skip("gemini"):
        try:
            full = f"{system}\n\n{prompt}" if system else prompt
            r = _session.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-1.5-flash:generateContent?key={gemini}",
                json={"contents": [{"parts": [{"text": full}]}],
                      "generationConfig": {"maxOutputTokens": max_tokens}},
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                _ok("gemini")
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            _fail("gemini")
        except Exception:
            _fail("gemini")

    return (
        "[No LLM key connected yet] Add a free key (GROQ_API_KEY, GEMINI_API_KEY, "
        "OPENROUTER_API_KEY, or TOGETHER_API_KEY) so the bots can think with real models."
    )


def is_llm_available() -> bool:
    return any(_key(k) for k in
               ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "TOGETHER_API_KEY"))


def provider_status() -> dict:
    """For the dashboard health panel."""
    out = {}
    for name, env in (("groq", "GROQ_API_KEY"), ("openrouter", "OPENROUTER_API_KEY"),
                      ("gemini", "GEMINI_API_KEY"), ("together", "TOGETHER_API_KEY")):
        if not _key(env):
            out[name] = "no key"
        elif _skip(name):
            out[name] = f"cooling down {int(_cooldowns[name] - time.time())}s"
        else:
            out[name] = "ready"
    return out
