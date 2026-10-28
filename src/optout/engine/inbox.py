"""Read broker replies, match them to requests, and classify them locally.

Nothing here leaves the machine. Email content is untrusted: it is parsed as
text, never executed, links are never opened, and the only effect a message can
have is to update the status of the one request it was matched to, through the
same rules as a manual reply (never to "removed").
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime

from sqlmodel import Session, select

from ..actions import apply_outcome
from ..brokers.schema import BrokerDef
from ..db import InboxMessage, Submission, update_status

MAX_TEXT = 8000

# --- parsing -----------------------------------------------------------------


@dataclass
class ParsedMail:
    message_id: str
    in_reply_to: list[str]
    from_addr: str
    to_addrs: list[str]
    subject: str
    date: datetime | None
    text: str
    raw_ids: list[str] = field(default_factory=list)  # every Message-ID seen (bounces)


_QUOTE_START = re.compile(
    r"^\s*(On .{0,200}wrote:|-{2,}\s*Original Message\s*-{2,}|From:\s.+|"
    r"_{10,}|Sent from my .+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_MSGID = re.compile(r"<[^<>\s@]+@[^<>\s]+>")
# In a bounce, the original message's ID appears in a quoted header line.
_ID_HEADER = re.compile(r"(?im)^\s*(?:message-id|in-reply-to|references)\s*:\s*(.+)$")


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    return re.sub(r"[ \t]+", " ", text).replace(" .", ".").replace(" ,", ",")


def _body_text(msg: EmailMessage) -> str:
    plain, rich = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            continue
        (plain if ctype == "text/plain" else rich).append(str(content))
    text = "\n".join(plain) if plain else _html_to_text("\n".join(rich))
    return text


def strip_quoted(text: str) -> str:
    """Drop the quoted history (our own request) so it can't sway classification."""
    m = _QUOTE_START.search(text)
    if m and m.start() > 0:
        text = text[: m.start()]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_message(raw: bytes) -> ParsedMail:
    msg = message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)
    try:
        date = parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else None
    except (TypeError, ValueError):
        date = None
    refs = " ".join(str(msg.get(h, "")) for h in ("In-Reply-To", "References"))
    to = getaddresses([str(msg.get(h, "")) for h in ("To", "Cc", "Delivered-To")])
    sender = getaddresses([str(msg.get("From", ""))])
    body = _body_text(msg)
    return ParsedMail(
        message_id=str(msg.get("Message-ID", "")).strip(),
        in_reply_to=_MSGID.findall(refs),
        from_addr=(sender[0][1] if sender else "").lower(),
        to_addrs=[a.lower() for _, a in to if a],
        subject=str(msg.get("Subject", ""))[:300],
        date=date,
        text=strip_quoted(body)[:MAX_TEXT],
        raw_ids=[
            mid
            for line in _ID_HEADER.findall(raw.decode("utf-8", "replace"))
            for mid in _MSGID.findall(line)
        ],
    )


# --- classification ----------------------------------------------------------

