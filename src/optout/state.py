"""Carry this tool's state between machines, e.g. to a scheduled GitHub Actions run.

The state is everything that isn't the profile itself: the request database (what was sent
to whom, replies, deadlines), the imported broker list, drafts, and what you've taught it
(contact corrections, confirm flows, form recipes, enabled/disabled brokers). It is packed
into one file and encrypted with a passphrase you choose (``OPTOUT_STATE_KEY``), using
OpenSSL's AES-256 with a slow key derivation. ``config.yml`` (your profile) and the SMTP
password are never included: a scheduled run gets those from its own secrets.

``push`` / ``pull`` keep that file on a branch of your own repository (``optout-state``)
as a single commit with no history, so earlier copies don't pile up.
"""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .config import config_dir
from .db import db_path

BRANCH = "optout-state"
FILENAME = "state.enc"
KEY_ENV = "OPTOUT_STATE_KEY"
_DATA_PARTS = ("inbox-state.json", "imported", "drafts")  # plus optout.db, copied safely
_CONFIG_SKIP = {"config.yml", "smtp_password"}
_MAX_BYTES = 200_000_000


def _key(given: str | None = None) -> str:
    key = given if given is not None else os.environ.get(KEY_ENV, "")
    if len(key) < 16:
        raise ValueError(f"Set {KEY_ENV} to a passphrase of at least 16 characters.")
    return key


def _openssl(args: list[str], data: bytes, key: str | None = None) -> bytes:
    if shutil.which("openssl") is None:
        raise ValueError("OpenSSL isn't installed; it's needed to encrypt the state.")
    base = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-md", "sha256"]
    proc = subprocess.run(
        [*base, *args, "-pass", f"env:{KEY_ENV}"],
        input=data,
        capture_output=True,
        env={**os.environ, KEY_ENV: _key(key)},
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError("Couldn't decrypt the state: wrong OPTOUT_STATE_KEY, or a damaged file.")
    return proc.stdout


def _pack() -> bytes:
    data_dir, cfg = db_path().parent, config_dir()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar, tempfile.TemporaryDirectory() as tmp:
        if db_path().exists():
            copy = Path(tmp) / "optout.db"
            src, dst = sqlite3.connect(db_path()), sqlite3.connect(copy)
            try:
                src.backup(dst)  # a consistent copy even if the dashboard is running
            finally:
                src.close()
                dst.close()
            tar.add(copy, arcname="data/optout.db")
        for part in _DATA_PARTS:
            if (data_dir / part).exists():
                tar.add(data_dir / part, arcname=f"data/{part}")
        for f in sorted(cfg.glob("*.yml")) if cfg.exists() else []:
            if f.name not in _CONFIG_SKIP:
                tar.add(f, arcname=f"config/{f.name}")
    return buf.getvalue()


def export_state(path: Path, key: str | None = None) -> int:
    """Write the encrypted state to path. Returns its size in bytes."""
    blob = _openssl(["-e", "-salt"], _pack(), key)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(blob)
    return len(blob)


def import_state(path: Path) -> list[str]:
    """Replace the local state with the one in path. Returns the files restored."""
    raw = path.read_bytes()
    if len(raw) > _MAX_BYTES:
        raise ValueError("That state file is unexpectedly large; not restoring it.")
    plain = _openssl(["-d"], raw)
    data_dir, cfg = db_path().parent, config_dir()
    restored: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        members = []
        for m in tar.getmembers():
            top, _, rest = m.name.partition("/")
            if top not in ("data", "config") or not rest or ".." in Path(rest).parts:
                continue
            if top == "config" and (Path(rest).name in _CONFIG_SKIP or "/" in rest):
                continue
            if not (m.isfile() or m.isdir()):
                continue
            members.append(m)
        with tempfile.TemporaryDirectory() as tmp:
            tar.extractall(tmp, members=members, filter="data")
            for top, dest in (("data", data_dir), ("config", cfg)):
                root = Path(tmp) / top
                if not root.exists():
                    continue
                dest.mkdir(parents=True, exist_ok=True)
                for item in root.iterdir():
                    target = dest / item.name
                    if target.is_dir():
                        shutil.rmtree(target)
                    shutil.move(str(item), target)
                    restored.append(f"{top}/{item.name}")
    return sorted(restored)


# --- keeping it on a branch ----------------------------------------------------------------


def _git(args: list[str], repo: Path, data: bytes | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, input=data, capture_output=True, check=False)
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ValueError(f"git {args[0]} failed: {msg[-1] if msg else 'unknown error'}")
    return proc.stdout.decode("utf-8", "replace").strip()


def push_state(repo: Path, remote: str = "origin") -> int:
    """Encrypt the state and put it on the optout-state branch (replacing what was there)."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / FILENAME
        size = export_state(f)
        blob = _git(["hash-object", "-w", str(f)], repo)
    tree = _git(["mktree"], repo, f"100644 blob {blob}\t{FILENAME}\n".encode())
    commit = _git(["commit-tree", tree, "-m", "optout state (encrypted)"], repo)
    _git(["push", "--force", remote, f"{commit}:refs/heads/{BRANCH}"], repo)
    return size


def pull_state(repo: Path, remote: str = "origin") -> list[str] | None:
    """Restore the state from the optout-state branch. None if there isn't one yet."""
    try:
        _git(["fetch", "--depth", "1", remote, BRANCH], repo)
    except ValueError:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / FILENAME
        proc = subprocess.run(
            ["git", "show", f"FETCH_HEAD:{FILENAME}"], cwd=repo, capture_output=True, check=False
        )
        if proc.returncode != 0:
            return None
        f.write_bytes(proc.stdout)
        return import_state(f)
