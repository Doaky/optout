"""Checks whether user data appears on a broker's search results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..brokers.schema import BrokerDef
from ..config import Profile
from ..engine.methods.web_form import _STATE_NAMES


@dataclass
class ScanMatch:
    text: str
    url: str | None = None


@dataclass
class ScanOutcome:
    result: str  # "clean" | "found" | "inconclusive"
    matches: list[ScanMatch] = field(default_factory=list)
    error: str | None = None


def _name_slug(name: str) -> str:
    """'Jane Q Public' → 'Jane-Q-Public'."""
    return "-".join(name.split())


def _name_in_text(name: str, text: str) -> bool:
    """True if every word of name appears (case-insensitive) in text."""
    words = name.lower().split()
    text_lower = text.lower()
    return all(w in text_lower for w in words)


def _build_scan_ctx(profile: Profile) -> dict[str, str]:
    """All template variables available in scan step/URL templates."""
    raw = getattr(profile, "legal_name", "").lower()
    name_slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    name_title_slug = "-".join(w.capitalize() for w in name_slug.split("-"))
    parts = getattr(profile, "legal_name", "").split()
    name_first = parts[0] if parts else ""
    name_last = " ".join(parts[1:]) if len(parts) > 1 else ""
    addr = getattr(profile, "current_address", None)
    state = getattr(addr, "state", "") if addr is not None else ""
    city = getattr(addr, "city", "") if addr is not None else ""
    state_full = _STATE_NAMES.get(state.upper(), state)
    state_name = state_full.replace("-", " ")
    return {
        "name_slug": name_slug,
        "name_title_slug": name_title_slug,
        "name_first": name_first,
        "name_last": name_last,
        "state": state,
        "state_name": state_name,
        "state_full": state_full,
        "city": city,
    }


def _execute_search_step(step: Any, page: Any, ctx: dict[str, str]) -> None:
    """Execute a single navigate/form_fill/click/wait_for step during a scan."""
    step_type = getattr(step, "type", None)

    if step_type == "navigate":
        url = step.url.format_map(ctx)
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")

    elif step_type == "form_fill":
        for _field_name, field_cfg in step.fields.items():
            sel = field_cfg.selector
            val = field_cfg.value.format_map(ctx)
            if field_cfg.is_select:
                page.select_option(sel, val)
            elif field_cfg.use_type:
                page.locator(sel).first.click()
                page.keyboard.type(val, delay=20)
            else:
                page.fill(sel, val)

    elif step_type == "click":
        sel = step.selector.format_map(ctx)
        if sel.startswith("keyboard:"):
            page.keyboard.press(sel.split(":", 1)[1])
        else:
            page.locator(sel).first.click(force=True)

    elif step_type == "wait_for":
        timeout = getattr(step, "timeout_ms", 10_000)
        if step.selector:
            page.wait_for_selector(step.selector, timeout=timeout)
        elif step.js_condition:
            # js_condition is raw JS — not interpolated
            page.wait_for_function(step.js_condition, timeout=timeout)


def _run_scan_on_page(page: Any, broker: BrokerDef, profile: Profile) -> ScanOutcome:
    """Execute a scan using an already-open Playwright page."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    scan_cfg = broker.scan
    if not scan_cfg:
        return ScanOutcome(result="inconclusive", error="Broker has no scan config")

    ctx = _build_scan_ctx(profile)

    try:
        if scan_cfg.search_steps:
            for step in scan_cfg.search_steps:
                try:
                    _execute_search_step(step, page, ctx)
                except Exception:
                    if not getattr(step, "optional", False):
                        raise
        elif scan_cfg.search_url_template:
            url = scan_cfg.search_url_template.format_map(ctx)
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        else:
            return ScanOutcome(
                result="inconclusive", error="Broker scan config has no search method"
            )

        wait_for = scan_cfg.result_selector
        if scan_cfg.empty_selector:
            wait_for = f"{wait_for}, {scan_cfg.empty_selector}"
        page.wait_for_selector(wait_for, timeout=90_000)
    except PlaywrightTimeoutError:
        return ScanOutcome(result="inconclusive", error="Timeout waiting for search results")
    except Exception as exc:
        return ScanOutcome(result="inconclusive", error=str(exc))

    elements = page.query_selector_all(scan_cfg.result_selector)
    if not elements:
        return ScanOutcome(result="clean")

    names_to_check = [profile.legal_name] + list(getattr(profile, "aliases", []))
    matches: list[ScanMatch] = []
    for el in elements:
        try:
            text = el.inner_text()
        except Exception:
            continue

        href: str | None = None
        try:
            href = el.get_attribute("href")
            if not href:
                link = el.query_selector("a")
                if link:
                    href = link.get_attribute("href")
        except Exception:
            pass

        if any(_name_in_text(n, text) for n in names_to_check):
            matches.append(ScanMatch(text=text[:300], url=href))

    return ScanOutcome(result="found" if matches else "clean", matches=matches)


def scan_broker(
    broker: BrokerDef,
    profile: Profile,
    *,
    headless: bool = False,
    slow_mo_ms: int = 0,
    _page: object = None,
) -> ScanOutcome:
    """Scan a single broker and return a ScanOutcome.

    _page injects a Playwright page object (or mock) for testing.
    """
    if _page is not None:
        return _run_scan_on_page(_page, broker, profile)

    if not broker.scan:
        return ScanOutcome(result="inconclusive", error="Broker has no scan config")

    from ..config import PlaywrightConfig
    from ..engine.browser import launch_browser

    playwright_config = PlaywrightConfig(headless=headless, slow_mo_ms=slow_mo_ms)
    with launch_browser(playwright_config) as ctx:
        page = ctx.new_page()
        try:
            return _run_scan_on_page(page, broker, profile)
        finally:
            try:
                page.close()
            except Exception:
                pass
