"""Where to send a broker's request, when the registry's address isn't the right one.

California's registry lists a contact email per broker, and a fair number of those
mailboxes reply "not monitored, use this other address". A correction is stored as
``broker slug -> email`` and applied on top of the generated broker list, so it
survives re-importing the registry. Two sources, the user's own winning:

1. ``data/contact_overrides.yml`` in the package: corrections shared with everyone
2. ``<config dir>/contact_overrides.yml``: this user's (written by "Send my request there")
"""

from __future__ import annotations

import re
from importlib.resources import files

import yaml

from optout import yaml_io

from ..config import config_dir, write_private
from .schema import BrokerDef

_EMAIL_OK = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")
_SLUG_OK = re.compile(r"^[a-z0-9-]{1,80}$")
# Reserved for examples and tests (RFC 2606/6761): never a broker's real mailbox.
_RESERVED = re.compile(r"(\.|@)(example|test|invalid|localhost)$|[.@]example\.(com|net|org)$")


def _parse(data: object) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("broker", "")).strip()
        email = str(item.get("email", "")).strip().lower()
        if _SLUG_OK.match(slug) and _EMAIL_OK.match(email):
            out[slug] = {"email": email, "note": str(item.get("note", ""))[:240]}
    return out


def _local_path():
    return config_dir() / "contact_overrides.yml"


def shared_contacts() -> dict[str, dict[str, str]]:
    try:
        src = files("optout").joinpath("data/contact_overrides.yml")
        return _parse(yaml_io.safe_load(src.read_text()) or [])
    except (OSError, yaml.YAMLError):
        return {}


def local_contacts() -> dict[str, dict[str, str]]:
    try:
        return _parse(yaml_io.safe_load(_local_path().read_text()) or [])
    except (OSError, yaml.YAMLError):
        return {}


def load_contacts() -> dict[str, dict[str, str]]:
    """Shared corrections, with the user's own on top."""
    return {**shared_contacts(), **local_contacts()}


def save_local(slug: str, email: str, note: str = "") -> None:
    """Remember a correction for this user. Raises ValueError for anything malformed."""
    email = email.strip().lower()
    if not _SLUG_OK.match(slug) or not _EMAIL_OK.match(email):
        raise ValueError("not a valid broker or address")
    entries = [{"broker": s, **v} for s, v in local_contacts().items() if s != slug] + [
        {"broker": slug, "email": email, "note": note.strip()[:240]}
    ]
    path = _local_path()
    write_private(path, yaml.safe_dump(entries, sort_keys=False, allow_unicode=True))


def apply_contact_overrides(brokers: dict[str, BrokerDef]) -> dict[str, BrokerDef]:
    out = dict(brokers)
    for slug, entry in load_contacts().items():
        broker = out.get(slug)
        if broker is not None and broker.method == "email":
            out[slug] = broker.model_copy(update={"opt_out_email": entry["email"]})
    return out


def learned_contacts() -> list[dict[str, str]]:
    """The user's corrections that the shared list doesn't already have, for sharing.
    Leaves out placeholder addresses and brokers that aren't in the list."""
    from .registry import all_brokers

    known = {b.slug for b in all_brokers()}
    shared = shared_contacts()
    return [
        {"broker": s, "email": v["email"], "note": v["note"]}
        for s, v in sorted(local_contacts().items())
        if shared.get(s, {}).get("email") != v["email"]
        and s in known
        and not _RESERVED.search(v["email"])
    ]
