"""Tests for `optout log` (manual outreach) and `optout history`."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

import optout.brokers.registry as reg_mod
import optout.db as db_mod
from optout.cli import app
from optout.db import get_engine, get_session, init_db, list_events, list_submissions

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures" / "brokers"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    monkeypatch.setattr(db_mod, "get_engine", lambda path=None: engine)
    monkeypatch.setattr(db_mod, "db_path", lambda: tmp_path / "test.db")
    monkeypatch.setenv("OPTOUT_BROKERS_DIR", str(FIXTURES))
    reg_mod._registry.clear()
    reg_mod._loaded = False
    yield engine
    reg_mod._registry.clear()
    reg_mod._loaded = False


def test_log_known_broker_uses_its_deadline(isolated_db):
    result = runner.invoke(
        app, ["log", "test-email-broker", "--date", "2026-10-01", "-n", "ticket 42"]
    )
    assert result.exit_code == 0, result.output
    assert "unlisted" not in result.output
    with get_session(isolated_db) as s:
        (sub,) = list_submissions(s)
        assert sub.status == "submitted"
        assert sub.submitted_at.date() == date(2026, 10, 1)
        assert sub.deadline_at - sub.submitted_at == timedelta(days=30)
        assert [e.event_type for e in list_events(s, sub.id)] == ["created", "submitted"]


def test_log_unlisted_company(isolated_db):
    result = runner.invoke(app, ["log", "Some Spammer", "--days", "30"])
    assert result.exit_code == 0, result.output
    assert "unlisted" in result.output
    with get_session(isolated_db) as s:
        (sub,) = list_submissions(s)
        assert sub.broker_slug == "some spammer"
        assert sub.deadline_at - sub.submitted_at == timedelta(days=30)


def test_logged_request_is_never_removed(isolated_db):
    runner.invoke(app, ["log", "test-email-broker"])
    with get_session(isolated_db) as s:
        (sub,) = list_submissions(s)
        assert sub.status not in ("confirmed", "removed")


@pytest.mark.parametrize(
    "args",
    [["--date", "2026-13-01"], ["--date", "2999-01-01"], ["--method", "carrier-pigeon"]],
)
def test_log_rejects_bad_input(args):
    result = runner.invoke(app, ["log", "test-email-broker", *args])
    assert result.exit_code == 1


def test_history_shows_real_contact_date(isolated_db):
    runner.invoke(app, ["log", "test-email-broker", "--date", "2026-09-03", "-n", "ref ABC"])
    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0, result.output
    assert "Sep  3" in result.output
    assert "ref ABC" in result.output
    assert "submitted" in result.output


def test_history_empty(isolated_db):
    result = runner.invoke(app, ["history"])
    assert "No history yet" in result.output
