"""Webhook notifications (ntfy-style). Messages carry counts and broker slugs only."""

from __future__ import annotations

import urllib.request
from urllib.parse import urlparse


def send_webhook(url: str, title: str, message: str, *, timeout: float = 10) -> bool:
    """POST *message* to *url*. Returns False on any failure; never raises."""
    if urlparse(url).scheme not in ("http", "https"):
        return False
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={"Title": title.encode("ascii", "replace").decode(), "User-Agent": "optout/0.2"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):  # noqa: S310 (scheme checked above)
            return True
    except OSError:
        return False
