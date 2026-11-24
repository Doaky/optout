"""Confirming "click the button in this email" steps, for senders we recognise.

A reply's link is untrusted input, so this only ever clicks one when ALL of these hold:

- the email is a reply matched to one of our own requests
- its sender and wording match a known flow (``data/confirm_flows.yml`` plus the user's own)
- the link is HTTPS, on a host that flow allows, with no credentials or IP address
- every redirect stays on that same set of hosts

It sends no cookies and no personal details: it just opens the link, the way you would,
and reads whether the page says it worked. Anything it can't tell is left for you.
"""

from __future__ import annotations

import html
import ipaddress
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from urllib.parse import urlsplit

import yaml
from sqlmodel import Session, select

from optout import yaml_io

from ..config import config_dir
from ..db import InboxMessage, Submission, list_events, record_event, update_status

_LABELLED_LINK = re.compile(r"([^()\n]{0,60}?)\((https?://[^\s)]+)\)")
# A link on its own, not in "label (url)" form: "click the verification link below:\n<url>".
_BARE_LINK = re.compile(r"(?<![(\w])<?(https?://[^\s<>()\"]+)")
_HOST_OK = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
_DEFAULT_ANCHOR = r"confirm|verify"
MAX_ATTEMPTS = 2
_CUE = re.compile(r"verif|confirm|click", re.I)
# Email click-trackers: the link starts on the tracker and redirects to the real page.
TRACKER_HOSTS = ("pstmrk.it", "sendgrid.net", "mandrillapp.com", "list-manage.com", "mailgun.org")


@dataclass(frozen=True)
class Flow:
    sender: str  # "@domain.com" or one exact address
    phrase: str  # wording that appears in the email
    link_hosts: tuple[str, ...]  # the link must be on one of these (or a subdomain)
    anchor: re.Pattern[str]  # the link's label, e.g. "Confirm email"
    note: str = ""
    # Some portals open a page with a "Verify Email" button instead of confirming on arrival.
    # This names the button to press there (only ever on the flow's own hosts).
    press: re.Pattern[str] | None = None


# --- the mapping --------------------------------------------------------------


def _parse(data: object) -> list[Flow]:
    out: list[Flow] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        hosts = tuple(
            h
            for h in (str(x).lower().strip() for x in item.get("link_hosts") or [])
            if _HOST_OK.match(h)
        )
        sender = str(item.get("from", "")).lower().strip()
        phrase = str(item.get("phrase", "")).lower().strip()
        try:
            anchor = re.compile(str(item.get("anchor") or _DEFAULT_ANCHOR), re.IGNORECASE)
        except re.error:
            continue
        press = None
        if item.get("press"):
            try:
                press = re.compile(str(item["press"]), re.IGNORECASE)
            except re.error:
                continue
        if sender and len(phrase) >= 12 and hosts:
            out.append(Flow(sender, phrase, hosts, anchor, str(item.get("note", ""))[:240], press))
    return out


def _local_path():
    return config_dir() / "confirm_flows.yml"


_flows_cache: tuple[object, list[Flow]] | None = None


def load_flows() -> list[Flow]:
    """Known flows (shared, yours, and those implied by form recipes). Re-read only when your
    files change: the dashboard asks once per item in Needs attention."""
    global _flows_cache
    from ..forms import _local_path as recipes_path
    from ..forms import _stamp

    key = (_stamp(_local_path()), _stamp(recipes_path()))
    if _flows_cache is None or _flows_cache[0] != key:
        _flows_cache = (key, _load_flows())
    return list(_flows_cache[1])


def _load_flows() -> list[Flow]:
    flows: list[Flow] = []
    for src in (files("optout").joinpath("data/confirm_flows.yml"), _local_path()):
        try:
            flows += _parse(yaml_io.safe_load(src.read_text()) or [])
        except (OSError, yaml.YAMLError):
            continue
    from ..forms import recipe_flows

    return flows + recipe_flows()


