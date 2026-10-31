"""Statute citations: every quote matches the state's official text (saved copies offline,
the live pages with OPTOUT_LIVE_STATUTES=1), and only checked laws are ever cited."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from optout.utils import statute_check
from optout.utils.statutes import STATE_LAW, for_state

FIXTURES = Path(__file__).parent / "fixtures" / "statutes"


def _saved_texts() -> dict[str, str]:
    """url -> saved official text, from the fixture files ("# url" headers)."""
    out: dict[str, str] = {}
    for f in FIXTURES.glob("*.txt"):
        for block in f.read_text().split("\n\n# "):
            url, _, text = block.removeprefix("# ").partition("\n")
            out[url] = out.get(url, "") + " " + text
    return out


@pytest.mark.parametrize("entry", statute_check.entries(), ids=lambda e: e["key"])
def test_every_quote_is_in_the_official_text(entry):
    r = statute_check.check(entry, _saved_texts())
    assert r.problems == [], r.problems


def test_a_changed_quote_is_caught():
    entry = dict(statute_check.entries()[0])
    entry["quote"] = entry["quote"] + " and anything else we wish it said"
    assert statute_check.check(entry, _saved_texts()).problems


def test_a_wrong_deadline_number_is_caught():
    entry = dict(next(e for e in statute_check.entries() if e["state"] == "VA"))
    entry["response_days"] = 30
    assert "response_days" in " ".join(statute_check.check(entry, _saved_texts()).problems)


@pytest.mark.skipif(not os.environ.get("OPTOUT_LIVE_STATUTES"), reason="fetches official sites")
def test_live_official_text_still_matches():
    bad = [(r.state, r.problems) for r in statute_check.verify() if r.problems]
    assert bad == []


def test_only_verified_laws_are_cited_by_state():
    verified = {e["state"] for e in statute_check.entries() if e.get("verified")}
    assert set(STATE_LAW) == verified
    assert for_state("VA").citation == "Va. Code § 59.1-577(A)(3)"
    assert for_state("ca") is None  # not checked yet: general wording, no citation
    assert for_state("RI").response_days == 0  # deadline not checked: no number claimed


def _draft(kind, state):
    from optout.brokers.schema import BrokerDef
    from optout.config import Address, EmailSet, PhoneSet, Profile
    from optout.db import Submission
    from optout.engine.escalation import make_draft

    profile = Profile(
        legal_name="Jane Q Public",
        dob=date(1990, 5, 14),
        current_address=Address(street="1 Main St", city="Town", state=state, zip="00000"),
        phones=PhoneSet(),
        emails=EmailSet(current=["jane@example.invalid"]),
    )
    broker = BrokerDef(
        slug="acme",
        name="Acme",
        domain="acme.example",
        category="data_broker",
        method="email",
        opt_out_email="p@acme.example",
        legal_basis=["CCPA"],
        statutory_response_days=45,
    )
    sub = Submission(
        broker_slug="acme",
        status="rejected",
        method_used="email",
        submitted_at=datetime(2026, 9, 1, tzinfo=UTC),
        deadline_at=datetime(2026, 10, 16, tzinfo=UTC),
    )
    return make_draft(kind, sub, broker, profile, contact_email="jane@example.invalid").body


def test_follow_ups_cite_the_users_own_verified_law():
    assert "Tex. Bus. & Com. Code § 541.052" in _draft("followup", "TX")
    assert "Va. Code § 59.1-577(B)(1)" in _draft("followup", "VA")
    general = _draft("followup", "CA")
    assert "§" not in general and "generally require a response within 45 days" in general
    assert "within 60 days" in _draft("appeal", "MT")  # Montana sets an appeal deadline
    assert "§" not in _draft("appeal", "TX")  # Texas's appeal deadline isn't checked yet


def _request_body(state, minimal):
    from optout.brokers.schema import BrokerDef
    from optout.config import Address, EmailSet, PhoneSet, Profile
    from optout.engine.methods.email import _render_body

    profile = Profile(
        legal_name="Jane Q Public",
        dob=date(1990, 5, 14),
        current_address=Address(street="1 Main St", city="Town", state=state, zip="00000"),
        phones=PhoneSet(),
        emails=EmailSet(current=["jane@example.invalid"]),
    )
    broker = BrokerDef(
        slug="acme",
        name="Acme",
        domain="acme.example",
        category="data_broker",
        method="email",
        opt_out_email="p@acme.example",
        legal_basis=["CCPA"],
        statutory_response_days=45,
        source="ca-registry",  # bulk-imported: cites the user's own state's law
    )
    return _render_body(broker, profile, contact_email="jane@example.invalid", minimal=minimal)


@pytest.mark.parametrize("minimal", [False, True])
def test_request_email_cites_the_users_state_law_and_claims_only_what_was_checked(minimal):
    tx = _request_body("TX", minimal)
    assert "Tex. Bus. & Com. Code § 541.051(b)(3)" in tx
    assert '"delete personal data provided by or obtained about the consumer."' in tx
    ri = _request_body("RI", minimal)
    assert "R.I. Gen. Laws § 6-48.1-5(e)(2)" in ri
    assert "Statutory response deadline: 0" not in ri  # its deadline isn't checked: not stated
    ca = _request_body("CA", minimal)
    assert "§" not in ca  # no verified citation for California yet: general wording
