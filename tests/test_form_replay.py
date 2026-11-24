"""The replay engine against a local page: labels found, values from the profile, success seen."""

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from optout.engine.form_replay import replay  # noqa: E402
from optout.forms import Recipe  # noqa: E402

PAGE = """<html><body>
<form onsubmit="document.body.innerHTML='<p>Your request has been received</p>';return false">
<label for=e>Email</label><input id=e>
<label for=s>State</label><select id=s><option>CA</option><option>VA</option></select>
<label><input type=checkbox id=c>Data Deletion</label>
<button>Submit Request</button></form></body></html>"""

RECIPE = {
    "broker": "acme",
    "url": "https://acme.example/form",
    "steps": [
        {"action": "check", "label": "Data Deletion"},
        {"action": "fill", "field": "email", "label": "Email"},
        {"action": "select", "field": "state", "label": "State", "text": "CA"},
        {"action": "click", "text": "Submit Request"},
        {"action": "wait_text", "text": "Your request has been received"},
    ],
    "success_text": "Your request has been received",
}


@pytest.fixture
def page():
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception:
            pytest.skip("no browser installed")
        p = browser.new_page()
        p.route(
            "https://acme.example/form", lambda r: r.fulfill(body=PAGE, content_type="text/html")
        )
        yield p
        browser.close()


def test_replays_with_the_users_own_values(page):
    r = replay(page, Recipe.model_validate(RECIPE), {"email": "a@b.co", "state": "VA"})
    assert r.status == "done"


def test_reports_a_missing_field_instead_of_raising(page):
    bad = {**RECIPE, "steps": [{"action": "fill", "field": "email", "label": "Nonexistent"}]}
    r = replay(page, Recipe.model_validate(bad), {"email": "a@b.co"})
    assert r.status == "stuck" and "Nonexistent" in r.detail


def test_refuses_what_needs_an_id_upload(page):
    r = replay(page, Recipe.model_validate({**RECIPE, "needs_human": ["id_upload"]}), {})
    assert r.status == "refused"


def test_a_stuck_run_records_what_the_page_offered_but_not_what_was_typed(
    page, tmp_path, monkeypatch
):
    import json

    from optout import forms

    monkeypatch.setattr(forms, "runs_dir", lambda: tmp_path)
    bad = {
        **RECIPE,
        "steps": [
            {"action": "fill", "field": "email", "label": "Email"},
            {"action": "click", "text": "Nope"},
        ],
    }
    r = replay(page, Recipe.model_validate(bad), {"email": "secret@example.com"})
    assert r.status == "stuck" and r.screenshot
    assert any("Email" in line for line in r.inventory)
    assert r.steps[0]["ok"] is True and r.steps[1]["ok"] is False
    name = forms.save_run("acme", r)
    saved = (tmp_path / name).with_suffix(".json").read_text()
    assert "secret@example.com" not in saved and json.loads(saved)["broker"] == "acme"
    assert (tmp_path / name).with_suffix(".png").exists()
