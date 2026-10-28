"""Verification: 'removed' is only reachable through a verifying re-search."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlmodel import Session, create_engine

from optout.brokers.schema import BrokerDef
from optout.config import Address, EmailSet, PhoneSet, Profile
from optout.db import (
    VerificationCheck,
    create_submission,
    init_db,
    is_drifted,
    mark_removed,
    mark_submitted,
    record_health,
    record_verification,
    update_status,
)
from optout.engine.verify import run_verification, verify_listing

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


@pytest.fixture()
def profile():
    return Profile(
        legal_name="Jane Q Public",
        dob=date(1990, 1, 1),
        current_address=Address(street="1 A St", city="Richmond", state="VA", zip="23219"),
        phones=PhoneSet(),
        emails=EmailSet(),
    )


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    init_db(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


def _broker(scan=True, empty=None) -> BrokerDef:
    data = dict(
        slug="ppl",
        name="Ppl",
        domain="ppl.example",
        category="people_search",
        method="web_form",
        opt_out_url="https://ppl.example/optout",
        legal_basis=["VCDPA"],
        statutory_response_days=45,
    )
    if scan:
        data["scan"] = {
            "search_url_template": "https://ppl.example/{name_slug}",
            "result_selector": ".card",
            **({"empty_selector": empty} if empty else {}),
        }
    return BrokerDef.model_validate(data)


class El:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text

    def get_attribute(self, _):
        return None

    def query_selector(self, _):
        return None


class Page:
    def __init__(self, cards=(), title="People search", timeout=False):
        self.cards, self._title, self.timeout = list(cards), title, timeout

    def goto(self, *a, **k):
        pass

    def wait_for_selector(self, *a, **k):
        if self.timeout:
            from playwright.sync_api import TimeoutError as PWTimeout

            raise PWTimeout("timeout")

    def query_selector_all(self, _):
        return [El(t) for t in self.cards]

    def title(self):
        return self._title


def _sent(session, days_ago=10) -> object:
    sub = create_submission(
        session,
        broker_slug="ppl",
        method_used="web_form",
        legal_basis_cited=["VCDPA"],
        statutory_response_days=45,
    )
    mark_submitted(session, sub, {}, None, 45, submitted_at=NOW - timedelta(days=days_ago))
    update_status(session, sub, "awaiting_broker_confirmation")
    return sub


# --- the guard itself -------------------------------------------------------


def test_update_status_cannot_set_removed(session):
    sub = _sent(session)
    with pytest.raises(ValueError, match="mark_removed"):
        update_status(session, sub, "removed")


@pytest.mark.parametrize("outcome", ["still_listed", "unverifiable"])
def test_mark_removed_rejects_non_clear_outcomes(session, outcome):
    sub = _sent(session)
    check = record_verification(session, sub, outcome)
    with pytest.raises(ValueError, match="verification outcome"):
        mark_removed(session, sub, check)
    assert sub.status == "awaiting_broker_confirmation"


def test_mark_removed_rejects_check_from_before_the_request(session):
    sub = _sent(session)
    stale = VerificationCheck(
        submission_id=sub.id,
        broker_slug="ppl",
        outcome="verified_clear",
        checked_at=NOW - timedelta(days=30),
    )
    with pytest.raises(ValueError, match="after the request"):
        mark_removed(session, sub, stale)


def test_mark_removed_rejects_other_submissions_check(session):
    a, b = _sent(session), _sent(session)
    check = record_verification(session, a, "verified_clear")
    with pytest.raises(ValueError, match="different submission"):
        mark_removed(session, b, check)


def test_mark_removed_happy_path(session):
    sub = _sent(session)
    check = record_verification(session, sub, "verified_clear")
    mark_removed(session, sub, check)
    assert sub.status == "removed"


# --- classification: ambiguity is never 'clear' -----------------------------


def test_clear_when_results_render_without_user(profile):
    r = verify_listing(Page(cards=["John Doe, Richmond VA"]), _broker(), profile)
    assert r.outcome == "verified_clear"


def test_still_listed_when_user_appears(profile):
    r = verify_listing(Page(cards=["Jane Q Public, Richmond VA"]), _broker(), profile)
    assert r.outcome == "still_listed"


def test_timeout_is_unverifiable_not_clear(profile):
    assert verify_listing(Page(timeout=True), _broker(), profile).outcome == "unverifiable"


@pytest.mark.parametrize(
    "title",
    ["Just a moment...", "Access Denied", "This Domain Has Been Transferred by Court Order"],
)
def test_block_pages_are_unverifiable_even_if_empty(profile, title):
    r = verify_listing(Page(cards=[], title=title), _broker(), profile)
    assert r.outcome == "unverifiable" and r.blocked


def test_no_scan_config_is_unverifiable(profile):
    assert verify_listing(Page(), _broker(scan=False), profile).outcome == "unverifiable"


def test_empty_selector_lets_a_no_results_page_count_as_clear(profile):
    r = verify_listing(Page(cards=[]), _broker(empty=".no-results"), profile)
    assert r.outcome == "verified_clear"


# --- end to end through run_verification ------------------------------------


def _run(session, sub, page, profile, **kw):
    return run_verification(
        session, [sub], lambda slug: _broker(), profile, lambda b: page, now=NOW, **kw
    )


def test_not_due_before_seven_days(session, profile):
    sub = _sent(session, days_ago=3)
    rep = _run(session, sub, Page(cards=["John Doe"]), profile)
    assert rep.not_due == 1 and sub.status == "awaiting_broker_confirmation"


def test_clear_after_seven_days_marks_removed(session, profile):
    sub = _sent(session, days_ago=8)
    rep = _run(session, sub, Page(cards=["John Doe"]), profile)
    assert rep.removed == ["ppl"] and sub.status == "removed"


def test_still_listed_stays_open_and_rechecks_later(session, profile):
    sub = _sent(session, days_ago=8)
    rep = _run(session, sub, Page(cards=["Jane Q Public"]), profile)
    assert rep.still_listed == ["ppl"] and sub.status == "awaiting_broker_confirmation"
    assert _run(session, sub, Page(cards=["Jane Q Public"]), profile).not_due == 1  # same day


def test_blocked_verification_leaves_status_and_is_not_drift(session, profile):
    sub = _sent(session, days_ago=8)
    rep = _run(session, sub, Page(title="Just a moment..."), profile)
    assert rep.unverifiable and sub.status == "awaiting_broker_confirmation"
    assert not is_drifted(session, "ppl")


# --- drift ------------------------------------------------------------------


def test_drift_after_three_consecutive_failures(session):
    for ok in (True, False, False):
        record_health(session, "ppl", "probe", ok)
    assert not is_drifted(session, "ppl")
    record_health(session, "ppl", "probe", False)
    assert is_drifted(session, "ppl")
    record_health(session, "ppl", "probe", True)
    assert not is_drifted(session, "ppl")
