"""Checking our statute quotes against the states' official text.

Each entry in data/statutes.yml names an official source page and the exact words we quote
from it. `verify` fetches the page, turns it into plain text, and confirms every quote is
there word for word (ignoring spacing and curly-vs-straight quotes). A legislature that
amends a section changes its text, and the check fails before a wrong citation goes out.
"""

from __future__ import annotations

import html
import re
import urllib.request
from dataclasses import dataclass, field
from importlib.resources import files

import yaml

_QUOTES = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " "}


def normalise(text: str) -> str:
    for a, b in _QUOTES.items():
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip().lower()


def page_text(raw: bytes) -> str:
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("cp1252", "replace")
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return normalise(s)


def fetch(url: str, timeout: float = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (optout statute check)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed official URLs)
        return page_text(resp.read(5_000_000))


@dataclass
class Result:
    key: str
    state: str
    problems: list[str] = field(default_factory=list)


def entries() -> list[dict]:
    data = yaml.safe_load(files("optout").joinpath("data/statutes.yml").read_text())
    return [e for e in data or [] if isinstance(e, dict)]


def check(entry: dict, texts: dict[str, str]) -> Result:
    """Every quote in *entry* must appear in the text of its source (texts: url -> text)."""
    r = Result(entry.get("key", "?"), entry.get("state", "?"))
    pairs = [("quote", entry.get("source"))]
    if entry.get("deadline_quote"):
        pairs.append(("deadline_quote", entry.get("deadline_source") or entry.get("source")))
    if entry.get("appeal_quote"):
        pairs.append(("appeal_quote", entry.get("appeal_source") or entry.get("source")))
    for field_name, url in pairs:
        text = texts.get(url or "")
        if text is None:
            r.problems.append(f"{field_name}: couldn't read {url}")
        elif normalise(entry[field_name]) not in text:
            r.problems.append(f"{field_name} not found at {url}")
    days = entry.get("response_days")
    if (
        days
        and str(days) not in entry.get("deadline_quote", "")
        and "forty-five" not in entry.get("deadline_quote", "")
    ):
        r.problems.append("response_days doesn't match the deadline quote")
    return r


def verify(fetcher=fetch) -> list[Result]:
    """Fetch every source once and check every entry. A page that can't be read is reported
    against the entries that need it."""
    texts: dict[str, str] = {}
    errors: dict[str, str] = {}
    results = []
    for e in entries():
        for url in [e.get(k) for k in ("source", "deadline_source", "appeal_source")]:
            if not url or url in texts or url in errors:
                continue
            try:
                texts[url] = fetcher(url)
            except Exception as exc:  # report it, check the rest
                errors[url] = f"{type(exc).__name__}: {exc}"[:160]
        r = check(e, texts)
        r.problems = [
            next((f"{u}: {why}" for u, why in errors.items() if u in p), p) for p in r.problems
        ]
        results.append(r)
    return results
