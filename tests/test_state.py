"""The encrypted state file: round trip, wrong key, nothing personal outside it."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from optout import state

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv(state.KEY_ENV, "correct horse battery staple")
    from optout.db import init_db

    init_db()
    cfg = tmp_path / "cfg" / "optout"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yml").write_text("profile: secret\n")
    (cfg / "smtp_password").write_text("hunter2")
    (cfg / "form_recipes.yml").write_text("[]\n")
    return tmp_path


def test_round_trip_restores_history_but_never_the_profile_or_password(dirs):
    from optout.db import create_submission, get_session, list_submissions

    with get_session() as s:
        create_submission(
            s,
            broker_slug="acme",
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
    out = dirs / "state.enc"
    state.export_state(out)
    raw = out.read_bytes()
    assert b"hunter2" not in raw and b"acme" not in raw  # encrypted

    (dirs / "data" / "optout" / "optout.db").unlink()
    restored = state.import_state(out)
    assert "data/optout.db" in restored and "config/form_recipes.yml" in restored
    assert not any("config.yml" in r or "smtp_password" in r for r in restored)
    with get_session() as s:
        assert [x.broker_slug for x in list_submissions(s)] == ["acme"]


def test_wrong_or_short_key_is_refused(dirs, monkeypatch):
    out = dirs / "state.enc"
    state.export_state(out)
    monkeypatch.setenv(state.KEY_ENV, "a different passphrase entirely")
    with pytest.raises(ValueError, match="decrypt"):
        state.import_state(out)
    monkeypatch.setenv(state.KEY_ENV, "short")
    with pytest.raises(ValueError, match="16"):
        state.export_state(out)


def test_push_and_pull_through_a_branch(dirs, tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=work, check=True)
    for k, v in (("user.name", "t"), ("user.email", "t@example.invalid")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    assert state.pull_state(work) is None  # nothing saved yet
    assert state.push_state(work) > 0
    assert "data/optout.db" in (state.pull_state(work) or [])
