"""Small shared helper for the trading package.

Exception text from a failed exchange call can carry request details —
worst case, a URL a key was embedded in. Strip any configured secret out
before it lands in the trading activity log the dashboard displays.
"""


def redact_secrets(text: str, secrets: list) -> str:
    if not text:
        return text
    redacted = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
