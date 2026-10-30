"""Collects what the dashboard shows. Broker names, dates, and statuses only.

Deliberately excludes the user's name, address, email, and phone: the report
file can sit on disk or be shared without leaking identity.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
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
    heard: bool = False  # the broker has replied in some way (even just an acknowledgement)
    retryable: bool = False  # an email that failed to send (not a bounce): can be resent
    failure: str = ""  # why it failed, for the tooltip
    next_send: str = ""  # when this broker gets emailed again (finished email requests only)


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
    confirm: str = ""  # "known" (we can click it for you) or "teach" (unfamiliar sender)
    alt_emails: list[str] = field(default_factory=list)  # other addresses their reply names
    gmail_url: str = ""  # opens this exact email in Gmail (search by Message-ID)
    coverage: str = ""  # who the broker covers, if limited ("educators")
    coverage_note: str = ""
    coverage_groups: list[str] = field(default_factory=list)
    coverage_prompt: str = ""  # redacted prompt for Claude (contributor tools only)
    expires_in: float | None = None  # days left before their link/step lapses ("within 5 days")


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
    # Daily cumulative progress: (ISO date, sent, broker replied, broker says done)
    series: list[tuple[str, int, int, int]] = field(default_factory=list)
    verifiable: int = 0  # contacted brokers whose site we can re-search ourselves
    contributor_tools: bool = False
    attention_prompt: str = ""
    attention_redactions: str = ""
    resend_due_30d: int = 0  # brokers coming round for another email in the next 30 days
    followups_due: int = 0  # past their deadline, unanswered: a follow-up can go out now
    resend_after_days: int = 180


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


_TZ: tzinfo = UTC  # set per request by collect()


def _date(dt: datetime | None) -> str:
    return _aware(dt).astimezone(_TZ).strftime("%Y-%m-%d") if dt else "—"


_REPLY_WORDS = {
    "need-info": "asked for something",
    "auto-reply": "acknowledged",
    "deleted": "says deleted",
    "no-data": "has no data",
    "denied": "refused",
    "bounced": "bounced",
}


def _reply_summary(m) -> str:
    """'Oct 29 · asked for something': the broker's latest email, in a few words."""
    if m is None:
        return "—"
    when = _date(m.received_at) if m.received_at else ""
    word = _REPLY_WORDS.get(m.kind, "replied")
    return f"{when[5:] and _short_date(when)} · {word}" if when != "—" else word


def _short_date(iso: str) -> str:
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return iso
    return f"{d:%b} {d.day}"


def friendly_time(dt: datetime, tz: tzinfo) -> str:
    """'Oct 28, 8:04 PM EDT'"""
    local = dt.astimezone(tz)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local:%b} {local.day}, {hour}:{local:%M %p} {local.tzname() or ''}".strip()


_DONE = {"confirmed", "removed", "closed"}


