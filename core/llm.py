"""Thin LLM abstraction over whichever free-tier provider is configured.

Checked in priority order: Groq, then Google AI Studio, then OpenRouter.
Free tiers occasionally return malformed JSON in json mode — callers that
need structured output should use complete_json and always handle its
`default` fallback rather than assume valid JSON came back.
"""

import json

import requests

from config import settings

TIMEOUT = 60


class LLMError(Exception):
    pass


def active_provider():
    if settings.GROQ_API_KEY:
        return "groq"
    if settings.GOOGLE_API_KEY:
        return "google"
    if settings.OPENROUTER_API_KEY:
        return "openrouter"
    return None


def _chat_messages(system, user):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    return messages


def _openai_style_complete(url, api_key, model, system, user, json_mode):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": _chat_messages(system, user),
        "temperature": 0.7,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    resp = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _groq_complete(system, user, json_mode):
    return _openai_style_complete(
        "https://api.groq.com/openai/v1/chat/completions",
        settings.GROQ_API_KEY,
        settings.GROQ_MODEL,
        system,
        user,
        json_mode,
    )


def _openrouter_complete(system, user, json_mode):
    return _openai_style_complete(
        "https://openrouter.ai/api/v1/chat/completions",
        settings.OPENROUTER_API_KEY,
        settings.OPENROUTER_MODEL,
        system,
        user,
        json_mode,
    )


def _google_complete(system, user, json_mode):
    # The key goes in a header, never the URL: a URL is what ends up in
    # exception messages, request logs, and any proxy in between, and
    # those messages get written straight into the activity log the
    # dashboard displays.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GOOGLE_MODEL}:generateContent"
    headers = {"x-goog-api-key": settings.GOOGLE_API_KEY}
    prompt = f"{system}\n\n{user}" if system else user
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if json_mode:
        payload["generationConfig"] = {"response_mime_type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def complete(user: str, system: str = None, json_mode: bool = False) -> str:
    provider = active_provider()
    if provider == "groq":
        return _groq_complete(system, user, json_mode)
    if provider == "google":
        return _google_complete(system, user, json_mode)
    if provider == "openrouter":
        return _openrouter_complete(system, user, json_mode)
    raise LLMError(
        "No LLM API key configured. Set GROQ_API_KEY, GOOGLE_API_KEY, or OPENROUTER_API_KEY."
    )


def complete_json(user: str, system: str = None, default=None):
    raw = complete(user, system=system, json_mode=True)
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return default
