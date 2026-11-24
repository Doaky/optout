"""Read broker replies over plain IMAP, for any email provider (not only Gmail).

Gmail gets labels and a precise search from its own extensions (see gmail_label.py). Other
providers only have standard IMAP, so this finds replies the plain way: in the Inbox, since
you sent your first request, it looks for mail that carries our request's subject, comes from
an address a broker has answered from (or is expected to), or is a bounce notice. The mailbox
is opened read-only and BODY.PEEK is used, so nothing is marked read, moved or deleted.
Uses the same app password as sending.
"""

from __future__ import annotations

import imaplib
import re
from collections.abc import Callable, Iterable
from datetime import date

from .gmail_label import _ADDR_RE, SUBJECT, _q

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MSGID_RE = re.compile(r"[A-Za-z0-9._%+@=-]+")

# SMTP host -> IMAP host, for providers whose names don't follow smtp.X -> imap.X.
_IMAP_HOSTS = {
    "smtp.mail.me.com": "imap.mail.me.com",
    "smtp.office365.com": "outlook.office365.com",
    "smtp-mail.outlook.com": "outlook.office365.com",
    "smtp.mail.yahoo.com": "imap.mail.yahoo.com",
}


def imap_host_for(smtp_host: str, override: str | None = None) -> str:
    """The IMAP server that goes with an SMTP server: your own setting, a known provider, or
    the usual smtp.example.com -> imap.example.com."""
    if override:
        return override.strip()
    host = smtp_host.strip().lower()
    if host in _IMAP_HOSTS:
        return _IMAP_HOSTS[host]
    return "imap." + host[5:] if host.startswith("smtp.") else host


def _imap_date(d: date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _or_from(addresses: list[str]) -> list[str]:
    """IMAP search terms for 'from any of these' (OR takes exactly two keys)."""
    terms = [f"FROM {_q(a)}" for a in addresses]
    expr = terms[-1]
    for t in reversed(terms[:-1]):
        expr = f"OR {t} {expr}"
    return [expr]


class ImapReader:
    """The same read-only interface as GmailLabeler, without labels or marking as read."""

    def __init__(
        self,
        username: str,
        password: str,
        host: str,
        *,
        connect: Callable[[], imaplib.IMAP4] | None = None,
    ) -> None:
        self._username, self._password = username, password
        self._connect = connect or (lambda: imaplib.IMAP4_SSL(host, 993, timeout=30))
        self._imap: imaplib.IMAP4 | None = None
        self._expected: list[str] = []

    def __enter__(self) -> ImapReader:
        imap = self._connect()
        imap.login(self._username, self._password)
        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("could not open the Inbox over IMAP")
        self._imap = imap
        return self

    @property
    def _conn(self) -> imaplib.IMAP4:
        """The open connection; a clear error (not an assert) when used outside `with`."""
        if self._imap is None:
            raise RuntimeError("not connected: use this inside a `with` block")
        return self._imap

    def __exit__(self, *exc) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass  # already closed or dropped; nothing left to do

    # -- the GmailLabeler interface ---------------------------------------
    def label_replies(self, addresses: Iterable[str], *, since: date) -> int:
        """No labels here: just remember which addresses to look for. Returns 0."""
        self._expected = sorted({a.lower() for a in addresses if _ADDR_RE.match(a.lower())})
        return 0

    def mark_read_message_ids(self, message_ids: list[str]) -> int:
        return 0  # reading is read-only on purpose

    def fetch_replies(
        self, already_seen: set[str], *, since: date, limit: int = 300
    ) -> list[bytes]:
        after = ["SINCE", _imap_date(since)]
        queries = [
            [*after, "SUBJECT", _q(SUBJECT)],
            [*after, "FROM", _q("mailer-daemon")],
        ]
        for i in range(0, len(self._expected), 20):
            queries.append([*after, *_or_from(self._expected[i : i + 20])])
        uids: list[bytes] = []
        for q in queries:
            uids.extend(u for u in self._search(*q) if u not in uids)
        uids = uids[-limit:]
        new = [u for u, mid in self._message_ids(uids).items() if mid not in already_seen]
        return [raw for u in new if (raw := self._fetch(u))]

    def display_names(self, message_ids: list[str]) -> dict[str, str]:
        from email.utils import parseaddr

        out: dict[str, str] = {}
        for mid in message_ids[:200]:
            uids = self._by_message_id(mid)
            if not uids:
                continue
            _s, data = self._conn.uid(
                "FETCH", uids[0].decode(), "(BODY.PEEK[HEADER.FIELDS (FROM)])"
            )
            for item in data or []:
                if isinstance(item, tuple):
                    raw = item[1].decode("utf-8", "replace")
                    out[mid] = parseaddr(raw.split(":", 1)[-1].strip())[0][:200]
        return out

    def fetch_message(self, message_id: str) -> bytes | None:
        uids = self._by_message_id(message_id)
        return self._fetch(uids[0]) if uids else None

    # -- plumbing ----------------------------------------------------------
    def _search(self, *criteria: str) -> list[bytes]:
        status, data = self._conn.uid("SEARCH", *criteria)
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def _by_message_id(self, message_id: str) -> list[bytes]:
        token = message_id.strip().strip("<>")
        if not _MSGID_RE.fullmatch(token):
            return []  # never put odd characters into a search
        return self._search("HEADER", "Message-ID", _q(f"<{token}>"))

    def _message_ids(self, uids: list[bytes]) -> dict[bytes, str]:
        out: dict[bytes, str] = {}
        for i in range(0, len(uids), 100):
            chunk = b",".join(uids[i : i + 100]).decode()
            _status, data = self._conn.uid(
                "FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
            )
            for item in data or []:
                if not isinstance(item, tuple):
                    continue
                m = re.search(rb"UID (\d+)", item[0])
                hdr = re.search(rb"Message-ID:\s*(<[^>]+>)", item[1], re.IGNORECASE)
                if m:
                    out[m.group(1)] = hdr.group(1).decode(errors="replace") if hdr else ""
        return out

    def _fetch(self, uid: bytes) -> bytes | None:
        _status, data = self._conn.uid("FETCH", uid.decode(), "(BODY.PEEK[]<0.300000>)")
        for item in data or []:
            if isinstance(item, tuple):
                return bytes(item[1])
        return None
