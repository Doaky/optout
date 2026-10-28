"""Collects what the dashboard shows. Broker names, dates, and statuses only.

Deliberately excludes the user's name, address, email, and phone: the report
file can sit on disk or be shared without leaking identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlmodel import Session, select

from ..brokers.registry import all_brokers
from ..config import UserConfig
from ..db import Submission, SubmissionEvent, is_drifted, latest_verification
from ..engine.campaign import select_brokers, sent_today
from ..labels import STATUS_LABEL


@dataclass
class Row:
    slug: str
    name: str
    status: str
    label: str
    method: str
    submitted: str
    deadline: str
    days_left: int | None
    overdue: bool
    last_check: str
    drifted: bool


@dataclass
class Dashboard:
    generated: str
    rows: list[Row] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    attention: list[tuple[str, str, str]] = field(default_factory=list)  # (slug, name, why)
    eligible: int = 0
    skipped: int = 0
    sent_today: int = 0
    daily_limit: int = 0
    drafts: int = 0
    # Daily cumulative progress: (ISO date, sent, broker replied, verified removed)
    series: list[tuple[str, int, int, int]] = field(default_factory=list)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


_DONE = {"confirmed", "removed"}


def collect(
    session: Session, config: UserConfig | None, now: datetime, drafts: int = 0
) -> Dashboard:
    names = {b.slug: b.name for b in all_brokers()}
    dash = Dashboard(generated=now.strftime("%Y-%m-%d %H:%M UTC"), drafts=drafts)
    drift_cache: dict[str, bool] = {}

    newest_first = Submission.created_at.desc()  # type: ignore[attr-defined]
    subs = list(session.exec(select(Submission).order_by(newest_first)))
    for sub in subs:
        drift = drift_cache.setdefault(sub.broker_slug, is_drifted(session, sub.broker_slug))
        v = latest_verification(session, sub.id)
        days = None
        overdue = False
        if sub.deadline_at and sub.status not in _DONE | {"failed", "expired", "queued"}:
            days = (_aware(sub.deadline_at) - now).days
            overdue = _aware(sub.deadline_at) < now
        row = Row(
            slug=sub.broker_slug,
            name=names.get(sub.broker_slug, sub.broker_slug),
            status=sub.status,
            label=STATUS_LABEL.get(sub.status, sub.status),
            method=sub.method_used,
            submitted=_date(sub.submitted_at),
            deadline=_date(sub.deadline_at),
            days_left=days,
            overdue=overdue,
            last_check=f"{v.outcome.replace('_', ' ')} {_date(v.checked_at)}" if v else "—",
            drifted=drift,
        )
        dash.rows.append(row)
        dash.counts[row.label] = dash.counts.get(row.label, 0) + 1
        if overdue and days is not None:
            dash.attention.append((row.slug, row.name, f"overdue by {-days} day(s)"))
        elif sub.status == "rejected":
            dash.attention.append((row.slug, row.name, "refused: consider an appeal"))
        elif sub.status == "awaiting_user_input":
            dash.attention.append((row.slug, row.name, "needs your input"))
        if drift:
            dash.attention.append((row.slug, row.name, "form may have changed (drift)"))

    dash.series = progress_series(session, subs, now)

    if config is not None:
        sel = select_brokers(all_brokers(), session, now=now)
        dash.eligible, dash.skipped = len(sel.eligible), len(sel.skipped)
        dash.sent_today = sent_today(session, now)
        dash.daily_limit = config.email.daily_limit
    return dash


_REPLY_EVENTS = {"confirmation_received", "rejection_received", "broker_acknowledged"}


def progress_series(
    session: Session, subs: list[Submission], now: datetime
) -> list[tuple[str, int, int, int]]:
    """Cumulative counts per day from the first request sent until today."""
    from datetime import timedelta

    sent_days = [_aware(s.submitted_at).date() for s in subs if s.submitted_at]
    if not sent_days:
        return []
    ids = {s.id for s in subs}
    first_reply: dict = {}
    removed: dict = {}
    for ev in session.exec(select(SubmissionEvent)):
        if ev.submission_id not in ids:
            continue
        day = _aware(ev.created_at).date()
        if ev.event_type in _REPLY_EVENTS:
            first_reply[ev.submission_id] = min(day, first_reply.get(ev.submission_id, day))
        elif ev.event_type == "removed_verified":
            removed[ev.submission_id] = min(day, removed.get(ev.submission_id, day))

    start, end = min(sent_days), now.date()
    out: list[tuple[str, int, int, int]] = []
    day = start
    while day <= end:
        out.append(
            (
                day.isoformat(),
                sum(1 for d in sent_days if d <= day),
                sum(1 for d in first_reply.values() if d <= day),
                sum(1 for d in removed.values() if d <= day),
            )
        )
        day += timedelta(days=1)
    return out
