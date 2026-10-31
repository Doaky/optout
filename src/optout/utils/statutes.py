"""
Statutory right-to-delete citations used in opt-out emails and letters.

The state laws a request cites by the user's state come from data/statutes.yml, where each
quote is checked word for word against the state's official text (`optout statutes verify`).
The entries below are older, unverified defaults kept for broker definitions that name them.

Last reviewed: 2026-04-01
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Statute:
    key: str  # matches the key used in broker YAML legal_basis lists
    name: str  # full statute name
    citation: str  # pinpoint citation
    right_text: str  # the actual statutory language granting the deletion right
    response_days: int  # default statutory response deadline
    response_note: str  # any extension or tolling rules
    reviewed_at: str  # ISO date of last review
    state: str = ""  # the state whose residents it covers (verified state laws only)
    source: str = ""  # official text it was checked against
    deadline_citation: str = ""
    extension_days: int = 0
    appeal_days: int | None = None
    verified: bool = False  # every quote found word for word at the official source


STATUTES: dict[str, Statute] = {
    "CCPA": Statute(
        key="CCPA",
        name="California Consumer Privacy Act",
        citation="Cal. Civ. Code § 1798.105(a)",
        right_text=(
            "A consumer shall have the right to request that a business delete any "
            "personal information about the consumer which the business has collected "
            "from the consumer."
        ),
        response_days=45,
        response_note="May be extended once by an additional 45 days with notice.",
        reviewed_at="2026-04-01",
    ),
    "CPRA": Statute(
        key="CPRA",
        name="California Privacy Rights Act",
        citation="Cal. Civ. Code § 1798.105(a) (as amended by CPRA)",
        right_text=(
            "A consumer shall have the right to request that a business and a business's "
            "service providers or contractors delete personal information about the consumer "
            "which the business or its service providers or contractors have collected from "
            "the consumer."
        ),
        response_days=45,
        response_note="May be extended once by an additional 45 days with notice.",
        reviewed_at="2026-04-01",
    ),
    "GDPR": Statute(
        key="GDPR",
        name="General Data Protection Regulation",
        citation="GDPR Art. 17(1)",
        right_text=(
            "The data subject shall have the right to obtain from the controller the "
            "erasure of personal data concerning him or her without undue delay and the "
            "controller shall have the obligation to erase personal data without undue "
            "delay where one of the following grounds applies: the personal data are no "
            "longer necessary in relation to the purposes for which they were collected "
            "or otherwise processed."
        ),
        response_days=30,
        response_note="No extension permitted under Art. 12(3) without justification.",
        reviewed_at="2026-04-01",
    ),
    "VCDPA": Statute(
        key="VCDPA",
        name="Virginia Consumer Data Protection Act",
        citation="Va. Code § 59.1-577(A)(3)",
        right_text=("To delete personal data provided by or obtained about the consumer."),
        response_days=45,
        response_note=(
            "Va. Code § 59.1-577(B)(1): may be extended once by 45 additional days "
            "when reasonably necessary, with notice and reason within the first 45 days."
        ),
        reviewed_at="2026-10-28",
    ),
    "CPA": Statute(
        key="CPA",
        name="Colorado Privacy Act",
        citation="C.R.S. § 6-1-1306(1)(c)",
        right_text=(
            "A consumer has the right to delete personal data concerning the consumer "
            "that the consumer provided to the controller."
        ),
        response_days=45,
        response_note="May be extended once by an additional 45 days with notice.",
        reviewed_at="2026-04-01",
    ),
    "CTDPA": Statute(
        key="CTDPA",
        name="Connecticut Data Privacy Act",
        citation="C.G.S. § 42-520(a)(3)",
        right_text=(
            "A consumer has the right to delete personal data provided by or obtained "
            "about the consumer."
        ),
        response_days=45,
        response_note="May be extended once by an additional 45 days with notice.",
        reviewed_at="2026-04-01",
    ),
    "UCPA": Statute(
        key="UCPA",
        name="Utah Consumer Privacy Act",
        citation="Utah Code § 13-61-302(1)(c)",
        right_text=(
            "A consumer has the right to delete personal data that the consumer provided "
            "to the controller."
        ),
        response_days=45,
        response_note="No extension provision; 45-day deadline is firm.",
        reviewed_at="2026-04-01",
    ),
}


def get_statutes(keys: list[str]) -> list[Statute]:
    """Return Statute objects for the given keys, silently dropping unknowns."""
    return [STATUTES[k] for k in keys if k in STATUTES]


def _load_verified() -> dict[str, Statute]:
    """State laws from data/statutes.yml: each quote checked against the official text."""
    from importlib.resources import files

    import yaml

    out: dict[str, Statute] = {}
    for e in yaml.safe_load(files("optout").joinpath("data/statutes.yml").read_text()) or []:
        if not e.get("verified"):
            continue
        days, ext = e.get("response_days"), e.get("extension_days")
        note = (
            f"{e['deadline_citation']}: may be extended once by {ext} additional days when "
            "reasonably necessary, with notice within the first period."
            if days and ext
            else ""
        )
        out[e["key"]] = Statute(
            key=e["key"],
            name=e["name"],
            citation=e["citation"],
            right_text=e["quote"].rstrip(";.") + ".",
            response_days=int(days or 0),
            response_note=note,
            reviewed_at=str(e["verified"]),
            state=e["state"],
            source=e["source"],
            deadline_citation=e.get("deadline_citation", "") if days else "",
            extension_days=int(ext or 0) if days else 0,
            appeal_days=e.get("appeal_days"),
            verified=True,
        )
    return out


VERIFIED = _load_verified()
STATUTES.update(VERIFIED)
# Which law a bulk request cites, by the user's state: only laws checked against the
# official text (data/statutes.yml). Anywhere else, requests use general wording.
STATE_LAW: dict[str, str] = {s.state: s.key for s in VERIFIED.values()}


def for_state(state: str) -> Statute | None:
    key = STATE_LAW.get(state.strip().upper())
    return STATUTES[key] if key else None


def statutes_for_state(state: str) -> list[Statute]:
    key = STATE_LAW.get(state.strip().upper())
    return [STATUTES[key]] if key else []