_I = re.IGNORECASE
RULES: list[tuple[str, str, re.Pattern[str]]] = [
    # (kind, rule name, pattern), checked against subject + text
    (
        "bounced",
        "delivery failure",
        re.compile(
            r"delivery status notification|undeliverable|delivery (has )?failed|"
            r"address not found|mailbox (unavailable|not found)|user unknown|"
            r"message (was )?not delivered|returned mail",
            _I,
        ),
    ),
    (
        "denied",
        "refusal",
        re.compile(
            r"\b(unable|not able) to (process|honou?r|comply with|fulfil?l)\b|"
            r"\b(cannot|can't|can not|will not|won't) (process|honou?r|comply|delete|"
            r"fulfil?l)\b|\bnot subject to\b|\bexempt(ion)?\b|\b(we )?(must )?decline\b|"
            r"\b(request|it) (is|was|has been) denied\b|\bdoes not apply to\b",
            _I,
        ),
    ),
    (
        "need-info",
        "asks for something",
        re.compile(
            r"\bverify (your|the) (identity|request|email)\b|\bidentity verification\b|"
            r"\bclick (on )?(the|this) link\b|\bconfirm (your|this) (email|request)\b|"
            r"\b(additional|more|further) information\b|\bplease (provide|submit|complete|"
            r"fill out|reply with|send us)\b|\b(web ?form|online form|portal)\b",
            _I,
        ),
    ),
    (
        "no-data",
        "no record of you",
        re.compile(
            r"\b(no|not find any|did not find|could not find|couldn't find|unable to "
            r"(locate|find|identify)|do not have|don't have|does not have|not able to "
            r"(locate|find))\b[^.\n]{0,60}\b(records?|information|data|profile|account|"
            r"match(es)?)\b",
            _I,
        ),
    ),
    (
        "deleted",
        "confirms deletion",
        re.compile(
            r"\b(has|have|had) been (deleted|removed|erased|suppressed|purged|opted out)\b|"
            r"\bwe (have )?(deleted|removed|erased|suppressed|purged)\b|"
            r"\bsuccessfully (deleted|removed|processed|opted)\b|"
            r"\b(request|deletion) (has been|was|is) (completed|processed|fulfilled|"
            r"honou?red)\b|\b(you are|you've been|you have been) opted out\b",
            _I,
        ),
    ),
    (
        "auto-reply",
        "acknowledgement",
        re.compile(
            r"\bauto(matic|mated)?[- ]?(reply|response|generated)\b|\bdo not reply\b|"
            r"\bno-?reply\b|\bthank(s| you) for (contacting|reaching out|your (email|"
            r"message|request|inquiry))\b|\bwe(('ve| have))? received your (request|"
            r"message|email)\b|\bticket (number|#)|\bwithin \d+ (business )?days\b",
            _I,
        ),
    ),
]
# Answers that settle the request outweigh an acknowledgement in the same email.
_PRIORITY = ["bounced", "denied", "need-info", "no-data", "deleted", "auto-reply"]


@dataclass(frozen=True)
class KnownReply:
    """One entry of data/known_replies.yml: what a particular broker's reply means."""

    sender: str  # "@domain.com" or a full address
    phrase: str  # case-insensitive text in the subject or body
    outcome: str  # a kind: deleted | no-data | denied | need-info | auto-reply
    broker: str | None = None  # slug, lets a known sender match a request


def load_known_replies() -> list[KnownReply]:
    from importlib.resources import files

    import yaml

    from ..config import config_dir

    sources = [files("optout").joinpath("data/known_replies.yml")]
    local = config_dir() / "known_replies.yml"  # your own additions
    out: list[KnownReply] = []
    for src in [*sources, local]:
        try:
            data = yaml.safe_load(src.read_text()) or []
        except (OSError, yaml.YAMLError):
            continue
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            kr = KnownReply(
                sender=str(item.get("from", "")).lower().strip(),
                phrase=str(item.get("phrase", "")).lower().strip(),
                outcome=str(item.get("outcome", "")),
                broker=item.get("broker"),
            )
            if kr.sender and kr.phrase and kr.outcome in _PRIORITY:
                out.append(kr)
    return out


def _sender_matches(rule_sender: str, from_addr: str) -> bool:
    if rule_sender.startswith("@"):
        domain = from_addr.rsplit("@", 1)[-1]
        d = rule_sender[1:]
        return domain == d or domain.endswith("." + d)
    return from_addr == rule_sender


def known_reply(mail: ParsedMail, known: list[KnownReply]) -> KnownReply | None:
    blob = f"{mail.subject}\n{mail.text}".lower()
    for kr in known:
        if _sender_matches(kr.sender, mail.from_addr) and kr.phrase in blob:
            return kr
    return None


def classify(subject: str, text: str) -> tuple[str, str, list[str]]:
    """Returns (kind, rule, every kind that matched). 'unknown' if nothing matched."""
    blob = f"{subject}\n{text}"
    hits = [(kind, name) for kind, name, rx in RULES if rx.search(blob)]
    kinds = [k for k, _ in hits]
    if not hits:
        return "unknown", "", []
    best = min(hits, key=lambda h: _PRIORITY.index(h[0]))
    return best[0], best[1], kinds


def is_confident(kinds: list[str]) -> bool:
    """Apply automatically only when the signals agree.

    An acknowledgement alongside a real answer is fine ("Thanks for contacting us. We have
    deleted your data."). Two different real answers (e.g. "deleted" and "need info") are
    not: a person should look.
    """
    answers = {k for k in kinds if k != "auto-reply"} - {"no-data", "deleted"} | (
        {"deleted"} if {"no-data", "deleted"} & set(kinds) else set()
    )
    return len(answers) <= 1


