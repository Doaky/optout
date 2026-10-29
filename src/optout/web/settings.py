"""Edit the user's config from the dashboard. Every change goes through the same
pydantic models as the config file, so nothing invalid can be saved."""

from __future__ import annotations

import re
from datetime import date

from pydantic import ValidationError

from ..config import Address, PreviousAddress, UserConfig

LIMITS = {"daily_limit": (1, 200), "send_delay_seconds": (0, 120)}


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


def apply_form(cfg: UserConfig, form: dict[str, str]) -> UserConfig:
    """A new config with the form's values. Raises ValueError naming the bad field."""

    try:
        dob = date.fromisoformat(form.get("dob", "").strip())
    except ValueError:
        raise ValueError("date of birth (use YYYY-MM-DD)") from None
    nums = {}
    for key, (lo, hi) in LIMITS.items():
        try:
            nums[key] = int(form.get(key, ""))
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
            state=form.get("state", "").strip().upper(),
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
            raise ValueError(key)
    data["email"].update(
        daily_limit=nums["daily_limit"],
        send_delay_seconds=nums["send_delay_seconds"],
        gmail_label=form.get("gmail_label", "").strip() or None,
        reply_label=form.get("reply_label", "").strip() or None,
        alias_pattern=form.get("alias_pattern", "").strip() or None,
        from_address=form.get("from_address", "").strip() or None,
        allow_unattended=form.get("allow_unattended") == "on",
        daily_send_time=form.get("daily_send_time", "").strip() or "10:00",
    )
    data["timezone"] = form.get("timezone", "").strip() or None
    try:
        return UserConfig.model_validate(data)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = err.get("loc") or ()
        if loc:
            raise ValueError(str(loc[-1]).replace("_", " ")) from None
        # A whole-model check (e.g. an unknown time zone): use its own message.
        raise ValueError(str(err.get("msg", "a setting")).removeprefix("Value error, ")) from None
