"""Pydantic models for broker YAML definitions."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Resilience primitives
# ---------------------------------------------------------------------------


class RetryConfig(BaseModel):
    attempts: int = 3
    backoff_seconds: float = 1.0


class StepBase(BaseModel):
    id: str
    label: str | None = None
    retry: RetryConfig | None = None
    optional: bool = False
    on_failure: Literal["abort", "skip", "prompt"] = "abort"


# ---------------------------------------------------------------------------
# Step types (discriminated union on `type`)
# ---------------------------------------------------------------------------


class SearchStep(StepBase):
    type: Literal["search"]
    search_url_template: str
    result_selector: str
    pick_strategy: str = "prompt_user"


class NavigateStep(StepBase):
    type: Literal["navigate"]
    url: str


class FormField(BaseModel):
    selector: str
    value: str
    use_type: bool = False  # type char-by-char; needed for React forms that ignore fill()
    is_select: bool = False  # use select_option() instead of fill() for <select> elements
    force: bool = False  # fill hidden/covered elements (uses state="attached" + force=True)
    use_js: bool = False  # React controlled inputs: native value setter + input/change events


class FormFillStep(StepBase):
    type: Literal["form_fill"]
    fields: dict[str, FormField]
    frame: str | None = None  # CSS selector for an iframe to fill inside


class PromptUserStep(StepBase):
    type: Literal["prompt_user"]
    message: str


class PromptUserIfPresentStep(StepBase):
    type: Literal["prompt_user_if_present"]
    selector: str
    message: str
    frame: str | None = None  # CSS selector for an iframe to probe


class ClickStep(StepBase):
    type: Literal["click"]
    selector: str
    frame: str | None = None  # CSS selector for an iframe to click inside


class SmsVerificationStep(StepBase):
    type: Literal["sms_verification_prompt"]
    code_selector: str
    message: str


class CaptureStep(StepBase):
    type: Literal["capture"]
    method: str  # "screenshot" is the only current value
    save_as: str


class WaitForStep(StepBase):
    type: Literal["wait_for"]
    selector: str | None = None
    js_condition: str | None = None  # JS predicate: () => bool; polled until truthy
    timeout_ms: int = 120_000
    message: str | None = None
    frame: str | None = None  # CSS selector for an iframe to probe

    @model_validator(mode="after")
    def _requires_selector_or_condition(self) -> WaitForStep:
        if not self.selector and not self.js_condition:
            raise ValueError("wait_for step requires 'selector' or 'js_condition'")
        return self


class OpenInBrowserStep(StepBase):
    type: Literal["open_in_browser"]
    url: str
    instructions: str


class CollectInputStep(StepBase):
    type: Literal["collect_input"]
    message: str
    store_as: str  # key written into StepState, e.g. "found_listing_url"


class AssertStep(StepBase):
    type: Literal["assert"]
    selector: str | None = None
    text_contains: str | None = None
    url_matches: str | None = None
    js_condition: str | None = None

    @model_validator(mode="after")
    def _requires_at_least_one_condition(self) -> AssertStep:
        if not any([self.selector, self.text_contains, self.url_matches, self.js_condition]):
            raise ValueError("assert step requires at least one condition field")
        return self


BrokerStep = Annotated[
    SearchStep
    | NavigateStep
    | FormFillStep
    | PromptUserStep
    | PromptUserIfPresentStep
    | ClickStep
    | SmsVerificationStep
    | CaptureStep
    | WaitForStep
    | OpenInBrowserStep
    | CollectInputStep
    | AssertStep,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Scan config (how to check if the user appears on a broker)
# ---------------------------------------------------------------------------


class BrokerScanConfig(BaseModel):
    search_url_template: str | None = None
    search_steps: list[BrokerStep] = []
    result_selector: str
    match_fields: list[str] = []

    @model_validator(mode="after")
    def _requires_search_method(self) -> BrokerScanConfig:
        if not self.search_url_template and not self.search_steps:
            raise ValueError("scan config requires either 'search_url_template' or 'search_steps'")
        return self


# ---------------------------------------------------------------------------
# Top-level BrokerDef
# ---------------------------------------------------------------------------


class BrokerDef(BaseModel):
    slug: str
    name: str
    domain: str
    category: str
    parent_company: str | None = None

    method: Literal["web_form", "email", "postal", "manual"]
    opt_out_url: str | None = None
    opt_out_email: str | None = None
    opt_out_postal_address: str | None = None

    required_fields: list[str] = []
    optional_fields: list[str] = []

    requires_listing_url: bool = False
    requires_phone_verification: bool = False
    requires_email_verification: bool = False
    requires_id_upload: bool = False
    requires_notarization: bool = False

    legal_basis: list[str]
    statutory_response_days: int
    re_add_window_days: int | None = None

    last_verified_at: date | None = None
    notes: str | None = None

    scan: BrokerScanConfig | None = None
    steps: list[BrokerStep] = []

    @model_validator(mode="after")
    def _legal_basis_known(self) -> BrokerDef:
        from optout.utils.statutes import STATUTES

        unknown = [k for k in self.legal_basis if k not in STATUTES]
        if unknown:
            raise ValueError(f"unknown legal_basis key(s): {', '.join(unknown)}")
        return self

    @model_validator(mode="after")
    def _method_fields_consistent(self) -> BrokerDef:
        if self.method == "email" and not self.opt_out_email:
            raise ValueError(
                f"Broker '{self.slug}': opt_out_email is required when method is 'email'"
            )
        if self.method == "web_form" and not self.opt_out_url:
            raise ValueError(
                f"Broker '{self.slug}': opt_out_url is required when method is 'web_form'"
            )
        return self
