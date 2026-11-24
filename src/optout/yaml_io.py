"""Fast YAML reading: PyYAML's C parser (libyaml) when it's installed, else the Python one.

Same safe subset as yaml.safe_load; the C parser is several times faster, which matters
when hundreds of broker files load at start-up.
"""

from __future__ import annotations

from typing import Any

import yaml

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def safe_load(text: str) -> Any:
    return yaml.load(text, Loader=_Loader)  # noqa: S506 (a SafeLoader)
