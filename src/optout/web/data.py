"""Collects what the dashboard shows. Broker names, dates, and statuses only.

Deliberately excludes the user's name, address, email, and phone: the report
file can sit on disk or be shared without leaking identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from urllib.parse import quote, urlsplit

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
    reply_from: list[str] = field(default_factory=list)
    only_covers: str = ""  # e.g. "educators" when the broker serves a specific group


@dataclass
class Attention:
    slug: str
    name: str
    kind: str  # need-info | refused | overdue | drift
    why: str  # short label
    hint: str  # what to do about it
    quote: str = ""  # the sentence from their reply that matters
    email: str = ""  # their latest reply, in full
    link: str = ""  # first web link in their reply (shown, never opened for you)
    link_host: str = ""
    gmail_url: str = ""  # opens this exact email in Gmail (search by Message-ID)
    coverage: str = ""  # who the broker covers, if limited ("educators")
    coverage_note: str = ""
    coverage_groups: list[str] = field(default_factory=list)
    coverage_prompt: str = ""  # redacted prompt for Claude (contributor tools only)


@dataclass
class Dashboard:
    generated: str
    rows: list[Row] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    attention: list[Attention] = field(default_factory=list)
    eligible: int = 0
    skipped: int = 0
    sent_today: int = 0
    daily_limit: int = 0
    drafts: int = 0
    # Daily cumulative progress: (ISO date, sent, broker replied, verified removed)
    series: list[tuple[str, int, int, int]] = field(default_factory=list)
    verifiable: int = 0  # contacted brokers whose site we can re-search ourselves
    contributor_tools: bool = False


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


_TZ: tzinfo = UTC  # set per request by collect()


def _date(dt: datetime | None) -> str:
    return _aware(dt).astimezone(_TZ).strftime("%Y-%m-%d") if dt else "—"


def friendly_time(dt: datetime, tz: tzinfo) -> str:
    """'Oct 28, 8:04 PM EDT'"""
    local = dt.astimezone(tz)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local:%b} {local.day}, {hour}:{local:%M %p} {local.tzname() or ''}".strip()


_DONE = {"confirmed", "removed", "closed"}


def collect(
    session: Session, config: UserConfig | None, now: datetime, drafts: int = 0
) -> Dashboard:
    global _TZ
    from ..config import user_tz

    _TZ = user_tz(config)
    names = {b.slug: b.name for b in all_brokers()}
    dash = Dashboard(
        generated=friendly_time(now, _TZ),
        drafts=drafts,
        contributor_tools=bool(config and config.contributor_tools),
    )
    drift_cache: dict[str, bool] = {}

    newest_first = Submission.created_at.desc()  # type: ignore[attr-defined]
    subs = list(session.exec(select(Submission).order_by(newest_first)))
    from ..engine.inbox import senders_by_broker

    reply_from = senders_by_broker(session)
    from ..audiences import load_coverage

    coverage = load_coverage()
    seen: set[str] = set()
    for sub in subs:  # newest first: keep one row per broker, the latest attempt
        if sub.broker_slug in seen:
            continue
        seen.add(sub.broker_slug)
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
            reply_from=reply_from.get(sub.broker_slug, []),
            only_covers=(
                coverage[sub.broker_slug].label()
                if sub.broker_slug in coverage and coverage[sub.broker_slug].niche
                else ""
            ),
        )
        dash.rows.append(row)
        dash.counts[row.label] = dash.counts.get(row.label, 0) + 1
        if overdue and days is not None:
            dash.attention.append(
                Attention(
                    row.slug,
                    row.name,
                    "overdue",
                    f"Overdue by {-days} day(s)",
                    "No answer within the legal deadline. Send a short follow-up.",
                )
            )
        elif sub.status == "rejected":
            dash.attention.append(
                _with_reply(
                    session,
                    sub,
                    Attention(
                        row.slug,
                        row.name,
                        "refused",
                        "Refused",
                        "You can appeal, or close it if their reason applies to you.",
                    ),
                )
            )
        elif sub.status == "awaiting_user_input":
            dash.attention.append(
                _with_reply(
                    session,
                    sub,
                    Attention(
                        row.slug,
                        row.name,
                        "need-info",
                        "Wants something from you",
                        "Do what their reply asks, then mark it done so the deadline restarts.",
                    ),
                )
            )
        if drift:
            dash.attention.append(
                Attention(
                    row.slug,
                    row.name,
                    "drift",
                    "Their form may have changed",
                    "Automatic web-form submission may fail for this broker.",
                )
            )

    for item in dash.attention:
        cov = coverage.get(item.slug)
        if cov is not None:
            item.coverage, item.coverage_note = cov.label(), cov.note
            item.coverage_groups = list(cov.audiences)
        if config is not None and config.contributor_tools and item.email:
            from ..audiences import coverage_prompt
            from ..engine.assist import redact

            item.coverage_prompt = coverage_prompt(
                item.slug, item.name, redact(item.email, config.profile)
            )

    dash.series = progress_series(session, subs, now)
    scannable = {b.slug for b in all_brokers() if b.scan}
    dash.verifiable = len({s.broker_slug for s in subs if s.submitted_at} & scannable)

    if config is not None:
        sel = select_brokers(all_brokers(), session, now=now, my_groups=config.my_groups)
        dash.eligible, dash.skipped = len(sel.eligible), len(sel.skipped)
        dash.sent_today = sent_today(session, now, _TZ)
        dash.daily_limit = config.email.daily_limit
    return dash


_REPLY_EVENTS = {"confirmation_received", "rejection_received", "broker_acknowledged"}


def progress_series(
    session: Session, subs: list[Submission], now: datetime
) -> list[tuple[str, int, int, int]]:
    """Cumulative counts per day from the first request sent until today."""
    from datetime import timedelta

    sent_days = [_aware(s.submitted_at).astimezone(_TZ).date() for s in subs if s.submitted_at]
    if not sent_days:
        return []
    ids = {s.id for s in subs}
    first_reply: dict = {}
    removed: dict = {}
    for ev in session.exec(select(SubmissionEvent)):
        if ev.submission_id not in ids:
            continue
        day = _aware(ev.created_at).astimezone(_TZ).date()
        if ev.event_type in _REPLY_EVENTS:
            first_reply[ev.submission_id] = min(day, first_reply.get(ev.submission_id, day))
        elif ev.event_type == "removed_verified":
            removed[ev.submission_id] = min(day, removed.get(ev.submission_id, day))

    start, end = min(sent_days), now.astimezone(_TZ).date()
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


_LINK = re.compile(r"https?://[^\s<>\"')\]]+")


def _with_reply(session: Session, sub: Submission, a: Attention) -> Attention:
    """Attach the latest reply: the sentence that matters, the email, its first link."""
    from ..db import InboxMessage
    from ..engine.inbox import deciding_sentence

    # The reply that caused this state, not just the newest one (an acknowledgement can
    # arrive alongside the "please confirm your email" message).
    wanted = {"need-info": ("need-info",), "refused": ("denied",)}.get(a.kind, ())
    rows = session.exec(
        select(InboxMessage)
        .where(InboxMessage.submission_id == sub.id)
        .order_by(InboxMessage.received_at.desc())  # type: ignore[union-attr]
    ).all()
    row = next((r for r in rows if r.kind in wanted), rows[0] if rows else None)
    if row is None:
        return a
    a.email = row.text
    mid = row.message_id.strip().strip("<>")
    if mid:
        a.gmail_url = "https://mail.google.com/mail/u/0/#search/" + quote(f"rfc822msgid:{mid}")
    a.quote = deciding_sentence(row.text, row.kind or "")
    a.link = _best_link(row.text)
    a.link_host = urlsplit(a.link).hostname or "" if a.link else ""
    return a


_FORMISH = re.compile(r"privacy|request|form|portal|dsar|dsr|rights|onetrust|consent|delete", re.I)
_ADS = re.compile(r"aboutads|youradchoices|networkadvertising|optout\.aboutads", re.I)


def _best_link(text: str) -> str:
    """The link most likely to be their privacy form: one that looks like a request form or
    portal, never the industry ad-choices page that many replies also mention."""
    links = [m.group(0).rstrip(".,;:") for m in _LINK.finditer(text)]
    links = [u for u in links if not _ADS.search(u)]
    for u in links:
        if _FORMISH.search(u):
            return u
    return links[0] if links else ""
