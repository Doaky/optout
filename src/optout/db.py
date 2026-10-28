"""SQLite schema, engine setup, and session helpers."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlmodel import Field, Session, SQLModel, create_engine, select

# ---------------------------------------------------------------------------
# Status / event constants (Literals for type-checking, plain str in DB)
# ---------------------------------------------------------------------------

SubmissionStatus = Literal[
    "queued",
    "awaiting_user_input",
    "submitted",
    "awaiting_broker_confirmation",
    "confirmed",  # broker said (in writing) that it deleted; NOT verified by us
    "removed",  # only ever set by mark_removed(), after a verifying re-search
    "rejected",
    "expired",
    "failed",
]

EventType = Literal[
    "created",
    "submitted",
    "broker_acknowledged",
    "confirmation_received",
    "rejection_received",
    "deadline_passed",
    "escalation_sent",
    "removed_verified",
    "re_added_detected",
    "verification_checked",
]

VerificationOutcome = Literal["verified_clear", "still_listed", "unverifiable"]

ScanResult = Literal["clean", "found", "inconclusive"]


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Table models
# ---------------------------------------------------------------------------


class Submission(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    broker_slug: str = Field(index=True)
    status: str = Field(index=True)  # SubmissionStatus
    submitted_at: datetime | None = None
    confirmed_at: datetime | None = None
    deadline_at: datetime | None = None  # submitted_at + broker.statutory_response_days
    method_used: str
    payload_json: str = "{}"  # exact fields/bytes sent to broker
    artifact_path: str | None = None  # screenshot, .eml, or PDF path
    broker_response: str | None = None
    broker_reference_id: str | None = None
    failure_reason: str | None = None
    retry_count: int = 0
    next_retry_at: datetime | None = None
    legal_basis_cited: str = ""  # comma-separated, e.g. "CCPA,CPRA"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class SubmissionEvent(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    submission_id: uuid.UUID = Field(foreign_key="submission.id", index=True)
    event_type: str  # EventType
    payload_json: str = "{}"
    created_at: datetime = Field(default_factory=_now)


class MonitoringScan(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    broker_slug: str = Field(index=True)
    scanned_at: datetime = Field(default_factory=_now)
    result: str  # ScanResult
    matches_json: str | None = None
    triggered_resubmission_id: uuid.UUID | None = None


class VerificationCheck(SQLModel, table=True):
    """A re-search of a broker after a request, used to decide whether data is gone."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    submission_id: uuid.UUID = Field(foreign_key="submission.id", index=True)
    broker_slug: str = Field(index=True)
    checked_at: datetime = Field(default_factory=_now)
    outcome: str  # VerificationOutcome
    detail: str = ""  # reason only; never listing text or other people's data


class InboxMessage(SQLModel, table=True):
    """A reply (or bounce) read from the mailbox, and what was decided about it."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    message_id: str = Field(index=True, unique=True)
    submission_id: uuid.UUID | None = Field(default=None, foreign_key="submission.id", index=True)
    broker_slug: str | None = Field(default=None, index=True)
    matched_by: str = ""  # message-id | alias | domain | ""
    from_addr: str = ""
    subject: str = ""
    received_at: datetime | None = None
    text: str = ""  # reply text with quoted history removed, capped; stays local
    kind: str = ""  # deleted | no-data | denied | need-info | auto-reply | bounced | unknown
    rule: str = ""  # which pattern decided it
    state: str = "review"  # applied | review | ignored | resolved
    processed_at: datetime = Field(default_factory=_now)


class BrokerHealth(SQLModel, table=True):
    """One observation of whether a broker's flow still works (for drift detection)."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    broker_slug: str = Field(index=True)
    observed_at: datetime = Field(default_factory=_now)
    source: str  # "submission" | "probe" | "verify"
    ok: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# DB location (XDG_DATA_HOME-compliant)
# ---------------------------------------------------------------------------


def db_path() -> Path:
    env = os.environ.get("XDG_DATA_HOME")
    base = Path(env) if env else Path.home() / ".local" / "share"
    return base / "optout" / "optout.db"


