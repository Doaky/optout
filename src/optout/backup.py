"""Automatic encrypted backups of your history to a folder you choose.

Your request history (who was emailed when, replies, deadlines, what you've taught the tool)
only exists on this computer. A backup is the same encrypted file `optout state export`
writes, made whenever one is due: each time the dashboard starts, and while it runs. Starting
the app only now and then is fine: a backup that's due is made straight away.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .config import UserConfig, config_dir

KEEP = 4  # newest backups kept in the folder; older ones are deleted


def key_path() -> Path:
    return config_dir() / "backup_key"


def read_key() -> str | None:
    try:
        key = key_path().read_text().strip()
    except OSError:
        return None
    return key or None


def save_key(passphrase: str) -> None:
    passphrase = passphrase.strip()
    if len(passphrase) < 16:
        raise ValueError("backup passphrase (at least 16 characters)")
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(passphrase)


def last_backup() -> tuple[date, str] | None:
    from .engine.campaign import _schedule_state

    st = _schedule_state()
    try:
        return date.fromisoformat(st["last_backup"]), str(st.get("last_backup_file", ""))
    except (KeyError, ValueError):
        return None


def problem(config: UserConfig) -> str | None:
    """Why backups can't run, or None if they're set up."""
    if not config.backup_dir:
        return "no backup folder chosen"
    if read_key() is None:
        return "no backup passphrase set"
    return None


def due(config: UserConfig, today: date) -> bool:
    if problem(config):
        return False
    last = last_backup()
    return last is None or today >= last[0] + timedelta(days=max(1, config.backup_every_days))


def run(config: UserConfig, now: datetime | None = None) -> Path:
    """Write a backup now. Raises ValueError with a readable reason."""
    from .engine.campaign import _save_schedule_state
    from .state import export_state

    if why := problem(config):
        raise ValueError(why)
    folder = Path(str(config.backup_dir)).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"can't use the backup folder ({exc.strerror})") from exc
    now = now or datetime.now(UTC)
    path = folder / f"optout-backup-{now:%Y-%m-%d}.enc"
    export_state(path, key=read_key())
    for old in sorted(folder.glob("optout-backup-*.enc"))[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    _save_schedule_state(last_backup=now.date().isoformat(), last_backup_file=str(path))
    return path
