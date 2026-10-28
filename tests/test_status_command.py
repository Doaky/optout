"""Tests for `optout status`."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import create_engine
from typer.testing import CliRunner

from optout.cli import app
from optout.config import (
    Address,
    EmailConfig,
    EmailSet,
    MonitoringConfig,
    PhoneSet,
    PlaywrightConfig,
    Profile,
    SmtpConfig,
    UserConfig,
)
from optout.db import (
    Submission,
    create_submission,
    get_session,
    init_db,
    update_status,
)

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures" / "brokers"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path: Path):
    e = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(e)
    return e


@pytest.fixture()
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine):
    """Wire config, DB, and registry for every status test."""
    import optout.db as db_mod
    from optout.brokers import registry as reg_mod
    from optout.config import save_config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("OPTOUT_BROKERS_DIR", str(FIXTURES))
    monkeypatch.setenv("COLUMNS", "200")

    reg_mod._registry.clear()
    reg_mod._loaded = False
    monkeypatch.setattr(db_mod, "get_engine", lambda path=None: engine)

    # Also patch db_path so the "no database" guard passes
    db_file = tmp_path / "optout" / "test.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(db_mod, "db_path", lambda: db_file)
    # touch the file so the guard sees it
    db_file.touch()

    cfg = UserConfig(
        profile=Profile(
            legal_name="Jane Q Public",
            dob=date(1990, 5, 14),
            current_address=Address(street="1 Main", city="Austin", state="TX", zip="78701"),
            phones=PhoneSet(current=["+15125550000"]),
            emails=EmailSet(current=["jane@example.com"]),
        ),
        email=EmailConfig(
            method="smtp",
            smtp=SmtpConfig(host="localhost", port=25, username="j@e.com", password_env="NOOP"),
        ),
        playwright=PlaywrightConfig(),
        monitoring=MonitoringConfig(),
    )
    save_config(cfg)


def _make_sub(
    engine,
    *,
    broker_slug: str,
    status: str = "queued",
    submitted_days_ago: int | None = None,
    deadline_days: int | None = None,
    failure_reason: str | None = None,
    artifact_path: str | None = None,
) -> Submission:
    with get_session(engine) as session:
        sub = create_submission(
            session,
            broker_slug=broker_slug,
            method_used="email",
            legal_basis_cited=["CCPA"],
            statutory_response_days=30,
        )
        now = datetime.now(UTC)
        if submitted_days_ago is not None:
            sub.submitted_at = now - timedelta(days=submitted_days_ago)
        if deadline_days is not None:
            sub.deadline_at = now + timedelta(days=deadline_days)
        if artifact_path:
            sub.artifact_path = artifact_path
        update_status(session, sub, status, failure_reason=failure_reason)
    return sub


# ---------------------------------------------------------------------------
# No database / no submissions guards
# ---------------------------------------------------------------------------


class TestStatusGuards:
    def test_no_db_exits_zero_with_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import optout.db as db_mod

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(db_mod, "db_path", lambda: tmp_path / "nonexistent.db")
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "No database" in result.output or "init" in result.output

    def test_empty_db_exits_zero_with_hint(self, _env) -> None:
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "No submissions" in result.output
        assert "queue" in result.output.lower()


# ---------------------------------------------------------------------------
# Table content
# ---------------------------------------------------------------------------


class TestStatusTable:
    def test_shows_broker_name(self, _env, engine) -> None:
        _make_sub(engine, broker_slug="test-email-broker")
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        # Registry maps slug → friendly name
        assert "Test Email Broker" in result.output

    def test_shows_status_column(self, _env, engine) -> None:
        _make_sub(engine, broker_slug="test-email-broker", status="queued")
        result = runner.invoke(app, ["status"])
        assert "Queued" in result.output

    def test_shows_submitted_date(self, _env, engine) -> None:
        _make_sub(
            engine,
            broker_slug="test-email-broker",
            status="awaiting_broker_confirmation",
            submitted_days_ago=3,
            deadline_days=27,
        )
        result = runner.invoke(app, ["status"])
        # _fmt_date renders as "Mmm DD" (e.g. "May  8") — check for a month abbreviation
        months = [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
        assert any(m in result.output for m in months)

    def test_shows_multiple_brokers(self, _env, engine) -> None:
        _make_sub(engine, broker_slug="test-email-broker")
        _make_sub(engine, broker_slug="test-web-broker")
        result = runner.invoke(app, ["status"])
        assert "Test Email Broker" in result.output
        assert "Test Web Broker" in result.output

    def test_artifact_submission_renders(self, _env, engine) -> None:
        # Artifact path doesn't add a checkmark (no artifact column); just verify no crash.
        _make_sub(
            engine,
            broker_slug="test-email-broker",
            artifact_path="/tmp/some-id.eml",
        )
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "Test Email Broker" in result.output

    def test_no_artifact_no_checkmark_in_art_col(self, _env, engine) -> None:
        # Just verify it doesn't crash with no artifact
        _make_sub(engine, broker_slug="test-email-broker")
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Days left logic
# ---------------------------------------------------------------------------


class TestDaysLeft:
    def test_overdue_label(self, _env, engine) -> None:
        _make_sub(
            engine,
            broker_slug="test-email-broker",
            status="awaiting_broker_confirmation",
            submitted_days_ago=35,
            deadline_days=-5,  # 5 days past deadline
        )
        result = runner.invoke(app, ["status"])
        assert "OVERDUE" in result.output

    def test_confirmed_shows_checkmark(self, _env, engine) -> None:
        _make_sub(
            engine,
            broker_slug="test-email-broker",
            status="confirmed",
            submitted_days_ago=10,
            deadline_days=20,
        )
        result = runner.invoke(app, ["status"])
        # A broker saying "done" is not verified removal: never display it as Removed.
        assert "Broker says done" in result.output
        assert "Removed" not in result.output
        assert "✓" in result.output

    def test_queued_shows_dash_for_days(self, _env, engine) -> None:
        _make_sub(engine, broker_slug="test-email-broker", status="queued")
        result = runner.invoke(app, ["status"])
        # no deadline set → dash
        assert "—" in result.output

    def test_plenty_of_time(self, _env, engine) -> None:
        _make_sub(
            engine,
            broker_slug="test-email-broker",
            status="awaiting_broker_confirmation",
            submitted_days_ago=1,
            deadline_days=28,
        )
        result = runner.invoke(app, ["status"])
        # Allow ±1 day for sub-second timing differences in timedelta.days
        assert any(str(n) in result.output for n in range(26, 30))


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilters:
    def test_broker_filter(self, _env, engine) -> None:
        _make_sub(engine, broker_slug="test-email-broker")
        _make_sub(engine, broker_slug="test-web-broker")
        result = runner.invoke(app, ["status", "--broker", "test-email-broker"])
        assert result.exit_code == 0
        assert "Test Email Broker" in result.output
        assert "Test Web Broker" not in result.output

    def test_status_filter_matches(self, _env, engine) -> None:
        _make_sub(engine, broker_slug="test-email-broker", status="queued")
        _make_sub(
            engine,
            broker_slug="test-web-broker",
            status="confirmed",
            submitted_days_ago=5,
            deadline_days=25,
        )
        result = runner.invoke(app, ["status", "--status", "queued"])
        assert result.exit_code == 0
        assert "Queued" in result.output
        assert "Removed" not in result.output

    def test_status_filter_no_match_shows_hint(self, _env, engine) -> None:
        _make_sub(engine, broker_slug="test-email-broker", status="queued")
        result = runner.invoke(app, ["status", "--status", "confirmed"])
        assert result.exit_code == 0
        assert "No submissions" in result.output


# ---------------------------------------------------------------------------
# Summary and callouts
# ---------------------------------------------------------------------------


class TestSummaryAndCallouts:
    def test_summary_line_present(self, _env, engine) -> None:
        _make_sub(engine, broker_slug="test-email-broker", status="queued")
        result = runner.invoke(app, ["status"])
        assert "Queued" in result.output  # appears in summary as "1 Queued"

    def test_overdue_callout(self, _env, engine) -> None:
        _make_sub(
            engine,
            broker_slug="test-email-broker",
            status="awaiting_broker_confirmation",
            submitted_days_ago=40,
            deadline_days=-10,
        )
        result = runner.invoke(app, ["status"])
        assert "cppa.ca.gov" in result.output

    def test_needs_input_callout(self, _env, engine) -> None:
        _make_sub(
            engine,
            broker_slug="test-email-broker",
            status="awaiting_user_input",
        )
        result = runner.invoke(app, ["status"])
        assert "submit" in result.output.lower()

    def test_failure_details_shown(self, _env, engine) -> None:
        _make_sub(
            engine,
            broker_slug="test-email-broker",
            status="failed",
            failure_reason="SMTP connection refused",
        )
        result = runner.invoke(app, ["status"])
        assert "SMTP connection refused" in result.output

    def test_multiple_statuses_in_summary(self, _env, engine) -> None:
        _make_sub(engine, broker_slug="test-email-broker", status="queued")
        _make_sub(
            engine,
            broker_slug="test-web-broker",
            status="confirmed",
            submitted_days_ago=5,
            deadline_days=25,
        )
        result = runner.invoke(app, ["status"])
        assert "Queued" in result.output
        assert "Broker says done" in result.output


class TestVerifiedRemoval:
    def test_removed_status_is_the_only_one_labelled_verified(self, _env, engine) -> None:
        from optout.db import Submission, get_session, mark_removed, record_verification

        _make_sub(
            engine,
            broker_slug="test-email-broker",
            status="confirmed",
            submitted_days_ago=10,
            deadline_days=20,
        )
        with get_session(engine) as s:
            sub = s.exec(__import__("sqlmodel").select(Submission)).first()
            mark_removed(s, sub, record_verification(s, sub, "verified_clear", "ok"))
        result = runner.invoke(app, ["status"])
        assert "Removed ✓ verified" in result.output
        assert "verified clear" in result.output
