"""Guards the 'PII stays local' rule: nothing personal in files tracked by git.

Two checks:
- SSN-shaped strings and .eml/.db/config artifacts must never be tracked.
- If a real config exists on this machine, none of its identifying values
  (name, street, emails, phones) may appear in any tracked file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
FORBIDDEN_SUFFIXES = (".eml", ".db", ".sqlite", ".sqlite3")


def _tracked() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.decode().split("\0") if p]


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def test_no_artifacts_tracked():
    bad = [str(p.relative_to(ROOT)) for p in _tracked() if p.suffix in FORBIDDEN_SUFFIXES]
    assert not bad, f"PII-bearing artifacts are tracked: {bad}"


def test_no_ssn_shaped_strings():
    hits = [
        str(p.relative_to(ROOT)) for p in _tracked() if p.name != "uv.lock" and SSN.search(_text(p))
    ]
    assert not hits, f"SSN-shaped strings in: {hits}"


def test_real_config_values_not_in_tracked_files():
    from optout.config import config_path, load_config

    if not config_path().exists():
        pytest.skip("no local config to check against")
    cfg = load_config().profile
    secrets = {cfg.legal_name, cfg.current_address.street, *cfg.emails.current, *cfg.phones.current}
    secrets |= {a.street for a in cfg.previous_addresses}
    secrets = {s for s in secrets if s and len(s) >= 6}
    example_values = {"Jane Q Example", "123 Example St"}  # placeholders ship in examples/
    leaks = [
        f"{p.relative_to(ROOT)}: {s[:3]}…"
        for p in _tracked()
        for s in secrets - example_values
        if s.lower() in _text(p).lower()
    ]
    assert not leaks, f"real config values found in tracked files: {leaks}"
