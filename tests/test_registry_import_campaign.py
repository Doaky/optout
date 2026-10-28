"""Registry import, layered broker loading, overrides, and the email campaign."""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlmodel import create_engine

import optout.brokers.registry as reg
from optout.brokers.importer import parse_ca_registry, to_broker_dict, write_yaml_dir
from optout.brokers.schema import BrokerDef
from optout.config import (
    Address,
    EmailConfig,
    EmailSet,
    PhoneSet,
    Profile,
    SmtpConfig,
    UserConfig,
)
from optout.db import Session, create_submission, init_db, mark_submitted
from optout.engine.campaign import run_campaign, select_brokers, sent_today
from optout.engine.methods.email import _contact_email, _render_body

NAME = "Data broker name:"
WEB = "Data broker primary website:"
EMAIL = "Data broker primary contact email address:"
PRIV = "Data broker's primary website that contains details on how consumers can exercise their CA Consumer Privacy rights:"
FCRA = "The data broker or any of its subsidiaries is regulated by the federal Fair Credit Reporting Act"
DEL = "Requests to delete - Total requests received"
DEN = "Requests to delete - Total requests received - Denied"
COLS = [NAME, WEB, EMAIL, PRIV, FCRA, DEL, DEN]


def _write_csv(path: Path, rows: list[dict]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    return path


def _row(name, email, web="https://www.example.com", fcra="No", **kw):
    return {
        NAME: name,
        WEB: web,
        EMAIL: email,
        PRIV: "https://example.com/privacy",
        FCRA: fcra,
        DEL: kw.get("recv", "10"),
        DEN: kw.get("denied", "2"),
    }


@pytest.fixture()
def sample_csv(tmp_path):
    return _write_csv(
        tmp_path / "reg.csv",
        [
            _row("Acme Data, Inc.", "privacy@acme.example"),
            _row("Acme Marketing LLC", "PRIVACY@acme.example"),  # same contact -> merged
            _row("Bad Email Co", "not an email"),
            _row("Inject Co", "a@b.example\nBcc: x@y.example"),  # header injection attempt
            _row("Credit Bureau Corp", "dsar@bureau.example", fcra="Yes"),
            _row("Acme Data, Inc.", "other@acme2.example"),  # slug collision
        ],
    )


def test_parse_merges_dedupes_and_rejects_bad_emails(sample_csv):
    res = parse_ca_registry(sample_csv)
    by_slug = {b.slug: b for b in res.brokers}
    assert set(by_slug) == {"acme-data", "credit-bureau", "acme-data-2"}
    assert by_slug["acme-data"].covers == ["Acme Marketing LLC"]
    assert by_slug["credit-bureau"].exemptions == ["FCRA"]
    assert {n for n, _ in res.skipped} == {"Bad Email Co", "Inject Co"}
    assert by_slug["acme-data"].domain == "example.com"


def test_generated_definitions_validate_and_default_off_when_exempt(sample_csv, tmp_path):
    res = parse_ca_registry(sample_csv)
    d = {b.slug: to_broker_dict(b, "ca-registry", "2026-10-28") for b in res.brokers}
    for data in d.values():
        BrokerDef.model_validate(data)
    assert d["acme-data"]["enabled"] is True
    assert d["credit-bureau"]["enabled"] is False
    assert d["acme-data"]["legal_basis"] == ["VCDPA"]
    out = tmp_path / "out"
    assert write_yaml_dir(res, out, "ca-registry", "2026-10-28") == 3
    assert (
        yaml.safe_load((out / "acme-data.yml").read_text())["opt_out_email"]
        == "privacy@acme.example"
    )


@pytest.fixture()
def layered(tmp_path, monkeypatch):
    monkeypatch.delenv("OPTOUT_BROKERS_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    res = parse_ca_registry(
        _write_csv(tmp_path / "r.csv", [_row("Acme Data", "privacy@acme.example")])
    )
    write_yaml_dir(res, reg.imported_dir(), "ca-registry", "2026-10-28")
    yield
    reg._registry.clear()
    reg._loaded = False


def test_registry_layers_imported_and_user_overrides(layered):
    reg.user_dir().mkdir(parents=True)
    (reg.imported_dir() / "broken.yml").write_text("slug: [unclosed")
    reg.load_registry()
    assert "acme-data" in reg._registry and "spokeo" in reg._registry
    assert any(e.startswith("broken.yml") for e in reg.load_errors)  # reported, not fatal

    fixed = yaml.safe_load((reg.imported_dir() / "acme-data.yml").read_text())
    fixed["opt_out_email"] = "dsar@acme.example"
    (reg.user_dir() / "acme-data.yml").write_text(yaml.safe_dump(fixed))
    reg.load_registry()
    assert reg.get_broker("acme-data").opt_out_email == "dsar@acme.example"


def test_enable_disable_overrides(layered):
    reg.set_enabled("acme-data", False)
    reg.load_registry()
    assert reg.get_broker("acme-data").enabled is False
    reg.set_enabled("acme-data", True)
    reg.load_registry()
    assert reg.get_broker("acme-data").enabled is True


# ---------------------------------------------------------------------------
# Email content + campaign
# ---------------------------------------------------------------------------


@pytest.fixture()
def profile():
    return Profile(
        legal_name="Jane Q Public",
        dob=date(1990, 5, 14),
        current_address=Address(street="123 Main St", city="Richmond", state="VA", zip="23219"),
        phones=PhoneSet(current=["+15125551234"]),
        emails=EmailSet(current=["jane@example.invalid"]),
    )


@pytest.fixture()
def config(profile):
    return UserConfig(
        profile=profile,
        email=EmailConfig(
            method="smtp",
            smtp=SmtpConfig(host="h", username="jane@example.invalid", password_env="X"),
            alias_pattern="optout-{slug}@alias.example.invalid",
            daily_limit=3,
            send_delay_seconds=7,
        ),
    )


def _broker(slug="acme", **kw):
    data = dict(
        slug=slug,
        name=slug.title(),
        domain=f"{slug}.example",
        category="data_broker",
        method="email",
        opt_out_email=f"privacy@{slug}.example",
        legal_basis=["VCDPA"],
        statutory_response_days=45,
        source="ca-registry",
        covers=["Acme Marketing LLC"],
    )
    data.update(kw)
    return BrokerDef.model_validate(data)


def test_minimal_body_omits_dob_and_phone_and_uses_alias(config, profile):
    b = _broker()
    contact = _contact_email(config, b, profile)
    assert contact == "optout-acme@alias.example.invalid"
    body = _render_body(b, profile, contact_email=contact, minimal=True)
    assert "Jane Q Public" in body and "123 Main St" in body and contact in body
    assert "1990" not in body and "5125551234" not in body
    assert "jane@example.invalid" not in body  # real address never in the body
    assert "Va. Code § 59.1-577(A)(3)" in body
    assert "Acme Marketing LLC" in body and "resident of VA" in body


def test_alias_pattern_requires_slug():
    with pytest.raises(ValueError, match="slug"):
        EmailConfig(
            method="smtp",
            smtp=SmtpConfig(host="h", username="u", password_env="X"),
            alias_pattern="static@example.invalid",
        )


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    init_db(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


NOW = datetime(2026, 10, 28, 15, tzinfo=UTC)


def test_selection_skips_disabled_nonemail_active_and_recent(session):
    ok, off = _broker("ok"), _broker("off", enabled=False)
    web = _broker("web", method="web_form", opt_out_url="https://web.example/optout")
    busy, recent = _broker("busy"), _broker("recent")
    create_submission(
        session,
        broker_slug="busy",
        method_used="email",
        legal_basis_cited=[],
        statutory_response_days=45,
    )
    sub = create_submission(
        session,
        broker_slug="recent",
        method_used="email",
        legal_basis_cited=[],
        statutory_response_days=45,
    )
    mark_submitted(session, sub, {}, None, 45, submitted_at=NOW - timedelta(days=10))
    sub.status = "confirmed"
    session.add(sub)
    session.commit()

    sel = select_brokers([ok, off, web, busy, recent], session, now=NOW)
    assert [b.slug for b in sel.eligible] == ["ok"]
    assert dict(sel.skipped) == {
        "off": "disabled",
        "busy": "request already in progress",
        "recent": "contacted within 90 days",
    }


def _fake_runner(outcomes):
    calls = []

    def runner(session, sub, broker, profile, config):
        calls.append(broker.slug)
        outcome = outcomes.get(broker.slug, "ok")
        if outcome == "ok":
            sub.status = "awaiting_broker_confirmation"
            sub.submitted_at = NOW
        elif outcome == "auth":
            sub.status = "awaiting_user_input"
            sub.failure_reason = "SMTP authentication failed"
        else:
            sub.status = "failed"
            sub.failure_reason = "SMTP error"
        session.add(sub)
        session.commit()

    return runner, calls


def test_campaign_respects_daily_limit_and_pauses(session, config):
    brokers = [_broker(f"b{i}") for i in range(5)]
    runner, calls = _fake_runner({})
    sleeps: list[float] = []
    report = run_campaign(
        session, config, brokers, limit=10, runner=runner, sleep=sleeps.append, now=lambda: NOW
    )
    assert calls == ["b0", "b1", "b2"]  # daily_limit=3
    assert sleeps == [7, 7]
    assert len(report.sent) == 3


def test_campaign_daily_cap_survives_restart(session, config):
    for i in range(3):
        sub = create_submission(
            session,
            broker_slug=f"p{i}",
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
        mark_submitted(session, sub, {}, None, 45, submitted_at=NOW)
    assert sent_today(session, NOW) == 3
    runner, calls = _fake_runner({})
    report = run_campaign(
        session,
        config,
        [_broker("new")],
        limit=5,
        runner=runner,
        sleep=lambda s: None,
        now=lambda: NOW,
    )
    assert calls == [] and "daily limit" in report.stopped_reason


def test_campaign_stops_on_auth_failure(session, config):
    runner, calls = _fake_runner({"b0": "auth"})
    report = run_campaign(
        session,
        config,
        [_broker("b0"), _broker("b1")],
        limit=5,
        runner=runner,
        sleep=lambda s: None,
        now=lambda: NOW,
    )
    assert calls == ["b0"] and "authentication" in report.stopped_reason


def test_campaign_circuit_breaker_on_repeated_failures(session, config):
    config.email.daily_limit = 10
    runner, calls = _fake_runner({f"b{i}": "fail" for i in range(6)})
    report = run_campaign(
        session,
        config,
        [_broker(f"b{i}") for i in range(6)],
        limit=10,
        runner=runner,
        sleep=lambda s: None,
        now=lambda: NOW,
    )
    assert len(calls) == 3 and "3 failures in a row" in report.stopped_reason
