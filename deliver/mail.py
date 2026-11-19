"""The full digest, by email.

The issue cannot carry a busy morning. GitHub refuses a body over 65,536 characters, so
`digest._fit` withholds the least urgent rows. From 2026-11-11 to 11-18 the issue body
ended within 260 characters of that ceiling on five mornings of eight. GitHub's
new-issue notification also arrived as a pointer to the issue rather than the list. So
one render now goes out twice. The issue is unchanged, because it is the delivery log
that `issue.already_sent` reads for the one-a-day cap. This module emails the untrimmed
body as HTML.

Resend rather than SMTP because it is one HTTPS call through the client this stage
already uses, and the sending domain is verified by DNS records rather than by a mailbox
password. Every setting comes from the environment and none is committed: the repo is
public, and the two addresses are personal.

Unconfigured is a normal state, not an error. A fork, a laptop and a test all run
without these variables, and the issue alone still delivers -- exactly as before this
module existed.
"""
from __future__ import annotations

import html
import os
import re

import httpx
import markdown

from core import paths

RESEND_URL = "https://api.resend.com/emails"
ENV_KEY = "RESEND_API_KEY"
ENV_FROM = "DIGEST_EMAIL_FROM"
ENV_TO = "DIGEST_EMAIL_TO"

# Inline styles rather than a <style> block: Outlook, which is what a university
# Microsoft 365 inbox renders with, ignores most of a stylesheet but keeps attributes.
_FONT = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, "
         "sans-serif")
_STYLES = {
    "table": "border-collapse: collapse; width: 100%; margin: 8px 0 16px;",
    "th": ("border: 1px solid #d0d7de; padding: 6px 8px; text-align: left; "
           "background: #f6f8fa; vertical-align: top;"),
    "td": "border: 1px solid #d0d7de; padding: 6px 8px; vertical-align: top;",
    "h2": "font-size: 16px; margin: 20px 0 6px; border-bottom: 1px solid #d0d7de; "
          "padding-bottom: 4px;",
    "blockquote": "margin: 8px 0; padding: 4px 12px; border-left: 3px solid #d0d7de; "
                  "color: #57606a;",
}

# GitHub collapses RULED OUT into a <details> block. Most mail clients cannot open one,
# and markdown inside it would not be converted, so the email shows it as a heading.
_DETAILS_OPEN = re.compile(r"^<details><summary>(.*)</summary>\s*$", re.MULTILINE)
_DETAILS_CLOSE = re.compile(r"^</details>\s*$", re.MULTILINE)
_TAG = re.compile(r"<(table|th|td|h2|blockquote)(\s[^>]*)?>")
# The digest's own markup is three tags. Any other "<" came from posting or page text,
# which GitHub sanitises and a mail client does not, so it is escaped before conversion:
# a title reading "Quant <Intern>" must print, not open an element and swallow the row.
_FOREIGN_TAG = re.compile(r"<(?!br>|/?details>|/?summary>)")


def _style(match: re.Match) -> str:
    tag, attrs = match.group(1), match.group(2) or ""
    # The tables extension writes `style="text-align: ..."` on aligned columns; ours are
    # not aligned, but keep any attributes it does write rather than drop them.
    return f'<{tag}{attrs} style="{_STYLES[tag]}">'


def to_html(title: str, body: str) -> str:
    """The digest's markdown as a self-contained HTML email. Pure: no I/O."""
    text = _FOREIGN_TAG.sub("&lt;", body)
    text = _DETAILS_OPEN.sub(r"## \1", text)
    text = _DETAILS_CLOSE.sub("", text)
    converted = markdown.markdown(text, extensions=["tables"])
    converted = _TAG.sub(_style, converted)
    return (
        "<!doctype html>\n"
        '<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title></head>\n"
        f'<body style="margin: 0; padding: 16px; background: #ffffff; color: #1f2328; '
        f'font-family: {_FONT}; font-size: 14px; line-height: 1.45;">\n'
        f'<div style="max-width: 1100px; margin: 0 auto;">\n{converted}\n</div>\n'
        "</body></html>\n"
    )


def configured() -> list[str]:
    """The settings that are missing; empty when the email can be sent."""
    return [name for name in (ENV_KEY, ENV_FROM, ENV_TO) if not os.environ.get(name)]


def send(subject: str, html_body: str, text_body: str) -> str:
    """Send one email. Returns a human-readable description of what happened.

    Unconfigured returns a message and sends nothing. A refusal raises, so the caller
    can report it through the issue, which is the channel that still works.
    """
    missing = configured()
    if missing:
        return f"email not sent: not configured ({', '.join(missing)} unset)"
    recipients = [a.strip() for a in os.environ[ENV_TO].split(",") if a.strip()]
    response = httpx.post(
        RESEND_URL,
        headers={
            "Authorization": f"Bearer {os.environ[ENV_KEY]}",
            "User-Agent": paths.USER_AGENT,
        },
        json={
            "from": os.environ[ENV_FROM],
            "to": recipients,
            "subject": subject,
            "html": html_body,
            "text": text_body,
        },
        timeout=paths.HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code >= 300:
        raise RuntimeError(
            f"could not send email: HTTP {response.status_code} {response.text[:300]}"
        )
    # Says how many recipients and the size, never the addresses: this line lands in a
    # public Actions log.
    return (f"emailed the full digest to {len(recipients)} recipient(s), "
            f"{len(html_body):,} characters of HTML")
