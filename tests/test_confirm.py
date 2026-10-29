"""Confirming 'click the button in this email': recognised senders only, strictly bounded."""

from __future__ import annotations

import urllib.error

import pytest
from sqlmodel import Session, create_engine

from optout.db import (
    InboxMessage,
    create_submission,
    init_db,
    list_events,
    mark_submitted,
    update_status,
)
from optout.engine import confirm as cf
from optout.engine.confirm import (
    Fetched,
    confirm_reply,
    find_link,
    flow_for,
    host_allowed,
    judge,
    load_flows,
    propose_flow,
)

TEXT = (
    "ROR Partners\n\nDear Jane,\n\nOne more step is required before we can start working on "
    "your request. Please confirm your email by clicking on the button below.\n\n"
    "Request ID: 76HHC55DDN\n\nConfirm email "
    "(https://rorpartners-privacy.my.onetrust.com/privacyaccess/verify/abc123)\n\n"
    "Unsubscribe (https://evil.example/unsub)\n"
)
SENDER = "noreply@m.onetrust.com"


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    init_db(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def pending(session):
    sub = create_submission(
        session,
        broker_slug="activimpact",
        method_used="email",
        legal_basis_cited=[],
        statutory_response_days=45,
    )
    mark_submitted(session, sub, {}, None, 45)
    update_status(session, sub, "awaiting_user_input")
    row = InboxMessage(
        message_id="<c1@x>",
        submission_id=sub.id,
        broker_slug="activimpact",
        from_addr=SENDER,
        subject="Your Privacy Request",
        text=TEXT,
        kind="need-info",
        state="applied",
    )
    session.add(row)
    session.commit()
    return sub, row


def fetcher(result: Fetched, calls: list):
    def fetch(url, hosts):
        calls.append((url, hosts))
        return result

    return fetch


OK = Fetched(200, "Thank you. Your email has been verified.", "rorpartners-privacy.my.onetrust.com")


# --- what counts as a safe link -------------------------------------------------


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://rorpartners-privacy.my.onetrust.com/privacyaccess/x", True),
        ("https://onetrust.com/x", True),
        ("http://rorpartners-privacy.my.onetrust.com/x", False),  # not HTTPS
        ("https://evilonetrust.com/x", False),  # lookalike, not a subdomain
        ("https://onetrust.com.evil.example/x", False),
        ("https://user:pw@my.onetrust.com/x", False),  # credentials in the address
        ("https://93.184.216.34/x", False),  # a bare IP
        ("javascript:alert(1)", False),
        ("https://my.onetrust.com/" + "a" * 4000, False),  # absurdly long
    ],
)
def test_host_allowed(url, ok):
    assert host_allowed(url, ("onetrust.com",)) is ok


def test_flow_needs_both_the_sender_and_the_wording():
    flows = load_flows()
    assert flow_for(SENDER, TEXT, flows) is not None
    assert flow_for("noreply@evil.example", TEXT, flows) is None  # right words, wrong sender
    assert flow_for(SENDER, "Your newsletter is here.", flows) is None  # right sender, no match
    # strict: a subdomain of the sender is a different mailbox, so it doesn't count
    assert flow_for("x@sub.m.onetrust.com", TEXT, flows) is None


def test_only_the_labelled_link_on_an_allowed_host_is_found():
    flow = flow_for(SENDER, TEXT, load_flows())
    assert find_link(TEXT, flow) == (
        "https://rorpartners-privacy.my.onetrust.com/privacyaccess/verify/abc123"
    )
    hostile = TEXT.replace("rorpartners-privacy.my.onetrust.com", "phish.example")
    assert find_link(hostile, flow) is None  # right label, wrong site: nothing is offered


def test_an_unknown_sender_can_be_taught_but_only_on_a_confirm_labelled_link():
    sender, host, url = propose_flow(
        "noreply@newplatform.example",
        TEXT.replace("rorpartners-privacy.my.onetrust.com", "tickets.my.newplatform.example"),
    )
    assert (sender, host) == ("@newplatform.example", "my.newplatform.example")
    assert propose_flow("a@b.example", "See https://b.example/page for details") is None


def test_saved_flows_must_be_specific_and_are_not_re_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ValueError):
        cf.save_local_flow(
            "noreply@x.example", "please confirm your email address now", "x.example"
        )
    with pytest.raises(ValueError):
        cf.save_local_flow("@x.example", "too short", "x.example")
    cf.save_local_flow("@x.example", "please confirm your email address now", "x.example")
    cf.save_local_flow(
        "@m.onetrust.com",
        "please confirm your email by clicking on the button below",
        "onetrust.com",
    )  # already shipped
    shared = [(e["from"]) for e in cf.learned_flows()]
    assert shared == ["@x.example"]
    assert any(f.sender == "@x.example" for f in load_flows())


# --- reading the page ------------------------------------------------------------


@pytest.mark.parametrize(
    "fetched,expected",
    [
        (Fetched(200, "Thank you, your email has been verified."), "confirmed"),
        (Fetched(200, "This email was already verified."), "confirmed"),
        (Fetched(200, "This link has expired. Please try again."), "failed"),
        (Fetched(200, "<div id='app'></div>"), "unclear"),  # a page we can't read
        (Fetched(404, "Not found"), "failed"),
        (Fetched(0, "", error="URLError"), "failed"),
    ],
)
def test_judge_only_trusts_a_page_that_plainly_says_so(fetched, expected):
    assert judge(fetched) == expected


