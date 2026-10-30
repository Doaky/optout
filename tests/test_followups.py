"""Sending follow-ups in a batch: who's due, threading, limits, and never twice."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from optout.brokers.schema import BrokerDef
from optout.config import Address, EmailConfig, EmailSet, PhoneSet, Profile, SmtpConfig, UserConfig
from optout.db import (
    create_submission,
    get_session,
    init_db,
    list_events,
    mark_submitted,
    update_status,
)
from optout.engine import followups

NOW = datetime(2026, 12, 20, 12, tzinfo=UTC)


def _cfg(limit=40):
    return UserConfig(
        profile=Profile(
            legal_name="Jane Q Public",
            dob=date(1990, 5, 14),
            current_address=Address(street="1 Main St", city="Richmond", state="VA", zip="23219"),
            phones=PhoneSet(current=[]),
            emails=EmailSet(current=["jane@example.invalid"]),
        ),
        email=EmailConfig(
            method="smtp",
            smtp=SmtpConfig(host="h", username="jane@example.invalid"),
            daily_limit=limit,
            send_delay_seconds=0,
        ),
    )


def _broker(slug):
    return BrokerDef(
        slug=slug,
        name=slug.title(),
        domain=f"{slug}.example",
        category="data_broker",
        method="email",
        opt_out_email=f"privacy@{slug}.example",
        legal_basis=["CCPA"],
        statutory_response_days=45,
    )


@pytest.fixture()
def world():
    init_db()
    with get_session() as s:
        subs = {}
        for slug, status, days in (
            ("late", "awaiting_broker_confirmation", 60),  # past 45 days, silent: due
            ("fresh", "awaiting_broker_confirmation", 10),  # not yet
            ("done", "confirmed", 60),  # answered
        ):
            sub = create_submission(
                s,
                broker_slug=slug,
                method_used="email",
                legal_basis_cited=[],
                statutory_response_days=45,
            )
            mark_submitted(
                s,
                sub,
                {"message_id": f"<orig-{slug}@mail>"},
                None,
                45,
                submitted_at=NOW - timedelta(days=days),
            )
            update_status(s, sub, status)
            subs[slug] = sub
        yield s, {k: _broker(k) for k in subs}, subs


def test_only_unanswered_requests_past_their_deadline_are_due(world):
    s, brokers, _ = world
    assert [b.slug for _, b in followups.due(s, brokers, NOW)] == ["late"]


def test_a_followup_is_threaded_onto_the_original_and_sent_once(world):
    s, brokers, subs = world
    sent = []
    items = followups.due(s, brokers, NOW)
    rep = followups.send(s, _cfg(), items, limit=10, deliver=sent.append, now=lambda: NOW)
    assert rep.sent == ["late"] and len(sent) == 1
    msg = sent[0]
    assert msg["In-Reply-To"] == "<orig-late@mail>" and msg["To"] == "privacy@late.example"
    assert msg["Subject"].startswith("Follow-up:") and "Va. Code" in msg.get_content()
    kinds = [
        json.loads(e.payload_json)["kind"]
        for e in list_events(s, subs["late"].id)
        if e.event_type == "escalation_sent"
    ]
    assert kinds == ["followup"]
    assert followups.due(s, brokers, NOW) == []  # never twice


def test_followups_respect_the_daily_limit_and_stop_on_sign_in_failure(world):
    s, brokers, _ = world
    items = followups.due(s, brokers, NOW)
    rep = followups.send(s, _cfg(limit=0), items, limit=10, deliver=lambda m: None, now=lambda: NOW)
    assert rep.sent == [] and "daily limit" in (rep.stopped_reason or "")

    class SMTPAuthenticationError(Exception):
        pass

    def boom(msg):
        raise SMTPAuthenticationError()

    rep = followups.send(s, _cfg(), items, limit=10, deliver=boom, now=lambda: NOW)
    assert rep.sent == [] and "sign in" in (rep.stopped_reason or "")
    assert [b.slug for _, b in followups.due(s, brokers, NOW)] == ["late"]  # still due
