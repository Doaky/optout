from __future__ import annotations

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
        g.label_replies(["33across.com", "6sense.com"])
    verbs = {c[0] for c in fake.calls if c[0] in ("FETCH", "COPY", "EXPUNGE", "DELETE", "STORE")}
    assert verbs == {"STORE"}
    stores = [c for c in fake.calls if c[0] == "STORE"]
    assert all(c[2] == "+X-GM-LABELS" for c in stores)  # add-only, never replace/remove


def test_rejects_odd_message_ids_and_domains():
    fake = FakeIMAP()
    with _labeler(fake) as g:
        assert g.label_message_ids(['<a" OR in:anywhere@x.y>']) == 0
        assert g.label_replies(["bad domain", "x.com) OR (from:y"]) == 0
    assert not any(c[0] == "SEARCH" for c in fake.calls)


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
