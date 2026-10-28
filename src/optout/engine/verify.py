"""Verifying re-search: the only thing that can move a request to 'removed'.

A request being sent, or a broker saying it complied, is not proof. We look
again, and only a page that demonstrably rendered without the user's listing
counts. Anything ambiguous (block page, timeout, changed layout, no search
config) is 'unverifiable', never 'clear'.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session

from ..brokers.schema import BrokerDef
from ..config import Profile
from ..db import (
    Submission,
    latest_verification,
    mark_removed,
    record_health,
    record_verification,
)
from ..scan.scanner import _run_scan_on_page
from .probe import blocked_reason

VERIFY_AFTER_DAYS = 7  # first check this long after sending
RECHECK_DAYS = 7  # retry cadence for still_listed / unverifiable
VERIFIABLE_STATUSES = frozenset({"submitted", "awaiting_broker_confirmation", "confirmed"})


@dataclass
class VerifyResult:
    outcome: str  # verified_clear | still_listed | unverifiable
    detail: str = ""
    blocked: bool = False


def verify_listing(page: Any, broker: BrokerDef, profile: Profile) -> VerifyResult:
    """Re-search *broker* on an open page and classify what we saw."""
    if not broker.scan:
        return VerifyResult("unverifiable", "broker has no search to re-check")

    scan = _run_scan_on_page(page, broker, profile)

    try:
        title = page.title()
    except Exception:
        title = None
    if reason := blocked_reason(title):
        return VerifyResult("unverifiable", f"challenge or block page ({reason})", blocked=True)

    if scan.result == "found":
        return VerifyResult("still_listed", f"{len(scan.matches)} matching result(s)")
    if scan.result == "clean":
        return VerifyResult("verified_clear", "search rendered with no matching listing")
    return VerifyResult("unverifiable", scan.error or "search was inconclusive")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_due(session: Session, sub: Submission, now: datetime) -> bool:
    if sub.status not in VERIFIABLE_STATUSES or sub.submitted_at is None:
        return False
    if now < _aware(sub.submitted_at) + timedelta(days=VERIFY_AFTER_DAYS):
        return False
    last = latest_verification(session, sub.id)
    return last is None or now >= _aware(last.checked_at) + timedelta(days=RECHECK_DAYS)


@dataclass
class VerifyReport:
    removed: list[str] = field(default_factory=list)
    still_listed: list[str] = field(default_factory=list)
    unverifiable: list[tuple[str, str]] = field(default_factory=list)
    not_due: int = 0


def run_verification(
    session: Session,
    subs: list[Submission],
    get_broker: Callable[[str], BrokerDef],
    profile: Profile,
    page_factory: Callable[[BrokerDef], Any],
    *,
    now: datetime | None = None,
    force: bool = False,
) -> VerifyReport:
    now = now or datetime.now(UTC)
    report = VerifyReport()
    for sub in subs:
        if not force and not is_due(session, sub, now):
            report.not_due += 1
            continue
        if sub.status not in VERIFIABLE_STATUSES:
            continue
        try:
            broker = get_broker(sub.broker_slug)
        except KeyError:
            report.unverifiable.append((sub.broker_slug, "broker definition missing"))
            continue
        if not broker.scan:
            # Nothing to re-search; leave as reply-based and don't spam the log.
            report.unverifiable.append((broker.slug, "no search to re-check"))
            continue

        result = verify_listing(page_factory(broker), broker, profile)
        check = record_verification(session, sub, result.outcome, result.detail)
        if not result.blocked:
            record_health(
                session, broker.slug, "verify", result.outcome != "unverifiable", result.detail
            )

        if result.outcome == "verified_clear":
            mark_removed(session, sub, check)
            report.removed.append(broker.slug)
        elif result.outcome == "still_listed":
            report.still_listed.append(broker.slug)
        else:
            report.unverifiable.append((broker.slug, result.detail))
    return report
