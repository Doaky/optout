from __future__ import annotations

from datetime import date

import pytest

from optout.config import EmailConfig, SmtpConfig
from optout.gmail_label import GmailLabeler, valid_label


class FakeIMAP:
    """Records commands; pretends Gmail files a message after `delay` searches."""

    def __init__(self, hits=None, delay=0):
        self.calls, self.hits, self.delay, self.searches = [], hits or {}, delay, 0

    def login(self, u, p):
        self.calls.append(("login", u))

    def list(self):
        return "OK", [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"']

    def select(self, name):
        self.calls.append(("select", name))
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        self.calls.append((cmd, *args))
        if cmd == "SEARCH":
            self.searches += 1
            if self.searches <= self.delay:
                return "OK", [b""]
            return "OK", [self.hits.get(args[1].strip('"'), b"5 6")]
        return "OK", [b""]

    def logout(self):
        self.calls.append(("logout",))


def _labeler(fake, label="OPTOUT"):
    return GmailLabeler("u@gmail.com", "pw", label, connect=lambda: fake)


def test_opens_all_mail_by_flag_and_labels_by_message_id():
    fake = FakeIMAP()
    with _labeler(fake) as g:
        assert g.label_message_ids(["<abc.123@mail.gmail.com>"]) == 2
    assert ("select", '"[Gmail]/All Mail"') in fake.calls
    assert ("SEARCH", "X-GM-RAW", '"rfc822msgid:abc.123@mail.gmail.com"') in fake.calls
    assert ("STORE", "5,6", "+X-GM-LABELS", '("OPTOUT")') in fake.calls
    assert fake.calls[-1] == ("logout",)


def test_waits_for_gmail_to_file_the_message(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeIMAP(delay=2)
    with _labeler(fake) as g:
        assert g.label_message_ids(["<x@y.z>"]) == 2
    assert fake.searches == 3


def test_gives_up_quietly_when_message_never_appears(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeIMAP(delay=99)
    with _labeler(fake) as g:
        assert g.label_message_ids(["<x@y.z>"]) == 0


def test_only_search_and_label_never_fetch_or_delete():
    fake = FakeIMAP()
    with _labeler(fake) as g:
        g.label_all_sent()
        g.label_replies(["privacy@33across.com", "dsar@6sense.com"], since=date(2026, 10, 27))
    verbs = {c[0] for c in fake.calls if c[0] in ("FETCH", "COPY", "EXPUNGE", "DELETE", "STORE")}
    assert verbs == {"STORE"}
    stores = [c for c in fake.calls if c[0] == "STORE"]
    assert all(c[2] == "+X-GM-LABELS" for c in stores)  # add-only, never replace/remove


def test_rejects_odd_message_ids_and_domains():
    fake = FakeIMAP()
    with _labeler(fake) as g:
        assert g.label_message_ids(['<a" OR in:anywhere@x.y>']) == 0
        g.label_replies(["bad address", "x@y.com) OR (from:z"], since=date(2026, 10, 27))
    searches = [c[2] for c in fake.calls if c[0] == "SEARCH"]
    # only the subject search ran; odd "addresses" never reached a search expression
    assert len(searches) == 1 and "from:" not in searches[0].replace("-from:me", "")


@pytest.mark.parametrize("bad", ['OPT"OUT', "OPT\\OUT", "OPT\nOUT", "", "x" * 61])
def test_label_name_is_validated(bad):
    assert not valid_label(bad)
    with pytest.raises(ValueError):
        EmailConfig(method="smtp", smtp=SmtpConfig(host="h", username="u"), gmail_label=bad)


def test_label_failure_never_fails_the_send(monkeypatch, tmp_path):
    from optout.engine.methods import email as em

    class Boom:
        def __init__(self, *a, **k):
            raise OSError("imap down")

    monkeypatch.setattr("optout.gmail_label.GmailLabeler", Boom)
    cfg = EmailConfig(
        method="smtp",
        smtp=SmtpConfig(host="smtp.gmail.com", username="u@gmail.com"),
        gmail_label="OPTOUT",
    )

    class C:
        email = cfg

    assert em._try_label(C(), cfg.smtp, "<m@id>") == "label-failed: OSError"
    other = SmtpConfig(host="smtp.example.invalid", username="u")
    assert em._try_label(C(), other, "<m@id>") is None  # not Gmail: skipped


def test_marks_only_labelled_broker_replies_read_and_only_when_asked():
    fake = FakeIMAP()
    with _labeler(fake) as g:
        g.label_all_sent()
        g.label_replies(["privacy@33across.com"], since=date(2026, 10, 27))
    assert not [c for c in fake.calls if c[0] == "STORE" and c[2] == "+FLAGS"]  # off by default

    fake = FakeIMAP()
    with GmailLabeler("u@gmail.com", "pw", "OPTOUT", connect=lambda: fake, mark_read=True) as g:
        g.label_all_sent()  # our own sent mail: labelled, never flagged
        sent_stores = len([c for c in fake.calls if c[0] == "STORE"])
        g.label_replies(["privacy@33across.com"], since=date(2026, 10, 27))
    flags = [c for c in fake.calls if c[0] == "STORE" and c[2] == "+FLAGS"]
    assert flags and all(c[3] == "(\\Seen)" for c in flags)  # add-only: read, nothing removed
    assert sent_stores == 1  # the sent-mail labelling added no flags


def test_mark_read_by_message_id_only_adds_the_read_flag():
    fake = FakeIMAP()
    with _labeler(fake) as g:
        assert g.mark_read_message_ids(["<abc.123@mail.gmail.com>", '<a" OR in:anywhere@x.y>']) == 2
    stores = [c for c in fake.calls if c[0] == "STORE"]
    assert stores == [("STORE", "5,6", "+FLAGS", "(\\Seen)")]  # one message, flag added only
    searches = [c[2] for c in fake.calls if c[0] == "SEARCH"]
    assert searches == ['"rfc822msgid:abc.123@mail.gmail.com"']  # the odd id never searched


def test_reply_labelling_batches_addresses_and_skips_mail_already_labelled():
    fake = FakeIMAP()
    addresses = [f"privacy{i}@broker{i}.com" for i in range(250)]
    g = GmailLabeler(
        "u@gmail.com", "pw", "OPTOUT", connect=lambda: fake, reply_label="OPTOUT Response"
    )
    with g:
        g.label_replies(addresses, since=date(2026, 10, 27))
    searches = [c[2] for c in fake.calls if c[0] == "SEARCH"]
    assert len(searches) == 1 + 3  # the subject search, then 100 addresses per search
    assert all("-label:OPTOUT-Response" in s for s in searches)
    assert sum(s.count("from:privacy") for s in searches) == 250


class HeaderIMAP(FakeIMAP):
    """Answers a header FETCH with Message-ID and From for each requested UID."""

    def __init__(self, heads):
        super().__init__()
        self.heads = heads  # uid -> (message id, from header)

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.calls.append((cmd, *args))
            return "OK", [b" ".join(self.heads)]
        if cmd == "FETCH":
            self.calls.append((cmd, *args))
            out = []
            for u in args[0].encode().split(b","):
                mid, frm = self.heads[u]
                out.append(
                    (
                        b"%s (UID %s BODY[...]" % (u, u),
                        f"Message-ID: <{mid}>\r\nFrom: {frm}\r\n\r\n".encode(),
                    )
                )
                out.append(b")")
            return "OK", out
        return super().uid(cmd, *args)


def test_display_names_are_looked_up_in_one_search_and_one_fetch():
    fake = HeaderIMAP(
        {
            b"7": ("a.1@x.com", '"Acme Privacy" <privacy@acme.com>'),
            b"8": ("b.2@y.com", "privacy@bare.com"),
            b"9": ("unrelated@z.com", '"Someone Else" <s@z.com>'),
        }
    )
    with _labeler(fake) as g:
        names = g.display_names(["<a.1@x.com>", "<b.2@y.com>", "bad id with spaces"])
    assert names == {"<a.1@x.com>": "Acme Privacy", "<b.2@y.com>": ""}
    verbs = [c[0] for c in fake.calls if c[0] in ("SEARCH", "FETCH")]
    assert verbs == ["SEARCH", "FETCH"]
    query = next(c[2] for c in fake.calls if c[0] == "SEARCH")
    assert query == '"rfc822msgid:a.1@x.com OR rfc822msgid:b.2@y.com"'


class BodyIMAP(FakeIMAP):
    """Search finds UIDs 7, 8, 9; headers give each a Message-ID; bodies come back in one
    response. `uid_after` puts the UID after the body, as some servers do."""

    def __init__(self, uid_after=False):
        super().__init__()
        self.uid_after = uid_after

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.calls.append((cmd, *args))
            return "OK", [b"7 8 9"]
        if cmd == "FETCH" and "HEADER.FIELDS" in args[1]:
            self.calls.append((cmd, *args))
            out = []
            for u in args[0].encode().split(b","):
                out += [
                    (b"1 (UID %s BODY[HEADER] {9}" % u, b"Message-ID: <m%s@x>\r\n\r\n" % u),
                    b")",
                ]
            return "OK", out
        if cmd == "FETCH":
            self.calls.append((cmd, *args))
            out = []
            for u in args[0].encode().split(b","):
                if self.uid_after:
                    out += [(b"1 (BODY[]<0> {5}", b"RAW-" + u), b" UID %s)" % u]
                else:
                    out += [(b"1 (UID %s BODY[]<0> {5}" % u, b"RAW-" + u), b")"]
            return "OK", out
        return super().uid(cmd, *args)


@pytest.mark.parametrize("uid_after", [False, True])
def test_new_replies_are_downloaded_in_one_request_in_order(uid_after):
    fake = BodyIMAP(uid_after)
    with _labeler(fake) as g:
        raws = g.fetch_replies({"<m8@x>"}, since=date(2026, 11, 1))
    assert raws == [b"RAW-7", b"RAW-9"]  # the one already seen is skipped
    bodies = [c for c in fake.calls if c[0] == "FETCH" and "BODY.PEEK[]" in c[2]]
    assert len(bodies) == 1 and bodies[0][1] == "7,9"  # one round trip, not one per message


def test_marking_read_reuses_what_was_just_read_and_batches_the_rest():
    fake = BodyIMAP()
    with _labeler(fake) as g:
        g.fetch_replies(set(), since=date(2026, 11, 1))
        searches_before = sum(1 for c in fake.calls if c[0] == "SEARCH")
        n = g.mark_read_message_ids(["<m7@x>", "<m9@x>", "<other1@x>", "<other2@x>", "bad id"])
    searches = [c for c in fake.calls if c[0] == "SEARCH"][searches_before:]
    assert len(searches) == 1  # only the two not read this session, in one search
    assert "rfc822msgid:other1@x OR rfc822msgid:other2@x" in searches[0][2]
    stores = [c for c in fake.calls if c[0] == "STORE"]
    assert len(stores) == 1 and stores[0][2:] == ("+FLAGS", "(\\Seen)")
    assert set(stores[0][1].split(",")) == {"7", "9", "8"} and n == 3
