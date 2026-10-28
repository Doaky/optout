"""Follow-up, appeal, and complaint drafts after a request goes unanswered.

Everything here produces *text for the user to send or file themselves*. Nothing
is sent, and nothing is filed with any agency. Statute references were checked
against Va. Code § 59.1-577 (B)(1)-(2) and (C) on 2026-10-28.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..brokers.schema import BrokerDef
from ..config import Profile
from ..db import Submission

AG_COMPLAINT_URL = "https://www.oag.state.va.us/consumer-protection/index.php/file-a-complaint"
APPEAL_RESPONSE_DAYS = 60  # Va. Code § 59.1-577(C)
COMPLAINT_GRACE_DAYS = 15  # wait after a follow-up before suggesting a complaint


@dataclass
class Draft:
    kind: str  # followup | appeal | complaint
    to: str  # an email address, or a URL for the complaint form
    subject: str
    body: str


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _who(profile: Profile) -> str:
    a = profile.current_address
    return f"{profile.legal_name}\n{a.street_line}, {a.city}, {a.state} {a.zip}"


def stage_for(sub: Submission, escalations_sent: list[str], now: datetime) -> str | None:
    """Which draft, if any, is warranted right now."""
    if sub.status == "rejected":
        return "complaint" if "appeal" in escalations_sent else "appeal"
    if sub.status in ("confirmed", "removed", "failed", "queued", "awaiting_user_input"):
        return None
    if sub.deadline_at is None or now <= _aware(sub.deadline_at):
        return None
    if "followup" not in escalations_sent:
        return "followup"
    if "complaint" not in escalations_sent and now > _aware(sub.deadline_at) + timedelta(
        days=COMPLAINT_GRACE_DAYS
    ):
        return "complaint"
    return None


def make_draft(
    kind: str, sub: Submission, broker: BrokerDef, profile: Profile, *, contact_email: str
) -> Draft:
    sent = _aware(sub.submitted_at).strftime("%B %d, %Y") if sub.submitted_at else "(date)"
    deadline = _aware(sub.deadline_at).strftime("%B %d, %Y") if sub.deadline_at else "(date)"
    if kind == "followup":
        body = (
            f"Dear {broker.name} Privacy Team,\n\n"
            f"On {sent} I sent you a request to delete my personal data. Under Va. Code "
            f"§ 59.1-577(B)(1), a controller must respond within 45 days of receipt "
            f"(extendable once by 45 days with notice during the first period). That "
            f"date was {deadline}, and I have not received a response.\n\n"
            "Please confirm in writing whether my data has been deleted. If you need "
            f"anything to verify my identity, reply to {contact_email}.\n\n"
            f"{_who(profile)}\n"
        )
        return Draft(
            kind, broker.opt_out_email or "", f"Follow-up: deletion request of {sent}", body
        )
    if kind == "appeal":
        body = (
            f"Dear {broker.name} Privacy Team,\n\n"
            f"I am appealing your refusal to act on my deletion request of {sent}, as "
            "provided in Va. Code § 59.1-577(C). Please reconsider and delete my "
            "personal data. Under that subsection, you must inform me in writing of "
            f"your decision within {APPEAL_RESPONSE_DAYS} days of receiving this appeal, "
            "with your reasons if you deny it.\n\n"
            f"Reply to {contact_email}.\n\n{_who(profile)}\n"
        )
        return Draft(kind, broker.opt_out_email or "", f"Appeal: deletion request of {sent}", body)
    body = (
        "Draft facts for a complaint to the Virginia Attorney General, Consumer "
        "Protection Section (Virginia Consumer Data Protection Act). Review and edit; "
        "you file it yourself.\n\n"
        f"Business: {broker.name} ({broker.domain})\n"
        f"Contact on file: {broker.opt_out_email or broker.privacy_url or 'unknown'}\n"
        f"My request: deletion of my personal data, sent {sent} by email under "
        "Va. Code § 59.1-577(A)(3).\n"
        f"Response due: {deadline} (§ 59.1-577(B)(1)).\n"
        "Outcome: no response / request refused. Follow-up or appeal sent: see my records.\n\n"
        f"Complainant:\n{_who(profile)}\nContact: {contact_email}\n\n"
        f"File at: {AG_COMPLAINT_URL}\n"
    )
    return Draft("complaint", AG_COMPLAINT_URL, f"VCDPA complaint draft: {broker.name}", body)
