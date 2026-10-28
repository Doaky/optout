"""Tests for the email method handler.

Unit tests mock or bypass SMTP.
The integration test class (TestSendViaSmtp) spins up a real aiosmtpd server.
"""

from __future__ import annotations

import uuid
from datetime import date
from email import message_from_bytes
from pathlib import Path

import pytest

from optout.brokers.schema import BrokerDef
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
from optout.db import Submission
from optout.engine import SubmissionFailed
from optout.engine.methods.email import (
    _build_message,
    _render_body,
    _save_eml,
    run,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def profile() -> Profile:
    return Profile(
        legal_name="Jane Q Public",
        aliases=["Janie Public"],
        dob=date(1990, 5, 14),
        current_address=Address(street="123 Main St", city="Austin", state="TX", zip="78701"),
        previous_addresses=[],
        phones=PhoneSet(current=["+15125551234"]),
        emails=EmailSet(current=["jane@example.com"]),
        relatives=["John Public"],
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
        legal_basis=["CCPA", "GDPR"],
        statutory_response_days=30,
    )


@pytest.fixture()
def smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host="127.0.0.1",
        port=18025,
        username="jane@example.com",
        password_env="OPTOUT_TEST_SMTP_PASSWORD",
    )


@pytest.fixture()
def user_config(smtp_config: SmtpConfig) -> UserConfig:
    return UserConfig(
        profile=Profile(
            legal_name="Jane Q Public",
            dob=date(1990, 5, 14),
            current_address=Address(street="123 Main St", city="Austin", state="TX", zip="78701"),
            phones=PhoneSet(current=["+15125551234"]),
            emails=EmailSet(current=["jane@example.com"]),
        ),
        email=EmailConfig(method="smtp", smtp=smtp_config),
        playwright=PlaywrightConfig(),
        monitoring=MonitoringConfig(),
    )


