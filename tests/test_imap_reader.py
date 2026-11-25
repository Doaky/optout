"""Reading broker replies over plain IMAP (any provider, no Gmail extensions)."""

from __future__ import annotations

from datetime import date

from optout.imap_reader import ImapReader, imap_host_for

HDR = b"Message-ID: <a@x.example>\r\n\r\n"


class FakeIMAP:
    def __init__(self):
        self.calls = []

    def login(self, u, p):
        self.calls.append(("login", u))

    def select(self, name, readonly=False):
        self.calls.append(("select", name, readonly))
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        self.calls.append((cmd, *args))
        if cmd == "SEARCH":
            return "OK", [b"7"]
        if cmd == "FETCH" and "MESSAGE-ID" in args[1]:
            return "OK", [(b"7 (UID 7 BODY[HEADER.FIELDS (MESSAGE-ID)] {30}", HDR), b")"]
        if cmd == "FETCH":
            return "OK", [(b"7 (UID 7 BODY[] {5}", b"hello"), b")"]
        return "OK", [b""]

    def logout(self):
        self.calls.append(("logout",))


def test_imap_host_is_worked_out_from_the_smtp_host():
    assert imap_host_for("smtp.mail.me.com") == "imap.mail.me.com"
    assert imap_host_for("smtp.office365.com") == "outlook.office365.com"
    assert imap_host_for("smtp.fastmail.com") == "imap.fastmail.com"
    assert imap_host_for("mail.example.org") == "mail.example.org"
    assert imap_host_for("smtp.fastmail.com", "imap.custom.net") == "imap.custom.net"


def test_reads_the_inbox_read_only_using_standard_search_only():
    fake = FakeIMAP()
    with ImapReader("u@x.example", "pw", "imap.x.example", connect=lambda: fake) as r:
        r.label_replies(["privacy@broker.example", "bad address"], since=date(2026, 11, 1))
        raws = r.fetch_replies(set(), since=date(2026, 11, 1))
    assert raws == [b"hello"]
    assert ("select", "INBOX", True) in fake.calls  # read-only
    searches = [c for c in fake.calls if c[0] == "SEARCH"]
    assert all(c[2] != "X-GM-RAW" for c in searches)  # no Gmail extensions
    flat = " ".join(str(x) for c in searches for x in c)
    assert "SINCE 01-Nov-2026" in flat and "mailer-daemon" in flat
    assert '"privacy@broker.example"' in flat and "bad address" not in flat
    assert not any(c[0] == "STORE" for c in fake.calls)  # nothing is changed


def test_already_seen_messages_are_skipped_and_odd_ids_never_searched():
    fake = FakeIMAP()
    with ImapReader("u@x.example", "pw", "h", connect=lambda: fake) as r:
        assert r.fetch_replies({"<a@x.example>"}, since=date(2026, 11, 1)) == []
        assert r.fetch_message('<a" OR ALL>') is None
        assert r.mark_read_message_ids(["<a@x.example>"]) == 0


def test_new_replies_are_downloaded_in_one_request():
    class Many(FakeIMAP):
        def uid(self, cmd, *args):
            self.calls.append((cmd, *args))
            if cmd == "SEARCH":
                return "OK", [b"7 8"]
            if cmd == "FETCH" and "MESSAGE-ID" in args[1]:
                return "OK", [
                    (b"1 (UID 7 BODY[HEADER] {9}", b"Message-ID: <a@x>\r\n\r\n"),
                    b")",
                    (b"2 (UID 8 BODY[HEADER] {9}", b"Message-ID: <b@x>\r\n\r\n"),
                    b")",
                ]
            if cmd == "FETCH":
                return "OK", [
                    (b"1 (UID 7 BODY[] {1}", b"A"),
                    b")",
                    (b"2 (UID 8 BODY[] {1}", b"B"),
                    b")",
                ]
            return "OK", [b""]

    fake = Many()
    with ImapReader("u@x.example", "pw", "h", connect=lambda: fake) as r:
        assert r.fetch_replies(set(), since=date(2026, 11, 1)) == [b"A", b"B"]
    bodies = [c for c in fake.calls if c[0] == "FETCH" and "BODY.PEEK[]" in c[2]]
    assert len(bodies) == 1 and bodies[0][1] == "7,8"


def test_a_saved_reply_is_read_again_by_message_id():
    fake = FakeIMAP()
    with ImapReader("u@x.example", "pw", "h", connect=lambda: fake) as r:
        assert r.fetch_message("<a@x.example>") == b"hello"
        assert r.fetch_messages(["<a@x.example>", "<gone@x.example>"]) == {
            "<a@x.example>": b"hello"
        }