def collect(
    session: Session, config: UserConfig | None, now: datetime, drafts: int = 0
) -> Dashboard:
    global _TZ, _NOW
    from ..config import user_tz

    _TZ = user_tz(config)
    _NOW = now
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
    from ..db import InboxMessage

    latest_reply: dict = {}  # submission id -> its most recent real reply
    for m in session.exec(
        select(InboxMessage).where(
            InboxMessage.submission_id != None,  # noqa: E711 (SQL IS NOT NULL)
            InboxMessage.state != "ignored",
        )
    ):
        cur = latest_reply.get(m.submission_id)
        if cur is None or (m.received_at or datetime.min) >= (cur.received_at or datetime.min):
            latest_reply[m.submission_id] = m
    from ..engine.campaign import next_resend

    by_broker: dict[str, list[Submission]] = {}
    for sub in subs:
        by_broker.setdefault(sub.broker_slug, []).append(sub)
    resend_days = config.email.resend_after_days if config is not None else 180
    dash.resend_after_days = resend_days
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
            last_check=(
                f"{v.outcome.replace('_', ' ')} {_date(v.checked_at)}"
                if v
                else _reply_summary(latest_reply.get(sub.id))
            ),
            heard=sub.id in latest_reply,
            drifted=drift,
            reply_from=reply_from.get(sub.broker_slug, []),
            retryable=(
                sub.status == "failed" and sub.submitted_at is None and sub.method_used == "email"
            ),
            failure=(sub.failure_reason or "")[:200] if sub.status == "failed" else "",
            only_covers=(
                coverage[sub.broker_slug].label()
                if sub.broker_slug in coverage and coverage[sub.broker_slug].niche
                else ""
            ),
        )
        when = (
            next_resend(by_broker[sub.broker_slug], resend_days)
            if sub.method_used == "email"
            else None
        )
        if when is not None:
            row.next_send = _date(when)
            dash.resend_due_30d += (when - now).days <= 30
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
        elif (stalled := _stalled_form(session, sub, now)) is not None:
            dash.attention.append(stalled)
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
                        "Action needed",
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

    from ..engine.followups import due as followups_due

    dash.followups_due = len(followups_due(session, {b.slug: b for b in all_brokers()}, now))
    # Steps that lapse soonest first: a confirm link that expires tomorrow beats a refusal.
    dash.attention.sort(key=lambda a: a.expires_in if a.expires_in is not None else 1e9)
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

    if config is not None and any(a.kind in ("need-info", "refused") for a in dash.attention):
        from ..engine.assist import build_attention_prompt, describe_redactions

        wanted = [a for a in dash.attention if a.kind in ("need-info", "refused")]
        dash.attention_prompt, counts = build_attention_prompt(
            wanted, config.profile, config.my_groups
        )
        dash.attention_redactions = describe_redactions(counts)
    dash.series = progress_series(session, subs, now)
    scannable = {b.slug for b in all_brokers() if b.scan}
    dash.verifiable = len({s.broker_slug for s in subs if s.submitted_at} & scannable)

    if config is not None:
        sel = select_brokers(
            all_brokers(),
            session,
            now=now,
            my_groups=config.my_groups,
            resend_after_days=config.email.resend_after_days,
        )
        dash.eligible, dash.skipped = len(sel.eligible), len(sel.skipped)
        dash.sent_today = sent_today(session, now, _TZ)
        dash.daily_limit = config.email.daily_limit
    return dash


_REPLY_EVENTS = {"confirmation_received", "rejection_received", "broker_acknowledged"}


def progress_series(
    session: Session, subs: list[Submission], now: datetime
) -> list[tuple[str, int, int, int]]:
    """Cumulative counts per day from the first request sent until today."""

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
        if ev.event_type in ("confirmation_received", "removed_verified"):
            # "Says done": the broker confirmed deletion, or a re-search verified it.
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


def _stalled_form(session: Session, sub: Submission, now: datetime) -> Attention | None:
    """A form was submitted that should be followed by an email, and it never came."""
    from ..forms import _payload, pending_followup

    if sub.status != "awaiting_broker_confirmation":
        return None
    ev = pending_followup(session, sub, now)
    if ev is None:
        return None
    wait = int(_payload(ev).get("wait_hours") or 24)
    if _aware(now) < _aware(ev.created_at) + timedelta(hours=wait):
        return None
    name = next((b.name for b in all_brokers() if b.slug == sub.broker_slug), sub.broker_slug)
    return Attention(
        sub.broker_slug,
        name,
        "stalled",
        "Their follow-up email never came",
        "You submitted their form and they should have emailed you. Check spam, then "
        "resubmit the form or close this.",
    )


_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "seven": 7,
    "ten": 10,
    "fourteen": 14,
    "fifteen": 15,
    "thirty": 30,
}
_EXPIRY = re.compile(
    r"\b(?:within|in|after|expires? in|expire within)\s+(?:(\w+)\s+\()?(\d+|[a-z]+)\)?\s*"
    r"(business\s+)?(hours?|days?)\b",
    re.I,
)
_EXPIRY_CUE = re.compile(r"expire|confirm|verif|click|closed|resubmit|re-initiate", re.I)
# "We will process/respond within 45 days" is their promise, not a deadline for you.
_THEIR_PROMISE = re.compile(
    r"\b(we|our team)\s*('ll|\s+will|\s+aim to|\s+shall)(?!\s+not)[^.]{0,40}"
    r"\b(process|respond|resolve|fulfil|review)",
    re.I,
)


