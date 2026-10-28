"""Persistent-context Chrome launcher.

Launches real installed Chrome with a persistent user-data directory at
~/.config/optout/browser_profile/. Cloudflare and other bot-detection
systems set cookies after the first human-solved challenge; those cookies
persist across runs so the user only has to solve the challenge once per
broker, not every time.

Using launch_persistent_context (not launch + new_context) is deliberate:
it is the only Playwright API that retains cookies, localStorage, and the
full browser fingerprint between sessions.

No stealth flags or fingerprint changes: the browser identifies as automated.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import BrowserContext, sync_playwright

from ..config import PlaywrightConfig, config_dir


@contextmanager
def launch_browser(config: PlaywrightConfig) -> Generator[BrowserContext, None, None]:
    profile_dir: Path = config_dir() / "browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=config.channel,
            headless=config.headless,
            viewport=None,
            slow_mo=config.slow_mo_ms,
        )
        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except Exception:
                pass
