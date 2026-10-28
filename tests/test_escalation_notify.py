from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from optout.brokers.schema import BrokerDef
from optout.config import Address, EmailSet, PhoneSet, Profile
from optout.db import Submission
from optout.engine.escalation import AG_COMPLAINT_URL, make_draft, stage_for
from optout.notify import send_webhook

NOW = datetime(2026, 10, 28, tzinfo=UTC)


def _sub(status="awaiting_broker_confirmation", deadline_days=-5):
    return Submission(
        broker_slug="acme",
        status=status,
        method_used="email",
        submitted_at=NOW - timedelta(days=50),
        deadline_at=NOW + timedelta(days=deadline_days),
    )


@pytest.fixture()
def profile():
    return Profile(
        legal_name="Jane Q Public",
        dob=date(1990, 1, 1),
        current_address=Address(street="1 A St", city="Richmond", state="VA", zip="23219"),
        phones=PhoneSet(current=["+15125550000"]),
        emails=EmailSet(),
    )


@pytest.fixture()
def broker():
    return BrokerDef(
        slug="acme",
        name="Acme",
        domain="acme.example",
        category="data_broker",
        method="email",
        opt_out_email="privacy@acme.example",
        legal_basis=["VCDPA"],
        statutory_response_days=45,
    )


def test_stage_progression():
    assert stage_for(_sub(deadline_days=+5), [], NOW) is None  # not overdue
    assert stage_for(_sub(), [], NOW) == "followup"
    assert stage_for(_sub(), ["followup"], NOW) is None  # grace period
    assert stage_for(_sub(deadline_days=-20), ["followup"], NOW) == "complaint"
    assert stage_for(_sub(deadline_days=-20), ["followup", "complaint"], NOW) is None
    assert stage_for(_sub("rejected"), [], NOW) == "appeal"
    assert stage_for(_sub("rejected"), ["appeal"], NOW) == "complaint"


@pytest.mark.parametrize("status", ["confirmed", "removed", "failed", "queued"])
def test_no_escalation_for_settled_or_unsent(status):
    assert stage_for(_sub(status), [], NOW) is None


def test_drafts_cite_statute_and_exclude_dob_phone(profile, broker):
    for kind in ("followup", "appeal", "complaint"):
        d = make_draft(kind, _sub(), broker, profile, contact_email="a@alias.example.invalid")
        assert "1990" not in d.body and "5125550000" not in d.body
        assert "59.1-577" in d.body
    assert (
        make_draft("complaint", _sub(), broker, profile, contact_email="x").to == AG_COMPLAINT_URL
    )
    assert "60 days" in make_draft("appeal", _sub(), broker, profile, contact_email="x").body


def test_webhook_rejects_non_http_urls():
    assert send_webhook("file:///etc/passwd", "t", "m") is False
    assert send_webhook("ftp://example.invalid/x", "t", "m") is False


def test_webhook_posts_and_survives_failure(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout):
        seen["body"], seen["url"] = req.data, req.full_url
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    assert send_webhook("https://ntfy.example.invalid/topic", "T", "hello") is True
    assert seen["body"] == b"hello"

    def boom(req, timeout):
        raise OSError("down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert send_webhook("https://ntfy.example.invalid/topic", "T", "hello") is False
