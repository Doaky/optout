"""Display labels shared by the CLI and the web views."""

from __future__ import annotations

STATUS_LABEL: dict[str, str] = {
    "queued": "Queued",
    "awaiting_user_input": "⚡ Needs input",
    "submitted": "Submitted",
    "awaiting_broker_confirmation": "Submitted, awaiting reply",
    "confirmed": "Broker says done",
    "removed": "Removed ✓ verified",
    "rejected": "Rejected",
    "expired": "Expired",
    "failed": "Failed",
}
