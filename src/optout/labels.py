"""Display labels shared by the CLI and the web views."""

from __future__ import annotations

STATUS_LABEL: dict[str, str] = {
    "queued": "Queued",
    "awaiting_user_input": "⚡ Needs you",
    "submitted": "Submitted",
    "awaiting_broker_confirmation": "Awaiting reply",
    "confirmed": "Broker says done",
    "removed": "Removed ✓ verified",
    "rejected": "Broker said no",
    "expired": "Expired",
    "failed": "Failed to send",
    "closed": "Closed",
}
