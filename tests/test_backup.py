"""Automatic backups: due after the interval (caught up on start), encrypted, newest four kept."""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime, timedelta

import pytest

from optout import backup, state
from optout.config import Address, EmailConfig, EmailSet, PhoneSet, Profile, SmtpConfig, UserConfig
from optout.db import create_submission, get_session, init_db, list_submissions

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


def _cfg(folder):
    return UserConfig(
        profile=Profile(
            legal_name="Jane Q Public",
            dob=date(1990, 5, 14),
            current_address=Address(street="1 Main St", city="Richmond", state="VA", zip="23219"),
            phones=PhoneSet(current=[]),
            emails=EmailSet(current=["jane@example.invalid"]),
        ),
        email=EmailConfig(
            method="smtp", smtp=SmtpConfig(host="h", username="jane@example.invalid")
        ),
        backup_dir=str(folder) if folder else None,
        backup_every_days=7,
    )


def test_off_until_a_folder_and_passphrase_are_set(tmp_path):
    assert backup.problem(_cfg(None)) == "no backup folder chosen"
    assert backup.problem(_cfg(tmp_path)) == "no backup passphrase set"
    with pytest.raises(ValueError):
        backup.save_key("short")
    backup.save_key("a long enough passphrase")
    assert backup.problem(_cfg(tmp_path)) is None
    assert oct(backup.key_path().stat().st_mode & 0o777) == "0o600"


def test_due_when_the_interval_has_passed_even_after_a_long_gap(tmp_path, monkeypatch):
    init_db()
    backup.save_key("a long enough passphrase")
    cfg = _cfg(tmp_path / "backups")
    today = date(2026, 11, 1)
    assert backup.due(cfg, today)  # never backed up
    backup.run(cfg, now=datetime(2026, 11, 1, 12, tzinfo=UTC))
    assert not backup.due(cfg, today + timedelta(days=6))
    assert backup.due(cfg, today + timedelta(days=40))  # app closed for weeks: due at once


def test_a_backup_restores_and_only_the_newest_four_are_kept(tmp_path, monkeypatch):
    init_db()
    with get_session() as s:
        create_submission(
            s,
            broker_slug="acme",
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
    backup.save_key("a long enough passphrase")
    cfg = _cfg(tmp_path / "backups")
    for d in range(6):
        backup.run(cfg, now=datetime(2026, 11, 1 + d, tzinfo=UTC))
    files = sorted((tmp_path / "backups").glob("optout-backup-*.enc"))
    assert [f.name for f in files][0] == "optout-backup-2026-11-03.enc" and len(files) == 4
    assert b"acme" not in files[-1].read_bytes()  # encrypted

    from optout.db import db_path

    db_path().unlink()
    monkeypatch.setenv(state.KEY_ENV, "a long enough passphrase")
    state.import_state(files[-1])
    with get_session() as s:
        assert [x.broker_slug for x in list_submissions(s)] == ["acme"]
