"""Validate every YAML in the production brokers/ directory against BrokerDef schema."""

from __future__ import annotations

from pathlib import Path

import pytest

from optout.brokers.loader import load_broker
from optout.brokers.schema import (
    BrokerDef,
    CaptureStep,
    NavigateStep,
    PromptUserIfPresentStep,
    SearchStep,
    SmsVerificationStep,
)

BROKERS_DIR = Path(__file__).parent.parent / "src" / "optout" / "data" / "brokers"

_ALL_YAMLS = sorted(BROKERS_DIR.glob("*.yml"))


def _broker_id(path: Path) -> str:
    return path.stem


@pytest.mark.parametrize("broker_path", _ALL_YAMLS, ids=_broker_id)
def test_broker_yaml_validates(broker_path: Path) -> None:
    """Every file in brokers/ must parse and validate without error."""
    broker = load_broker(broker_path)
    assert isinstance(broker, BrokerDef)
    assert broker.slug == broker_path.stem, (
        f"{broker_path.name}: slug '{broker.slug}' must match filename stem '{broker_path.stem}'"
    )


@pytest.mark.parametrize("broker_path", _ALL_YAMLS, ids=_broker_id)
def test_broker_required_fields_present(broker_path: Path) -> None:
    broker = load_broker(broker_path)
    assert broker.slug, "slug must not be empty"
    assert broker.name, "name must not be empty"
    assert broker.domain, "domain must not be empty"
    assert broker.category, "category must not be empty"
    assert broker.method in ("email", "web_form", "postal", "manual")
    assert broker.legal_basis, "legal_basis must not be empty"
    assert broker.statutory_response_days > 0


@pytest.mark.parametrize("broker_path", _ALL_YAMLS, ids=_broker_id)
def test_broker_method_consistency(broker_path: Path) -> None:
    broker = load_broker(broker_path)
    if broker.method == "email":
        assert broker.opt_out_email is not None, (
            f"{broker.slug}: email method requires opt_out_email"
        )
    elif broker.method == "web_form":
        assert broker.opt_out_url is not None, (
            f"{broker.slug}: web_form method requires opt_out_url"
        )


@pytest.mark.parametrize("broker_path", _ALL_YAMLS, ids=_broker_id)
def test_broker_listing_url_flag_vs_steps(broker_path: Path) -> None:
    broker = load_broker(broker_path)
    if broker.requires_listing_url:
        has_search = any(isinstance(s, SearchStep) for s in broker.steps)
        assert has_search, f"{broker.slug}: requires_listing_url=true but has no SearchStep"


@pytest.mark.parametrize("broker_path", _ALL_YAMLS, ids=_broker_id)
def test_broker_phone_verification_flag_vs_steps(broker_path: Path) -> None:
    broker = load_broker(broker_path)
    if broker.requires_phone_verification:
        has_phone_step = any(
            isinstance(s, (SmsVerificationStep, PromptUserIfPresentStep)) for s in broker.steps
        )
        assert has_phone_step, (
            f"{broker.slug}: requires_phone_verification=true but has no phone-verification step"
        )


@pytest.mark.parametrize("broker_path", _ALL_YAMLS, ids=_broker_id)
def test_broker_step_ids_unique(broker_path: Path) -> None:
    broker = load_broker(broker_path)
    ids = [s.id for s in broker.steps]
    assert len(ids) == len(set(ids)), (
        f"{broker.slug}: duplicate step IDs: {[x for x in ids if ids.count(x) > 1]}"
    )


@pytest.mark.parametrize("broker_path", _ALL_YAMLS, ids=_broker_id)
def test_broker_web_form_ends_with_capture(broker_path: Path) -> None:
    broker = load_broker(broker_path)
    if broker.method == "web_form" and broker.steps:
        assert isinstance(broker.steps[-1], CaptureStep), (
            f"{broker.slug}: web_form broker steps should end with a capture step"
        )


class TestSpecificBrokers:
    """Spot-checks on individual broker definitions."""

    def _load(self, slug: str) -> BrokerDef:
        return load_broker(BROKERS_DIR / f"{slug}.yml")

    def test_whitepages_phone_verify(self) -> None:
        b = self._load("whitepages")
        assert b.requires_phone_verification is True
        assert b.requires_listing_url is True
        # Whitepages uses a phone call (not SMS): the engine clicks "Call now" then
        # waits via prompt_user_if_present for the user to complete the call.
        phone_steps = [s for s in b.steps if isinstance(s, PromptUserIfPresentStep)]
        assert len(phone_steps) >= 1

    def test_spokeo_email_verify(self) -> None:
        b = self._load("spokeo")
        assert b.requires_email_verification is True
        assert b.requires_listing_url is True
        search_steps = [s for s in b.steps if isinstance(s, SearchStep)]
        assert len(search_steps) == 1

    def test_beenverified_no_listing_url_needed(self) -> None:
        b = self._load("beenverified")
        assert b.requires_listing_url is False
        assert b.requires_email_verification is True
        navigate_steps = [s for s in b.steps if isinstance(s, NavigateStep)]
        assert len(navigate_steps) >= 1

    def test_radaris_manual_after_domain_seizure(self) -> None:
        # radaris.com was transferred by court order (checked 2026-10-28).
        b = self._load("radaris")
        assert b.method == "manual"
        assert "Court Order" in (b.notes or "")
        assert b.opt_out_url is not None
        assert "GDPR" in b.legal_basis

    def test_mylife_web_form_method(self) -> None:
        b = self._load("mylife")
        assert b.method == "web_form"
        assert b.opt_out_url is not None
        assert b.re_add_window_days == 90

    def test_all_expected_brokers_present(self) -> None:
        slugs = {p.stem for p in BROKERS_DIR.glob("*.yml")}
        expected = {"whitepages", "spokeo", "beenverified", "radaris", "mylife"}
        missing = expected - slugs
        assert not missing, f"Missing expected broker files: {missing}"
