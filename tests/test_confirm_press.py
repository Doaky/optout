"""Confirm flows that need a button pressed on the portal page (DataGrail's "Verify Email")."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from optout.db import InboxMessage, create_submission, get_session, init_db, update_status
from optout.engine import confirm as cf

EMAIL = (
    "Please verify your email address by following this link:\n"
    "https://portal.example-grail.io/verification?d=abc123\n"
)


@pytest.fixture()
def world():
    init_db()
    with get_session() as s:
        sub = create_submission(
            s,
            broker_slug="acme",
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
        update_status(s, sub, "awaiting_user_input")
        row = InboxMessage(
            message_id="<v1@x>",
            submission_id=sub.id,
            broker_slug="acme",
            from_addr="no-reply@acme.example",
            subject="Verify",
            text=EMAIL,
            kind="need-info",
            rule="x",
            state="applied",
            received_at=datetime.now(UTC),
        )
        s.add(row)
        s.commit()
        yield s, row


def _flow(press=None):
    return cf.Flow(
        "@acme.example",
        "please verify your email address by following this link",
        ("example-grail.io",),
        re.compile("verify"),
        "",
        re.compile(press, re.I) if press else None,
    )


def test_a_flow_can_name_the_button_to_press_and_only_then_is_it_passed_on(world):
    s, row = world
    seen = []

    def fetch(url, hosts, press=None):
        seen.append(press.pattern if press else None)
        return cf.Fetched(200, "Email verified. Identity Verified", "portal.example-grail.io")

    res, _ = cf.confirm_reply(s, row, flows=[_flow("verify email")], fetch=fetch)
    assert res == "confirmed" and seen == ["verify email"]


def test_a_flow_without_a_button_keeps_the_two_argument_fetch(world):
    s, row = world
    calls = []

    def fetch(url, hosts):  # an older stand-in with no `press`: must still work
        calls.append(url)
        return cf.Fetched(200, "Thank you, verified.", "portal.example-grail.io")

    assert cf.confirm_reply(s, row, flows=[_flow()], fetch=fetch)[0] == "confirmed" and calls


def test_press_is_read_from_the_flow_file():
    flows = cf._parse(
        [
            {
                "from": "@a.example",
                "phrase": "please verify your email address",
                "link_hosts": ["a.example"],
                "press": "verify email",
            }
        ]
    )
    assert flows[0].press is not None and flows[0].press.search("Verify Email")


def test_retry_overrides_the_attempt_cap_but_never_a_confirmed_one(world):
    s, row = world
    bad = lambda url, hosts, press=None: cf.Fetched(200, "Something went wrong, try again", "h")  # noqa: E731
    flows = [_flow("verify")]
    assert [cf.confirm_reply(s, row, flows=flows, fetch=bad)[0] for _ in range(3)] == [
        "failed",
        "failed",
        "already",
    ]  # two tries, then the cap
    good = lambda url, hosts, press=None: cf.Fetched(200, "Email verified", "h")  # noqa: E731
    assert cf.confirm_reply(s, row, flows=flows, fetch=good, retry=True)[0] == "confirmed"
    assert cf.confirm_reply(s, row, flows=flows, fetch=good, retry=True)[0] == "already"
