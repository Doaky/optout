"""Regenerate the README screenshots from the real app, using made-up data only.

    uv run python scripts/screenshots.py

Starts the dashboard against a throwaway home folder (fictional brokers on .example
domains, and the sample person "Jane Q Example"), then saves:

    docs/dashboard.png, docs/dashboard-dark.png
    docs/screenshots/{plan,preview,replies,requests,broker-page,settings}.png

Nothing touches your real settings, database or mailbox. Needs Playwright's Chromium
(`uv run playwright install chromium`).
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SHOTS = DOCS / "screenshots"
WIDTH, HEIGHT = 1280, 900

# (slug, name, method, extra fields)
BROKERS: list[tuple[str, str, str, dict]] = [
    ("peoplelookup", "PeopleLookup", "web_form", {"priority": 1, "category": "people_search"}),
    ("findanyone", "FindAnyone", "web_form", {"priority": 1, "category": "people_search"}),
    ("acme-data", "Acme Data Co.", "email", {}),
    ("brightline", "Brightline Marketing", "email", {}),
    ("cobalt-audience", "Cobalt Audience", "email", {}),
    ("delta-insights", "Delta Insights", "email", {}),
    ("evergreen-lists", "Evergreen Lists", "email", {}),
    ("foxglove", "Foxglove Analytics", "email", {}),
    ("granite", "Granite Consumer Data", "email", {}),
    ("harbor-reach", "Harbor Reach", "email", {}),
    ("ironwood", "Ironwood Identity", "email", {}),
    ("juniper", "Juniper Signals", "email", {}),
    ("keystone", "Keystone Leads", "email", {}),
    ("lumen-graph", "Lumen Graph", "email", {}),
    ("meridian", "Meridian Profiles", "email", {}),
    ("northstar", "Northstar Append", "email", {"requires_email_verification": True}),
    ("oakmont", "Oakmont Data", "email", {"requires_email_verification": True}),
    ("quartz-forms", "Quartz Forms Inc.", "web_form", {}),
    ("ridgeline", "Ridgeline Records", "web_form", {"requires_phone_verification": True}),
    ("orbit-ads", "Orbit Ad Exchange", "email", {"requires_device_ids": True}),
    ("pinnacle", "Pinnacle ID Check", "email", {"requires_id_upload": True}),
]

# slug -> (days since sent, status, reply kind, reply text)
SENT: dict[str, tuple[int, str, str, str]] = {
    "acme-data": (40, "confirmed", "deleted", "We have deleted the personal information we held."),
    "brightline": (38, "confirmed", "no-data", "We searched our systems and hold no data on you."),
    "foxglove": (35, "confirmed", "deleted", "Your request has been completed."),
    "cobalt-audience": (
        6,
        "awaiting_user_input",
        "need-info",
        "Please verify your identity using the link below within 5 days: "
        "https://privacy.cobalt-audience.example/verify",
    ),
    "delta-insights": (
        20,
        "rejected",
        "denied",
        "We are unable to process your request as you are not a resident of a state with a "
        "consumer privacy law.",
    ),
    "evergreen-lists": (12, "awaiting_broker_confirmation", "auto-reply", "We received it."),
    "granite": (9, "awaiting_broker_confirmation", "", ""),
    "harbor-reach": (4, "awaiting_broker_confirmation", "", ""),
    "ironwood": (50, "awaiting_broker_confirmation", "", ""),
}

# Replies the tool couldn't sort on its own (for the Broker replies card).
UNSURE = [
    ("juniper", "privacy@juniper.example", "Re: Data Deletion Request", 3),
    ("keystone", "dpo@keystone.example", "Your privacy request #48213", 2),
    ("lumen-graph", "noreply@lumen-graph.example", "Re: Data Deletion Request", 1),
]


def _setup(home: Path) -> None:
    # Only the fictional brokers below are loaded (this folder replaces the bundled set).
    _write_brokers(home / "brokers")
    os.environ["OPTOUT_BROKERS_DIR"] = str(home / "brokers")
    os.environ["XDG_CONFIG_HOME"] = str(home / "cfg")
    os.environ["XDG_DATA_HOME"] = str(home / "data")
    os.environ["OPTOUT_SCREENSHOTS_PW"] = "app-password"


def _write_brokers(brokers: Path) -> None:
    """The fictional brokers, one YAML file each."""
    brokers.mkdir(parents=True, exist_ok=True)
    for slug, name, method, extra in BROKERS:
        doc = {
            "slug": slug,
            "name": name,
            "domain": f"{slug}.example",
            "category": "data_broker",
            "method": method,
            "legal_basis": ["VCDPA"],
            "statutory_response_days": 45,
            "source": "ca-registry",
            **extra,
        }
        if method == "email":
            doc["opt_out_email"] = f"privacy@{slug}.example"
        else:
            doc["opt_out_url"] = f"https://{slug}.example/opt-out"
            doc["search_url"] = f"https://{slug}.example/search"
        (brokers / f"{slug}.yml").write_text(yaml.safe_dump(doc))


def _seed() -> None:
    from optout.config import (
        Address,
        EmailConfig,
        EmailSet,
        PhoneSet,
        Profile,
        SmtpConfig,
        UserConfig,
        save_config,
    )
    from optout.db import (
        InboxMessage,
        create_submission,
        get_session,
        init_db,
        mark_submitted,
        update_status,
    )

    save_config(
        UserConfig(
            profile=Profile(
                legal_name="Jane Q Example",
                dob=date(1990, 1, 1),
                current_address=Address(
                    street="123 Example St", city="Richmond", state="VA", zip="23219"
                ),
                phones=PhoneSet(current=["555-0100"]),
                emails=EmailSet(current=["you@example.com"]),
            ),
            email=EmailConfig(
                method="smtp",
                smtp=SmtpConfig(
                    host="smtp.example.invalid",
                    username="you@example.com",
                    password_env="OPTOUT_SCREENSHOTS_PW",
                ),
            ),
        )
    )
    init_db()
    from optout.brokers.registry import imported_dir

    _write_brokers(imported_dir())  # as if the registry was imported: no "Load the list" card
    now = datetime.now(UTC)
    with get_session() as s:
        subs = {}
        for slug, (days, status, kind, text) in SENT.items():
            sub = create_submission(
                s,
                broker_slug=slug,
                method_used="email",
                legal_basis_cited=["VCDPA"],
                statutory_response_days=45,
            )
            mark_submitted(s, sub, {}, None, 45, submitted_at=now - timedelta(days=days))
            if status != "submitted":
                update_status(s, sub, status)
            subs[slug] = sub
            if kind:
                s.add(
                    InboxMessage(
                        message_id=f"<{slug}.reply@{slug}.example>",
                        submission_id=sub.id,
                        broker_slug=slug,
                        matched_by="message-id",
                        from_addr=f"privacy@{slug}.example",
                        subject="Re: Data Deletion Request",
                        received_at=now - timedelta(days=max(1, days - 3)),
                        text=text,
                        kind=kind,
                        state="applied",
                    )
                )
        for slug, sender, subject, days in UNSURE:
            s.add(
                InboxMessage(
                    message_id=f"<{slug}.unsure@{slug}.example>",
                    from_addr=sender,
                    subject=subject,
                    received_at=now - timedelta(days=days),
                    text="Thank you for contacting us. Before we can proceed we need a "
                    "little more information about your request.",
                    kind="unknown",
                    state="review",
                )
            )
        s.commit()


def main() -> int:
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="optout-shots-") as tmp:
        _setup(Path(tmp))
        _seed()
        from optout.brokers import registry as reg
        from optout.web.server import make_server

        reg._registry.clear()
        reg._loaded = False
        httpd, app = make_server(0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{app.port}"
        SHOTS.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                shots = (("light", DOCS / "dashboard.png"), ("dark", DOCS / "dashboard-dark.png"))
                for scheme, out in shots:
                    page = browser.new_page(
                        viewport={"width": WIDTH, "height": HEIGHT}, color_scheme=scheme
                    )
                    page.goto(f"{base}/?t={app.token}")
                    page.wait_for_timeout(700)  # entrances settle
                    page.screenshot(path=str(out))
                    page.close()

                page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
                page.goto(f"{base}/?t={app.token}")
                page.wait_for_timeout(700)
                cards = (("#plan", "plan"), ("#replies", "replies"), ("#requests", "requests"))
                for card, name in cards:
                    page.locator(card).screenshot(path=str(SHOTS / f"{name}.png"))
                # A window taller than the panel, so the floating Save bar sits where it belongs.
                page.set_viewport_size({"width": WIDTH, "height": 2400})
                page.locator("#settings > summary").click()
                page.wait_for_timeout(500)
                page.locator("#settings").screenshot(path=str(SHOTS / "settings.png"))
                page.set_viewport_size({"width": WIDTH, "height": HEIGHT})

                page.goto(f"{base}/preview?count=3")
                page.wait_for_timeout(300)
                page.locator("details.email").first.locator("summary").click()
                page.screenshot(path=str(SHOTS / "preview.png"))

                page.goto(f"{base}/broker/delta-insights")
                page.wait_for_timeout(300)
                page.screenshot(path=str(SHOTS / "broker-page.png"))
                browser.close()
        finally:
            httpd.shutdown()
    print("saved", DOCS / "dashboard.png", DOCS / "dashboard-dark.png", "and", SHOTS)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    raise SystemExit(main())
