"""The plan: which group each broker is in, and what moves it between groups."""

from __future__ import annotations

import pytest

from optout.brokers.schema import BrokerDef
from optout.brokers.traits import says_form_only
from optout.plan import tier


@pytest.fixture()
def session():
    from optout.db import get_session, init_db

    init_db()
    with get_session() as s:
        yield s


def _b(**kw):
    base = {
        "slug": "x",
        "name": "X",
        "domain": "x.example",
        "category": "data_broker",
        "method": "email",
        "opt_out_email": "p@x.example",
        "legal_basis": ["CCPA"],
        "statutory_response_days": 45,
    }
    return BrokerDef.model_validate({**base, **kw})


def test_each_kind_of_broker_lands_in_its_group():
    assert tier(_b()) == "email"
    assert tier(_b(requires_email_verification=True)) == "email-confirm"
    assert tier(_b(form_only=True)) == "form"
    assert tier(_b(method="manual", opt_out_url="https://x.example/f")) == "form"
    assert tier(_b(form_only=True, requires_phone_verification=True)) == "form-plus"
    assert tier(_b(category="people_search", method="manual")) == "people-search"
    assert tier(_b(requires_id_upload=True)) == "too-much"
    assert tier(_b(requires_device_ids=True, category="people_search")) == "too-much"


def test_form_only_is_read_from_their_wording():
    assert says_form_only("We do not accept privacy requests received via email.")
    assert says_form_only("Requests must be submitted through our Privacy Rights Web Form.")
    assert says_form_only("Please submit your request through our online form.")
    assert says_form_only("This inbox is not monitored for privacy requests.")
    assert not says_form_only("Thank you, we have received your request and will respond.")
    assert not says_form_only("Your data has been deleted.")


def test_form_only_and_too_much_brokers_are_not_emailed(session):
    from datetime import UTC, datetime

    from optout.engine.campaign import select_brokers

    form = _b(slug="f", form_only=True)
    face = _b(slug="g", requires_id_upload=True)
    plain = _b(slug="p")
    sel = select_brokers([form, face, plain], session, now=datetime(2026, 11, 1, tzinfo=UTC))
    assert [b.slug for b in sel.eligible] == ["p"]
    reasons = dict(sel.skipped)
    assert "web form" in reasons["f"] and "asks too much" in reasons["g"]


def test_an_email_broker_that_asked_for_more_leaves_email_only(session):
    from optout.db import Submission
    from optout.plan import summarise

    a, b = _b(slug="a"), _b(slug="b")
    session.add(Submission(broker_slug="a", status="awaiting_user_input", method_used="email"))
    session.add(Submission(broker_slug="b", status="submitted", method_used="email"))
    session.commit()
    by = {t.key: t for t in summarise(session, [a, b])}
    assert (by["email"].total, by["email"].needs_you, by["email"].under_way) == (1, 0, 1)
    assert (by["follow-up"].total, by["follow-up"].needs_you) == (1, 1)


def test_a_refused_email_broker_is_not_counted_as_not_started(session):
    from optout.db import Submission
    from optout.plan import summarise

    session.add(Submission(broker_slug="r", status="rejected", method_used="email"))
    session.commit()
    t = {x.key: x for x in summarise(session, [_b(slug="r")])}["email"]
    assert (t.refused, t.todo, t.needs_you) == (1, [], 0)
