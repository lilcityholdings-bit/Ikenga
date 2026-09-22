"""
Escape model output before it becomes a web page.

The bots read pages from the open web, so anything they write may contain text
an attacker planted. Nothing from a model is ever trusted as markup.
"""
import html
import re

_BAD = re.compile(
    r"(?is)<\s*(script|iframe|object|embed|form|link|meta|style)\b.*?>"
    r"|javascript:|data:text/html|on\w+\s*="
)


def strip_dangerous(text: str) -> str:
    return _BAD.sub("", text or "")


def esc(text: str) -> str:
    return html.escape(strip_dangerous(text or ""), quote=True)


def paragraphs(body: str) -> str:
    """Plain text into escaped <p> blocks, with ## lines becoming subheads."""
    out = []
    for line in (body or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            out.append(f"<h2>{esc(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h2>{esc(line[2:])}</h2>")
        elif line.startswith(("- ", "* ")):
            out.append(f"<li>{esc(line[2:])}</li>")
        else:
            out.append(f"<p>{esc(line)}</p>")
    return "\n  ".join(out)