def _expires_in(text: str, received: datetime, now: datetime) -> float | None:
    """Days left before a step they asked for lapses, from wording like "confirm your email
    within five (5) days" or "this link will expire in 24 hours". None if they set no limit."""
    flat = re.sub(r"\s+", " ", text)
    best: float | None = None
    for m in _EXPIRY.finditer(flat):
        start = max(flat.rfind(".", 0, m.start()), 0)
        end = flat.find(".", m.end())
        sentence = flat[start : end if end > 0 else len(flat)]
        if not _EXPIRY_CUE.search(sentence) or _THEIR_PROMISE.search(sentence):
            continue
        raw = m.group(2).lower()
        n = int(raw) if raw.isdigit() else _WORDS.get(raw)
        if not n or n > 60:
            continue
        days = n / 24 if m.group(4).lower().startswith("hour") else float(n)
        if m.group(3):
            days *= 7 / 5
        left = days - (_aware(now) - _aware(received)).total_seconds() / 86400
        best = left if best is None else min(best, left)
    return best


_NOW: datetime | None = None


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
    if a.kind == "need-info":
        # The tightest deadline across what they've asked for (a later "we left a comment"
        # email doesn't cancel the "confirm within 5 days" one before it).
        lefts = [
            x
            for r in rows
            if r.kind == "need-info" and r.received_at is not None
            for x in [_expires_in(r.text, r.received_at, _NOW or datetime.now(UTC))]
            if x is not None
        ]
        a.expires_in = min(lefts) if lefts else None
    mid = row.message_id.strip().strip("<>")
    if mid:
        a.gmail_url = "https://mail.google.com/mail/u/0/#search/" + quote(f"rfc822msgid:{mid}")
    a.quote = deciding_sentence(row.text, row.kind or "")
    from ..brokers.registry import get_broker

    a.link = _best_link(row.text)
    if not a.link and a.kind == "need-info":
        try:  # their reply has no link: fall back to the broker's known opt-out form
            a.link = get_broker(sub.broker_slug).opt_out_url or ""
        except KeyError:
            pass
    a.link_host = urlsplit(a.link).hostname or "" if a.link else ""

    a.alt_emails = alternate_addresses(row.text, row.from_addr, sent_to(sub, get_broker))
    if a.kind == "need-info":
        from ..engine.confirm import find_link as find_confirm_link
        from ..engine.confirm import flow_for, load_flows, propose_flow

        flow = flow_for(row.from_addr, row.text, load_flows())
        if flow is not None and find_confirm_link(row.text, flow):
            a.confirm = "known"
        elif propose_flow(row.from_addr, row.text):
            a.confirm = "teach"
    if a.kind == "need-info":
        a.why = action_label(row.text, has_link=bool(a.link), has_other_email=bool(a.alt_emails))
    return a


_CONFIRM = re.compile(
    r"confirm (your|this) (email|e-mail|request)|verify (your )?(email|e-mail)|"
    r"click (on )?(the )?(button|link)|one more step",
    re.I,
)
_FORM = re.compile(
    r"web ?form|online form|webform|portal|privacy (center|choices|rights)|fill (out|in)|"
    r"submit (your|a|the) (request|form)|complete (the|our|this) (form|request)|"
    r"(form|portal) (below|here|at)",
    re.I,
)
_EMAIL_US = re.compile(
    r"\b(e-?mail|write|send|direct)\b[^.\n]{0,60}[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.", re.I
)
_MORE_INFO = re.compile(
    r"verify (your|the) identity|identity verification|additional (information|identifiers)|"
    r"insufficient|more information|proof of|full address|date of birth",
    re.I,
)
_PHONE_MAIL = re.compile(
    r"\b(call|telephone|toll-free|phone)\b|\b(u\.?s\.? mail|postal|p\.?o\.? box)", re.I
)


def action_label(text: str, *, has_link: bool, has_other_email: bool) -> str:
    """A specific pill for a reply that wants something: 'Action needed: Form submission'.
    Up to two things are named when the reply offers a choice."""
    flat = re.sub(r"\s+", " ", text)
    found: list[str] = []
    if _CONFIRM.search(flat) and has_link:
        found.append("Confirm your email")
    if _FORM.search(flat) or (has_link and not found):
        found.append("Form submission")
    if has_other_email and _EMAIL_US.search(flat):
        found.append("New email")
    if _MORE_INFO.search(flat) and not found:
        found.append("More information")
    if _PHONE_MAIL.search(flat) and len(found) < 2:
        found.append("Phone or mail")
    if not found:
        return "Action needed"
    first, second = found[0], (found[1].lower() if len(found) > 1 else "")
    return f"Action needed: {first}" + (f" or {second}" if second else "")