def local_flow_entries() -> list[dict[str, object]]:
    try:
        data = yaml_io.safe_load(_local_path().read_text()) or []
    except (OSError, yaml.YAMLError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def learned_flows() -> list[dict[str, object]]:
    """The user's own flows the shared list doesn't already have, for sharing."""
    shared = {(f.sender, f.phrase) for f in _parse(_shared_raw())}
    return [
        e
        for e in local_flow_entries()
        if (str(e.get("from", "")).lower(), str(e.get("phrase", "")).lower()) not in shared
    ]


def _shared_raw() -> object:
    try:
        return yaml_io.safe_load(files("optout").joinpath("data/confirm_flows.yml").read_text())
    except (OSError, yaml.YAMLError):
        return []


def save_local_flow(sender: str, phrase: str, link_host: str, note: str = "") -> None:
    """Remember a flow for this user. Raises ValueError if it wouldn't be a safe one."""
    sender, phrase, link_host = sender.lower().strip(), phrase.lower().strip(), link_host.lower()
    if not sender.startswith("@") or not _HOST_OK.match(sender[1:]):
        raise ValueError("sender must be a whole domain")
    if len(phrase) < 12 or not _HOST_OK.match(link_host):
        raise ValueError("flow is too vague to trust")
    entries = [
        e
        for e in local_flow_entries()
        if (str(e.get("from", "")).lower(), str(e.get("phrase", "")).lower()) != (sender, phrase)
    ]
    entries.append(
        {
            "from": sender,
            "phrase": phrase,
            "anchor": _DEFAULT_ANCHOR,
            "link_hosts": [link_host],
            "note": note[:240],
        }
    )
    global _flows_cache
    _flows_cache = None
    path = _local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entries, sort_keys=False, allow_unicode=True))


# --- finding the link ------------------------------------------------------------


def host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or u.username or u.password or len(url) > 4000:
        return False
    try:
        ipaddress.ip_address(host)
        return False  # a bare IP address is never a privacy platform
    except ValueError:
        pass
    return any(host == h or host.endswith("." + h) for h in hosts)


def teach_phrase(text: str) -> str:
    """A sentence from the email that says to confirm/verify/click, to recognise it by next
    time (the most specific one). Empty if there isn't one."""
    found = []
    text = re.sub(r"<[^>]+>", "\n", text)  # an HTML-only email: tags aren't part of a sentence
    for sent in re.split(r"(?<=[.!?])\s+|\n", text):
        flat = re.sub(r"\s+", " ", sent).strip()
        if 12 <= len(flat) <= 200 and _CUE.search(flat) and "http" not in flat:
            found.append(flat.lower())
    return max(found, key=len, default="")


_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac"}


def _site(host: str) -> str:
    """The registrable domain, roughly: example.com, or example.co.uk."""
    parts = host.lower().split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def redirect_ok(url: str, hosts: tuple[str, ...]) -> bool:
    """A redirect target: on the flow's hosts, or anywhere over plain HTTPS when the flow's
    link is an email click-tracker (which always redirects to the real page)."""
    if host_allowed(url, hosts):
        return True
    # links.example.com -> api.example.com: the same company's own site is fine.
    if host_allowed(url, (urlsplit(url).hostname or "",)) and any(
        _site(urlsplit(url).hostname or "") == _site(h) for h in hosts
    ):
        return True
    if not any(h == t or h.endswith("." + t) for h in hosts for t in TRACKER_HOSTS):
        return False
    return host_allowed(url, (urlsplit(url).hostname or "",))


def flow_for(from_addr: str, text: str, flows: list[Flow]) -> Flow | None:
    flat = re.sub(r"\s+", " ", text).lower()
    addr = from_addr.lower()
    for f in flows:
        sender_ok = addr.endswith(f.sender) if f.sender.startswith("@") else addr == f.sender
        if sender_ok and f.phrase in flat:
            return f
    return None


def _links(text: str) -> Iterator[tuple[str, str]]:
    """(label, url) for each link. A bare link's label is the text just before it on its
    line, or the line above when it stands alone."""
    for m in _LABELLED_LINK.finditer(text):
        yield m.group(1), html.unescape(m.group(2).rstrip(".,;"))
    for m in _BARE_LINK.finditer(text):
        lines = text[: m.start()].split("\n")
        label = lines[-1].strip()
        if not label:
            label = next((ln.strip() for ln in reversed(lines[:-1]) if ln.strip()), "")
        yield label[-120:], html.unescape(m.group(1).rstrip(".,;"))  # "&amp;" in HTML mail


