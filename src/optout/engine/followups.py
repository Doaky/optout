"""Sending follow-ups in a batch: a short, polite nudge to every broker past its legal deadline
with no answer.

Same rules as the request batch: one batch at a time, within the daily limit, paced, and it
stops on a sign-in problem or repeated failures. Each follow-up goes in the same thread as
the original request (In-Reply-To), so the broker sees the history. Only plain follow-ups are
sent this way; appeals and complaints stay drafts you send yourself.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from ..brokers.schema import BrokerDef
from ..config import UserConfig
from ..db import Submission, SubmissionEvent, list_events, record_event
from .campaign import SendLock, sent_today
from .escalation import make_draft, stage_for
from .methods import email as email_method

MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class FollowupReport:
    sent: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped_reason: str | None = None


def _sent_kinds(session: Session, sub: Submission) -> list[str]:
    return [
        json.loads(e.payload_json or "{}").get("kind", "")
        for e in list_events(session, sub.id)
        if e.event_type == "escalation_sent"
    ]


def due(
    session: Session, brokers: dict[str, BrokerDef], now: datetime
) -> list[tuple[Submission, BrokerDef]]:
    """Emailed requests past their deadline, unanswered, not followed up yet."""
    latest: dict[str, Submission] = {}
    for sub in session.exec(select(Submission)).all():
        cur = latest.get(sub.broker_slug)
        if cur is None or sub.created_at > cur.created_at:
            latest[sub.broker_slug] = sub
    out = []
    for slug, sub in sorted(latest.items()):
        b = brokers.get(slug)
        if b is None or not b.enabled or not b.opt_out_email or sub.method_used != "email":
            continue
        if stage_for(sub, _sent_kinds(session, sub), now) == "followup":
            out.append((sub, b))
    return out


def _followups_last_day(session: Session, now: datetime) -> int:
    since = now - timedelta(hours=24)
    events = session.exec(
        select(SubmissionEvent).where(SubmissionEvent.event_type == "escalation_sent")
    ).all()
    return sum(
        1
        for e in events
        if json.loads(e.payload_json or "{}").get("auto")
        and (e.created_at if e.created_at.tzinfo else e.created_at.replace(tzinfo=UTC)) >= since
    )


def build(config: UserConfig, sub: Submission, broker: BrokerDef):
    """The follow-up email for one request, threaded onto the original."""
    contact = email_method._contact_email(config, broker, config.profile)
    draft = make_draft("followup", sub, broker, config.profile, contact_email=contact)
    smtp = config.email.smtp
    msg = email_method._build_message(
        from_addr=config.email.from_address or (smtp.username if smtp else ""),
        reply_to=contact if config.email.alias_pattern else None,
        to_addr=draft.to,
        subject=draft.subject,
        body=draft.body,
        submission_id=sub.id,
    )
    original = json.loads(sub.payload_json or "{}").get("message_id")
    if original:
        msg["In-Reply-To"] = original
        msg["References"] = original
    return msg


def send(
    session: Session,
    config: UserConfig,
    items: list[tuple[Submission, BrokerDef]],
    *,
    limit: int,
    deliver: Callable | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_progress: Callable[[str, str], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> FollowupReport:
    report = FollowupReport()
    smtp = config.email.smtp
    if smtp is None:
        report.stopped_reason = "no email account set up"
        return report
    deliver = deliver or (lambda msg: email_method._send(smtp, msg))
    with SendLock() as got:
        if not got:
            report.stopped_reason = "another batch is already sending"
            return report
        used = sent_today(session, now()) + _followups_last_day(session, now())
        budget = min(limit, max(0, config.email.daily_limit - used))
        if budget == 0:
            report.stopped_reason = f"daily limit of {config.email.daily_limit} already reached"
            return report
        consecutive = 0
        with email_method.batch_session(config) as batch:
            for i, (sub, broker) in enumerate(items[:budget]):
                if should_stop():
                    report.stopped_reason = "cancelled by you"
                    break
                if "followup" in _sent_kinds(session, sub):  # sent by another run meanwhile
                    continue
                if i:
                    sleep(config.email.send_delay_seconds)
                msg = build(config, sub, broker)
                try:
                    deliver(msg)
                except Exception as exc:  # noqa: BLE001 (report it, keep the batch going)
                    reason = type(exc).__name__
                    report.failed.append((broker.slug, reason))
                    if on_progress:
                        on_progress(broker.slug, "failed")
                    if "Auth" in reason or "UserInputRequired" in reason:
                        report.stopped_reason = f"couldn't sign in to send ({reason})"
                        break
                    consecutive += 1
                    if consecutive >= MAX_CONSECUTIVE_FAILURES:
                        report.stopped_reason = f"{consecutive} failures in a row; last: {reason}"
                        break
                    continue
                consecutive = 0
                batch.message_ids.append(msg["Message-ID"])
                record_event(
                    session,
                    sub,
                    "escalation_sent",
                    {"kind": "followup", "auto": True, "message_id": msg["Message-ID"]},
                )
                report.sent.append(broker.slug)
                if on_progress:
                    on_progress(broker.slug, "sent")
    return report
