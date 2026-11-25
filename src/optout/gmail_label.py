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
_ADDRESSES_PER_SEARCH = 100
_IDS_PER_SEARCH = 50
_BODIES_PER_FETCH = 25  # up to 300 KB each, so a round trip stays a few MB at most
_UID_RE = re.compile(rb"UID (\d+)")
_HDR_MSGID = re.compile(r"^Message-ID:\s*<([^>]+)>", re.IGNORECASE | re.MULTILINE)
_HDR_FROM = re.compile(r"^From:(.*(?:\r?\n[ \t].*)*)", re.IGNORECASE | re.MULTILINE)


def fetch_bodies(conn: imaplib.IMAP4, uids: list[bytes]) -> list[bytes]:
    """The first 300 KB of each message, in the order asked, fetched a batch per round trip
    (one request for 25 messages, not 25 requests). BODY.PEEK, so nothing is marked read."""
    got = bodies_by_uid(conn, uids)
    return [got[u] for u in uids if u in got]


def bodies_by_uid(conn: imaplib.IMAP4, uids: list[bytes]) -> dict[bytes, bytes]:
    """Like fetch_bodies, keyed by UID. A server may put the UID before or after the body,
    so both places are checked."""
    got: dict[bytes, bytes] = {}
    for i in range(0, len(uids), _BODIES_PER_FETCH):
        part = uids[i : i + _BODIES_PER_FETCH]
        _status, data = conn.uid("FETCH", b",".join(part).decode(), "(UID BODY.PEEK[]<0.300000>)")
        items = list(data or [])
        for n, item in enumerate(items):
            if not isinstance(item, tuple):
                continue
            m = _UID_RE.search(item[0])
            if m is None and n + 1 < len(items) and isinstance(items[n + 1], bytes):
                m = _UID_RE.search(items[n + 1])
            uid = m.group(1) if m else (part[0] if len(part) == 1 else None)
            if uid is not None:
                got[uid] = bytes(item[1])
    return got


