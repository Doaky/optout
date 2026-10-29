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
    if sub.status == "closed":
        return None
    if sub.status == "rejected":
        return "complaint" if "appeal" in escalations_sent else "appeal"
    if sub.status in ("confirmed", "removed", "failed", "queued", "awaiting_user_input", "closed"):
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


def _is_virginia(profile: Profile) -> bool:
    return profile.current_address.state.strip().upper() == "VA"


def make_draft(
    kind: str, sub: Submission, broker: BrokerDef, profile: Profile, *, contact_email: str
) -> Draft:
    """Draft text. Statute citations only for Virginia, where they were checked; elsewhere
    the wording stays general rather than citing a law that may not apply."""
    sent = _aware(sub.submitted_at).strftime("%B %d, %Y") if sub.submitted_at else "(date)"
    deadline = _aware(sub.deadline_at).strftime("%B %d, %Y") if sub.deadline_at else "(date)"
    va = _is_virginia(profile)
    to = broker.opt_out_email or ""
    if kind == "followup":
        rule = (
            "Under Va. Code § 59.1-577(B)(1), a controller must respond within 45 days of "
            "receipt (extendable once by 45 days with notice during the first period)."
            if va
            else "Privacy laws that cover deletion requests generally require a response "
            "within 45 days."
        )
        body = (
            f"Dear {broker.name} Privacy Team,\n\n"
            f"On {sent} I sent you a request to delete my personal data. {rule} That date "
            f"was {deadline}, and I have not received a response.\n\n"
            "Please confirm in writing whether my data has been deleted. If you need "
            f"anything to verify my identity, reply to {contact_email}.\n\n"
            f"{_who(profile)}\n"
        )
        return Draft(kind, to, f"Follow-up: deletion request of {sent}", body)
    if kind == "clarify":
        basis = "under Va. Code § 59.1-577(A)(3) " if va else ""
        body = (
            f"Dear {broker.name} Privacy Team,\n\n"
            f"Thank you for your automated reply to my request of {sent}. It points me to "
            "an advertising opt-out page, but my request was not about ad preferences. It "
            f"is a request {basis}to delete any personal data you hold about me, such as my "
            "name, address, and email address, wherever it is stored.\n\n"
            "Please confirm in writing either that you have deleted that data, or that you "
            f"hold none. You may reply to {contact_email}.\n\n{_who(profile)}\n"
        )
        return Draft(kind, to, f"Clarification: deletion request of {sent}", body)
    if kind == "appeal":
        rule = (
            "as provided in Va. Code § 59.1-577(C). Under that subsection, you must inform me "
            f"in writing of your decision within {APPEAL_RESPONSE_DAYS} days of receiving "
            "this appeal, with your reasons if you deny it."
            if va
            else "Please tell me in writing what you decide, and why if you refuse."
        )
        body = (
            f"Dear {broker.name} Privacy Team,\n\n"
            f"I am appealing your refusal to act on my deletion request of {sent}, {rule} "
            "Please reconsider and delete my personal data.\n\n"
            f"Reply to {contact_email}.\n\n{_who(profile)}\n"
        )
        return Draft(kind, to, f"Appeal: deletion request of {sent}", body)

    where = (
        "the Virginia Attorney General, Consumer Protection Section (Virginia Consumer Data "
        "Protection Act)"
        if va
        else "your state Attorney General's consumer protection office"
    )
    body = (
        f"Draft facts for a complaint to {where}. Review and edit; you file it yourself.\n\n"
        f"Business: {broker.name} ({broker.domain})\n"
        f"Contact on file: {broker.opt_out_email or broker.privacy_url or 'unknown'}\n"
        f"My request: deletion of my personal data, sent {sent} by email"
        f"{' under Va. Code § 59.1-577(A)(3)' if va else ''}.\n"
        f"Response due: {deadline}{' (§ 59.1-577(B)(1))' if va else ''}.\n"
        "Outcome: no response / request refused. Follow-up or appeal sent: see my records.\n\n"
        f"Complainant:\n{_who(profile)}\nContact: {contact_email}\n"
        + (f"\nFile at: {AG_COMPLAINT_URL}\n" if va else "")
    )
    return Draft(
        "complaint", AG_COMPLAINT_URL if va else "", f"Complaint draft: {broker.name}", body
    )
