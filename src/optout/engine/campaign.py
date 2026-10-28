"""Bulk email campaign: pick eligible email brokers and send within safe limits.

Safety properties:
- never contacts a broker that is disabled, non-email, or already contacted recently
- respects a per-day cap (counted from the database, so restarts can't bypass it)
- pauses between sends and stops on repeated failures or an SMTP auth error
- every send goes through the normal dispatcher, so state and audit events match
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from ..brokers.schema import BrokerDef
from ..config import UserConfig
from ..db import Submission, create_submission
from .dispatcher import run_submission

ACTIVE = frozenset({"queued", "awaiting_user_input", "submitted", "awaiting_broker_confirmation"})
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class Selection:
    eligible: list[BrokerDef] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (slug, reason)


@dataclass
class CampaignReport:
    sent: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped_reason: str | None = None


def sent_today(session: Session, now: datetime) -> int:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = select(Submission).where(
        Submission.method_used == "email", Submission.submitted_at >= start
    )
    return len(list(session.exec(stmt)))


def select_brokers(
    brokers: list[BrokerDef],
    session: Session,
    *,
    now: datetime,
    only: list[str] | None = None,
    recontact_after_days: int = 90,
) -> Selection:
    sel = Selection()
    cutoff = now - timedelta(days=recontact_after_days)
    for b in brokers:
        if only and b.slug not in only:
            continue
        if b.method != "email" or not b.opt_out_email:
            continue
        if not b.enabled:
            sel.skipped.append((b.slug, "disabled"))
            continue
        subs = list(session.exec(select(Submission).where(Submission.broker_slug == b.slug)))
        if any(s.status in ACTIVE for s in subs):
            sel.skipped.append((b.slug, "request already in progress"))
            continue
        recent = [s for s in subs if s.submitted_at and _aware(s.submitted_at) >= cutoff]
        if recent:
            sel.skipped.append((b.slug, f"contacted within {recontact_after_days} days"))
            continue
        sel.eligible.append(b)
    return sel


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def run_campaign(
    session: Session,
    config: UserConfig,
    brokers: list[BrokerDef],
    *,
    limit: int,
    runner: Callable[..., None] = run_submission,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_progress: Callable[[str, str], None] | None = None,
) -> CampaignReport:
    report = CampaignReport()
    quota = max(0, config.email.daily_limit - sent_today(session, now()))
    budget = min(limit, quota)
    if budget == 0:
        report.stopped_reason = f"daily limit of {config.email.daily_limit} already reached"
        return report

    consecutive = 0
    for i, broker in enumerate(brokers[:budget]):
        if i:
            sleep(config.email.send_delay_seconds)
        sub = create_submission(
            session,
            broker_slug=broker.slug,
            method_used=broker.method,
            legal_basis_cited=broker.legal_basis,
            statutory_response_days=broker.statutory_response_days,
        )
        runner(session, sub, broker, config.profile, config)
        if sub.status == "awaiting_broker_confirmation":
            report.sent.append(broker.slug)
            consecutive = 0
            if on_progress:
                on_progress(broker.slug, "sent")
            continue
        reason = sub.failure_reason or sub.status
        report.failed.append((broker.slug, reason))
        if on_progress:
            on_progress(broker.slug, "failed")
        if sub.status == "awaiting_user_input":  # SMTP auth problem: every send would fail
            report.stopped_reason = reason
            break
        consecutive += 1
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            report.stopped_reason = f"{consecutive} failures in a row; last: {reason}"
            break
    return report
