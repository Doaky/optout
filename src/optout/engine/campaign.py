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
from datetime import UTC, date, datetime, timedelta

from sqlmodel import Session, select

from ..brokers.schema import BrokerDef
from ..config import UserConfig
from ..db import Submission, create_submission, record_event
from .dispatcher import run_submission
from .methods import email as email_method

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


def sent_today(session: Session, now: datetime, tz=None) -> int:
    """Emails sent since midnight in the user's zone (UTC if none given)."""
    local = now.astimezone(tz or UTC)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
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
    my_groups: list[str] | None = None,
) -> Selection:
    from ..audiences import applies_to, load_coverage

    coverage = load_coverage()
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
        if not applies_to(coverage.get(b.slug), my_groups):
            sel.skipped.append((b.slug, f"only covers {coverage[b.slug].label()}"))
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


def _retry_or_new(session: Session, broker: BrokerDef) -> Submission:
    """Reuse a never-sent failed attempt for this broker, so a retry doesn't add a row."""
    stmt = select(Submission).where(
        Submission.broker_slug == broker.slug,
        Submission.status == "failed",
        Submission.submitted_at == None,  # noqa: E711 (SQL IS NULL)
    )
    failed = session.exec(stmt).first()
    if failed is None:
        return create_submission(
            session,
            broker_slug=broker.slug,
            method_used=broker.method,
            legal_basis_cited=broker.legal_basis,
            statutory_response_days=broker.statutory_response_days,
        )
    failed.status = "queued"
    failed.failure_reason = None
    session.add(failed)
    session.commit()
    record_event(session, failed, "retried")
    return failed


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
    from ..config import user_tz

    quota = max(0, config.email.daily_limit - sent_today(session, now(), user_tz(config)))
    budget = min(limit, quota)
    if budget == 0:
        report.stopped_reason = f"daily limit of {config.email.daily_limit} already reached"
        return report

    consecutive = 0
    with email_method.batch_session(config):
        for i, broker in enumerate(brokers[:budget]):
            if i:
                sleep(config.email.send_delay_seconds)
            sub = _retry_or_new(session, broker)
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


# --- automatic daily sending ------------------------------------------------


def auto_send_due(now_local: datetime, send_time: str, last_run: date | None) -> bool:
    """True once per local day, from the configured time onward (catches up if optout
    starts later that day)."""
    hh, mm = (int(x) for x in send_time.split(":"))
    if last_run == now_local.date():
        return False
    return (now_local.hour, now_local.minute) >= (hh, mm)


def _schedule_path():
    from ..db import db_path

    return db_path().parent / "schedule-state.json"


def last_auto_run() -> date | None:
    import json

    try:
        raw = json.loads(_schedule_path().read_text()).get("last_auto_run")
        return date.fromisoformat(raw) if raw else None
    except (OSError, ValueError):
        return None


def mark_auto_run(day: date) -> None:
    import json

    path = _schedule_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_auto_run": day.isoformat()}))
