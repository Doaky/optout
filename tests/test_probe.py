"""Tests for the read-only selector probe."""

from __future__ import annotations

from optout.brokers.schema import BrokerDef
from optout.engine.probe import build_plan, run_checks


def _broker(steps: list[dict], scan: dict | None = None) -> BrokerDef:
    return BrokerDef.model_validate(
        {
            "slug": "x",
            "name": "X",
            "domain": "x.example",
            "category": "people_search",
            "method": "web_form",
            "opt_out_url": "https://x.example/optout",
            "legal_basis": ["CCPA"],
            "statutory_response_days": 45,
            "scan": scan,
            "steps": steps,
        }
    )


def test_selectors_after_click_are_unreachable():
    b = _broker(
        [
            {"id": "nav", "type": "navigate", "url": "https://x.example/optout"},
            {"id": "w", "type": "wait_for", "selector": "#form"},
            {"id": "go", "type": "click", "selector": "#next"},
            {"id": "ok", "type": "assert", "selector": ".done"},
        ]
    )
    plan = build_plan(b)
    assert [p.url for p in plan.pages] == ["https://x.example/optout"]
    assert [c.selector for c in plan.pages[0].checks] == ["#form", "#next"]
    assert [c.selector for c in plan.unreachable] == [".done"]


def test_search_urls_use_placeholder_identity_not_profile():
    b = _broker(
        [
            {
                "id": "s",
                "type": "search",
                "search_url_template": "https://x.example/{name_title_slug}/{state_full}",
                "result_selector": ".r",
            }
        ],
        scan={"search_url_template": "https://x.example/s/{name_slug}", "result_selector": ".r"},
    )
    urls = [p.url for p in build_plan(b).pages]
    assert urls == ["https://x.example/s/john-smith", "https://x.example/John-Smith/Virginia"]


def test_form_fill_values_never_rendered():
    """Probe checks selectors only; profile-valued templates must not be touched."""
    b = _broker(
        [
            {"id": "nav", "type": "navigate", "url": "https://x.example/optout"},
            {
                "id": "f",
                "type": "form_fill",
                "fields": {"email": {"selector": "#e", "value": "{profile.emails.current[0]}"}},
            },
        ]
    )
    checks = build_plan(b).pages[0].checks
    assert [(c.role, c.selector) for c in checks] == [("form_fill:email", "#e")]


class _Loc:
    def __init__(self, n):
        self.n = n

    def count(self):
        if self.n is None:
            raise ValueError("bad selector\nmore")
        return self.n


class _Page:
    def __init__(self, counts):
        self.counts = counts

    def locator(self, sel):
        return _Loc(self.counts.get(sel, 0))

    def frame_locator(self, sel):
        return self


def test_run_checks_statuses():
    b = _broker(
        [
            {"id": "nav", "type": "navigate", "url": "https://x.example/"},
            {"id": "a", "type": "wait_for", "selector": "#here"},
            {"id": "b", "type": "wait_for", "selector": "#gone"},
            {"id": "c", "type": "wait_for", "selector": "#broken"},
        ]
    )
    page = build_plan(b).pages[0]
    run_checks(_Page({"#here": 2, "#broken": None}), page)
    assert [(c.status, c.count) for c in page.checks] == [
        ("found", 2),
        ("missing", 0),
        ("error", 0),
    ]
    assert page.checks[2].detail == "ValueError: bad selector"


def test_all_shipped_brokers_build_a_plan():
    from importlib.resources import files

    import yaml

    ymls = sorted(files("optout.data.brokers").glob("*.yml"))
    assert len(ymls) >= 5
    for path in ymls:
        broker = BrokerDef.model_validate(yaml.safe_load(path.read_text()))
        assert build_plan(broker).pages, broker.slug
