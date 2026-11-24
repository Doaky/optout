"""Edit the user's config from the dashboard. Every change goes through the same
pydantic models as the config file, so nothing invalid can be saved."""

from __future__ import annotations

import re
from datetime import date

from pydantic import ValidationError

from ..audiences import GROUPS, expand_groups
from ..config import Address, PreviousAddress, UserConfig

LIMITS = {
    "daily_limit": (1, 500),
    "send_delay_seconds": (0, 120),
    "resend_after_days": (30, 730),
    "backup_every_days": (1, 90),
}


def _list(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def _previous(raw: str, existing: list[PreviousAddress]) -> list[PreviousAddress]:
    """One per line: "12 Oak St, Apt 4, Richmond, VA 23219" (apartment optional).
    Move-in/out years already on file are kept for the same street."""
    years = {a.street.lower(): (a.from_year, a.to_year) for a in existing}
    out = []
    for n, line in enumerate(row.strip() for row in raw.splitlines()):
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        m = re.fullmatch(r"([A-Za-z]{2})\s+(\d{5}(-\d{4})?)", parts[-1]) if parts else None
        if len(parts) < 3 or not m:
            raise ValueError(f"previous address {n + 1}: use 'street, city, ST 12345'")
        start, end = years.get(parts[0].lower(), (None, None))
        out.append(
            PreviousAddress.model_validate(
                {
                    "street": parts[0],
                    "street2": ", ".join(parts[1:-2]),
                    "city": parts[-2],
                    "state": m.group(1).upper(),
                    "zip": m.group(2),
                    "from": start,
                    "to": end,
                }
            )
        )
    return out


def previous_text(cfg: UserConfig) -> str:
    return "\n".join(
        ", ".join(x for x in (a.street, a.street2, a.city, f"{a.state} {a.zip}") if x)
        for a in cfg.profile.previous_addresses
    )


US_STATES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "AS": "American Samoa",
    "GU": "Guam",
    "MP": "Northern Mariana Islands",
    "PR": "Puerto Rico",
    "VI": "U.S. Virgin Islands",
}
_BY_NAME = {name.lower(): code for code, name in US_STATES.items()}
_ZIP = re.compile(r"^\d{5}(-\d{4})?$")


def state_code(raw: str) -> str:
    """A two-letter state code from a code or a full name ("va", "Virginia"). Raises ValueError."""
    text = " ".join(raw.split()).strip(".")
    code = text.upper() if text.upper() in US_STATES else _BY_NAME.get(text.lower(), "")
    if not code:
        raise ValueError("state (pick your state, or use its 2-letter code, e.g. VA)")
    return code


def apply_form(cfg: UserConfig, form: dict[str, str]) -> UserConfig:
    """A new config with the form's values. Raises ValueError naming the bad field."""

    try:
        dob = date.fromisoformat(form.get("dob", "").strip())
    except ValueError:
        raise ValueError("date of birth (use YYYY-MM-DD)") from None
    nums = {}
    current = {  # older forms lack these
        "resend_after_days": str(cfg.email.resend_after_days),
        "backup_every_days": str(cfg.backup_every_days),
    }
    for key, (lo, hi) in LIMITS.items():
        try:
            nums[key] = int(form.get(key) or current.get(key, ""))
        except ValueError:
            raise ValueError(key.replace("_", " ")) from None
        if not lo <= nums[key] <= hi:
            raise ValueError(f"{key.replace('_', ' ')} (between {lo} and {hi})")
    name = form.get("legal_name", "").strip()
    if not name:
        raise ValueError("name")
    data = cfg.model_dump(by_alias=True)
    data["profile"].update(
        legal_name=name,
        aliases=_list(form.get("aliases", "")),
        dob=dob,
        current_address=Address(
            street=form.get("street", "").strip(),
            street2=form.get("street2", "").strip(),
            city=form.get("city", "").strip(),
            state=state_code(form.get("state", "")) if form.get("state", "").strip() else "",
            zip=form.get("zip", "").strip(),
        ).model_dump(),
        previous_addresses=[
            a.model_dump(by_alias=True)
            for a in _previous(form.get("previous", ""), cfg.profile.previous_addresses)
        ],
        relatives=_list(form.get("relatives", "")),
    )
    data["profile"]["phones"]["current"] = _list(form.get("phones", ""))
    for key in ("street", "city", "state", "zip"):
        if not data["profile"]["current_address"][key]:
            raise ValueError("ZIP code" if key == "zip" else key)
    if not _ZIP.match(data["profile"]["current_address"]["zip"]):
        raise ValueError("ZIP code (5 digits, e.g. 23219)")
    pattern = form.get("alias_pattern", "").strip()
    if pattern and "{slug}" not in pattern:
        raise ValueError(
            "reply address: it must include {slug} with the braces, e.g. you+{slug}@gmail.com"
        )
    data["email"].update(
        daily_limit=nums["daily_limit"],
        send_delay_seconds=nums["send_delay_seconds"],
        resend_after_days=nums["resend_after_days"],
        gmail_label=form.get("gmail_label", "").strip() or None,
        reply_label=form.get("reply_label", "").strip() or None,
        alias_pattern=form.get("alias_pattern", "").strip() or None,
        from_address=form.get("from_address", "").strip() or None,
        allow_unattended=form.get("allow_unattended") == "on",
        mark_replies_read=form.get("mark_replies_read") == "on",
        fetch_after_batch=form.get("fetch_after_batch") == "on",
        daily_send_time=form.get("daily_send_time", "").strip() or "10:00",
    )
    data["timezone"] = form.get("timezone", "").strip() or None
    # Groups: only once the user has seen the section (the form says so), otherwise a save
    # from an old page must not wipe the setting.
    if form.get("groups_none") == "on" and not any(form.get(f"group_{g}") == "on" for g in GROUPS):
        data["my_groups"] = []  # said so outright: skip brokers that only cover groups
    elif form.get("groups_present") == "1":
        picked = [g for g in GROUPS if form.get(f"group_{g}") == "on"]
        data["my_groups"] = expand_groups(picked)
    if "backup_dir" in form:
        data["backup_dir"] = form.get("backup_dir", "").strip() or None
    data["backup_every_days"] = nums["backup_every_days"]
    data["contributor_tools"] = form.get("contributor_tools") == "on"
    data["confirm_links"] = "auto" if form.get("confirm_links") == "auto" else "ask"
    try:
        return UserConfig.model_validate(data)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = err.get("loc") or ()
        if loc:
            raise ValueError(str(loc[-1]).replace("_", " ")) from None
        # A whole-model check (e.g. an unknown time zone): use its own message.
        raise ValueError(str(err.get("msg", "a setting")).removeprefix("Value error, ")) from None
