"""Tests for engine/dispatcher.py and the optout queue / optout submit CLI commands."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlmodel import create_engine
from typer.testing import CliRunner

from optout.brokers.schema import BrokerDef
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
)
from optout.engine import SubmissionFailed, SubmissionResult, UserInputRequired
from optout.engine.dispatcher import run_submission

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path: Path):
    e = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(e)
    return e


@pytest.fixture()
def profile() -> Profile:
    return Profile(
        legal_name="Jane Q Public",
        dob=date(1990, 5, 14),
        current_address=Address(street="123 Main St", city="Austin", state="TX", zip="78701"),
        phones=PhoneSet(current=["+15125551234"]),
        emails=EmailSet(current=["jane@example.com"]),
    )


@pytest.fixture()
def user_config(profile: Profile) -> UserConfig:
    return UserConfig(
        profile=profile,
        email=EmailConfig(
            method="smtp",
            smtp=SmtpConfig(
                host="localhost",
                port=25,
                username="jane@example.com",
                password_env="OPTOUT_SMTP_PASSWORD",
            ),
        ),
        playwright=PlaywrightConfig(headless=True),
        monitoring=MonitoringConfig(),
    )


@pytest.fixture()
def email_broker() -> BrokerDef:
    return BrokerDef(
        slug="radaris",
        name="Radaris",
        domain="radaris.com",
        category="people_search",
        method="email",
        opt_out_email="optout@radaris.com",
        legal_basis=["CCPA"],
        statutory_response_days=30,
    )


@pytest.fixture()
def web_broker() -> BrokerDef:
    return BrokerDef(
        slug="whitepages",
        name="Whitepages",
        domain="whitepages.com",
        category="people_search",
        method="web_form",
        opt_out_url="https://whitepages.com/suppression_requests",
        legal_basis=["CCPA"],
        statutory_response_days=45,
    )


@pytest.fixture()
def queued_email_sub(engine) -> Submission:
    with get_session(engine) as session:
        sub = create_submission(
            session,
            broker_slug="radaris",
            method_used="email",
            legal_basis_cited=["CCPA"],
            statutory_response_days=30,
        )
    return sub


# ---------------------------------------------------------------------------
# Dispatcher unit tests
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_success_sets_awaiting_confirmation(
        self, engine, queued_email_sub, email_broker, profile, user_config
    ) -> None:
        def mock_email(broker, profile, submission, *, _config):
            return SubmissionResult(payload={"to": "optout@radaris.com"}, artifact_path=None)

        with get_session(engine) as session:
            sub = session.get(Submission, queued_email_sub.id)
            with patch.dict("optout.engine.dispatcher._HANDLERS", {"email": mock_email}):
                run_submission(session, sub, email_broker, profile, user_config)

            assert sub.status == "awaiting_broker_confirmation"
            assert sub.submitted_at is not None
            assert sub.deadline_at is not None
            assert sub.payload_json == '{"to": "optout@radaris.com"}'

    def test_deadline_computed_from_statutory_days(
        self, engine, queued_email_sub, email_broker, profile, user_config
    ) -> None:
        def mock_email(broker, profile, submission, *, _config):
            return SubmissionResult(payload={}, artifact_path=None)

        with get_session(engine) as session:
            sub = session.get(Submission, queued_email_sub.id)
            with patch.dict("optout.engine.dispatcher._HANDLERS", {"email": mock_email}):
                run_submission(session, sub, email_broker, profile, user_config)

            delta = sub.deadline_at - sub.submitted_at
            assert delta.days == 30  # email_broker.statutory_response_days

    def test_artifact_path_stored(
        self, engine, queued_email_sub, email_broker, profile, user_config
    ) -> None:
        def mock_email(broker, profile, submission, *, _config):
            return SubmissionResult(payload={}, artifact_path="/tmp/test.eml")

        with get_session(engine) as session:
            sub = session.get(Submission, queued_email_sub.id)
            with patch.dict("optout.engine.dispatcher._HANDLERS", {"email": mock_email}):
                run_submission(session, sub, email_broker, profile, user_config)

            assert sub.artifact_path == "/tmp/test.eml"

    def test_user_input_required_sets_awaiting_user_input(
        self, engine, queued_email_sub, email_broker, profile, user_config
    ) -> None:
        def mock_email(broker, profile, submission, *, _config):
            raise UserInputRequired("CAPTCHA required")

        with get_session(engine) as session:
            sub = session.get(Submission, queued_email_sub.id)
            with patch.dict("optout.engine.dispatcher._HANDLERS", {"email": mock_email}):
                run_submission(session, sub, email_broker, profile, user_config)

            assert sub.status == "awaiting_user_input"
            assert "CAPTCHA" in (sub.failure_reason or "")

    def test_submission_failed_sets_failed(
        self, engine, queued_email_sub, email_broker, profile, user_config
    ) -> None:
        def mock_email(broker, profile, submission, *, _config):
            raise SubmissionFailed("Form changed")

        with get_session(engine) as session:
            sub = session.get(Submission, queued_email_sub.id)
            with patch.dict("optout.engine.dispatcher._HANDLERS", {"email": mock_email}):
                run_submission(session, sub, email_broker, profile, user_config)

            assert sub.status == "failed"
            assert sub.failure_reason == "Form changed"

    def test_unknown_method_sets_failed(self, engine, profile, user_config) -> None:
        with get_session(engine) as session:
            sub = create_submission(
                session,
                broker_slug="unknown-method-broker",
                method_used="carrier_pigeon",
                legal_basis_cited=["CCPA"],
                statutory_response_days=30,
            )
            unknown_broker = BrokerDef(
                slug="unknown-method-broker",
                name="Unknown",
                domain="x.com",
                category="people_search",
                method="email",  # valid model method
                opt_out_email="x@x.com",
                legal_basis=["CCPA"],
                statutory_response_days=30,
            )
            # Manually override to exercise the "no handler" branch
            with patch.dict("optout.engine.dispatcher._HANDLERS", {}, clear=True):
                run_submission(session, sub, unknown_broker, profile, user_config)

            assert sub.status == "failed"
            assert "No handler" in (sub.failure_reason or "")

    def test_submitted_event_recorded(
        self, engine, queued_email_sub, email_broker, profile, user_config
    ) -> None:
        from optout.db import list_events

        def mock_email(broker, profile, submission, *, _config):
            return SubmissionResult(payload={}, artifact_path=None)

        with get_session(engine) as session:
            sub = session.get(Submission, queued_email_sub.id)
            with patch.dict("optout.engine.dispatcher._HANDLERS", {"email": mock_email}):
                run_submission(session, sub, email_broker, profile, user_config)

            events = list_events(session, sub.id)
        event_types = [e.event_type for e in events]
        assert "submitted" in event_types
        assert "created" in event_types


# ---------------------------------------------------------------------------
# Helpers for CLI tests
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "brokers"


@pytest.fixture()
def _cli_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine,
) -> None:
    """Wire config, DB, and broker registry for CLI tests."""
    import optout.db as db_mod

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("OPTOUT_BROKERS_DIR", str(FIXTURES))
    monkeypatch.setenv("COLUMNS", "200")

    # Reset broker registry
    from optout.brokers import registry as reg_mod

    reg_mod._registry.clear()
    reg_mod._loaded = False

    # Point DB helpers at our in-memory/tmp engine
    monkeypatch.setattr(db_mod, "get_engine", lambda path=None: engine)

    # Write a minimal config
    from optout.config import EmailConfig, Profile, SmtpConfig, UserConfig, save_config

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
            smtp=SmtpConfig(
                host="127.0.0.1",
                port=18025,
                username="jane@example.com",
                password_env="OPTOUT_SMTP_PASSWORD",
            ),
        ),
        playwright=PlaywrightConfig(headless=True),
        monitoring=MonitoringConfig(),
    )
    save_config(cfg)


# ---------------------------------------------------------------------------
# optout queue CLI tests
# ---------------------------------------------------------------------------


class TestQueueCommand:
    def test_queues_single_broker(self, _cli_env) -> None:
        result = runner.invoke(app, ["queue", "--broker", "test-email-broker"])
        assert result.exit_code == 0
        assert "Queued" in result.output
        assert "test-email-broker" in result.output or "Test Email" in result.output

    def test_queues_all_brokers_when_no_slug(self, _cli_env) -> None:
        result = runner.invoke(app, ["queue"])
        assert result.exit_code == 0
        assert "Queued" in result.output

    def test_skips_duplicate_active_submission(self, _cli_env, engine) -> None:
        # Queue once
        runner.invoke(app, ["queue", "--broker", "test-email-broker"])
        # Queue again — should skip
        result = runner.invoke(app, ["queue", "--broker", "test-email-broker"])
        assert result.exit_code == 0
        assert "Skip" in result.output

    def test_unknown_slug_exits_nonzero(self, _cli_env) -> None:
        result = runner.invoke(app, ["queue", "--broker", "no-such-broker"])
        assert result.exit_code != 0

    def test_no_config_exits_nonzero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        result = runner.invoke(app, ["queue"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# optout submit CLI tests
# ---------------------------------------------------------------------------


class TestSubmitCommand:
    def test_no_queued_shows_hint(self, _cli_env) -> None:
        result = runner.invoke(app, ["submit"])
        assert result.exit_code == 0
        assert "No queued" in result.output

    def test_submit_email_broker_success(self, _cli_env, engine) -> None:
        # Queue the email broker first
        runner.invoke(app, ["queue", "--broker", "test-email-broker"])

        def mock_email(broker, profile, submission, *, _config):
            return SubmissionResult(payload={"to": "optout@example-email.com"}, artifact_path=None)

        with patch.dict("optout.engine.dispatcher._HANDLERS", {"email": mock_email}):
            result = runner.invoke(app, ["submit"])

        assert result.exit_code == 0
        assert "Awaiting reply" in result.output

    def test_submit_failed_shows_reason(self, _cli_env, engine) -> None:
        runner.invoke(app, ["queue", "--broker", "test-email-broker"])

        def mock_email_fail(broker, profile, submission, *, _config):
            raise SubmissionFailed("SMTP connection refused")

        with patch.dict("optout.engine.dispatcher._HANDLERS", {"email": mock_email_fail}):
            result = runner.invoke(app, ["submit"])

        assert result.exit_code == 0  # command itself succeeds; submission failed
        assert "failed" in result.output
        assert "SMTP connection refused" in result.output

    def test_submit_user_input_required_shows_status(self, _cli_env, engine) -> None:
        runner.invoke(app, ["queue", "--broker", "test-email-broker"])

        def mock_email_input(broker, profile, submission, *, _config):
            raise UserInputRequired("CAPTCHA appeared")

        with patch.dict("optout.engine.dispatcher._HANDLERS", {"email": mock_email_input}):
            result = runner.invoke(app, ["submit"])

        assert result.exit_code == 0
        assert "Waiting on you" in result.output

    def test_submit_broker_flag_filters(self, _cli_env, engine) -> None:
        runner.invoke(app, ["queue"])  # queue all

        with patch("optout.engine.dispatcher.email_method") as mock_handler:
            mock_handler.run.return_value = SubmissionResult(payload={}, artifact_path=None)
            result = runner.invoke(app, ["submit", "--broker", "test-email-broker"])

        assert result.exit_code == 0

    def test_no_config_exits_nonzero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        result = runner.invoke(app, ["submit"])
        assert result.exit_code != 0

    def test_summary_line_present(self, _cli_env, engine) -> None:
        runner.invoke(app, ["queue", "--broker", "test-email-broker"])

        def mock_email_noop(broker, profile, submission, *, _config):
            return SubmissionResult(payload={}, artifact_path=None)

        with patch.dict("optout.engine.dispatcher._HANDLERS", {"email": mock_email_noop}):
            result = runner.invoke(app, ["submit"])

        assert "Summary" in result.output
        assert "submitted" in result.output