# --- matching ----------------------------------------------------------------


@dataclass
class MatchIndex:
    by_message_id: dict[str, Submission]
    by_alias: dict[str, Submission]
    by_domain: dict[str, list[Submission]]
    by_sender: dict[str, Submission] = field(default_factory=dict)  # learned reply addresses


def build_index(
    subs: Iterable[Submission],
    brokers: dict[str, BrokerDef],
    alias_pattern: str | None,
    senders: dict[str, str] | None = None,
) -> MatchIndex:
    """senders: reply address -> broker slug, learned from replies already matched."""
    latest: dict[str, Submission] = {}
    by_mid: dict[str, Submission] = {}
    for s in subs:
        if s.submitted_at is None:
            continue
        if s.broker_slug not in latest or s.created_at > latest[s.broker_slug].created_at:
            latest[s.broker_slug] = s
        try:
            mid = json.loads(s.payload_json or "{}").get("message_id")
        except ValueError:
            mid = None
        if mid:
            by_mid[mid.strip()] = s
    by_alias: dict[str, Submission] = {}
    by_domain: dict[str, list[Submission]] = {}
    for slug, s in latest.items():
        if alias_pattern:
            by_alias[alias_pattern.format(slug=slug).lower()] = s
        b = brokers.get(slug)
        domains = {b.domain.lower()} if b else set()
        if b and b.opt_out_email:
            domains.add(b.opt_out_email.rsplit("@", 1)[1].lower())
        for d in domains:
            by_domain.setdefault(d, []).append(s)
    by_sender = {addr: latest[slug] for addr, slug in (senders or {}).items() if slug in latest}
    return MatchIndex(by_mid, by_alias, by_domain, by_sender)


def match(
    mail: ParsedMail, idx: MatchIndex, known: KnownReply | None = None
) -> tuple[Submission | None, str]:
    for mid in [*mail.in_reply_to, *mail.raw_ids]:
        if mid in idx.by_message_id:
            return idx.by_message_id[mid], "message-id"
    for addr in mail.to_addrs:
        if addr in idx.by_alias:
            return idx.by_alias[addr], "alias"
    if mail.from_addr in idx.by_sender:
        return idx.by_sender[mail.from_addr], "known-sender"
    if known and known.broker:
        hits = [s for subs in idx.by_domain.values() for s in subs if s.broker_slug == known.broker]
        if hits:
            return hits[0], "known-sender"
    domain = mail.from_addr.rsplit("@", 1)[-1]
    found: dict[str, Submission] = {}
    for d, subs in idx.by_domain.items():
        if domain == d or domain.endswith("." + d):
            for s in subs:
                found[s.broker_slug] = s
    if len(found) == 1:
        return next(iter(found.values())), "domain"
    return None, ""  # no match, or several brokers share the domain: ask a person


# --- applying ----------------------------------------------------------------

KIND_TO_OUTCOME = {
    "deleted": "deleted",
    "no-data": "deleted",
    "denied": "denied",
    "need-info": "need-info",
    "auto-reply": "auto-reply",
}


@dataclass
class SyncReport:
    applied: list[tuple[str, str]] = field(default_factory=list)  # (slug, kind)
    review: int = 0
    skipped: int = 0


def process(
    session: Session,
    mails: Iterable[ParsedMail],
    idx: MatchIndex,
    *,
    known: list[KnownReply] | None = None,
    dry_run: bool = False,
) -> SyncReport:
    report = SyncReport()
    known = load_known_replies() if known is None else known
    seen = set(session.exec(select(InboxMessage.message_id)).all())
    for mail in mails:
        if not mail.message_id or mail.message_id in seen:
            report.skipped += 1
            continue
        seen.add(mail.message_id)
        kr = known_reply(mail, known)
        sub, how = match(mail, idx, kr)
        if kr is not None:
            kind, rule, kinds = kr.outcome, f"known reply ({kr.sender})", [kr.outcome]
        else:
            kind, rule, kinds = classify(mail.subject, mail.text)
        auto = sub is not None and kind != "unknown" and is_confident(kinds)
        row = InboxMessage(
            message_id=mail.message_id[:300],
            submission_id=sub.id if sub else None,
            broker_slug=sub.broker_slug if sub else None,
            matched_by=how,
            from_addr=mail.from_addr[:200],
            subject=mail.subject[:300],
            received_at=mail.date.astimezone(UTC) if mail.date else None,
            text=mail.text,
            kind=kind,
            rule=rule,
            state="applied" if auto else "review",
        )
        if dry_run:
            report.applied.append((row.broker_slug or "?", f"{kind}{'' if auto else ' (review)'}"))
            continue
        session.add(row)
        session.commit()
        if auto and sub is not None:
            apply_kind(session, sub, kind, rule)
            report.applied.append((sub.broker_slug, kind))
        else:
            report.review += 1
    return report