def sent_to(sub: Submission, get_broker) -> str:
    """The address this request actually went to. Not the broker's current contact: that may
    have been corrected since, and the old request still needs to go to the new address."""
    try:
        to = json.loads(sub.payload_json or "{}").get("to")
    except ValueError:
        to = None
    if to:
        return str(to).lower()
    try:
        return (get_broker(sub.broker_slug).opt_out_email or "").lower()
    except KeyError:
        return ""


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PRIVACY_MAILBOX = re.compile(
    r"privacy|dsar|dsr|optout|opt-out|opt_out|datasubject|subjectrequest|dataprotection|"
    r"data-protection|data_protection|dpo|gdpr|ccpa|cpra|rights|delet|remov|suppress|"
    r"compliance|consumer|requests?$",
    re.I,
)
_NOT_A_MAILBOX = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster)", re.I
)


def alternate_addresses(text: str, sender: str, contacted: str, *, limit: int = 2) -> list[str]:
    """Addresses a reply tells us to write to instead. Reply text is untrusted, so only
    an address on the same company's domain as whoever replied (or as the address we
    wrote to) counts. Anything else could send your request to a stranger."""
    sender = sender.lower()
    contacted = contacted.lower()
    domains = {x.rsplit("@", 1)[1] for x in (sender, contacted) if "@" in x}

    def same_company(domain: str) -> bool:
        return any(
            domain == d or domain.endswith("." + d) or d.endswith("." + domain) for d in domains
        )

    found: list[str] = []
    for m in _EMAIL.finditer(text):
        addr = m.group(0).lower().rstrip(".")
        local, _, domain = addr.partition("@")
        if addr in (sender, contacted) or addr in found or _NOT_A_MAILBOX.match(local):
            continue
        # A privacy mailbox, not security@, bugbounty@, a named person, or sales@.
        if same_company(domain) and _PRIVACY_MAILBOX.search(local):
            found.append(addr)
    return found[:limit]


_FORMISH = re.compile(r"privacy|request|form|portal|dsar|dsr|rights|onetrust|consent|delete", re.I)
_ADS = re.compile(r"aboutads|youradchoices|networkadvertising|optout\.aboutads", re.I)
# Never the link to act on: California's DROP portal (California residents only), social
# media, images, unsubscribe pages.
_NOT_FOR_YOU = re.compile(
    r"privacy\.ca\.gov|drop\.privacy|facebook\.com|twitter\.com|linkedin\.com|glassdoor|"
    r"instagram\.com|youtube\.com|\.(png|jpe?g|gif|pdf)\b|unsubscribe",
    re.I,
)
_STRONG_FORM = re.compile(
    r"webform|dsar|dsr|request|/form|forms/|portal|preferences|onetrust|datasubject|bigid|"
    r"service-now|opt-?out|removal|remove|delete|privacy-choices|data-privacy-form|ccpa|"
    r"ownyourdata|your-?data|my-?data|privacy-?center",
    re.I,
)
_JUST_READING = re.compile(
    r"privacy-policy|policy|notice|terms|agent|faq|help-center|/legal/?$", re.I
)


def _link_score(url: str) -> int:
    return (
        2 * bool(_STRONG_FORM.search(url))
        + bool(_FORMISH.search(url))
        - 4 * bool(_JUST_READING.search(url))
    )


def _best_link(text: str) -> str:
    """The link most likely to be their privacy form: one that looks like a request form or
    portal, not their policy or terms page, never the ad-choices page or California's DROP
    portal that many replies also mention."""
    links = [m.group(0).rstrip(".,;:") for m in _LINK.finditer(text)]
    links = [u for u in links if not _ADS.search(u) and not _NOT_FOR_YOU.search(u)]
    if not links:
        return ""
    return max(links, key=lambda u: (_link_score(u), -links.index(u)))