@pytest.fixture()
def fake_submission() -> Submission:
    return Submission(
        id=uuid.uuid4(),
        broker_slug="radaris",
        status="queued",
        method_used="email",
        legal_basis_cited="CCPA,GDPR",
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRenderBody:
    def test_contains_legal_name(self, profile: Profile, email_broker: BrokerDef) -> None:
        body = _render_body(email_broker, profile)
        assert "Jane Q Public" in body

    def test_contains_broker_name(self, profile: Profile, email_broker: BrokerDef) -> None:
        body = _render_body(email_broker, profile)
        assert "Radaris" in body

    def test_contains_ccpa_citation(self, profile: Profile, email_broker: BrokerDef) -> None:
        body = _render_body(email_broker, profile)
        assert "1798.105" in body

    def test_contains_gdpr_citation(self, profile: Profile, email_broker: BrokerDef) -> None:
        body = _render_body(email_broker, profile)
        assert "Art. 17" in body

    def test_contains_statutory_days(self, profile: Profile, email_broker: BrokerDef) -> None:
        body = _render_body(email_broker, profile)
        assert "30" in body

    def test_contains_current_address(self, profile: Profile, email_broker: BrokerDef) -> None:
        body = _render_body(email_broker, profile)
        assert "123 Main St" in body
        assert "Austin" in body

    def test_aliases_included_when_present(self, profile: Profile, email_broker: BrokerDef) -> None:
        body = _render_body(email_broker, profile)
        assert "Janie Public" in body

    def test_unknown_statute_silently_dropped(
        self, profile: Profile, email_broker: BrokerDef
    ) -> None:
        broker = email_broker.model_copy(update={"legal_basis": ["CCPA", "MADE_UP_LAW"]})
        body = _render_body(broker, profile)
        assert "1798.105" in body
        assert "MADE_UP_LAW" not in body


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


class TestBuildMessage:
    def test_headers_set(self) -> None:
        sid = uuid.uuid4()
        msg = _build_message(
            from_addr="jane@example.com",
            to_addr="optout@radaris.com",
            subject="Data Deletion Request — Jane Q Public",
            body="test body",
            submission_id=sid,
        )
        assert msg["From"] == "jane@example.com"
        assert msg["To"] == "optout@radaris.com"
        assert "Data Deletion Request" in msg["Subject"]
        assert msg["Message-ID"]
        assert msg["Date"]
        assert str(sid) in msg["X-OptOut-Submission-ID"]

    def test_body_is_plain_text(self) -> None:
        msg = _build_message(
            from_addr="jane@example.com",
            to_addr="optout@radaris.com",
            subject="Subject",
            body="hello world",
            submission_id=uuid.uuid4(),
        )
        assert msg.get_content_type() == "text/plain"
        assert "hello world" in msg.get_payload(decode=True).decode()


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------


class TestSaveEml:
    def test_eml_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        sid = uuid.uuid4()
        msg = _build_message(
            from_addr="a@b.com",
            to_addr="c@d.com",
            subject="Test",
            body="body",
            submission_id=sid,
        )
        path = _save_eml(sid, msg)
        assert path.exists()
        assert path.suffix == ".eml"

    def test_eml_is_parseable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        sid = uuid.uuid4()
        msg = _build_message(
            from_addr="a@b.com",
            to_addr="c@d.com",
            subject="Hello",
            body="world",
            submission_id=sid,
        )
        path = _save_eml(sid, msg)
        parsed = message_from_bytes(path.read_bytes())
        assert parsed["Subject"] == "Hello"


# ---------------------------------------------------------------------------
# Guard: wrong broker method
# ---------------------------------------------------------------------------


class TestRunGuards:
    def test_missing_opt_out_email_raises_submission_failed(
        self,
        profile: Profile,
        fake_submission: Submission,
        user_config: UserConfig,
    ) -> None:
        web_broker = BrokerDef(
            slug="whitepages",
            name="Whitepages",
            domain="whitepages.com",
            category="people_search",
            method="web_form",
            opt_out_url="https://whitepages.com/suppression_requests",
            legal_basis=["CCPA"],
            statutory_response_days=45,
        )
        with pytest.raises(SubmissionFailed, match="no opt_out_email"):
            run(web_broker, profile, fake_submission, _config=user_config)

    def test_no_smtp_config_raises_submission_failed(
        self,
        email_broker: BrokerDef,
        profile: Profile,
        fake_submission: Submission,
    ) -> None:
        cfg = UserConfig(
            profile=profile,
            email=EmailConfig(
                method="smtp",
                smtp=SmtpConfig(host="localhost", port=25, username="x@x.com", password_env="NOOP"),
            ),
            playwright=PlaywrightConfig(),
            monitoring=MonitoringConfig(),
        )
        # Manually null out smtp to trigger the guard
        cfg.email.smtp = None  # type: ignore[assignment]
        with pytest.raises(SubmissionFailed, match="No SMTP configuration"):
            run(email_broker, profile, fake_submission, _config=cfg)


# ---------------------------------------------------------------------------
# Integration: real aiosmtpd server
# ---------------------------------------------------------------------------


class _CapturingHandler:
    """Minimal aiosmtpd handler that stores received envelopes."""

    def __init__(self) -> None:
        self.envelopes: list[tuple[list[str], bytes]] = []

    async def handle_DATA(self, server, session, envelope) -> str:  # type: ignore[override]
        self.envelopes.append((list(envelope.rcpt_tos), bytes(envelope.content)))
        return "250 Message accepted"


@pytest.fixture()
def smtp_server():
    """Spin up aiosmtpd on 127.0.0.1:18025 for the duration of the test."""
    pytest.importorskip("aiosmtpd")
    from aiosmtpd.controller import Controller

    handler = _CapturingHandler()
    controller = Controller(handler, hostname="127.0.0.1", port=18025)
    controller.start()
    yield controller, handler
    controller.stop()


class TestSendViaSmtp:
    def test_message_delivered(
        self,
        smtp_server: tuple,
        email_broker: BrokerDef,
        profile: Profile,
        fake_submission: Submission,
        user_config: UserConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        controller, handler = smtp_server

        result = run(email_broker, profile, fake_submission, _config=user_config)

        assert len(handler.envelopes) == 1
        rcpt_tos, raw = handler.envelopes[0]
        assert "optout@radaris.com" in rcpt_tos

        parsed = message_from_bytes(raw)
        assert "Jane Q Public" in parsed["Subject"]
        assert result.artifact_path is not None
        assert Path(result.artifact_path).exists()

    def test_payload_fields(
        self,
        smtp_server: tuple,
        email_broker: BrokerDef,
        profile: Profile,
        fake_submission: Submission,
        user_config: UserConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        result = run(email_broker, profile, fake_submission, _config=user_config)

        assert result.payload["to"] == "optout@radaris.com"
        assert result.payload["from"] == "jane@example.com"
        assert "message_id" in result.payload


def test_missing_password_fails_before_connecting(monkeypatch, tmp_path):
    from optout.engine import UserInputRequired
    from optout.engine.methods.email import _send

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # not the real password file
    monkeypatch.delenv("OPTOUT_TEST_NOPW", raising=False)
    cfg = SmtpConfig(
        host="smtp.gmail.com", port=587, username="a@b.example", password_env="OPTOUT_TEST_NOPW"
    )
    with pytest.raises(UserInputRequired, match="No SMTP password found"):
        _send(cfg, None)  # must raise before touching the message or the network


def test_password_file_preferred_and_permissions_enforced(tmp_path, monkeypatch):
    from optout.engine import UserInputRequired
    from optout.engine.methods.email import read_smtp_password

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("PW_ENV_X", "from-env")
    f = tmp_path / "pw"
    cfg = SmtpConfig(host="h", username="u", password_env="PW_ENV_X", password_file=str(f))

    assert read_smtp_password(cfg) == "from-env"  # no file yet: environment fallback
    f.write_text("from-file\n")
    f.chmod(0o644)
    with pytest.raises(UserInputRequired, match="chmod 600"):
        read_smtp_password(cfg)
    f.chmod(0o600)
    assert read_smtp_password(cfg) == "from-file"


def test_default_password_file_location(tmp_path, monkeypatch):
    from optout.engine.methods.email import read_smtp_password

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PW_ENV_Y", raising=False)
    (tmp_path / "optout").mkdir()
    pw = tmp_path / "optout" / "smtp_password"
    pw.write_text("secret")
    pw.chmod(0o600)
    cfg = SmtpConfig(host="h", username="u", password_env="PW_ENV_Y")
    assert read_smtp_password(cfg) == "secret"
