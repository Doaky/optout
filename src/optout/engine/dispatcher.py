"""Routes a queued Submission to the correct method handler and records the result."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from ..brokers.schema import BrokerDef
from ..config import UserConfig
from ..db import Submission, record_event, record_health, update_status
from . import SubmissionFailed, UserInputRequired

# Import handlers with aliases to avoid shadowing stdlib 'email' module
from .methods import email as email_method
from .methods import manual as manual_method
from .methods import postal as postal_method
from .methods import web_form as web_form_method

_HANDLERS = {
    "web_form": web_form_method.run,
    "email": email_method.run,
    "postal": postal_method.run,
    "manual": manual_method.run,
}


def run_submission(
    session: Session,
    submission: Submission,
    broker: BrokerDef,
    profile: object,
    config: UserConfig,
) -> None:
    """
    Execute the appropriate method handler for *submission* and persist the result.

    On success  → status set to "awaiting_broker_confirmation", event recorded.
    On UserInputRequired → status set to "awaiting_user_input".
    On SubmissionFailed  → status set to "failed".
    """
    handler = _HANDLERS.get(broker.method)
    if handler is None:
        update_status(
            session,
            submission,
            "failed",
            failure_reason=f"No handler registered for method '{broker.method}'.",
        )
        return

    try:
        result = handler(broker, profile, submission, _config=config)
    except UserInputRequired as exc:
        update_status(session, submission, "awaiting_user_input", failure_reason=exc.reason)
        return
    except SubmissionFailed as exc:
        update_status(session, submission, "failed", failure_reason=str(exc))
        # Email failures (bounces, SMTP trouble) say nothing about a broker's web flow.
        if broker.method == "web_form":
            record_health(session, broker.slug, "submission", False, str(exc)[:200])
        return

    # Success: persist submission details and advance status
    now = datetime.now(UTC)
    submission.submitted_at = now
    submission.deadline_at = now + timedelta(days=broker.statutory_response_days)
    submission.payload_json = json.dumps(result.payload)
    if result.artifact_path:
        submission.artifact_path = result.artifact_path
    session.add(submission)
    session.commit()

    record_event(session, submission, "submitted", result.payload)
    update_status(session, submission, "awaiting_broker_confirmation")
    if broker.method == "web_form":
        record_health(session, broker.slug, "submission", True)
