"""Walks the whole setup wizard with scripted answers (no terminal needed)."""

from __future__ import annotations

from collections import deque

import pytest

from optout import wizard


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def _script(monkeypatch, prompts, confirms, login_results):
    p, c, logins = deque(prompts), deque(confirms), deque(login_results)
    asked: list[str] = []

    def ask(question, *a, **k):
        asked.append(str(question))
        return p.popleft()

    def confirm(question, *a, **k):
        asked.append(str(question))
        return c.popleft()

    monkeypatch.setattr(wizard.Prompt, "ask", staticmethod(ask))
    monkeypatch.setattr(wizard.Confirm, "ask", staticmethod(confirm))
    monkeypatch.setattr("optout.engine.methods.email.check_login", lambda cfg, pw: logins.popleft())
    opened: list[str] = []
    monkeypatch.setattr("optout.web.server.open_in_browser", lambda url: opened.append(url) or True)
    imported: list[bool] = []

    def fake_import():
        imported.append(True)
        from optout.brokers.importer import ImportSummary

        return ImportSummary(imported=578, disabled=46, merged=25, skipped=0)

    monkeypatch.setattr("optout.brokers.importer.import_ca_registry", fake_import)
    return asked, opened, imported


PROFILE = [
    "Jane Q Example",
    "",
    "1990-01-01",
    "1 Example St",
    "",
    "Richmond",
    "va",
    "23219",
    "",
    "",
]


def test_gmail_happy_path(home, monkeypatch):
    from optout.config import config_path, load_config
    from optout.engine.methods.email import read_smtp_password

    asked, opened, imported = _script(
        monkeypatch,
        prompts=PROFILE + ["jane@gmail.com", "abcd efgh ijkl mnop"],
        #         attest, prev addr, open page, label, load list, (no dashboard prompt)
        confirms=[True, False, True, True, True],
        login_results=[None],
    )
    wizard.run_init_wizard(open_dashboard=False)

    cfg = load_config()
    assert cfg.email.smtp.host == "smtp.gmail.com" and cfg.email.smtp.port == 587
    assert cfg.email.smtp.username == "jane@gmail.com"
    assert cfg.email.gmail_label == "OPTOUT"
    assert cfg.profile.emails.current == ["jane@gmail.com"]
    assert cfg.profile.current_address.state == "VA"
    assert cfg.profile.phones.current == []  # phone is optional now
    assert read_smtp_password(cfg.email.smtp) == "abcd efgh ijkl mnop"
    assert config_path().stat().st_mode & 0o777 == 0o600
    assert opened == ["https://myaccount.google.com/apppasswords"]
    assert imported == [True]
    # never asks a Gmail user for a server name or port
    assert not any("SMTP" in q or "Port" in q for q in asked)


def test_regular_password_is_caught_and_retried(home, monkeypatch):
    from optout.config import load_config
    from optout.engine.methods.email import read_smtp_password

    _script(
        monkeypatch,
        prompts=PROFILE + ["jane@gmail.com", "Hunter2!pass", "abcdefghijklmnop"],
        # attest, prev addr, open page, "try anyway?"=no, label, load list
        confirms=[True, False, False, False, True, False],
        login_results=[None],
    )
    wizard.run_init_wizard(open_dashboard=False)
    assert read_smtp_password(load_config().email.smtp) == "abcdefghijklmnop"


def test_failed_login_is_not_saved(home, monkeypatch):
    from optout.config import load_config
    from optout.engine.methods.email import read_smtp_password

    _script(
        monkeypatch,
        prompts=PROFILE + ["jane@gmail.com", "abcdefghijklmnop"],
        # attest, prev addr, open page, try again?=no, label, load list
        confirms=[True, False, False, False, False, False],
        login_results=["the server rejected the email address or password"],
    )
    monkeypatch.delenv("OPTOUT_SMTP_PASSWORD", raising=False)
    wizard.run_init_wizard(open_dashboard=False)
    assert read_smtp_password(load_config().email.smtp) == ""


def test_other_provider_asks_for_server(home, monkeypatch):
    from optout.config import load_config

    asked, _, _ = _script(
        monkeypatch,
        prompts=PROFILE + ["jane@example.org", "smtp.example.org", "587", "secret"],
        # attest, prev addr, workspace?=no, load list
        confirms=[True, False, False, False],
        login_results=[None],
    )
    wizard.run_init_wizard(open_dashboard=False)
    cfg = load_config()
    assert cfg.email.smtp.host == "smtp.example.org" and cfg.email.gmail_label is None
    assert any("SMTP" in q for q in asked)


def test_not_my_name_stops_without_saving(home, monkeypatch):
    from optout.config import config_path

    _script(monkeypatch, prompts=["Jane Q Example"], confirms=[False], login_results=[])
    with pytest.raises(wizard.typer.Exit):
        wizard.run_init_wizard(open_dashboard=False)
    assert not config_path().exists()
