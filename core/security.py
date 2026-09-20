"""Security helpers shared by every module that touches the outside world.

The bot reads attacker-influenceable web pages and an LLM decides actions
from them, so everything that crosses that boundary goes through here:
HTML sanitization before anything reaches the published site, SSRF checks
before any URL is fetched, secret redaction before anything hits logs or
bot memory, and slug sanitization so generated content can never write
outside its own article folder.
"""

import ipaddress
import re
import socket
from urllib.parse import urlparse

import bleach

BLOCKED_HOSTNAMES = {"metadata.google.internal"}

ALLOWED_TAGS = [
    "p", "br", "strong", "em", "b", "i", "u",
    "h1", "h2", "h3", "h4",
    "ul", "ol", "li", "a", "blockquote", "code", "pre",
    "table", "thead", "tbody", "tr", "th", "td",
]
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "rel"],
}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def sanitize_html(html: str) -> str:
    """Strip anything that isn't plain content markup — no script, style,
    iframe, event handlers, or javascript: URLs survive this."""
    return bleach.clean(
        html or "",
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )


def _is_blocked_ip(ip_str: str) -> bool:
    if ip_str == "169.254.169.254":
        # The cloud metadata endpoint on Railway/Render/AWS/GCP — returns
        # host credentials if a bot is ever tricked into fetching it.
        return True
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_safe_url(url: str) -> bool:
    """Refuse anything that isn't a plain public http(s) URL."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    if host.lower() in BLOCKED_HOSTNAMES:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    return not any(_is_blocked_ip(info[4][0]) for info in infos)


def redact_secrets(text: str, secrets) -> str:
    """Replace any configured secret value that shows up verbatim in text
    before it reaches logs, bot memory, or the dashboard."""
    if not text:
        return text
    redacted = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_DASH_RUN_RE = re.compile(r"-{2,}")


def safe_slug(text: str) -> str:
    """Sanitize into a filename-safe slug of only [a-z0-9-]. This is the
    entire path-traversal defense for published content: because a slug can
    never contain '/', '..', or any other path-control sequence, generated
    content can't write outside its own article folder or into things like
    a CI workflow file."""
    slug = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    slug = _DASH_RUN_RE.sub("-", slug)
    return slug or "untitled"