def test_redirects_off_the_allowed_site_are_refused():
    handler = cf._SameSite(("onetrust.com",))
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/steal")

    # a redirect within the platform is fine
    class Req:
        full_url = "https://a.onetrust.com/x"
        headers: dict = {}
        origin_req_host = "a.onetrust.com"
        unverifiable = False
        data = None

        def get_method(self):
            return "GET"

    assert handler.redirect_request(Req(), None, 302, "Found", {}, "https://b.onetrust.com/y")


# --- doing it --------------------------------------------------------------------


def test_a_confirmed_link_returns_the_request_to_awaiting_a_reply(session, pending):
    sub, row = pending
    calls: list = []
    result, host = confirm_reply(session, row, flows=load_flows(), fetch=fetcher(OK, calls))
    assert (result, host) == ("confirmed", "rorpartners-privacy.my.onetrust.com")
    assert sub.status == "awaiting_broker_confirmation" and row.state == "resolved"
    assert calls[0][0].startswith("https://rorpartners-privacy.my.onetrust.com/")
    assert calls[0][1] == ("onetrust.com",)  # the fetcher is told which hosts are allowed
    assert [e.event_type for e in list_events(session, sub.id)].count("confirm_clicked") == 1
    # a second press does not open it again
    again = confirm_reply(session, row, flows=load_flows(), fetch=fetcher(OK, calls))
    assert again[0] == "already" and len(calls) == 1


def test_an_unclear_page_leaves_the_request_waiting_for_you(session, pending):
    sub, row = pending
    result, _ = confirm_reply(
        session, row, flows=load_flows(), fetch=fetcher(Fetched(200, "<div></div>"), [])
    )
    assert result == "unclear" and sub.status == "awaiting_user_input" and row.state == "applied"


def test_failures_are_retried_at_most_twice(session, pending):
    _, row = pending
    calls: list = []
    bad = fetcher(Fetched(500, "oops"), calls)
    results = [confirm_reply(session, row, flows=load_flows(), fetch=bad)[0] for _ in range(4)]
    assert results == ["failed", "failed", "already", "already"] and len(calls) == 2


def test_nothing_is_opened_for_an_unrecognised_sender(session, pending):
    _, row = pending
    row.from_addr = "noreply@evil.example"
    calls: list = []
    result, _ = confirm_reply(session, row, flows=load_flows(), fetch=fetcher(OK, calls))
    assert result == "no-flow" and calls == []


def test_auto_confirm_clicks_only_known_flows_and_respects_the_cap(session, pending):
    calls: list = []
    sub, row = pending
    other = InboxMessage(
        message_id="<c2@x>",
        submission_id=sub.id,
        broker_slug="activimpact",
        from_addr="a@unknown.example",
        subject="x",
        kind="need-info",
        text="Please confirm your email by clicking the button.",
        state="applied",
    )
    session.add(other)
    session.commit()
    done, unclear = cf.auto_confirm(session, load_flows(), fetch=fetcher(OK, calls), limit=5)
    assert (done, unclear) == (1, 0) and len(calls) == 1  # the unknown sender is left alone


def test_auto_close_only_for_groups_you_are_not_in(session):
    from optout.config import EmailConfig, SmtpConfig, UserConfig
    from optout.engine.inbox import auto_close_out_of_scope

    def waiting(slug, status):
        sub = create_submission(
            session,
            broker_slug=slug,
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
        mark_submitted(session, sub, {}, None, 45)
        update_status(session, sub, status)
        return sub

    agile = waiting("agile-education-marketing", "awaiting_user_input")  # educators only
    general = waiting("acxiom", "awaiting_user_input")  # covers everyone
    done = waiting("advisortarget", "confirmed")  # niche but already settled: leave it

    class Cfg:
        my_groups: list[str] | None = None

    assert auto_close_out_of_scope(session, Cfg()) == 0  # groups not set: nothing happens
    Cfg.my_groups = ["students"]
    assert auto_close_out_of_scope(session, Cfg()) == 1
    assert (agile.status, general.status, done.status) == (
        "closed",
        "awaiting_user_input",
        "confirmed",
    )
    Cfg.my_groups = ["educators"]
    again = waiting("agile-education-marketing", "awaiting_user_input")
    assert auto_close_out_of_scope(session, Cfg()) == 0 and again.status == "awaiting_user_input"
    del EmailConfig, SmtpConfig, UserConfig


def test_teach_phrase_and_tracker_redirects():
    from optout.engine.confirm import redirect_ok, teach_phrase

    text = "Email Verification\n\nYou must click on the following secure link to verify your email address.\n"
    assert teach_phrase(text) == "you must click on the following secure link to verify your email address."
    assert teach_phrase("Hello there.") == ""
    # A click-tracker may redirect to the real page; an ordinary site may not leave its hosts.
    assert redirect_ok("https://portal.osano.com/x", ("track.pstmrk.it",))
    assert not redirect_ok("http://portal.osano.com/x", ("track.pstmrk.it",))
    assert not redirect_ok("https://evil.example/x", ("onetrust.com",))