def find_link(text: str, flow: Flow) -> str | None:
    """The link whose label says confirm/verify, if it is on an allowed host."""
    for label, url in _links(text):
        if flow.anchor.search(label) and host_allowed(url, flow.link_hosts):
            return url
    return None


def propose_flow(from_addr: str, text: str) -> tuple[str, str, str] | None:
    """For a sender we don't know yet: (sender domain, link host, link) if the email has a
    labelled confirm/verify link on an ordinary HTTPS host. The user decides whether to trust it."""
    for label, url in _links(text):
        if not re.search(_DEFAULT_ANCHOR, label, re.IGNORECASE):
            continue
        host = (urlsplit(url).hostname or "").lower()
        parts = host.split(".")
        suffix = ".".join(parts[1:]) if len(parts) >= 4 else host  # my.onetrust.com
        if host_allowed(url, (suffix,)) and "@" in from_addr:
            return "@" + from_addr.rsplit("@", 1)[1].lower(), suffix, url
    return None


# --- opening it --------------------------------------------------------------------


@dataclass
class Fetched:
    status: int = 0
    text: str = ""
    final_host: str = ""
    error: str = ""


class _SameSite(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: tuple[str, ...]) -> None:
        self._hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not redirect_ok(newurl, self._hosts):
            raise urllib.error.URLError("redirected off the allowed site")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_plain(url: str, hosts: tuple[str, ...], timeout: float = 20) -> Fetched:
    opener = urllib.request.build_opener(_SameSite(hosts))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (optout)"})
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 (host-checked above)
            body = resp.read(300_000).decode("utf-8", "replace")
            return Fetched(resp.status, body, urlsplit(resp.geturl()).hostname or "")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Fetched(error=type(exc).__name__)


def fetch_browser(
    url: str,
    hosts: tuple[str, ...],
    timeout: float = 30,
    press: re.Pattern[str] | None = None,
) -> Fetched:
    """Many confirm pages only work with JavaScript, so load it in a headless browser. With
    *press*, a button on the page whose label matches it is pressed once (same site only)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(accept_downloads=False)
            page = ctx.new_page()
            resp = page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            page.wait_for_timeout(1500)
            host = urlsplit(page.url).hostname or ""
            if not redirect_ok(page.url, hosts):
                return Fetched(error="left the allowed site", final_host=host)
            if press is not None:
                button = page.get_by_role("button", name=press).first
                if button.count():
                    button.click()
                    page.wait_for_load_state("networkidle", timeout=timeout * 1000)
                    page.wait_for_timeout(2500)
                    host = urlsplit(page.url).hostname or ""
                    if not redirect_ok(page.url, hosts):
                        return Fetched(error="left the allowed site", final_host=host)
            return Fetched(resp.status if resp else 0, page.inner_text("body"), host)
        finally:
            browser.close()


def default_fetch(
    url: str, hosts: tuple[str, ...], press: re.Pattern[str] | None = None
) -> Fetched:
    try:
        return fetch_browser(url, hosts, press=press)
    except Exception:  # no browser installed, or it couldn't start
        return fetch_plain(url, hosts)


_OK = re.compile(
    r"verified|confirmed|thank you|success|received|submitted|all set|you may (now )?close", re.I
)
_BAD = re.compile(r"expired|invalid|error|try again|not found|something went wrong|unable to", re.I)
_ALREADY = re.compile(r"already (been )?(verified|confirmed)", re.I)


def judge(f: Fetched) -> str:
    """'confirmed' only when the page plainly says so; 'unclear' when we can't tell."""
    if f.error or not 200 <= f.status < 300:
        return "failed"
    head = f.text[:6000]
    if _ALREADY.search(head):
        return "confirmed"
    if _BAD.search(head):
        return "failed"
    return "confirmed" if _OK.search(head) else "unclear"


# --- doing it ------------------------------------------------------------------------


def _attempts(session: Session, sub: Submission, message_id: str) -> list[dict]:
    import json

    out = []
    for ev in list_events(session, sub.id):
        if ev.event_type == "confirm_clicked":
            p = json.loads(ev.payload_json or "{}")
            if p.get("message_id") == message_id[:120]:
                out.append(p)
    return out


