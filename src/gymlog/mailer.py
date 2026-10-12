"""Email the weekly insight, through Resend.

Only the scheduled run sends (`gymlog insight --email`, from the home
insight timer). The web app's "generate now" button doesn't: whoever pressed
it is already looking at the result.

A plain `urllib.request` call, for the reason insights.py gives for DeepSeek:
one POST a week doesn't earn an HTTP client dependency.
"""

from __future__ import annotations

import json
import urllib.request

from .model import Insight
from .settings import secret, settings

RESEND_URL = "https://api.resend.com/emails"


def send_insight(insight: Insight) -> None:
    """Email `insight` to INSIGHT_EMAIL_TO. Raises if it can't be sent."""
    cfg = settings()
    if not cfg.insight_email_to or not cfg.insight_email_from:
        raise RuntimeError("--email needs INSIGHT_EMAIL_TO and INSIGHT_EMAIL_FROM set")

    text = insight.summary
    if cfg.public_url:
        text += f"\n\n{cfg.public_url.rstrip('/')}/insights/ai"

    body = json.dumps(
        {
            "from": cfg.insight_email_from,
            "to": [cfg.insight_email_to],
            "subject": f"Training insight, week of {insight.week_of}",
            "text": text,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        RESEND_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret('RESEND-API-KEY')}",
            # Resend sits behind Cloudflare, which rejects urllib's default
            # `Python-urllib/3.x` User-Agent with a 403 before Resend sees it.
            "User-Agent": "gymlog",
            # A rerun of the same insight (systemd retrying, a hand rerun after
            # a timeout that actually delivered) can't send it twice.
            "Idempotency-Key": f"insight-{insight.id}",
        },
    )
    with urllib.request.urlopen(request, timeout=30):
        pass
