"""Web-form recipes: field names only, never the user's own details."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from optout import forms
from optout.config import Address, EmailConfig, EmailSet, PhoneSet, Profile, SmtpConfig, UserConfig


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return UserConfig(
        profile=Profile(
            legal_name="Jane Q Public",
            dob=date(1990, 5, 14),
            current_address=Address(street="1 Main St", city="Richmond", state="VA", zip="23219"),
            phones=PhoneSet(current=["+15125550000"]),
            emails=EmailSet(current=["jane@example.invalid"]),
        ),
        email=EmailConfig(
            method="smtp", smtp=SmtpConfig(host="h", username="jane@example.invalid")
        ),
    )


def _recipe(**over):
    base = {
        "broker": "acme",
        "url": "https://acme.example/privacy/remove?prefill-token=SECRET#x",
        "steps": [
            {"action": "fill", "field": "full_name", "label": "Full name"},
            {"action": "click", "text": "Submit"},
            {"action": "wait_text", "text": "Your data has been removed"},
        ],
        "success_text": "Your data has been removed",
    }
    return forms.Recipe.model_validate({**base, **over})


def test_url_loses_its_query_token_and_must_be_https():
    assert _recipe().url == "https://acme.example/privacy/remove"
    with pytest.raises(ValidationError):
        _recipe(url="http://acme.example/x")


def test_a_step_may_only_fill_a_known_profile_field():
    with pytest.raises(ValidationError):
        _recipe(steps=[{"action": "fill", "field": "ssn", "label": "SSN"}])


def test_profile_fields_split_the_name_and_date(cfg):
    got = forms.profile_fields(cfg)
    assert (got["first_name"], got["last_name"]) == ("Jane", "Public")
    assert (got["dob_month"], got["dob_day"], got["dob_year"]) == ("05", "14", "1990")


def test_a_recipe_containing_your_details_is_refused(cfg):
    bad = _recipe(notes="I typed jane@example.invalid and 1 Main St")
    assert forms.leaks(bad, cfg)
    with pytest.raises(ValueError):
        forms.save_local(bad, cfg)
    assert forms.local_recipes() == {}


def test_a_clean_recipe_is_saved_shared_and_loaded(cfg):
    saved = forms.save_local(_recipe(), cfg)
    assert saved.recorded
    assert forms.load_recipes()["acme"].steps[0].field == "full_name"
    assert [r["broker"] for r in forms.learned_recipes()] == ["acme"]


def test_shared_recipes_file_is_valid_and_clean():
    for slug, r in forms.shared_recipes().items():
        assert slug == r.broker and "@" not in r.url and "?" not in r.url


def test_prompt_carries_the_tasks_and_apply_records_outcomes(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from optout.db import create_submission, get_session, init_db, list_submissions, update_status

    prompt = forms.build_prompt(
        cfg,
        [
            {
                "broker": "acme",
                "name": "Acme",
                "link": "https://acme.example/f",
                "they_said": "Use the form.",
                "email": "Hi\nUse the form.",
                "recipe": False,
            }
        ],
    )
    assert (
        "F1. broker: acme" in prompt
        and "Jane Q Public" in prompt
        and "https://acme.example/f" in prompt
    )

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
        lines = forms.apply_results(
            s,
            cfg,
            [
                {
                    "broker": "acme",
                    "outcome": "removed",
                    "note": "ok",
                    "recipe": {
                        "broker": "acme",
                        "url": "https://acme.example/f?t=1",
                        "steps": [{"action": "fill", "field": "email", "label": "Email"}],
                    },
                },
                {"broker": "other", "outcome": "skipped", "note": "needs ID"},
            ],
        )
        assert lines[0] == "acme: removed, recipe saved" and "left alone" in lines[1]
        assert list_submissions(s, broker_slug="acme")[0].status == "confirmed"
    assert forms.load_recipes()["acme"].url == "https://acme.example/f"


def test_followup_is_linked_expected_and_flagged_when_it_never_comes(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from datetime import UTC, datetime, timedelta

    from optout.db import create_submission, get_session, init_db, update_status
    from optout.web.data import collect

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
        forms.mark_outcome(s, "acme", "submitted", "ok", expects_email=True, wait_hours=24)
        now = datetime.now(UTC)
        assert forms.pending_followup(s, sub, now) is not None
        assert not [a for a in collect(s, cfg, now).attention if a.kind == "stalled"]  # too soon
        later = now + timedelta(hours=30)
        stalled = [a for a in collect(s, cfg, later).attention if a.kind == "stalled"]
        assert [a.slug for a in stalled] == ["acme"]
        # the email arrives: it is linked, and nothing is stalled any more
        assert forms.note_followup(s, sub, "noreply@acme.example", "need-info", "<m1>")
        assert forms.pending_followup(s, sub, later) is None
        assert not [a for a in collect(s, cfg, later).attention if a.kind == "stalled"]
        assert forms.stats(s) == [
            {"broker": "acme", "forms": 1, "expected_email": 1, "followups": 1, "waiting": False}
        ]


def test_a_recipe_with_a_confirm_link_followup_becomes_a_confirm_flow(cfg):
    from optout.engine.confirm import flow_for, load_flows

    forms.save_local(
        _recipe(
            then={
                "sender": "@acme.example",
                "phrase": "Please click the button to verify your request",
                "action": "confirm_link",
                "link_hosts": ["acme.example"],
            }
        ),
        cfg,
    )
    flow = flow_for(
        "noreply@acme.example", "Hi! Please click the button to verify your request.", load_flows()
    )
    assert flow is not None and flow.link_hosts == ("acme.example",)
    with pytest.raises(ValidationError):
        _recipe(then={"sender": "jane@acme.example", "phrase": "twelve chars or more here"})


def test_a_taught_confirm_email_is_attached_to_the_recipe(cfg):
    forms.save_local(_recipe(), cfg)
    assert forms.attach_followup(
        "acme",
        "@acme.example",
        "please click the button to verify your request",
        "acme.example",
        cfg,
    )
    assert forms.load_recipes()["acme"].then.link_hosts == ["acme.example"]
    assert not forms.attach_followup(
        "acme", "@acme.example", "another long enough phrase", "acme.example", cfg
    )
    assert not forms.attach_followup(
        "nope", "@acme.example", "another long enough phrase", "acme.example", cfg
    )


def test_agent_wording_for_needs_human_is_normalised_and_implies_a_followup():
    r = _recipe(needs_human=["hCaptcha", "email verification link", "reCAPTCHA"])
    assert r.needs_human == ["captcha", "email_verify"]
    assert "email_verify" in forms._needs({"needs_human": ["email confirmation"]})
    assert forms._EMAIL_NEXT.search("Page said: Verification Email Sent. Check your email.")


def test_overlong_labels_are_clipped_not_rejected():
    r = _recipe(steps=[{"action": "check", "label": "I certify " * 20}])
    assert len(r.steps[0].label) == 80


def test_a_followup_read_before_the_form_was_recorded_is_still_linked(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from datetime import UTC, datetime, timedelta

    from optout.db import InboxMessage, create_submission, get_session, init_db

    init_db()
    with get_session() as s:
        sub = create_submission(
            s,
            broker_slug="acme",
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
        s.add(
            InboxMessage(
                message_id="<v1>",
                submission_id=sub.id,
                broker_slug="acme",
                from_addr="noreply@acme.example",
                subject="Complete your request",
                received_at=datetime.now(UTC) - timedelta(minutes=12),
                text="Click to verify",
                kind="need-info",
                rule="x",
                state="applied",
            )
        )
        s.commit()
        forms.mark_outcome(s, "acme", "submitted", "ok", expects_email=True)
        assert forms.link_followups(s) == 1
        assert forms.pending_followup(s, sub, datetime.now(UTC)) is None


def _sub(s, slug, status, days_ago):
    from datetime import UTC, datetime, timedelta

    from optout.db import create_submission, mark_submitted, update_status

    sub = create_submission(
        s,
        broker_slug=slug,
        method_used="web_form",
        legal_basis_cited=[],
        statutory_response_days=45,
    )
    mark_submitted(s, sub, {}, None, 45, submitted_at=datetime.now(UTC) - timedelta(days=days_ago))
    update_status(s, sub, status)
    return sub


@pytest.fixture()
def people(cfg, tmp_path, monkeypatch):
    """A registry with one people-search site that has a search page."""
    import yaml

    from optout.brokers import registry as reg
    from optout.db import init_db

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    d = tmp_path / "brokers"
    d.mkdir()
    (d / "findme.yml").write_text(
        yaml.safe_dump(
            {
                "slug": "findme",
                "name": "FindMe",
                "domain": "findme.example",
                "category": "people_search",
                "method": "manual",
                "opt_out_url": "https://findme.example/optout",
                "search_url": "https://findme.example/",
                "priority": 1,
                "legal_basis": ["CCPA"],
                "statutory_response_days": 45,
            }
        )
    )
    monkeypatch.setenv("OPTOUT_BROKERS_DIR", str(d))
    reg._registry.clear()
    reg._loaded = False
    init_db()
    yield cfg
    reg._registry.clear()
    reg._loaded = False


def test_a_never_contacted_people_search_site_is_queued_and_its_form_starts_a_request(people):
    from optout.db import get_session, list_submissions

    with get_session() as s:
        (item,) = forms.todo(s, people)
        assert (item["broker"], item["priority"], item["link"]) == (
            "findme",
            1,
            "https://findme.example/optout",
        )
        forms.mark_outcome(s, "findme", "submitted", "Page said: received")
        (sub,) = list_submissions(s, broker_slug="findme")
        assert sub.status == "awaiting_broker_confirmation" and sub.submitted_at is not None
        assert forms.todo(s, people) == []  # under way: not queued again


def test_a_site_emailed_first_leaves_the_queue_once_its_form_is_filled(people):
    from optout.db import create_submission, get_session, mark_submitted

    with get_session() as s:
        sub = create_submission(
            s, broker_slug="findme", method_used="email", legal_basis_cited=[],
            statutory_response_days=45,
        )
        mark_submitted(s, sub, {}, None, 45)
        assert [i["broker"] for i in forms.todo(s, people)] == ["findme"]  # email isn't enough
        forms.mark_outcome(s, "findme", "submitted", "Page said: received")
        assert forms.todo(s, people) == []


def test_recheck_confirms_removal_and_catches_relisting(people):
    from optout.db import get_session

    with get_session() as s:
        sub = _sub(s, "findme", "awaiting_broker_confirmation", days_ago=10)
        (due,) = forms.recheck_items(s)
        assert due["search_url"] == "https://findme.example/"
        prompt = forms.build_recheck_prompt(people, [due])
        assert "R1. broker: findme" in prompt and "Richmond" in prompt
        assert (
            forms.apply_recheck(s, {"broker": "findme", "listed": False, "note": "no match"})
            == "findme: not listed; removed"
        )
        s.refresh(sub)
        assert sub.status == "removed"
        assert forms.recheck_items(s) == []  # just checked
        line = forms.apply_recheck(s, {"broker": "findme", "listed": True, "note": "back"})
        s.refresh(sub)
        assert "LISTED AGAIN" in line and sub.status == "awaiting_user_input"


def test_recheck_that_cannot_tell_changes_nothing(people):
    from optout.db import get_session

    with get_session() as s:
        sub = _sub(s, "findme", "confirmed", days_ago=10)
        assert "couldn't tell" in forms.apply_recheck(
            s, {"broker": "findme", "listed": None, "note": "blocked"}
        )
        s.refresh(sub)
        assert sub.status == "confirmed"


def test_people_search_list_is_valid():
    from importlib.resources import files

    import yaml

    items = yaml.safe_load(files("optout").joinpath("data/people_search.yml").read_text())
    known = {"listing_url", "email_verify", "phone_verify", "id_upload", "account", "payment"}
    assert len(items) > 30
    for it in items:
        assert it["priority"] in (1, 2, 3) and it["opt_out_url"].startswith("https://")
        assert set(it.get("needs") or []) <= known, it["name"]
