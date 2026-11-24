"""Loads and validates broker YAML files against BrokerDef."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from optout import yaml_io

from .schema import BrokerDef


def load_broker(path: Path) -> BrokerDef:
    """Parse and validate a single broker YAML file."""
    data = yaml_io.safe_load(path.read_text())
    try:
        return BrokerDef.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Invalid broker definition at {path}:\n{exc}") from exc


def load_all_brokers(brokers_dir: Path) -> list[BrokerDef]:
    """Load every *.yml file in brokers_dir, sorted by slug."""
    results: list[BrokerDef] = []
    errors: list[str] = []

    for yml in sorted(brokers_dir.glob("*.yml")):
        try:
            results.append(load_broker(yml))
        except (ValueError, yaml.YAMLError) as exc:
            errors.append(str(exc))

    if errors:
        raise ValueError(f"{len(errors)} broker(s) failed validation:\n" + "\n\n".join(errors))

    return results
