"""Pydantic models for user config + XDG-aware loader."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------


class Address(BaseModel):
    street: str
    street2: str = ""  # apartment, suite, unit (optional)
    city: str
    state: str
    zip: str

    @property
    def street_line(self) -> str:
        """Street plus line 2 as one string, for single-field forms and emails."""
        return f"{self.street} {self.street2}".strip()


class PreviousAddress(BaseModel):
    street: str
    street2: str = ""
    city: str
    state: str
    zip: str
    # "from" and "to" are reserved words; use aliases for YAML round-trip
    from_year: int | None = Field(None, validation_alias="from", serialization_alias="from")
    to_year: int | None = Field(None, validation_alias="to", serialization_alias="to")

    @property
    def street_line(self) -> str:
        return f"{self.street} {self.street2}".strip()


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class PhoneSet(BaseModel):
    current: list[str] = []
    previous: list[str] = []


class EmailSet(BaseModel):
    current: list[str] = []
    previous: list[str] = []


class Profile(BaseModel):
    legal_name: str
    aliases: list[str] = []
    dob: date
    current_address: Address
    previous_addresses: list[PreviousAddress] = []
    phones: PhoneSet
    emails: EmailSet
    relatives: list[str] = []


# ---------------------------------------------------------------------------
# Email / SMTP config
# ---------------------------------------------------------------------------


class SmtpConfig(BaseModel):
    host: str
    port: int = 587
    username: str
    # Where to find the password, checked in this order (never stored in this file):
    #   1. password_file (default ~/.config/optout/smtp_password, mode 0600), if it exists
    #   2. the environment variable named here
    password_env: str = "OPTOUT_SMTP_PASSWORD"
    password_file: str | None = None


class EmailConfig(BaseModel):
    method: Literal["smtp", "gmail_oauth"]
    smtp: SmtpConfig | None = None
    # Address shown in From. Defaults to the SMTP username (which reveals it to brokers).
    from_address: str | None = None
    # Per-broker masked alias, e.g. "optout-{slug}@example.com" (needs a catch-all or
    # alias service). Used as Reply-To and as the contact address inside the request.
    alias_pattern: str | None = None
    # "minimal": name + address + contact email only (for bulk-imported brokers).
    # "full" adds date of birth, phones, and previous addresses.
    identifiers: Literal["minimal", "full"] = "minimal"
    # Gmail only: label to add to sent opt-out emails (created if missing), e.g. "OPTOUT".
    gmail_label: str | None = None
    # Lets `optout daily` send without a typed confirmation. Off unless you turn it on.
    allow_unattended: bool = False
    daily_limit: int = 40  # max campaign emails per UTC day
    send_delay_seconds: int = 5  # pause between campaign emails

    @model_validator(mode="after")
    def _label_is_plain(self) -> EmailConfig:
        if self.gmail_label is not None:
            from .gmail_label import valid_label

            if not valid_label(self.gmail_label):
                raise ValueError("email.gmail_label may only use letters, digits, space, _ - /")
        return self

    @model_validator(mode="after")
    def _alias_pattern_has_slug(self) -> EmailConfig:
        if self.alias_pattern and "{slug}" not in self.alias_pattern:
            raise ValueError("email.alias_pattern must contain {slug}")
        return self

    @model_validator(mode="after")
    def _smtp_required_when_method_is_smtp(self) -> EmailConfig:
        if self.method == "smtp" and self.smtp is None:
            raise ValueError("email.smtp block is required when method is 'smtp'")
        return self


# ---------------------------------------------------------------------------
# Playwright + Monitoring config
# ---------------------------------------------------------------------------


class NotifyConfig(BaseModel):
    webhook_url: str | None = None  # e.g. an ntfy topic URL; messages never include PII


class PlaywrightConfig(BaseModel):
    headless: bool = False
    slow_mo_ms: int = 0
    # "chrome" = real installed Chrome (preferred). None = Playwright's bundled Chromium.
    channel: str | None = "chrome"


class MonitoringConfig(BaseModel):
    rescan_interval_days: int = 30


# ---------------------------------------------------------------------------
# Top-level UserConfig
# ---------------------------------------------------------------------------


class UserConfig(BaseModel):
    profile: Profile
    email: EmailConfig
    playwright: PlaywrightConfig = PlaywrightConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    notify: NotifyConfig = NotifyConfig()


# ---------------------------------------------------------------------------
# Config file location (XDG-compliant)
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "optout"


def config_path() -> Path:
    return config_dir() / "config.yml"


def load_config() -> UserConfig:
    path = config_path()
    if not path.exists():
        raise FileNotFoundError(f"Config file not found at {path}. Run `optout init` first.")
    data = yaml.safe_load(path.read_text())
    return UserConfig.model_validate(data)


def save_config(cfg: UserConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.dump(cfg.model_dump(by_alias=True, mode="json"), allow_unicode=True)
    # Create with 0600 from the start so PII is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    path.chmod(0o600)  # also tighten a pre-existing file