def messages_by_id(
    conn: imaplib.IMAP4, uids: list[bytes], ids: dict[bytes, str], wanted: dict[str, str]
) -> dict[str, bytes]:
    """Bodies for the found *uids*, keyed by the caller's spelling of each Message-ID.
    *ids* maps UID -> Message-ID as found in the header; *wanted* maps the bare id -> the
    caller's spelling."""
    bodies = bodies_by_uid(conn, [u for u in uids if ids.get(u, "").strip("<>") in wanted])
    out: dict[str, bytes] = {}
    for u, raw in bodies.items():
        mid = wanted[ids[u].strip("<>")]
        out.setdefault(mid, raw)  # the first copy found, if a message is in two places
    return out


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
        self._uid_by_mid: dict[str, bytes] = {}  # Message-ID -> UID, for mail read this session

    # -- connection -------------------------------------------------------
    def __enter__(self) -> GmailLabeler:
        imap = self._connect()
        imap.login(self._username, self._password)
        self._imap = imap
        self._select_all_mail(imap)
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
        status, data = self._conn.uid("SEARCH", "X-GM-RAW", _q(raw_query))
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def _label(self, uids: Iterable[bytes], name: str | None = None, *, read: bool = False) -> int:
        uids = list(uids)
        for i in range(0, len(uids), 200):
            chunk = b",".join(uids[i : i + 200]).decode()
            self._conn.uid("STORE", chunk, "+X-GM-LABELS", f"({_q(name or self.label)})")
            if read:
                self._conn.uid("STORE", chunk, "+FLAGS", "(\\Seen)")
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

    def mark_read_message_ids(self, message_ids: list[str]) -> int:
        """Mark these messages (by Message-ID) as read. Adds the read flag only: nothing is
        moved, labelled or deleted, and ids with odd characters are ignored. Messages read
        in this session are already known; the rest are found 50 to a search, and the
        flag is set 100 messages per command."""
        uids: list[bytes] = []
        tokens: list[str] = []
        for mid in message_ids:
            token = mid.strip().strip("<>")
            if not re.fullmatch(r"[A-Za-z0-9._%+@=-]+", token):
                continue
            known = self._uid_by_mid.get(f"<{token}>")
            if known is not None:
                uids.append(known)
            else:
                tokens.append(token)
        for i in range(0, len(tokens), _IDS_PER_SEARCH):
            query = " OR ".join(f"rfc822msgid:{t}" for t in tokens[i : i + _IDS_PER_SEARCH])
            uids.extend(u for u in self._search(query) if u not in uids)
        for i in range(0, len(uids), 100):
            chunk = b",".join(uids[i : i + 100]).decode()
            self._conn.uid("STORE", chunk, "+FLAGS", "(\\Seen)")
        return len(uids)

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
        ids = self._message_ids(uids)
        # Remembered so marking these read later needs no search per message.
        self._uid_by_mid.update({mid: u for u, mid in ids.items() if mid})
        new = [u for u, mid in ids.items() if mid not in already_seen]
        return fetch_bodies(self._conn, new)

    def display_names(self, message_ids: list[str]) -> dict[str, str]:
        """Sender display names for saved messages (looked up by Message-ID, headers only)."""
        from email.utils import parseaddr

        wanted: dict[str, str] = {}  # bare Message-ID -> the caller's spelling
        for mid in message_ids[:200]:
            token = mid.strip().strip("<>")
            if re.fullmatch(r"[A-Za-z0-9._%+@=-]+", token):
                wanted[token] = mid
        tokens = list(wanted)
        # One search per batch and one header fetch, not two round trips per message.
        uids: list[bytes] = []
        for i in range(0, len(tokens), _IDS_PER_SEARCH):
            query = " OR ".join(f"rfc822msgid:{t}" for t in tokens[i : i + _IDS_PER_SEARCH])
            uids.extend(u for u in self._search(query) if u not in uids)
        out: dict[str, str] = {}
        for i in range(0, len(uids), 100):
            chunk = b",".join(uids[i : i + 100]).decode()
            _s, data = self._conn.uid(
                "FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)])"
            )
            for item in data or []:
                if not isinstance(item, tuple):
                    continue
                head = item[1].decode("utf-8", "replace")
                mid_m = re.search(r"^Message-ID:\s*<([^>]+)>", head, re.IGNORECASE | re.MULTILINE)
                from_m = re.search(
                    r"^From:(.*(?:\r?\n[ \t].*)*)", head, re.IGNORECASE | re.MULTILINE
                )
                if mid_m and from_m and mid_m.group(1) in wanted:
                    out[wanted[mid_m.group(1)]] = parseaddr(from_m.group(1).strip())[0][:200]
        return out

    def fetch_message(self, message_id: str) -> bytes | None:
        """One saved message again, found by its Message-ID (read-only, not marked read)."""
        return self.fetch_messages([message_id]).get(message_id)

    def fetch_messages(self, message_ids: list[str]) -> dict[str, bytes]:
        """Saved messages again, by Message-ID (read-only, not marked read): 50 ids per
        search and 25 bodies per download, instead of two round trips per message. Ids
        with odd characters, and messages no longer in the mailbox, are left out."""
        wanted: dict[str, str] = {}
        for mid in message_ids:
            token = mid.strip().strip("<>")
            if re.fullmatch(r"[A-Za-z0-9._%+@=-]+", token):
                wanted.setdefault(token, mid)
        tokens = list(wanted)
        uids: list[bytes] = []
        for i in range(0, len(tokens), _IDS_PER_SEARCH):
            query = " OR ".join(f"rfc822msgid:{t}" for t in tokens[i : i + _IDS_PER_SEARCH])
            uids.extend(u for u in self._search(query) if u not in uids)
        return messages_by_id(self._conn, uids, self._message_ids(uids), wanted)

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

    def label_replies(self, addresses: Iterable[str], *, since: date) -> int:
        """Label inbox mail since *since* that is plainly a reply to us: sent from an exact
        address we expect a broker to answer from, or carrying our request's subject. A
        broker's other mail (newsletters, account notices) is never touched."""
        clean = sorted({a.lower() for a in addresses if _ADDR_RE.match(a.lower())})
        # Mail labelled on an earlier run is left out, so it isn't labelled again every time.
        base = (
            f"in:inbox -from:me after:{since:%Y/%m/%d} -label:{self.reply_label.replace(' ', '-')}"
        )
        read = self.mark_read
        total = self._label(
            self._search(f'{base} subject:"{SUBJECT}"'), self.reply_label, read=read
        )
        # 100 addresses per search: Gmail returns the same mail as smaller batches, in a
        # fraction of the round trips (about 1,000 addresses is 11 searches, not 53).
        for i in range(0, len(clean), _ADDRESSES_PER_SEARCH):
            terms = " OR ".join(f"from:{a}" for a in clean[i : i + _ADDRESSES_PER_SEARCH])
            total += self._label(self._search(f"{base} ({terms})"), self.reply_label, read=read)
        return total
