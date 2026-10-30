"""What we've learned a broker's process requires, from what actually happened.

When a confirm link is clicked and works, that broker plainly requires email confirmation.
Learned traits are applied on top of the broker list, like contact corrections. Two
sources, the user's own adding to the shared ones:

1. ``data/broker_traits.yml`` in the package: shared with everyone
2. ``<config dir>/broker_traits.yml``: this user's (written as things happen)
"""

from __future__ import annotations

import re
from importlib.resources import files

import yaml

from ..config import config_dir
from .schema import BrokerDef

# Only flags that make a broker's process harder: learning one can't hide a step.
TRAITS = (
    "requires_email_verification",
    "requires_phone_verification",
    "requires_listing_url",
    "requires_account",
    "requires_id_upload",
    "requires_notarization",
    "requires_device_ids",
    "form_only",
)
_SLUG_OK = re.compile(r"^[a-z0-9-]{1,80}$")


def _parse(data: object) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not _SLUG_OK.match(str(item.get("broker", ""))):
            continue
        got = {t for t in item.get("traits") or [] if t in TRAITS}
        if got:
            out.setdefault(str(item["broker"]), set()).update(got)
    return out


def _local_path():
    return config_dir() / "broker_traits.yml"


def shared_traits() -> dict[str, set[str]]:
    try:
        return _parse(
            yaml.safe_load(files("optout").joinpath("data/broker_traits.yml").read_text())
        )
    except (OSError, yaml.YAMLError):
        return {}


def local_traits() -> dict[str, set[str]]:
    try:
        return _parse(yaml.safe_load(_local_path().read_text()))
    except (OSError, yaml.YAMLError):
        return {}


def note(slug: str, trait: str) -> bool:
    """Remember that this broker requires this. Returns True if it's new."""
    if trait not in TRAITS or not _SLUG_OK.match(slug):
        return False
    mine = local_traits()
    if trait in mine.get(slug, set()) or trait in shared_traits().get(slug, set()):
        return False
    mine.setdefault(slug, set()).add(trait)
    path = _local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            [{"broker": s, "traits": sorted(t)} for s, t in sorted(mine.items())],
            sort_keys=False,
        )
    )
    return True


def forget(slug: str, trait: str) -> bool:
    """Drop something learned locally (it turned out to be wrong). True if it was there."""
    mine = local_traits()
    if trait not in mine.get(slug, set()):
        return False
    mine[slug].discard(trait)
    path = _local_path()
    path.write_text(
        yaml.safe_dump(
            [{"broker": s, "traits": sorted(t)} for s, t in sorted(mine.items()) if t],
            sort_keys=False,
        )
    )
    return True


def apply_traits(brokers: dict[str, BrokerDef]) -> dict[str, BrokerDef]:
    out = dict(brokers)
    merged = shared_traits()
    for slug, t in local_traits().items():
        merged.setdefault(slug, set()).update(t)
    for slug, t in merged.items():
        if slug in out:
            out[slug] = out[slug].model_copy(update={k: True for k in t})
    return out


def learned_traits() -> list[dict]:
    """The user's learned traits the shared list doesn't have yet, for sharing."""
    shared = shared_traits()
    return [
        {"broker": s, "traits": sorted(t - shared.get(s, set()))}
        for s, t in sorted(local_traits().items())
        if t - shared.get(s, set())
    ]


# A reply that says "not by email, use our form" (checked only on replies sorted as need-info).
_FORM_ONLY = re.compile(
    r"\b(do not|don't|cannot|can't|unable to|will not|won't)\s+(accept|process|honou?r)\b[^.]{0,50}"
    r"\b(e-?mail|requests? (sent|submitted|received) (via|by|through))|"
    r"\b(not|no longer) (a |the )?(designated|monitored|supported|intended)\b[^.]{0,60}"
    r"\b(requests?|privacy|channel|method)|"
    r"\b(must|need to|required to|please)\s+(be\s+)?(submit(ted)?|use|complete|fill out|visit)\b"
    r"[^.]{0,60}\b(web ?form|online form|request form|form|portal|privacy center)\b|"
    r"\brequests? must be submitted through\b",
    re.I,
)


def says_form_only(text: str) -> bool:
    return _FORM_ONLY.search(re.sub(r"\s+", " ", text)) is not None