def artifacts_dir() -> Path:
    d = db_path().parent / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_engine(path: Path | None = None):
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{p}", echo=False)


def init_db(engine=None) -> None:
    """Create all tables. Safe to call multiple times (create_all is idempotent)."""
    SQLModel.metadata.create_all(engine or get_engine())


@contextmanager
def get_session(engine=None) -> Generator[Session, None, None]:
    with Session(engine or get_engine(), expire_on_commit=False) as session:
        yield session


# ---------------------------------------------------------------------------
# Helpers used by the engine dispatcher
# ---------------------------------------------------------------------------


def record_event(
    session: Session,
    submission: Submission,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> SubmissionEvent:
    event = SubmissionEvent(
        submission_id=submission.id,
        event_type=event_type,
        payload_json=json.dumps(payload or {}),
    )
    session.add(event)
    session.commit()
    return event


def update_status(
    session: Session,
    submission: Submission,
    status: str,
    *,
    failure_reason: str | None = None,
    **extra: Any,
) -> None:
    if status == "removed":
        raise ValueError("'removed' can only be set by mark_removed() after a verifying re-search")
    submission.status = status
    submission.updated_at = _now()
    if failure_reason is not None:
        submission.failure_reason = failure_reason
    for key, val in extra.items():
        setattr(submission, key, val)
    session.add(submission)
    session.commit()
    session.refresh(submission)


def create_submission(
    session: Session,
    *,
    broker_slug: str,
    method_used: str,
    legal_basis_cited: list[str],
    statutory_response_days: int,
) -> Submission:
    now = _now()
    sub = Submission(
        broker_slug=broker_slug,
        status="queued",
        method_used=method_used,
        legal_basis_cited=",".join(legal_basis_cited),
        created_at=now,
        updated_at=now,
    )
    session.add(sub)
    session.commit()
    session.refresh(sub)

    record_event(session, sub, "created")

    return sub


def mark_submitted(
    session: Session,
    submission: Submission,
    payload: dict[str, Any],
    artifact_path: str | None,
    statutory_response_days: int,
    *,
    submitted_at: datetime | None = None,
) -> None:
    now = submitted_at or _now()
    submission.submitted_at = now
    submission.deadline_at = now + timedelta(days=statutory_response_days)
    submission.payload_json = json.dumps(payload)
    submission.artifact_path = artifact_path
    update_status(session, submission, "submitted")
    record_event(session, submission, "submitted", payload)


def log_outreach(
    session: Session,
    *,
    broker_slug: str,
    method_used: str,
    legal_basis_cited: list[str],
    statutory_response_days: int,
    submitted_at: datetime,
    note: str | None = None,
) -> Submission:
    """Record a request the user made outside the tool (by hand, by email, etc.).

    It starts the same deadline clock as an automated submission. It is never
    treated as removal: that still needs a verifying re-search.
    """
    sub = create_submission(
        session,
        broker_slug=broker_slug,
        method_used=method_used,
        legal_basis_cited=legal_basis_cited,
        statutory_response_days=statutory_response_days,
    )
    payload: dict[str, Any] = {"source": "manual_log"}
    if note:
        payload["note"] = note
    mark_submitted(
        session,
        sub,
        payload,
        None,
        statutory_response_days,
        submitted_at=submitted_at,
    )
    return sub


def list_all_events(
    session: Session, broker_slug: str | None = None
) -> list[tuple[Submission, SubmissionEvent]]:
    """Every event joined to its submission, oldest first."""
    stmt = select(Submission, SubmissionEvent).where(SubmissionEvent.submission_id == Submission.id)
    if broker_slug:
        stmt = stmt.where(Submission.broker_slug == broker_slug)
    stmt = stmt.order_by(SubmissionEvent.created_at)
    return list(session.exec(stmt))


def get_submission(session: Session, submission_id: uuid.UUID) -> Submission | None:
    return session.get(Submission, submission_id)


def list_submissions(
    session: Session,
    broker_slug: str | None = None,
    status: str | list[str] | None = None,
) -> list[Submission]:
    stmt = select(Submission)
    if broker_slug:
        stmt = stmt.where(Submission.broker_slug == broker_slug)
    if status is not None:
        if isinstance(status, list):
            stmt = stmt.where(Submission.status.in_(status))
        else:
            stmt = stmt.where(Submission.status == status)
    return list(session.exec(stmt))


def list_events(session: Session, submission_id: uuid.UUID) -> list[SubmissionEvent]:
    stmt = select(SubmissionEvent).where(SubmissionEvent.submission_id == submission_id)
    return list(session.exec(stmt))


# ---------------------------------------------------------------------------
# Monitoring scan helpers
# ---------------------------------------------------------------------------


def record_scan(
    session: Session,
    broker_slug: str,
    result: str,
    *,
    matches_json: str | None = None,
    triggered_resubmission_id: uuid.UUID | None = None,
) -> MonitoringScan:
    scan = MonitoringScan(
        broker_slug=broker_slug,
        result=result,
        matches_json=matches_json,
        triggered_resubmission_id=triggered_resubmission_id,
    )
    session.add(scan)
    session.commit()
    session.refresh(scan)
    return scan


def get_last_scan(session: Session, broker_slug: str) -> MonitoringScan | None:
    stmt = (
        select(MonitoringScan)
        .where(MonitoringScan.broker_slug == broker_slug)
        .order_by(MonitoringScan.scanned_at.desc())  # type: ignore[arg-type]
        .limit(1)
    )
    return session.exec(stmt).first()


# ---------------------------------------------------------------------------
# Verification + health
# ---------------------------------------------------------------------------


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def record_verification(
    session: Session, submission: Submission, outcome: str, detail: str = ""
) -> VerificationCheck:
    check = VerificationCheck(
        submission_id=submission.id,
        broker_slug=submission.broker_slug,
        outcome=outcome,
        detail=detail,
    )
    session.add(check)
    session.commit()
    session.refresh(check)
    record_event(session, submission, "verification_checked", {"outcome": outcome})
    return check


def mark_removed(session: Session, submission: Submission, check: VerificationCheck) -> None:
    """The only path to status 'removed'.

    Requires a stored check for this submission that found the listing gone and
    that happened after the request was sent. A request being submitted, or even
    acknowledged by the broker, is never enough.
    """
    if check.submission_id != submission.id:
        raise ValueError("verification check belongs to a different submission")
    if check.outcome != "verified_clear":
        raise ValueError(f"cannot mark removed: verification outcome was '{check.outcome}'")
    if submission.submitted_at is None or _aware(check.checked_at) <= _aware(
        submission.submitted_at
    ):
        raise ValueError("cannot mark removed: verification must come after the request was sent")
    submission.status = "removed"
    submission.updated_at = _now()
    session.add(submission)
    session.commit()
    session.refresh(submission)
    record_event(session, submission, "removed_verified", {"check_id": str(check.id)})


def latest_verification(session: Session, submission_id: uuid.UUID) -> VerificationCheck | None:
    stmt = (
        select(VerificationCheck)
        .where(VerificationCheck.submission_id == submission_id)
        .order_by(VerificationCheck.checked_at.desc())  # type: ignore[attr-defined]
    )
    return session.exec(stmt).first()


def record_health(
    session: Session, broker_slug: str, source: str, ok: bool, detail: str = ""
) -> None:
    session.add(BrokerHealth(broker_slug=broker_slug, source=source, ok=ok, detail=detail))
    session.commit()


DRIFT_THRESHOLD = 3


def is_drifted(session: Session, broker_slug: str, threshold: int = DRIFT_THRESHOLD) -> bool:
    """True when the last *threshold* observations for a broker were all failures."""
    stmt = (
        select(BrokerHealth)
        .where(BrokerHealth.broker_slug == broker_slug)
        .order_by(BrokerHealth.observed_at.desc())  # type: ignore[attr-defined]
        .limit(threshold)
    )
    recent = list(session.exec(stmt))
    return len(recent) == threshold and not any(r.ok for r in recent)