Fetch = Callable[[str, tuple[str, ...]], Fetched]


def page_excerpt(f: Fetched, limit: int = 160) -> str:
    """What the page said, in a line, so you can judge for yourself whether it worked."""
    if f.error:
        return f.error
    flat = re.sub(r"\s+", " ", f.text).strip()
    return (
        flat[:limit] + ("…" if len(flat) > limit else "")
        if flat
        else f"HTTP {f.status}, empty page"
    )


def confirm_reply(
    session: Session,
    row: InboxMessage,
    *,
    flows: list[Flow],
    fetch: Fetch = default_fetch,
    said: list[str] | None = None,
    retry: bool = False,
) -> tuple[str, str]:
    """Click the confirm link in one stored reply, if a known flow allows it.
    Returns (result, host): confirmed | unclear | failed | already | no-flow | no-link."""
    sub = session.get(Submission, row.submission_id) if row.submission_id else None
    if sub is None:
        return "no-flow", ""
    flow = flow_for(row.from_addr, row.text, flows)
    if flow is None:
        return "no-flow", ""
    url = find_link(row.text, flow)
    if url is None:
        return "no-link", ""
    host = urlsplit(url).hostname or ""
    tried = _attempts(session, sub, row.message_id)
    if any(a.get("result") == "confirmed" for a in tried) or (
        len(tried) >= MAX_ATTEMPTS and not retry
    ):
        return "already", host
    page = (
        fetch(url, flow.link_hosts, press=flow.press) if flow.press else fetch(url, flow.link_hosts)
    )
    result = judge(page)
    if said is not None:
        said.append(page_excerpt(page))
    record_event(
        session,
        sub,
        "confirm_clicked",
        {
            "host": host,
            "result": result,
            "message_id": row.message_id[:120],
            "said": page_excerpt(page, 120),
        },
    )
    if result == "confirmed":
        from ..brokers.traits import note

        note(sub.broker_slug, "requires_email_verification")  # learned: this step exists
        if sub.status == "awaiting_user_input":
            update_status(session, sub, "awaiting_broker_confirmation")
        row.state = "resolved"
        session.add(row)
        session.commit()
    return result, host


def pending_confirmations(session: Session) -> list[InboxMessage]:
    return list(
        session.exec(
            select(InboxMessage).where(
                InboxMessage.kind == "need-info",
                InboxMessage.state.in_(("applied", "review")),  # type: ignore[attr-defined]
            )
        ).all()
    )


def confirmable_rows(session: Session, slug: str) -> list[InboxMessage]:
    """This broker's replies a person could still act on, newest first: unresolved ones, plus
    resolved ones while the request is still waiting on you (it was marked done too early)."""
    sub = max(
        (x for x in session.exec(select(Submission).where(Submission.broker_slug == slug)).all()),
        key=lambda x: x.created_at,
        default=None,
    )
    waiting = sub is not None and sub.status == "awaiting_user_input"
    states = ("applied", "review", "resolved") if waiting else ("applied", "review")
    rows = session.exec(
        select(InboxMessage).where(
            InboxMessage.broker_slug == slug,
            InboxMessage.kind == "need-info",
            InboxMessage.state.in_(states),  # type: ignore[attr-defined]
        )
    ).all()
    return sorted(rows, key=lambda r: r.received_at or datetime.min, reverse=True)


def auto_confirm(
    session: Session, flows: list[Flow], *, fetch: Fetch = default_fetch, limit: int = 10
) -> tuple[int, int]:
    """Confirm every pending reply a known flow covers. Returns (confirmed, unclear).
    Capped per run, and each message is tried at most MAX_ATTEMPTS times."""
    done = unclear = tried = 0
    for row in pending_confirmations(session):
        if tried >= limit:
            break
        if flow_for(row.from_addr, row.text, flows) is None:
            continue
        result, _ = confirm_reply(session, row, flows=flows, fetch=fetch)
        if result in ("confirmed", "unclear", "failed"):
            tried += 1
        done += result == "confirmed"
        unclear += result == "unclear"
    return done, unclear
