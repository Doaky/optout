"""Tests for broker YAML loader and schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from optout.brokers.loader import load_all_brokers, load_broker
from optout.brokers.schema import (
    FormFillStep,
)

FIXTURES = Path(__file__).parent / "fixtures" / "brokers"


class TestLoadBroker:
    def test_email_broker_loads(self) -> None:
        broker = load_broker(FIXTURES / "email_broker.yml")
        assert broker.slug == "test-email-broker"
        assert broker.method == "email"
        assert broker.opt_out_email == "optout@example-email.com"
        assert "CCPA" in broker.legal_basis
        assert broker.statutory_response_days == 30

    def test_web_form_broker_loads(self) -> None:
        broker = load_broker(FIXTURES / "web_form_broker.yml")
        assert broker.slug == "test-web-broker"
        assert broker.method == "web_form"
        assert broker.opt_out_url == "https://example-web.com/opt-out"
        assert broker.scan is not None
        assert "{name_slug}" in broker.scan.search_url_template

    def test_web_form_broker_steps(self) -> None:
        broker = load_broker(FIXTURES / "web_form_broker.yml")
        assert len(broker.steps) == 6

        step_types = [type(s).__name__ for s in broker.steps]
        assert step_types == [
            "SearchStep",
            "NavigateStep",
            "FormFillStep",
            "PromptUserIfPresentStep",
            "ClickStep",
            "CaptureStep",
        ]

    def test_form_fill_step_fields(self) -> None:
        broker = load_broker(FIXTURES / "web_form_broker.yml")
        fill = next(s for s in broker.steps if isinstance(s, FormFillStep))
        assert "listing_url" in fill.fields
        assert fill.fields["listing_url"].selector == "input[name='url']"

    def test_missing_opt_out_email_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yml"
        bad.write_text(
            "slug: bad\nname: Bad\ndomain: bad.com\ncategory: people_search\n"
            "method: email\nopt_out_email: null\n"
            "legal_basis: [CCPA]\nstatutory_response_days: 30\n"
        )
        with pytest.raises(ValueError, match="opt_out_email is required"):
            load_broker(bad)

    def test_missing_opt_out_url_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yml"
        bad.write_text(
            "slug: bad\nname: Bad\ndomain: bad.com\ncategory: people_search\n"
            "method: web_form\nopt_out_url: null\n"
            "legal_basis: [CCPA]\nstatutory_response_days: 30\n"
        )
        with pytest.raises(ValueError, match="opt_out_url is required"):
            load_broker(bad)

    def test_unknown_step_type_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yml"
        bad.write_text(
            "slug: bad\nname: Bad\ndomain: bad.com\ncategory: people_search\n"
            "method: web_form\nopt_out_url: https://bad.com/out\n"
            "legal_basis: [CCPA]\nstatutory_response_days: 30\n"
            "steps:\n  - id: x\n    type: totally_unknown\n    selector: '.x'\n"
        )
        with pytest.raises(ValueError):
            load_broker(bad)


class TestLoadAllBrokers:
    def test_loads_all_fixtures(self) -> None:
        brokers = load_all_brokers(FIXTURES)
        slugs = {b.slug for b in brokers}
        assert "test-email-broker" in slugs
        assert "test-web-broker" in slugs

    def test_raises_on_any_invalid(self, tmp_path: Path) -> None:
        (tmp_path / "good.yml").write_text(
            "slug: good\nname: Good\ndomain: good.com\ncategory: people_search\n"
            "method: email\nopt_out_email: ok@good.com\n"
            "legal_basis: [CCPA]\nstatutory_response_days: 30\n"
        )
        (tmp_path / "bad.yml").write_text(
            "slug: bad\nname: Bad\ndomain: bad.com\ncategory: people_search\n"
            "method: email\nopt_out_email: null\n"
            "legal_basis: [CCPA]\nstatutory_response_days: 30\n"
        )
        with pytest.raises(ValueError, match="failed validation"):
            load_all_brokers(tmp_path)


def test_unknown_legal_basis_rejected():
    import pytest
    from pydantic import ValidationError

    from optout.brokers.schema import BrokerDef

    with pytest.raises(ValidationError, match="unknown legal_basis"):
        BrokerDef(
            slug="x", name="X", domain="x.example", category="people_search",
            method="manual", legal_basis=["NOPE"], statutory_response_days=45,
        )


def test_vcdpa_is_known():
    from optout.utils.statutes import STATUTES

    assert STATUTES["VCDPA"].citation == "Va. Code § 59.1-577(A)(3)"
