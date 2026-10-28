"""Read-only selector probe for broker definitions.

Walks a broker's steps and checks that each selector still resolves on the
live page. It never fills, clicks, or submits anything: the only browser
actions are page loads. Search pages are loaded with a generic placeholder
identity, never the user's profile, so no PII is sent to the broker.

Selectors that only appear after a click (or after a user prompt that may
change the page) cannot be reached without interacting, so they are
reported as ``unreachable`` rather than guessed at.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from ..brokers.schema import BrokerDef

# Generic, obviously-not-the-user identity used to render search URL templates.
PLACEHOLDER_CTX: dict[str, str] = {
    "name_slug": "john-smith",
    "name_title_slug": "John-Smith",
    "name_first": "John",
    "name_last": "Smith",
    "state": "VA",
    "state_name": "Virginia",
    "state_full": "Virginia",
    "city": "Richmond",
}

CheckStatus = Literal["found", "missing", "unreachable", "error"]


@dataclass
class SelectorCheck:
    step_id: str
    role: str  # e.g. "wait_for", "form_fill:email", "click", "scan.result"
    selector: str
    frame: str | None = None
    status: CheckStatus = "unreachable"
    count: int = 0
    detail: str | None = None


@dataclass
class ProbePage:
    url: str
    source: str  # "scan", "search:<step id>", "navigate:<step id>"
    checks: list[SelectorCheck] = field(default_factory=list)
    load_error: str | None = None
    final_url: str | None = None  # after redirects; a block page often shows here
    title: str | None = None


@dataclass
class ProbePlan:
    pages: list[ProbePage]
    unreachable: list[SelectorCheck]


def _render(template: str) -> str:
    return template.format_map(defaultdict(str, PLACEHOLDER_CTX))


def _step_checks(step: Any) -> list[SelectorCheck]:
    t = step.type
    frame = getattr(step, "frame", None)
    if t == "form_fill":
        return [
            SelectorCheck(step.id, f"form_fill:{name}", f.selector, frame)
            for name, f in step.fields.items()
        ]
    if t == "wait_for" and step.selector:
        return [SelectorCheck(step.id, "wait_for", step.selector, frame)]
    if t in ("click", "prompt_user_if_present"):
        return [SelectorCheck(step.id, t, step.selector, frame)]
    if t == "assert" and step.selector:
        return [SelectorCheck(step.id, "assert", step.selector)]
    if t == "sms_verification_prompt":
        return [SelectorCheck(step.id, "sms_code", step.code_selector)]
    return []


def build_plan(broker: BrokerDef) -> ProbePlan:
    """Group a broker's selectors by the page they should be checked on."""
    pages: list[ProbePage] = []
    unreachable: list[SelectorCheck] = []

    if broker.scan and broker.scan.search_url_template:
        pages.append(
            ProbePage(
                url=_render(broker.scan.search_url_template),
                source="scan",
                checks=[SelectorCheck("scan", "scan.result", broker.scan.result_selector)],
            )
        )

    current: ProbePage | None = None
    for step in broker.steps:
        if step.type == "search":
            current = ProbePage(
                url=_render(step.search_url_template),
                source=f"search:{step.id}",
                checks=[SelectorCheck(step.id, "search.result", step.result_selector)],
            )
            pages.append(current)
            # Picking a result needs the user, so whatever follows is a new page.
            current = None
            continue
        if step.type in ("navigate", "open_in_browser"):
            current = ProbePage(url=_render(step.url), source=f"navigate:{step.id}")
            pages.append(current)
            continue
        if step.type in ("click", "collect_input", "prompt_user", "prompt_user_if_present"):
            # Checks on the clicked element itself are fine; anything after may
            # live on a different page.
            if current is not None:
                current.checks.extend(_step_checks(step))
            current = None
            continue

        checks = _step_checks(step)
        if current is None:
            unreachable.extend(checks)
        else:
            current.checks.extend(checks)

    return ProbePlan(pages=pages, unreachable=unreachable)


# Page titles that mean we hit a challenge or block page, not the broker's page.
_BLOCK_TITLE_MARKERS = (
    "just a moment",  # Cloudflare
    "attention required",  # Cloudflare
    "access denied",
    "transferred by court order",
)


def blocked_reason(title: str | None) -> str | None:
    """Return a short reason if the page title looks like a challenge/block page."""
    t = (title or "").lower()
    for marker in _BLOCK_TITLE_MARKERS:
        if marker in t:
            return marker
    return None


def run_checks(page: Any, probe_page: ProbePage) -> None:
    """Count matches for each selector on an already-loaded Playwright page."""
    for check in probe_page.checks:
        try:
            root = page.frame_locator(check.frame) if check.frame else page
            n = root.locator(check.selector).count()
        except Exception as exc:  # malformed selector, detached frame, etc.
            check.status = "error"
            check.detail = type(exc).__name__ + ": " + str(exc).splitlines()[0][:200]
            continue
        check.count = n
        check.status = "found" if n > 0 else "missing"


def summarize(plan: ProbePlan) -> tuple[bool | None, str]:
    """Reduce a finished probe to (ok, detail).

    ok is None when the result says nothing about the broker (every page was a
    challenge/block page or failed to load), so it is not counted as drift.
    """
    judged = [p for p in plan.pages if not p.load_error and not blocked_reason(p.title)]
    if not judged:
        return None, "no page could be read (blocked or failed to load)"
    bad = [c for p in judged for c in p.checks if c.status in ("missing", "error")]
    if bad:
        return False, "; ".join(f"{c.role} {c.selector}" for c in bad[:5])
    return True, "all reachable selectors found"
