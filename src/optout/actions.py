"""State-changing actions shared by the CLI and the local web UI."""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from .db import db_path, list_submissions, record_event, update_status

REPLY_OUTCOMES = {
    "deleted": ("confirmed", "confirmation_received"),
    "denied": ("rejected", "rejection_received"),
    "need-info": ("awaiting_user_input", "broker_acknowledged"),
    # An autoresponder is not an answer: record it, leave the status and deadline alone.
    "auto-reply": (None, "broker_acknowledged"),
}
_REPLY_MESSAGES = {
    "deleted": "Recorded as the broker's word. Shown as removed only after a re-search.",
    "denied": "Recorded as refused. Run escalate for an appeal draft.",
    "need-info": "Recorded. They want more information; reply to them directly.",
    "auto-reply": "Recorded as an automated reply. The deadline keeps running.",
}
ESCALATION_KINDS = ("followup", "appeal", "complaint", "clarify")


def record_reply(session: Session, slug: str, outcome: str, note: str | None) -> str:
    """Record a broker's answer on its latest request. Raises ValueError on bad input."""
    if outcome not in REPLY_OUTCOMES:
        raise ValueError("Outcome must be: deleted, denied, need-info, or auto-reply.")
    subs = list_submissions(session, broker_slug=slug)
    if not subs:
        raise ValueError(f"No request on record for '{slug}'.")
    sub = max(subs, key=lambda x: x.created_at)
    status, event = REPLY_OUTCOMES[outcome]
    note = (note or "").strip()[:200]
    if status is not None:
        update_status(
            session, sub, status, failure_reason=note or None if outcome != "deleted" else None
        )
    record_event(session, sub, event, {"source": "user_reported", "note": note})
    return _REPLY_MESSAGES[outcome]


def record_escalation_sent(session: Session, slug: str, kind: str) -> None:
    if kind not in ESCALATION_KINDS:
        raise ValueError("Kind must be followup, appeal, complaint, or clarify.")
    subs = list_submissions(session, broker_slug=slug)
    if not subs:
        raise ValueError(f"No request on record for '{slug}'.")
    latest = max(subs, key=lambda x: x.created_at)
    record_event(session, latest, "escalation_sent", {"kind": kind})


def drafts_dir() -> Path:
    return db_path().parent / "drafts"


def list_drafts() -> list[Path]:
    d = drafts_dir()
    return sorted(d.glob("*.txt")) if d.exists() else []


def _safe_name(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")[:60] or "broker"


def _broker_or_stub(slug: str):
    """The broker definition, or a bare stand-in for a company logged by hand."""
    from .brokers.registry import get_broker
    from .brokers.schema import BrokerDef

    try:
        return get_broker(slug)
    except KeyError:
        return BrokerDef(
            slug=slug,
            name=slug.title(),
            domain=slug,
            category="unlisted",
            method="manual",
            legal_basis=[],
            statutory_response_days=45,
        )


def _write_draft(config, sub, broker, kind: str, now) -> Path:
    import os

    from .engine.escalation import make_draft
    from .engine.methods.email import _contact_email

    draft = make_draft(
        kind,
        sub,
        broker,
        config.profile,
        contact_email=_contact_email(config, broker, config.profile),
    )
    d = drafts_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe_name(broker.slug)}-{kind}-{now:%Y%m%d}.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"To: {draft.to}\nSubject: {draft.subject}\n\n{draft.body}")
    return path


def write_draft_now(config, slug: str, kind: str, now) -> Path:
    """Write a draft for the broker's latest request right now, whatever the deadline."""
    from .db import get_session

    if kind not in ESCALATION_KINDS:
        raise ValueError("Unknown draft type.")
    with get_session() as session:
        subs = list_submissions(session, broker_slug=slug)
        if not subs:
            raise ValueError(f"No request on record for '{slug}'.")
        sub = max(subs, key=lambda x: x.created_at)
    return _write_draft(config, sub, _broker_or_stub(slug), kind, now)


def write_escalation_drafts(config, now) -> list[tuple[str, str, str]]:
    """Write drafts for overdue/refused requests. Returns (kind, broker name, path)."""
    import json

    from .db import get_session, list_events
    from .engine.escalation import stage_for

    written: list[tuple[str, str, str]] = []
    with get_session() as session:
        for sub in list_submissions(session):
            sent_kinds = [
                json.loads(e.payload_json).get("kind", "")
                for e in list_events(session, sub.id)
                if e.event_type == "escalation_sent"
            ]
            kind = stage_for(sub, sent_kinds, now)
            if not kind:
                continue
            broker = _broker_or_stub(sub.broker_slug)
            path = _write_draft(config, sub, broker, kind, now)
            written.append((kind, broker.name, str(path)))
    return written
