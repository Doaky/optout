"""Web-form opt-outs walked through in a browser, and the recipes recorded from them.

A broker that only takes requests through a form can't be emailed. Claude (in the user's
browser) reads the broker's reply and the page, fills the form in, and records what it did
as a *recipe*: which profile field goes in which input, what to click, what the page says
when it worked. A recipe holds field names and visible labels only, never the user's own
details, so it can be shared and replayed for the next person.

Two sources, the user's own winning:

1. ``data/form_recipes.yml`` in the package: recipes shared with everyone
2. ``<config dir>/form_recipes.yml``: this user's (written by ``optout forms save``)
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from importlib.resources import files
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlmodel import Session

from .config import Profile, UserConfig, config_dir

# What a step may fill in: a key from profile_fields(), never a literal value.
FIELDS = (
    "full_name",
    "first_name",
    "last_name",
    "email",
    "phone",
    "street",
    "street2",
    "city",
    "state",
    "zip",
    "dob",
    "dob_month",
    "dob_day",
    "dob_year",
)
_SLUG_OK = re.compile(r"^[a-z0-9-]{1,80}$")


class Step(BaseModel):
    action: Literal["fill", "select", "check", "click", "wait_text"]
    field: str | None = None  # for fill / select: a key from FIELDS
    label: str = Field("", max_length=80)  # the input's or button's visible label
    text: str = Field("", max_length=160)  # option / button / page text (select, click, wait_text)
    note: str = Field("", max_length=160)

    @field_validator("field")
    @classmethod
    def _known_field(cls, v: str | None) -> str | None:
        if v is not None and v not in FIELDS:
            raise ValueError(f"field must be one of {', '.join(FIELDS)}")
        return v


class Recipe(BaseModel):
    broker: str
    url: str  # the form's address, without query string or fragment
    steps: list[Step] = Field(min_length=1, max_length=40)
    success_text: str = Field("", max_length=200)  # what the page says once it worked
    needs_human: list[Literal["captcha", "email_verify", "id_upload", "phone_verify"]] = []
    notes: str = Field("", max_length=300)
    recorded: str = ""

    @field_validator("broker")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_OK.match(v):
            raise ValueError("not a broker slug")
        return v

    @field_validator("url")
    @classmethod
    def _plain_https(cls, v: str) -> str:
        u = urlsplit(v.strip())
        if u.scheme != "https" or not u.hostname or u.username or u.password:
            raise ValueError("url must be a plain https address")
        # Query strings and fragments carry personal tokens ("prefill-token=..."): drop them.
        return urlunsplit(("https", u.netloc.lower(), u.path, "", ""))


def profile_fields(config: UserConfig) -> dict[str, str]:
    """The values a form may ask for, keyed by FIELDS. For the user's own browser session."""
    p: Profile = config.profile
    first, _, last = p.legal_name.strip().partition(" ")
    last = p.legal_name.strip().rsplit(" ", 1)[-1] if " " in p.legal_name.strip() else ""
    a = p.current_address
    d: date = p.dob
    return {
        "full_name": p.legal_name,
        "first_name": first,
        "last_name": last,
        "email": p.emails.current[0] if p.emails.current else "",
        "phone": p.phones.current[0] if p.phones.current else "",
        "street": a.street,
        "street2": a.street2,
        "city": a.city,
        "state": a.state,
        "zip": a.zip,
        "dob": d.strftime("%m/%d/%Y"),
        "dob_month": f"{d.month:02d}",
        "dob_day": f"{d.day:02d}",
        "dob_year": str(d.year),
    }


def leaks(recipe: Recipe, config: UserConfig) -> list[str]:
    """Parts of the recipe that contain the user's own details. Must be empty to be saved."""
    values = {v.lower() for v in profile_fields(config).values() if len(v) >= 3}
    values |= {x.lower() for x in config.profile.emails.current + config.profile.phones.current}
    found: list[str] = []
    blobs = [recipe.url, recipe.success_text, recipe.notes]
    for s in recipe.steps:
        blobs += [s.label, s.text, s.note]
    for blob in blobs:
        low = blob.lower()
        found += [v for v in values if v in low]
        if "@" in blob:
            found.append("an email address")
    return sorted(set(found))


# --- storage ------------------------------------------------------------------------


def _local_path():
    return config_dir() / "form_recipes.yml"


def _parse(data: object) -> dict[str, Recipe]:
    out: dict[str, Recipe] = {}
    for item in data if isinstance(data, list) else []:
        try:
            r = Recipe.model_validate(item)
        except (ValidationError, TypeError):
            continue
        out[r.broker] = r
    return out


def shared_recipes() -> dict[str, Recipe]:
    try:
        return _parse(yaml.safe_load(files("optout").joinpath("data/form_recipes.yml").read_text()))
    except (OSError, yaml.YAMLError):
        return {}


def local_recipes() -> dict[str, Recipe]:
    try:
        return _parse(yaml.safe_load(_local_path().read_text()))
    except (OSError, yaml.YAMLError):
        return {}


def load_recipes() -> dict[str, Recipe]:
    return {**shared_recipes(), **local_recipes()}


def save_local(recipe: Recipe, config: UserConfig) -> Recipe:
    """Remember a recipe for this user. Raises ValueError if it contains personal details."""
    if bad := leaks(recipe, config):
        raise ValueError(f"the recipe contains your own details ({', '.join(bad)}); use fields")
    recipe = recipe.model_copy(update={"recorded": datetime.now(UTC).date().isoformat()})
    mine = {**local_recipes(), recipe.broker: recipe}
    path = _local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            [r.model_dump(exclude_defaults=False) for r in mine.values()],
            sort_keys=False,
            allow_unicode=True,
        )
    )
    return recipe


def learned_recipes() -> list[dict]:
    """The user's recipes the shared list doesn't have yet, for sharing."""
    shared = shared_recipes()
    return [
        r.model_dump()
        for s, r in sorted(local_recipes().items())
        if s not in shared or shared[s].steps != r.steps
    ]


# --- the queue ------------------------------------------------------------------------


def todo(session: Session, config: UserConfig, now: datetime | None = None) -> list[dict]:
    """Requests waiting on a web form, with the broker's own words and the link to open."""
    from .engine.assist import redact, trim_for_prompt
    from .web.data import collect

    recipes = load_recipes()
    out = []
    for a in collect(session, config, now or datetime.now(UTC)).attention:
        if a.kind != "need-info" or not a.link:
            continue
        out.append(
            {
                "broker": a.slug,
                "name": a.name,
                "link": a.link,
                "link_host": a.link_host,
                "they_said": a.quote,
                "email": redact(trim_for_prompt(a.email, 3000), config.profile),
                "recipe": a.slug in recipes,
                "covers_only": a.coverage,
            }
        )
    return out


def mark_outcome(session: Session, slug: str, outcome: str, note: str = "") -> str:
    """Record how a form went: 'removed' (the page said so) or 'submitted' (waiting on them)."""
    from .actions import record_reply
    from .db import list_submissions, record_event, update_status

    if outcome == "removed":
        return record_reply(session, slug, "deleted", note or "Their web form said removed.")
    if outcome == "submitted":
        subs = list_submissions(session, broker_slug=slug)
        if not subs:
            raise ValueError(f"No request on record for '{slug}'.")
        sub = max(subs, key=lambda x: x.created_at)
        update_status(session, sub, "awaiting_broker_confirmation")
        record_event(session, sub, "form_submitted", {"note": note[:200]})
        return "Recorded. The deadline restarts now."
    raise ValueError("outcome must be 'removed' or 'submitted'")
