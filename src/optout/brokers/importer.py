"""Turn a public data-broker registry CSV into broker definitions.

Registry contents are untrusted input. We only ever read the CSV, validate the
contact email strictly (a single bare address, no whitespace or separators),
and store URLs as inert text. Nothing here opens a URL.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
_SUFFIXES = {"inc", "llc", "corp", "corporation", "co", "ltd", "lp", "llp", "company", "limited"}
_EXEMPTION_COLS = {
    "FCRA": lambda k: "regulated by the federal fair credit" in k and not k.startswith("if "),
    "GLBA": lambda k: k.startswith("the data broker") and "gramm" in k,
    "HIPAA": lambda k: k.startswith("the data broker") and "hipaa" in k,
}


@dataclass
class ImportedBroker:
    name: str
    slug: str
    domain: str
    email: str
    privacy_url: str | None
    exemptions: list[str] = field(default_factory=list)
    covers: list[str] = field(default_factory=list)
    delete_received: int | None = None
    delete_denied: int | None = None


@dataclass
class ImportResult:
    brokers: list[ImportedBroker]
    skipped: list[tuple[str, str]]  # (name, reason)


def _clean(text: str | None) -> str:
    """Collapse whitespace and drop control characters."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", text or "")).strip()


def _slugify(name: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return "-".join(words)[:50].strip("-") or "broker"


def _domain(website: str, email: str) -> str:
    first = _clean(website).split(";")[0].split(",")[0].strip()
    if first:
        host = urlparse(first if "//" in first else f"//{first}").hostname or ""
        host = host.removeprefix("www.")
        if "." in host:
            return host.lower()
    return email.rsplit("@", 1)[1].lower()


def _to_int(value: str | None) -> int | None:
    try:
        return int(_clean(value).replace(",", ""))
    except ValueError:
        return None


def _find(row: dict[str, str], pred) -> str:
    for key, value in row.items():
        if key and pred(_clean(key).lower()):
            return value or ""
    return ""


def parse_ca_registry(path: Path) -> ImportResult:
    """Parse a California data-broker registry CSV (CPPA export)."""
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = list(csv.DictReader(fh))

    skipped: list[tuple[str, str]] = []
    by_email: dict[str, ImportedBroker] = {}
    used_slugs: set[str] = set()

    for row in rows:
        name = _clean(_find(row, lambda k: k.startswith("data broker name")))
        if not name:
            continue
        email = _clean(_find(row, lambda k: k.startswith("data broker primary contact email")))
        if not _EMAIL_RE.match(email):
            skipped.append((name, "no valid contact email"))
            continue
        key = email.lower()
        if key in by_email:
            by_email[key].covers.append(name)
            continue

        slug = _slugify(name)
        base, n = slug, 2
        while slug in used_slugs:
            slug, n = f"{base}-{n}", n + 1
        used_slugs.add(slug)

        privacy = _clean(_find(row, lambda k: k.startswith("data broker's primary website that")))
        exemptions = [
            label
            for label, pred in _EXEMPTION_COLS.items()
            if _clean(_find(row, pred)).lower() == "yes"
        ]
        by_email[key] = ImportedBroker(
            name=name,
            slug=slug,
            domain=_domain(
                _find(row, lambda k: k.startswith("data broker primary website")), email
            ),
            email=email,
            privacy_url=privacy.split(";")[0].strip() or None,
            exemptions=exemptions,
            delete_received=_to_int(
                _find(row, lambda k: k == "requests to delete - total requests received")
            ),
            delete_denied=_to_int(
                _find(row, lambda k: k == "requests to delete - total requests received - denied")
            ),
        )

    return ImportResult(brokers=list(by_email.values()), skipped=skipped)


def to_broker_dict(b: ImportedBroker, source: str, verified_on: str) -> dict:
    notes = [f"Imported from {source} on {verified_on}; contact email is unverified."]
    if b.delete_received is not None:
        notes.append(
            f"Registry-reported delete requests (2024): {b.delete_received} received, "
            f"{b.delete_denied if b.delete_denied is not None else '?'} denied."
        )
    if b.exemptions:
        notes.append(
            f"Self-reports as regulated by {', '.join(b.exemptions)}: state deletion "
            "rights usually exempt that data, so this broker is disabled by default."
        )
    data: dict = {
        "slug": b.slug,
        "name": b.name,
        "domain": b.domain,
        "category": "data_broker",
        "method": "email",
        "opt_out_email": b.email,
        "privacy_url": b.privacy_url,
        "required_fields": ["full_name", "current_address"],
        "legal_basis": ["VCDPA"],
        "statutory_response_days": 45,
        "enabled": not b.exemptions,
        "source": source,
        "exemptions": b.exemptions,
        "covers": b.covers,
        "notes": " ".join(notes),
    }
    return {k: v for k, v in data.items() if v not in (None, [])}


def write_yaml_dir(result: ImportResult, out_dir: Path, source: str, verified_on: str) -> int:
    """Replace the generated YAML files in out_dir. Returns the number written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.yml"):
        old.unlink()
    for b in result.brokers:
        text = yaml.safe_dump(to_broker_dict(b, source, verified_on), sort_keys=False)
        (out_dir / f"{b.slug}.yml").write_text(text)
    return len(result.brokers)
