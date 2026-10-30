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
# Finished in a way that a fresh email won't change by itself: they refused, or you closed it.
HELD = frozenset({"rejected", "closed"})
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class Selection:
    eligible: list[BrokerDef] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (slug, reason)


@dataclass
class CampaignReport:
    sent: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # already contacted: left alone
    stopped_reason: str | None = None


def sent_today(session: Session, now: datetime, tz=None) -> int:
    """Emails sent in the last 24 hours: a rolling window, like Gmail's own send limit.
    (*tz* is accepted for older callers and no longer matters.)"""
    start = now.astimezone(UTC) - timedelta(hours=24)
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
    resend_after_days: int = 180,
    my_groups: list[str] | None = None,
    force: bool = False,
) -> Selection:
    """Brokers to email now. A broker comes round again resend_after_days after its last
    request, once that one is finished. One that refused, or that you closed, is never
    re-sent by itself: only when you ask for it by name with force=True."""
    from ..audiences import applies_to, load_coverage

    coverage = load_coverage()
    sel = Selection()
    cutoff = now - timedelta(days=resend_after_days)
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
        latest = max(subs, key=lambda s: s.created_at) if subs else None
        explicit = force and only is not None and b.slug in only
        if latest is not None and latest.status in HELD and not explicit:
            sel.skipped.append((b.slug, f"{latest.status}: send again by hand if you want to"))
            continue
        recent = [s for s in subs if s.submitted_at and _aware(s.submitted_at) >= cutoff]
        if recent and not explicit:
            sel.skipped.append((b.slug, f"contacted within {resend_after_days} days"))
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


def already_contacted(session: Session, broker: BrokerDef, now: datetime, days: int = 90) -> bool:
    """A fresh look, right before sending: is there a request to this broker that is in
    flight or was sent recently? (A failed attempt that never left doesn't count.)"""
    cutoff = now - timedelta(days=days)
    for s in session.exec(select(Submission).where(Submission.broker_slug == broker.slug)):
        if s.submitted_at is None:
            continue
        if s.status in ACTIVE or _aware(s.submitted_at) >= cutoff:
            return True
    return False


class SendLock:
    """One batch at a time on this machine, across the dashboard, the command line, and
    the scheduled run. Two overlapping batches each pick 'who hasn't been emailed' before
    either has recorded anything, which is how someone could get two emails."""

    def __init__(self) -> None:
        from ..db import db_path

        self._path = db_path().parent / "send.lock"
        self._fh = None

    def __enter__(self) -> bool:
        import fcntl

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "w")  # noqa: SIM115 (held for the whole batch)
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        return True

    def __exit__(self, *exc) -> None:
        if self._fh is not None:
            self._fh.close()  # closing releases the lock


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
    should_stop: Callable[[], bool] = lambda: False,
) -> CampaignReport:
    report = CampaignReport()
    from ..config import user_tz

    with SendLock() as got:
        if not got:
            report.stopped_reason = "another batch is already sending"
            return report
        recover_interrupted(session, now())
        return _run_locked(
            session,
            config,
            brokers,
            limit,
            runner,
            sleep,
            now,
            on_progress,
            report,
            user_tz,
            should_stop,
        )


def _run_locked(
    session, config, brokers, limit, runner, sleep, now, on_progress, report, user_tz, should_stop
):
    quota = max(0, config.email.daily_limit - sent_today(session, now(), user_tz(config)))
    budget = min(limit, quota)
    if budget == 0:
        report.stopped_reason = f"daily limit of {config.email.daily_limit} already reached"
        return report

    consecutive = 0
    with email_method.batch_session(config):
        for i, broker in enumerate(brokers[:budget]):
            if should_stop():
                report.stopped_reason = "cancelled by you"
                break
            if already_contacted(session, broker, now()):
                report.skipped.append(broker.slug)
                continue
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


def resend_to(
    session: Session,
    config: UserConfig,
    broker: BrokerDef,
    address: str,
    *,
    runner: Callable[..., None] = run_submission,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CampaignReport:
    """Send this broker's request to a different address its own reply asked for.

    Deliberately narrow: one broker, one email, under the same one-batch-at-a-time lock and
    the same 24-hour limit. The earlier request is closed (it was never going to be read),
    and the new one starts its own deadline."""
    from ..config import user_tz
    from ..db import record_event, update_status

    report = CampaignReport()
    with SendLock() as got:
        if not got:
            report.stopped_reason = "another batch is already sending"
            return report
        used = sent_today(session, now(), user_tz(config))
        if used >= config.email.daily_limit:
            report.stopped_reason = f"daily limit of {config.email.daily_limit} already reached"
            return report
        old = list(session.exec(select(Submission).where(Submission.broker_slug == broker.slug)))
        target = broker.model_copy(update={"opt_out_email": address, "method": "email"})
        sub = create_submission(
            session,
            broker_slug=broker.slug,
            method_used="email",
            legal_basis_cited=broker.legal_basis,
            statutory_response_days=broker.statutory_response_days,
        )
        with email_method.batch_session(config):
            runner(session, sub, target, config.profile, config)
        if sub.status == "awaiting_broker_confirmation":
            report.sent.append(broker.slug)
            # Remember the address, so a later batch doesn't go back to the old one.
            from ..brokers.contacts import save_local

            try:
                save_local(broker.slug, address, "Their own reply named this address.")
            except ValueError:
                pass
            # It worked: a broker switched off because its old address bounced is back on.
            from ..brokers.registry import load_overrides, set_enabled

            if broker.slug in load_overrides()["disabled"]:
                set_enabled(broker.slug, True)
            for prev in old:
                if prev.id != sub.id and prev.status in ACTIVE | {"awaiting_user_input"}:
                    update_status(session, prev, "closed")
                    record_event(session, prev, "redirected", {"to_domain": address.split("@")[1]})
        else:
            report.failed.append((broker.slug, sub.failure_reason or sub.status))
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


def next_resend(subs: list[Submission], resend_after_days: int) -> datetime | None:
    """When this broker comes round for another email, or None (in progress, held, never sent)."""
    if not subs or any(s.status in ACTIVE for s in subs):
        return None
    latest = max(subs, key=lambda s: s.created_at)
    if latest.status in HELD:
        return None
    sent = [_aware(s.submitted_at) for s in subs if s.submitted_at]
    return max(sent) + timedelta(days=resend_after_days) if sent else None


INTERRUPTED_AFTER = timedelta(minutes=10)


def recover_interrupted(session: Session, now: datetime) -> int:
    """A request still 'queued' long after its batch means the app stopped mid-send (closed,
    crashed). Left alone it would block that broker for good as "in progress". It may or may
    not have gone out, so it becomes a failed attempt you can check and resend."""
    stale = session.exec(select(Submission).where(Submission.status == "queued")).all()
    n = 0
    for sub in stale:
        if _aware(sub.created_at) <= now - INTERRUPTED_AFTER:
            from ..db import update_status

            update_status(
                session,
                sub,
                "failed",
                failure_reason="Interrupted while sending: it may or may not have gone out. "
                "Check your Sent mail before resending.",
            )
            n += 1
    return n
