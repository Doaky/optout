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
