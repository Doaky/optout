"""Gmail over IMAP: label opt-out mail, and read broker replies.

Gmail's SMTP can't set labels and filters don't run on outgoing mail, so this
uses Gmail's IMAP extensions (X-GM-RAW search, X-GM-LABELS). It SEARCHes, adds a
label, and reads (with BODY.PEEK, so nothing is marked read) only messages that
carry your opt-out label or are bounce notices. Nothing is moved or deleted.
Uses the same app password as sending.

Labelling is best effort: a labelling problem never fails a send.
"""

from __future__ import annotations

import imaplib
import re
import time
from collections.abc import Callable, Iterable
from datetime import date

_LABEL_RE = re.compile(r"^[A-Za-z0-9 _/-]{1,60}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
_ADDR_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")
IMAP_HOST = "imap.gmail.com"
SUBJECT = "Data Deletion Request"


def valid_label(name: str) -> bool:
    return bool(_LABEL_RE.match(name))


def _q(text: str) -> str:
    """Quote a string for an IMAP command."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class GmailLabeler:
    def __init__(
        self,
        username: str,
        password: str,
        label: str,
        *,
        connect: Callable[[], imaplib.IMAP4] | None = None,
        reply_label: str | None = None,
        mark_read: bool = False,
    ) -> None:
        for name in (label, reply_label):
            if name is not None and not valid_label(name):
                raise ValueError("Label may only contain letters, digits, space, _ - /")
        self.label = label
        self.reply_label = reply_label or label
        self.mark_read = mark_read
        self._username, self._password = username, password
        self._connect = connect or (lambda: imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=30))
        self._imap: imaplib.IMAP4 | None = None

    # -- connection -------------------------------------------------------
    def __enter__(self) -> GmailLabeler:
        imap = self._connect()
        imap.login(self._username, self._password)
        self._imap = imap
        self._select_all_mail(imap)
        return self

    def __exit__(self, *exc) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass

    def _select_all_mail(self, imap: imaplib.IMAP4) -> None:
        """Gmail's 'All Mail' holds both sent and received; find it by its \\All flag."""
        _status, boxes = imap.list()
        name = "[Gmail]/All Mail"
        for raw in boxes or []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            if "\\All" in line:
                m = re.search(r'"/" (.+)$', line)
                if m:
                    name = m.group(1).strip().strip('"')
                    break
        status, _ = imap.select(_q(name))
        if status != "OK":
            raise RuntimeError("could not open Gmail All Mail over IMAP")

    # -- operations -------------------------------------------------------
    def _search(self, raw_query: str) -> list[bytes]:
        assert self._imap is not None
        status, data = self._imap.uid("SEARCH", "X-GM-RAW", _q(raw_query))
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def _label(self, uids: Iterable[bytes], name: str | None = None, *, read: bool = False) -> int:
        assert self._imap is not None
        uids = list(uids)
        for i in range(0, len(uids), 200):
            chunk = b",".join(uids[i : i + 200]).decode()
            self._imap.uid("STORE", chunk, "+X-GM-LABELS", f"({_q(name or self.label)})")
            if read:
                self._imap.uid("STORE", chunk, "+FLAGS", "(\\Seen)")
        return len(uids)

    def label_message_ids(
        self, message_ids: list[str], attempts: int = 3, wait: float = 2.0
    ) -> int:
        """Label sent messages by Message-ID, waiting briefly for Gmail to file them."""
        done = 0
        for mid in message_ids:
            token = mid.strip().strip("<>")
            if not re.fullmatch(r"[A-Za-z0-9._%+@=-]+", token):
                continue  # never put odd characters into a search expression
            for attempt in range(attempts):
                uids = self._search(f"rfc822msgid:{token}")
                if uids:
                    done += self._label(uids)
                    break
                if attempt < attempts - 1:
                    time.sleep(wait)
        return done

    def label_all_sent(self) -> int:
        """Catch up: every message you sent with the opt-out subject."""
        return self._label(self._search(f'from:me subject:"{SUBJECT}"'))

    def fetch_replies(
        self, already_seen: set[str], *, since: date, limit: int = 300
    ) -> list[bytes]:
        """Raw messages that carry one of our labels (not sent by you) since *since*, plus
        bounce notices from the same period. Nothing else in the mailbox is read."""
        tags = {name.replace(" ", "-") for name in (self.label, self.reply_label)}
        after = f"after:{since:%Y/%m/%d}"
        uids: list[bytes] = []
        queries = [f"label:{t} -from:me {after}" for t in sorted(tags)]
        queries.append(f"from:mailer-daemon {after}")
        for query in queries:
            uids.extend(u for u in self._search(query) if u not in uids)
        uids = uids[-limit:]
        new = [u for u, mid in self._message_ids(uids).items() if mid not in already_seen]
        return [raw for u in new if (raw := self._fetch(u))]

    def display_names(self, message_ids: list[str]) -> dict[str, str]:
        """Sender display names for saved messages (looked up by Message-ID, headers only)."""
        from email.utils import parseaddr

        assert self._imap is not None
        out: dict[str, str] = {}
        for mid in message_ids[:200]:
            token = mid.strip().strip("<>")
            if not re.fullmatch(r"[A-Za-z0-9._%+@=-]+", token):
                continue
            uids = self._search(f"rfc822msgid:{token}")
            if not uids:
                continue
            _s, data = self._imap.uid(
                "FETCH", uids[0].decode(), "(BODY.PEEK[HEADER.FIELDS (FROM)])"
            )
            for item in data or []:
                if isinstance(item, tuple):
                    raw = item[1].decode("utf-8", "replace")
                    out[mid] = parseaddr(raw.split(":", 1)[-1].strip())[0][:200]
        return out

    def fetch_message(self, message_id: str) -> bytes | None:
        """One saved message again, found by its Message-ID (read-only, not marked read)."""
        token = message_id.strip().strip("<>")
        if not re.fullmatch(r"[A-Za-z0-9._%+@=-]+", token):
            return None
        uids = self._search(f"rfc822msgid:{token}")
        return self._fetch(uids[0]) if uids else None

    def _message_ids(self, uids: list[bytes]) -> dict[bytes, str]:
        assert self._imap is not None
        out: dict[bytes, str] = {}
        for i in range(0, len(uids), 100):
            chunk = b",".join(uids[i : i + 100]).decode()
            _status, data = self._imap.uid(
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
        assert self._imap is not None
        _status, data = self._imap.uid("FETCH", uid.decode(), "(BODY.PEEK[]<0.300000>)")
        for item in data or []:
            if isinstance(item, tuple):
                return bytes(item[1])
        return None

    def label_replies(self, addresses: Iterable[str], *, since: date) -> int:
        """Label inbox mail since *since* that is plainly a reply to us: sent from an exact
        address we expect a broker to answer from, or carrying our request's subject. A
        broker's other mail (newsletters, account notices) is never touched."""
        clean = sorted({a.lower() for a in addresses if _ADDR_RE.match(a.lower())})
        after = f"after:{since:%Y/%m/%d}"
        read = self.mark_read
        total = self._label(
            self._search(f'in:inbox -from:me {after} subject:"{SUBJECT}"'),
            self.reply_label,
            read=read,
        )
        for i in range(0, len(clean), 20):
            terms = " OR ".join(f"from:{a}" for a in clean[i : i + 20])
            total += self._label(
                self._search(f"in:inbox -from:me {after} ({terms})"), self.reply_label, read=read
            )
        return total