def apply_kind(session: Session, sub: Submission, kind: str, note: str) -> None:
    if kind == "bounced":
        if sub.status not in ("confirmed", "removed"):
            update_status(
                session, sub, "failed", failure_reason="bounced: the broker's address rejected it"
            )
        return
    apply_outcome(session, sub, KIND_TO_OUTCOME[kind], note, "inbox")


def _state_path():
    from ..db import db_path

    return db_path().parent / "inbox-state.json"


def last_checked() -> datetime | None:
    try:
        raw = json.loads(_state_path().read_text()).get("last_checked")
        return datetime.fromisoformat(raw) if raw else None
    except (OSError, ValueError):
        return None


def mark_checked(when: datetime, error: str | None = None) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_checked": when.isoformat(), "error": error}))


def last_error() -> str | None:
    try:
        return json.loads(_state_path().read_text()).get("error")
    except (OSError, ValueError):
        return None


# --- the whole round trip ----------------------------------------------------


def gmail_ready(config) -> str | None:
    """None if replies can be read for this config, else why not."""
    smtp = config.email.smtp
    if smtp is None or not smtp.host.endswith("gmail.com"):
        return "reading replies needs a Gmail account"
    from .methods.email import read_smtp_password

    if not read_smtp_password(smtp):
        return "no app password saved (run: optout smtp-password)"
    return None


def run_sync(config, *, dry_run: bool = False) -> SyncReport:
    """Label new broker replies, read them, and apply what's clear. Raises ValueError."""
    from ..brokers.registry import all_brokers, load_registry
    from ..db import get_session
    from ..gmail_label import GmailLabeler
    from .methods.email import read_smtp_password

    now = datetime.now(UTC)
    if problem := gmail_ready(config):
        raise ValueError(problem)
    smtp = config.email.smtp
    load_registry()
    brokers = {b.slug: b for b in all_brokers()}
    try:
        with get_session() as session:
            subs = list(session.exec(select(Submission)))
            idx = build_index(subs, brokers, config.email.alias_pattern, reply_senders(session))
            domains = set(idx.by_domain)
            seen = set(session.exec(select(InboxMessage.message_id)).all())
            label = config.email.gmail_label or "OPTOUT"
            with GmailLabeler(
                smtp.username,
                read_smtp_password(smtp),
                label,
                reply_label=config.email.reply_label,
            ) as gmail:
                gmail.label_replies(domains)
                raws = gmail.fetch_replies(seen)
            mails = []
            for raw in raws:
                try:
                    mails.append(parse_message(raw))
                except Exception:  # a malformed message must not stop the rest
                    continue
            report = process(session, mails, idx, dry_run=dry_run)
    except Exception as exc:
        if not dry_run:
            mark_checked(now, error=type(exc).__name__)
        raise ValueError(f"Couldn't read replies from Gmail ({type(exc).__name__})") from exc
    if not dry_run:
        mark_checked(now)
    return report


def reply_senders(session: Session) -> dict[str, str]:
    """Every address a broker has replied from (address -> slug), from settled replies."""
    rows = session.exec(
        select(InboxMessage.from_addr, InboxMessage.broker_slug).where(
            InboxMessage.broker_slug != None,  # noqa: E711 (SQL IS NOT NULL)
            InboxMessage.kind != "bounced",
            InboxMessage.state != "ignored",
        )
    ).all()
    return {addr: slug for addr, slug in rows if addr and slug}


def senders_by_broker(session: Session) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for addr, slug in reply_senders(session).items():
        out.setdefault(slug, set()).add(addr)
    return {slug: sorted(addrs) for slug, addrs in out.items()}
