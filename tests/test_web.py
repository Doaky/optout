"""The local UI: authentication, request forgery defences, escaping, and the send flow."""

from __future__ import annotations

import http.client
import threading
from datetime import UTC, date, datetime
from urllib.parse import urlencode

import pytest
import yaml

import optout.brokers.registry as reg
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
from optout.db import get_session, init_db, list_events, list_submissions
from optout.web.server import COOKIE, make_server

EVIL = "<script>alert(1)</script>Evil Co"


@pytest.fixture()
def ui(tmp_path, monkeypatch):
    brokers = tmp_path / "brokers"
    brokers.mkdir()
    (brokers / "evil.yml").write_text(
        yaml.safe_dump(
            {
                "slug": "evil",
                "name": EVIL,
                "domain": "evil.example",
                "category": "data_broker",
                "method": "email",
                "opt_out_email": "privacy@evil.example",
                "legal_basis": ["VCDPA"],
                "statutory_response_days": 45,
                "source": "ca-registry",
            }
        )
    )
    monkeypatch.setenv("OPTOUT_BROKERS_DIR", str(brokers))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    save_config(
        UserConfig(
            profile=Profile(
                legal_name="Jane Q Public",
                dob=date(1990, 5, 14),
                current_address=Address(
                    street="1 Main St", city="Richmond", state="VA", zip="23219"
                ),
                phones=PhoneSet(current=["+15125550000"]),
                emails=EmailSet(current=["jane@example.invalid"]),
            ),
            email=EmailConfig(
                method="smtp",
                smtp=SmtpConfig(host="h", username="jane@example.invalid", password_env="X"),
            ),
        )
    )
    init_db()
    reg._registry.clear()
    reg._loaded = False
    httpd, app = make_server(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    class Ctx:
        port = app.port
        app_ = app
        token = app.token
        csrf = app.csrf
        host = f"127.0.0.1:{app.port}"
        server_ip = httpd.server_address[0]

        def req(self, method, path, headers=None, body=None, cookie=True, host=None):
            h = {"Host": host or self.host}
            if cookie:
                h["Cookie"] = f"{COOKIE}={self.token}"
            h.update(headers or {})
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request(method, path, body=body, headers=h)
            r = conn.getresponse()
            data = r.read().decode()
            conn.close()
            return r, data

        def post(self, path, fields, with_csrf=True, headers=None, **kw):
            if with_csrf:
                fields = {"csrf": self.csrf, **fields}
            h = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
            return self.req("POST", path, h, urlencode(fields), **kw)

    yield Ctx()
    httpd.shutdown()
    reg._registry.clear()
    reg._loaded = False


def test_binds_to_loopback_only(ui):
    assert ui.server_ip == "127.0.0.1"


def test_everything_requires_the_session_cookie(ui):
    for path in ("/", "/campaign", "/preview", "/drafts", "/broker/evil"):
        r, _ = ui.req("GET", path, cookie=False)
        assert r.status == 403, path
    r, _ = ui.req("GET", "/", headers={"Cookie": f"{COOKIE}=wrong"}, cookie=False)
    assert r.status == 403


def test_token_link_sets_strict_httponly_cookie_and_redirects(ui):
    r, _ = ui.req("GET", f"/?t={ui.token}", cookie=False)
    assert r.status == 303 and r.getheader("Location") == "/"
    cookie = r.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    r, _ = ui.req("GET", "/?t=wrong", cookie=False)
    assert r.status == 403


def test_wrong_host_header_is_rejected_even_with_valid_cookie(ui):
    for host in ("evil.example", f"evil.example:{ui.port}", "localhost"):
        r, _ = ui.req("GET", "/", host=host)
        assert r.status == 403, host
    r, _ = ui.req("GET", "/", host=f"localhost:{ui.port}")
    assert r.status == 200


def test_security_headers(ui):
    r, _ = ui.req("GET", "/")
    assert "default-src 'none'" in r.getheader("Content-Security-Policy")
    assert r.getheader("Cache-Control") == "no-store"
    assert r.getheader("X-Frame-Options") == "DENY"


def test_registry_text_is_escaped(ui):
    for path in ("/", "/campaign", "/preview", "/broker/evil"):
        r, body = ui.req("GET", path)
        assert r.status == 200, path
        assert "<script>alert(1)" not in body, path
    _, body = ui.req("GET", "/campaign")
    assert "&lt;script&gt;" in body


def test_posts_are_forgery_resistant(ui):
    ok = {"slug": "evil", "outcome": "auto-reply"}
    assert ui.post("/act/reply", ok, with_csrf=False)[0].status == 403
    assert ui.post("/act/reply", {**ok, "csrf": "nope"}, with_csrf=False)[0].status == 403
    assert ui.post("/act/reply", ok, headers={"Origin": "https://evil.example"})[0].status == 403
    assert ui.post("/act/reply", ok, headers={"Sec-Fetch-Site": "cross-site"})[0].status == 403
    assert ui.post("/act/reply", ok, cookie=False)[0].status == 403
    r, _ = ui.req("POST", "/act/reply", {"Content-Type": "application/json"}, "{}")
    assert r.status == 415
    r, _ = ui.req(
        "POST",
        "/act/reply",
        {"Content-Type": "application/x-www-form-urlencoded"},
        "a=" + "x" * 70000,
    )
    assert r.status == 413


def test_get_never_changes_state(ui):
    for path in ("/act/reply", "/act/campaign", "/act/toggle"):
        assert ui.req("GET", path)[0].status == 404
    with get_session() as s:
        assert list_submissions(s) == []


def test_path_traversal_in_draft_names_is_blocked(ui):
    for name in ("..%2F..%2Fetc%2Fpasswd", "%2Fetc%2Fpasswd", "x.txt"):
        assert ui.req("GET", f"/draft/{name}")[0].status == 404


def test_preview_shows_exact_email_and_minimal_identifiers(ui):
    r, body = ui.req("GET", "/preview?count=5")
    assert r.status == 200
    assert "Preview: 1 emails" in body and "Send these 1 emails" in body
    assert "1 Main St, Richmond, VA 23219" in body
    assert "1990" not in body and "5125550000" not in body


def test_send_is_one_click_but_still_forgery_protected(ui, monkeypatch):
    started = []
    monkeypatch.setattr(
        "optout.web.server.App.start_campaign",
        lambda self, n, only=None, **kw: started.append(n) or "started",
    )
    # forged: no CSRF / foreign origin -> nothing starts
    assert ui.post("/act/campaign", {"count": "1"}, with_csrf=False)[0].status == 403
    r, _ = ui.post("/act/campaign", {"count": "1"}, headers={"Origin": "https://evil.example"})
    assert r.status == 403 and started == []
    # the page's own button -> starts
    r, _ = ui.post("/act/campaign", {"count": "1"})
    assert r.getheader("Location") == "/?m=started#send" and started == [1]


def test_count_out_of_range_is_refused(ui):
    for count in ("0", "-1", "999", "abc"):
        r, _ = ui.post("/act/campaign", {"count": count})
        assert r.getheader("Location") == "/?m=err", count


def test_reply_and_draft_flow(ui):
    from optout.db import create_submission, mark_submitted

    with get_session() as s:
        sub = create_submission(
            s,
            broker_slug="evil",
            method_used="email",
            legal_basis_cited=["VCDPA"],
            statutory_response_days=45,
        )
        mark_submitted(s, sub, {}, None, 45)
        sub.status = "awaiting_broker_confirmation"
        s.add(sub)
        s.commit()

    r, _ = ui.post(
        "/act/reply", {"slug": "evil", "outcome": "auto-reply", "note": "ad opt-out page"}
    )
    assert r.status == 303
    with get_session() as s:
        (sub,) = list_submissions(s)
        assert sub.status == "awaiting_broker_confirmation"  # an autoresponder changes nothing

    r, _ = ui.post("/act/reply", {"slug": "evil", "outcome": "deleted"})
    with get_session() as s:
        (sub,) = list_submissions(s)
        assert sub.status == "confirmed"

    r, _ = ui.post("/act/draft", {"slug": "evil", "kind": "clarify"})
    assert r.status == 303 and r.getheader("Location").startswith("/draft/")
    r, body = ui.req("GET", r.getheader("Location"))
    assert r.status == 200 and "not about ad preferences" in body
    assert "1990" not in body and "5125550000" not in body

    r, body = ui.req("GET", "/")
    assert "Broker says done</span></span></td>" in body
    assert "Removed (verified)</span></span></td>" not in body
    assert "/act/row" in body


def test_main_page_has_everything_without_clicking_in(ui):
    r, body = ui.req("GET", "/")
    assert r.status == 200
    assert "Send a batch" in body and "Preview all 1 emails" in body
    assert "Send emails" in body  # one-click send lives on the main page
    assert "1 Main St, Richmond, VA 23219" in body  # the exact email text, inline
    send = body[body.index("id='send'") : body.index("</section>", body.index("id='send'"))]
    assert "1990" not in send and "5125550000" not in send  # emails omit birth date and phone


def _sent_request(slug="evil"):
    from optout.db import create_submission, mark_submitted

    with get_session() as s:
        sub = create_submission(
            s,
            broker_slug=slug,
            method_used="email",
            legal_basis_cited=["VCDPA"],
            statutory_response_days=45,
        )
        mark_submitted(s, sub, {}, None, 45)
        sub.status = "awaiting_broker_confirmation"
        s.add(sub)
        s.commit()


def test_row_actions_work_from_the_main_page(ui):
    _sent_request()
    _, body = ui.req("GET", "/")
    assert "/act/row" in body and "Write a follow-up email" in body

    r, _ = ui.post("/act/row", {"slug": "evil", "action": "reply:auto-reply"})
    assert r.getheader("Location") == "/?m=reply"
    with get_session() as s:
        assert list_submissions(s)[0].status == "awaiting_broker_confirmation"

    r, _ = ui.post("/act/row", {"slug": "evil", "action": "draft:clarify"})
    loc = r.getheader("Location")
    assert loc.startswith("/?draft=") and "ds=evil" in loc and "dk=clarify" in loc
    r, body = ui.req("GET", loc)
    assert "not about ad preferences" in body and "I sent it" in body  # inline on the main page

    r, _ = ui.post("/act/row", {"slug": "evil", "action": "reply:deleted"})
    with get_session() as s:
        assert list_submissions(s)[0].status == "confirmed"

    r, _ = ui.post("/act/row", {"slug": "evil", "action": "toggle:off"})
    _, body = ui.req("GET", "/")
    assert "Preview all" not in body  # a skipped broker leaves the batch


def test_row_actions_reject_unknown_input(ui):
    _sent_request()
    for action in ("rm:everything", "reply:maybe", "draft:../../x", "sent:nope", ""):
        r, _ = ui.post("/act/row", {"slug": "evil", "action": action})
        assert r.getheader("Location") == "/?m=err", action
    r, _ = ui.post("/act/row", {"slug": "nobody", "action": "reply:deleted"})
    assert r.getheader("Location") == "/?m=err"


def test_draft_viewer_only_opens_real_draft_files(ui):
    for q in ("..%2F..%2Fetc%2Fpasswd", "%2Fetc%2Fpasswd", "x.txt"):
        r, body = ui.req("GET", f"/?draft={q}")
        assert r.status == 200 and "Draft:" not in body


def test_search_filters_the_table(ui):
    _sent_request()
    _, hit = ui.req("GET", "/?q=evil")
    _, miss = ui.req("GET", "/?q=zzzz")
    assert "Awaiting reply</span>" in hit and "No requests match" in miss


def test_buttons_work_in_a_real_browser(ui, monkeypatch):
    """Browsers set Origin from the referrer policy; a script-only test can't catch that."""
    sync_api = pytest.importorskip("playwright.sync_api")
    started = []
    monkeypatch.setattr(
        "optout.web.server.App.start_campaign",
        lambda self, n, only=None, **kw: started.append(n) or "started",
    )
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:  # no browser installed (e.g. CI)
        pytest.skip(f"no browser: {type(exc).__name__}")
    try:
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{ui.port}/?t={ui.token}")
        assert page.title().startswith("optout")

        page.evaluate("window.__noReload = 1")
        page.get_by_role("button", name="Write drafts for overdue requests").click()
        page.get_by_text("New drafts are listed").wait_for()
        assert "Forbidden" not in page.content()

        page.get_by_role("button", name="Send emails").click()
        page.get_by_text("Batch started.").wait_for()
        assert started == [1]
        assert page.evaluate("window.__noReload") == 1  # updated in place, never reloaded
    finally:
        browser.close()
        pw.stop()


def test_job_status_endpoint_needs_login(ui):
    assert ui.req("GET", "/api/job", cookie=False)[0].status == 403
    r, body = ui.req("GET", "/api/job")
    assert r.status == 200 and r.getheader("Content-Type") == "application/json"
    assert '"state": "idle"' in body


def test_batch_progress_updates_live_without_a_refresh(ui, monkeypatch):
    """The page must follow the job by itself and land on the result when it finishes."""
    import time as _time

    from optout.web import server

    sync_api = pytest.importorskip("playwright.sync_api")

    def fake_start(self, n, only=None, **kw):
        self.job = {"state": "running", "sent": [], "failed": [], "total": 2}

        def run():
            _time.sleep(1.2)
            self.job["sent"].append("a")
            _time.sleep(1.2)
            self.job["sent"].append("b")
            self.job["state"] = "done"

        threading.Thread(target=run, daemon=True).start()
        return "started"

    monkeypatch.setattr(server.App, "start_campaign", fake_start)
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:
        pytest.skip(f"no browser: {type(exc).__name__}")
    try:
        page = browser.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(f"http://127.0.0.1:{ui.port}/?t={ui.token}")
        page.evaluate("window.__noReload = 1")
        page.get_by_role("button", name="Send emails").click()
        page.locator(".flash.live").wait_for()  # glowing "Batch started."
        page.get_by_text("Batch finished").first.wait_for(timeout=15000)  # no manual refresh
        assert page.evaluate("window.__noReload") == 1
        assert not [e for e in errors if "Content Security Policy" in e]
    finally:
        browser.close()
        pw.stop()


def test_one_row_per_broker_latest_attempt_wins(ui):
    from optout.db import create_submission, mark_submitted

    with get_session() as s:
        old = create_submission(
            s,
            broker_slug="evil",
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
        old.status = "failed"
        s.add(old)
        s.commit()
        new = create_submission(
            s,
            broker_slug="evil",
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
        mark_submitted(s, new, {}, None, 45)
    _, body = ui.req("GET", "/")
    assert body.count("<td class='name'>") == 1
    assert "Failed</span>" not in body


def test_instant_filters_and_auto_applying_menus(ui):
    from optout.db import create_submission, mark_submitted

    sync_api = pytest.importorskip("playwright.sync_api")
    with get_session() as s:
        for slug in ("evil", "other-co"):
            sub = create_submission(
                s,
                broker_slug=slug,
                method_used="email",
                legal_basis_cited=[],
                statutory_response_days=45,
            )
            mark_submitted(s, sub, {}, None, 45)
            sub.status = "awaiting_broker_confirmation"
            s.add(sub)
            s.commit()
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:
        pytest.skip(f"no browser: {type(exc).__name__}")
    try:
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{ui.port}/?t={ui.token}")
        page.evaluate("window.__noReload = 1")
        rows = page.locator("tr[data-name]")
        assert rows.count() == 2
        assert page.locator("details.rowmenu .menu:visible").count() == 0  # menus start closed

        page.fill("#q", "other")  # filters as you type, no Enter
        assert page.locator("tr[data-name]:visible").count() == 1
        page.fill("#q", "zzz")
        assert page.locator("#nomatch").is_visible()
        page.fill("#q", "")

        # choosing from the menu applies at once; a false "done" can be moved back
        def choose(label):
            page.locator("tr[data-name*='evil'] details.rowmenu > summary").click()
            page.locator("tr[data-name*='evil'] button.mitem", has_text=label).first.click()

        choose("Broker says done")
        page.locator("tr[data-name*='evil'] .pill", has_text="Broker says done").wait_for()
        choose("Awaiting reply")
        page.locator("tr[data-name*='evil'] .pill", has_text="Awaiting reply").wait_for()

        page.locator("a.fpill", has_text="Awaiting reply").click()  # instant, no navigation
        assert page.locator("tr[data-name]:visible").count() == 2
        assert page.evaluate("window.__noReload") == 1
    finally:
        browser.close()
        pw.stop()


def test_move_to_never_allows_removed(ui):
    _sent_request()
    r, _ = ui.post("/act/row", {"slug": "evil", "action": "status:removed"})
    assert r.getheader("Location") == "/?m=err"


def test_settings_edit_and_validate(ui):
    from optout.config import load_config

    _, body = ui.req("GET", "/")
    assert "id='settings'" in body and "name='daily_limit'" in body
    assert "name='password'" in body and "value='abcd" not in body  # password never shown

    form = {
        "legal_name": "Jane Q Public",
        "aliases": "Janie, J Public",
        "dob": "1990-05-14",
        "street": "1 Main St",
        "street2": "Apt 2",
        "city": "Richmond",
        "state": "va",
        "zip": "23219",
        "phones": "",
        "relatives": "",
        "previous": "9 Elm Rd, Norfolk, VA 23510",
        "daily_limit": "80",
        "send_delay_seconds": "3",
        "resend_after_days": "365",
        "gmail_label": "OPTOUT",
        "reply_label": "OPTOUT Response",
        "from_address": "",
        "alias_pattern": "",
    }
    r, _ = ui.post("/act/settings", form)
    assert r.getheader("Location") == "/?m=settings_ok#settings"
    cfg = load_config()
    assert cfg.email.daily_limit == 80 and cfg.email.reply_label == "OPTOUT Response"
    assert cfg.email.resend_after_days == 365
    assert cfg.profile.current_address.state == "VA"
    assert cfg.profile.current_address.street2 == "Apt 2"
    assert cfg.profile.previous_addresses[0].city == "Norfolk"
    assert cfg.profile.aliases == ["Janie", "J Public"]

    for bad in (
        {"daily_limit": "5000"},
        {"resend_after_days": "7"},
        {"dob": "not-a-date"},
        {"previous": "nonsense"},
        {"gmail_label": 'BAD"LABEL'},
        {"alias_pattern": "no-slug@x.example"},
        {"legal_name": "Taylor Swift"},
    ):
        r, _ = ui.post("/act/settings", {**form, **bad})
        assert r.getheader("Location") == "/?m=settings_err#settings", bad
        _, body = ui.req("GET", "/?m=settings_err")
        assert "Couldn&#x27;t save" in body
    assert load_config().email.daily_limit == 80  # nothing invalid was written


def test_password_is_tested_before_saving(ui, monkeypatch):
    from optout.config import load_config
    from optout.engine.methods.email import read_smtp_password

    monkeypatch.setattr(
        "optout.engine.methods.email.check_login",
        lambda cfg, pw: None if pw == "good pass word" else "rejected",
    )
    r, _ = ui.post("/act/password", {"password": "nope"})
    assert r.getheader("Location") == "/?m=pw_bad#settings"
    r, _ = ui.post("/act/password", {"password": "good pass word"})
    assert r.getheader("Location") == "/?m=pw_ok#settings"
    assert read_smtp_password(load_config().email.smtp) == "good pass word"


def test_limit_reached_points_to_settings(ui, monkeypatch):
    monkeypatch.setattr("optout.web.server.sent_today", lambda *args: 40)
    _, body = ui.req("GET", "/")
    assert "emails in the last 24 hours" in body and "href='#settings'" in body


def test_closed_connections_are_ignored_quietly(ui, capsys):
    import socket

    for _ in range(5):  # ask for the page, then hang up before reading it
        s = socket.create_connection(("127.0.0.1", ui.port))
        s.sendall(
            f"GET / HTTP/1.1\r\nHost: {ui.host}\r\nCookie: {COOKIE}={ui.token}\r\n\r\n".encode()
        )
        s.close()
    import time

    time.sleep(0.5)
    assert ui.req("GET", "/")[0].status == 200  # still serving
    err = capsys.readouterr().err
    assert "Traceback" not in err and "BrokenPipe" not in err


def test_attention_explains_what_to_do(ui):
    from optout.db import InboxMessage, list_submissions

    _sent_request()
    with get_session() as s:
        sub = list_submissions(s)[0]
        sub.status = "awaiting_user_input"
        s.add(sub)
        s.add(
            InboxMessage(
                message_id="<w1>",
                submission_id=sub.id,
                broker_slug="evil",
                from_addr="privacy@evil.example",
                subject="Automatic reply",
                kind="need-info",
                text="Thanks. We do not accept requests via email. You can submit a privacy "
                "request via our web form here: https://forms.evil.example/privacy?x=1. Bye.",
                state="applied",
            )
        )
        s.commit()
    _, body = ui.req("GET", "/")
    start = body.index("id='attention-h'")
    attn = body[start : body.index("class='card", start)]  # up to the next card
    assert "via our web form here" in attn  # the sentence that matters
    assert "Open their link" in attn and "forms.evil.example" in attn
    assert "rel='noreferrer noopener'" in attn  # never opened for you, no referrer
    assert ">Done<" in attn and ">Close<" in attn
    assert "mail.google.com/mail/u/0/#search/rfc822msgid%3Aw1" in attn  # the exact email
    assert "then click <b>Done</b>" in attn  # the section says how it works
    assert "https://forms.evil.example" not in attn.split("class='quote'")[1].split("</p>")[0]

    r, _ = ui.post("/act/row", {"slug": "evil", "action": "status:closed"})
    assert r.getheader("Location") == "/?m=toggle"
    _, body = ui.req("GET", "/")
    assert "id='attention-h'" not in body  # closed: no longer nagging
    assert "Closed</span>" in body


def test_verified_tile_only_when_something_can_be_verified(ui):
    _sent_request()  # an email broker: nothing to re-search
    _, body = ui.req("GET", "/")
    stats = body[
        body.index("aria-label='Summary'") : body.index(
            "</section>", body.index("aria-label='Summary'")
        )
    ]
    assert "Broker says done" in stats and "They told you it" in stats
    assert "Verified removed" not in stats


def test_time_zone_and_send_time_settings(ui):
    from optout.config import load_config

    _, body = ui.req("GET", "/")
    assert "name='timezone'" in body and "name='daily_send_time'" in body
    base = {
        "legal_name": "Jane Q Public",
        "dob": "1990-05-14",
        "street": "1 Main St",
        "city": "Richmond",
        "state": "VA",
        "zip": "23219",
        "daily_limit": "40",
        "send_delay_seconds": "5",
    }
    r, _ = ui.post(
        "/act/settings", {**base, "timezone": "America/New_York", "daily_send_time": "09:30"}
    )
    assert r.getheader("Location") == "/?m=settings_ok#settings"
    cfg = load_config()
    assert cfg.timezone == "America/New_York" and cfg.email.daily_send_time == "09:30"
    _, body = ui.req("GET", "/")
    assert " EDT</span>" in body or " EST</span>" in body  # times shown in that zone
    for bad in ({"timezone": "Mars/Olympus"}, {"daily_send_time": "25:00"}):
        r, _ = ui.post("/act/settings", {**base, "daily_send_time": "09:30", **bad})
        assert r.getheader("Location") == "/?m=settings_err#settings", bad


def test_groups_and_contributor_tools_in_settings(ui):
    from optout.config import load_config

    base = {
        "legal_name": "Jane Q Public",
        "dob": "1990-05-14",
        "street": "1 Main St",
        "city": "Richmond",
        "state": "VA",
        "zip": "23219",
        "daily_limit": "40",
        "send_delay_seconds": "5",
        "daily_send_time": "10:00",
    }
    _, body = ui.req("GET", "/")
    assert "Groups you belong to" in body and "Not set yet" in body
    assert "name='group_educators'" in body and "Contributor tools" in body
    assert load_config().my_groups is None  # untouched until saved

    # an old page (no groups_present) can't wipe the setting
    ui.post("/act/settings", {**base, "groups_present": "1", "group_educators": "on"})
    # an educator is also a work contact, so that group comes along
    assert load_config().my_groups == ["business-professionals", "educators"]
    ui.post("/act/settings", base)
    assert load_config().my_groups == ["business-professionals", "educators"]
    # saved with nothing ticked: set, and in no group
    ui.post("/act/settings", {**base, "groups_present": "1"})
    assert load_config().my_groups == []


def test_coverage_panel_only_with_contributor_tools_and_is_strict(ui, tmp_path):
    from optout.audiences import load_coverage
    from optout.config import load_config, save_config
    from optout.db import InboxMessage, list_submissions

    _sent_request()
    with get_session() as s:
        sub = list_submissions(s)[0]
        sub.status = "awaiting_user_input"
        s.add(sub)
        s.add(
            InboxMessage(
                message_id="<c1>",
                submission_id=sub.id,
                broker_slug="evil",
                from_addr="privacy@evil.example",
                subject="Re",
                kind="need-info",
                text="We only hold data on teachers. Dear Jane Q Public, use our form.",
                state="applied",
            )
        )
        s.commit()
    _, body = ui.req("GET", "/")
    assert "class='mini coverage'" not in body

    cfg = load_config()
    cfg.contributor_tools = True
    save_config(cfg)
    _, body = ui.req("GET", "/")
    assert "class='mini coverage'" in body and "name='aud_educators'" in body
    prompt = body[body.index("id='cov-evil'") :]
    prompt = prompt[: prompt.index("</textarea>")]
    assert "Jane" not in prompt and "[MY NAME]" in prompt  # redacted before copying

    r, _ = ui.post("/act/coverage", {"slug": "evil", "aud_educators": "on", "note": "Teachers."})
    assert r.getheader("Location") == "/?m=cov_ok"
    assert load_coverage()["evil"].audiences == ("educators",)
    _, body = ui.req("GET", "/")
    assert "Only covers: educators" in body

    good = "```yaml\n- broker: evil\n  audiences: [students]\n  note: Students.\n```"
    ui.post("/act/coverage-claude", {"slug": "evil", "answer": good})
    assert load_coverage()["evil"].audiences == ("students",)
    for bad in (
        "```yaml\n- broker: other\n  audiences: [educators]\n```",
        "```yaml\n- broker: evil\n  audiences: [wizards]\n```",
        "not yaml: [",
    ):
        r, _ = ui.post("/act/coverage-claude", {"slug": "evil", "answer": bad})
        assert r.getheader("Location") == "/?m=err", bad
    assert load_coverage()["evil"].audiences == ("students",)  # nothing bad was saved
    r, _ = ui.post("/act/coverage", {"slug": "evil"})  # nothing picked
    assert r.getheader("Location") == "/?m=err"


def test_row_menu_is_small_and_picks_the_right_email(ui):
    import re as _re

    _sent_request()
    _, body = ui.req("GET", "/")
    menu = body[body.index("<details class='rowmenu'>") :]
    menu = menu[: menu.index("</details>")]
    assert len(_re.findall(r"name='action' value=", menu)) <= 9  # was 19
    assert "Never contact this broker" in menu and "Set status" in menu

    for status, expected in (("awaiting_broker_confirmation", "followup"), ("rejected", "appeal")):
        with get_session() as s:
            sub = list_submissions(s)[0]
            sub.status = status
            s.add(sub)
            s.commit()
        r, _ = ui.post("/act/row", {"slug": "evil", "action": "draft:auto"})
        assert f"dk={expected}" in r.getheader("Location"), status

    # moving a status to "Broker says done" also counts as a reply for the chart
    ui.post("/act/row", {"slug": "evil", "action": "status:confirmed"})
    with get_session() as s:
        sub = list_submissions(s)[0]
        assert sub.status == "confirmed"
        events = [e.event_type for e in list_events(s, sub.id)]
    assert "confirmation_received" in events and "status_set" in events


def test_failed_email_can_be_resent_from_the_table(ui, monkeypatch):
    from optout.db import create_submission

    started: list[tuple[int, str | None]] = []
    monkeypatch.setattr(
        "optout.web.server.App.start_campaign",
        lambda self, n, only=None, **kw: started.append((n, only)) or "started",
    )
    with get_session() as s:
        sub = create_submission(
            s,
            broker_slug="evil",
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
        sub.status = "failed"
        sub.failure_reason = "SMTP error: Connection unexpectedly closed"
        s.add(sub)
        s.commit()
    _, body = ui.req("GET", "/")
    assert "action='/act/retry'" in body and "Try sending this email again" in body
    assert "Connection unexpectedly closed" in body  # why, on hover

    r, _ = ui.post("/act/retry", {"slug": "evil"})
    assert r.getheader("Location") == "/?m=started#send" and started == [(1, "evil")]
    assert ui.post("/act/retry", {"slug": "nobody"})[0].getheader("Location") == "/?m=err"


def test_bounced_or_sent_requests_have_no_resend_button(ui):
    from optout.db import create_submission, mark_submitted

    with get_session() as s:
        sub = create_submission(
            s,
            broker_slug="evil",
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
        mark_submitted(s, sub, {}, None, 45)
        sub.status = "failed"  # a bounce: it was sent, then rejected
        sub.failure_reason = "bounced: the broker's address rejected it"
        s.add(sub)
        s.commit()
    _, body = ui.req("GET", "/")
    assert (
        "action='/act/retry'"
        not in body.split("<main")[1].split("</main>")[0].split("action='/act/settings'")[0]
    )
    assert ui.post("/act/retry", {"slug": "evil"})[0].getheader("Location") == "/?m=err"


def test_attention_copy_prompt_and_honest_chart_label(ui):
    from optout.db import InboxMessage, list_submissions

    _sent_request()
    with get_session() as s:
        sub = list_submissions(s)[0]
        sub.status = "awaiting_user_input"
        s.add(sub)
        s.add(
            InboxMessage(
                message_id="<p1>",
                submission_id=sub.id,
                broker_slug="evil",
                from_addr="privacy@evil.example",
                subject="Re",
                kind="need-info",
                text="Use the form: https://forms.evil.example/x/SECRETTOKEN. Dear Jane.",
                state="applied",
            )
        )
        s.commit()
    _, body = ui.req("GET", "/")
    assert "Not sure what to do? Ask Claude" in body and "data-copy='attn-prompt'" in body
    assert "SECRETTOKEN" not in body.split("id='attn-prompt'")[1].split("</textarea>")[0]
    assert ">Says done<" in body or "Says done" in body  # chart legend, not "Removed"
    assert "Removed 0" not in body


def test_alternate_address_only_from_the_same_company(ui):
    from optout.web.data import alternate_addresses

    text = (
        "Email optout@experian.example<mailto:optout@experian.example> or privacy@evil.example. "
        "Also stranger@gmail.com and noreply@experian.example and ca_drop@experian.example."
    )
    got = alternate_addresses(text, "ca_drop@experian.example", "dsar@experian.example")
    assert got == ["optout@experian.example"]  # not another company, not noreply, not sender
    assert alternate_addresses("write to a@other.example", "x@acme.example", "p@acme.example") == []


def test_resend_to_button_and_route_are_guarded(ui, monkeypatch):
    from optout.db import InboxMessage, list_submissions

    _sent_request()
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "optout.web.server.App.start_resend",
        lambda self, slug, addr: started.append((slug, addr)) or "started",
    )
    with get_session() as s:
        sub = list_submissions(s)[0]
        sub.status = "awaiting_user_input"
        s.add(sub)
        s.add(
            InboxMessage(
                message_id="<r1>",
                submission_id=sub.id,
                broker_slug="evil",
                from_addr="auto@evil.example",
                subject="Auto",
                kind="need-info",
                state="applied",
                text="This inbox is not monitored. Please email optout@evil.example, "
                "or privacy@thief.example.",
            )
        )
        s.commit()
    _, body = ui.req("GET", "/")
    assert "Send my request there" in body and "optout@evil.example" in body
    assert "value='privacy@thief.example'" not in body  # no button for another company

    r, _ = ui.post("/act/resend-to", {"slug": "evil", "address": "optout@evil.example"})
    assert r.getheader("Location") == "/?m=started#send" and started == [
        ("evil", "optout@evil.example")
    ]
    for bad in ("privacy@thief.example", "someone@evil.example", ""):  # not in their reply
        r, _ = ui.post("/act/resend-to", {"slug": "evil", "address": bad})
        assert r.getheader("Location") == "/?m=err", bad
    assert len(started) == 1


@pytest.mark.parametrize(
    "text,link,other_email,expected",
    [
        (
            "One more step is required before we can start working on your request. Please "
            "confirm your email by clicking on the button below.",
            True,
            False,
            "Action needed: Confirm your email",
        ),
        (
            "You can submit a privacy request via our interactive web form here.",
            True,
            False,
            "Action needed: Form submission",
        ),
        (
            "Please email optout@x.example. To exercise all other privacy rights, visit our "
            "Privacy Rights Portal.",
            False,
            True,
            "Action needed: Form submission or new email",
        ),
        (
            "Please write to privacy@x.example with your full details.",
            False,
            True,
            "Action needed: New email",
        ),
        (
            "The information provided is insufficient to verify a matching record.",
            False,
            False,
            "Action needed: More information",
        ),
        (
            "Call us toll-free at 800-555-0100 or write to our P.O. Box.",
            False,
            False,
            "Action needed: Phone or mail",
        ),
        ("Thanks. We need something from you.", False, False, "Action needed"),
    ],
)
def test_action_label_says_what_to_do(text, link, other_email, expected):
    from optout.web.data import action_label

    assert action_label(text, has_link=link, has_other_email=other_email) == expected


def test_show_email_toggles_to_hide_email(ui):
    from optout.db import InboxMessage, list_submissions

    _sent_request()
    with get_session() as s:
        sub = list_submissions(s)[0]
        sub.status = "awaiting_user_input"
        s.add(sub)
        s.add(
            InboxMessage(
                message_id="<t1>",
                submission_id=sub.id,
                broker_slug="evil",
                from_addr="p@evil.example",
                subject="Re",
                kind="need-info",
                text="Please confirm your email by clicking https://x.evil.example/c.",
                state="applied",
            )
        )
        s.commit()
    _, body = ui.req("GET", "/")
    assert (
        "<span class='on-closed'>Show email</span><span class='on-open'>Hide email</span>" in body
    )
    assert "Action needed: Confirm your email" in body
    for label in ("Show as table",):
        assert f"<span class='on-closed'>{label}</span>" in body or label not in body


def test_hide_label_appears_in_a_real_browser(ui):
    from optout.db import InboxMessage, list_submissions

    sync_api = pytest.importorskip("playwright.sync_api")
    _sent_request()
    with get_session() as s:
        sub = list_submissions(s)[0]
        sub.status = "awaiting_user_input"
        s.add(sub)
        s.add(
            InboxMessage(
                message_id="<t2>",
                submission_id=sub.id,
                broker_slug="evil",
                from_addr="p@evil.example",
                subject="Re",
                kind="need-info",
                text="Please use our web form here: https://f.evil.example/x.",
                state="applied",
            )
        )
        s.commit()
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:
        pytest.skip(f"no browser: {type(exc).__name__}")
    try:
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{ui.port}/?t={ui.token}")
        summary = page.locator("details.mini > summary", has_text="Show email").first
        assert summary.inner_text().strip() == "Show email"
        summary.click()
        assert summary.inner_text().strip() == "Hide email"
        summary.click()
        assert summary.inner_text().strip() == "Show email"
    finally:
        browser.close()
        pw.stop()


def test_only_privacy_mailboxes_are_offered_as_a_new_address():
    from optout.web.data import alternate_addresses

    text = (
        "Questions? security@clearview.example, bugbounty@clearview.example, "
        "abadurina@clearview.example, sales@clearview.example. For privacy requests use "
        "privacy@clearview.example or datasubjectrequests@clearview.example "
        "or optout@clearview.example."
    )
    got = alternate_addresses(text, "auto@clearview.example", "x@clearview.example", limit=5)
    assert got == [
        "privacy@clearview.example",
        "datasubjectrequests@clearview.example",
        "optout@clearview.example",
    ]


def test_coverage_reaches_the_attention_prompt_on_the_dashboard(ui):
    from optout.audiences import save_local
    from optout.db import InboxMessage, list_submissions

    _sent_request()
    save_local("evil", ["educators"], "Teachers only.")
    with get_session() as s:
        sub = list_submissions(s)[0]
        sub.status = "awaiting_user_input"
        s.add(sub)
        s.add(
            InboxMessage(
                message_id="<c9>",
                submission_id=sub.id,
                broker_slug="evil",
                from_addr="p@evil.example",
                subject="Re",
                kind="need-info",
                text="Please use our web form here: https://f.evil.example/x.",
                state="applied",
            )
        )
        s.commit()
    _, body = ui.req("GET", "/")
    prompt = body.split("id='attn-prompt'")[1].split("</textarea>")[0]
    assert "Covers only a specific group: educators" in prompt


def test_corrections_are_offered_for_sharing_without_repeating_shared_ones(ui):
    from optout.brokers.contacts import save_local

    _sent_request()
    # A real-looking address: placeholder domains (.example, .test...) are never offered.
    save_local("evil", "optout@evil-data.net", "Their reply named it.")
    save_local("6sense-insights", "datasubjectrequests@6sense.com", "same as shipped")
    _, body = ui.req("GET", "/")
    share = body.split("# add to data/contact_overrides.yml")[1].split("</pre>")[0]
    assert "optout@evil-data.net" in share
    assert "6sense" not in share  # already in the shared list: not offered again


def test_resend_offer_compares_with_where_the_request_actually_went(ui, monkeypatch):
    from optout.brokers.contacts import save_local
    from optout.db import InboxMessage, create_submission, mark_submitted

    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "optout.web.server.App.start_resend",
        lambda self, slug, addr: started.append((slug, addr)) or "started",
    )
    with get_session() as s:
        sub = create_submission(
            s,
            broker_slug="evil",
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
        mark_submitted(s, sub, {"to": "old@evil.example"}, None, 45)
        sub.status = "awaiting_user_input"
        s.add(sub)
        s.add(
            InboxMessage(
                message_id="<o1>",
                submission_id=sub.id,
                broker_slug="evil",
                from_addr="auto@evil.example",
                subject="Re",
                kind="need-info",
                text="Not monitored. Please email privacy-new@evil.example.",
                state="applied",
            )
        )
        s.commit()
    # the shared list has since corrected this broker's contact to the very address they named
    save_local("evil", "privacy-new@evil.example", "corrected")
    _, body = ui.req("GET", "/")
    assert "Send my request there" in body  # the old request still needs to go there
    r, _ = ui.post("/act/resend-to", {"slug": "evil", "address": "privacy-new@evil.example"})
    assert started == [("evil", "privacy-new@evil.example")]


def test_sorting_the_table_in_a_real_browser(ui):
    from optout.db import create_submission, mark_submitted

    sync_api = pytest.importorskip("playwright.sync_api")
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    with get_session() as s:
        for slug, days_ago in (("evil", 5), ("zeta-co", 1), ("alpha-co", 3)):
            sub = create_submission(
                s,
                broker_slug=slug,
                method_used="email",
                legal_basis_cited=[],
                statutory_response_days=45,
            )
            mark_submitted(s, sub, {}, None, 45, submitted_at=now - timedelta(days=days_ago))
            sub.status = "awaiting_broker_confirmation"
            s.add(sub)
            s.commit()
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:
        pytest.skip(f"no browser: {type(exc).__name__}")
    try:
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{ui.port}/?t={ui.token}")

        def order():
            return [
                r.get_attribute("data-name").split()[-1]
                for r in page.locator("tr[data-name]:visible").all()
            ]

        def click(label):
            page.locator("th button.sortbtn", has_text=label).click()

        def names():
            return [
                r.get_attribute("data-name") for r in page.locator("tr[data-name]:visible").all()
            ]

        click("Broker")
        assert names() == sorted(names())  # A to Z by name
        assert page.locator("th[data-sortkey=name]").get_attribute("aria-sort") == "ascending"
        click("Broker")
        assert names() == sorted(names(), reverse=True)
        assert page.locator("th[data-sortkey=name]").get_attribute("aria-sort") == "descending"
        click("Sent")
        assert order() == ["zeta-co", "alpha-co", "evil"]  # newest first
        click("Sent")
        assert order() == ["evil", "alpha-co", "zeta-co"]  # then oldest first
        assert page.locator("th[data-sortkey=name]").get_attribute("aria-sort") == "none"
        # the arrow is a real character, not an escape that turned into a control character
        css = page.evaluate(
            "getComputedStyle(document.querySelector("
            "'th[data-sortkey=days] button'), '::after').content"
        )
        assert css.strip('"') in ("↕", "▲", "▼") and "95" not in css
        assert not errors, errors
    finally:
        browser.close()
        pw.stop()


def test_stats_separate_heard_back_from_silent_and_latest_reply_is_shown(ui):
    from optout.db import InboxMessage, create_submission, list_submissions, mark_submitted

    with get_session() as s:
        for slug in ("evil", "quiet-co", "chatty-co"):
            sub = create_submission(
                s,
                broker_slug=slug,
                method_used="email",
                legal_basis_cited=[],
                statutory_response_days=45,
            )
            mark_submitted(s, sub, {}, None, 45)
            sub.status = "awaiting_broker_confirmation"
            s.add(sub)
        s.commit()
        # "evil" sent an acknowledgement only: still waiting, but not silent
        evil = [x for x in list_submissions(s) if x.broker_slug == "evil"][0]
        s.add(
            InboxMessage(
                message_id="<h1>",
                submission_id=evil.id,
                broker_slug="evil",
                from_addr="p@evil.example",
                subject="Re",
                kind="auto-reply",
                state="applied",
                received_at=datetime(2026, 10, 28, 15, tzinfo=UTC),
            )
        )
        s.commit()
    _, body = ui.req("GET", "/")
    stats = body[body.index("aria-label='Summary'") :]
    stats = stats[: stats.index("</section>")]
    assert "Heard back" in stats and "No reply yet" in stats
    assert "<div class='n'>3</div><div class='l'><span class='dot'" in stats  # 3 sent
    heard = stats[stats.index("Heard back") - 220 : stats.index("Heard back")]
    silent = stats[stats.index("No reply yet") - 220 : stats.index("No reply yet")]
    assert ">1</div>" in heard and ">2</div>" in silent  # 1 replied, 2 silent
    assert "Latest reply" in body and "Last check" not in body
    assert "acknowledged" in body  # what the broker's latest email was


_CONFIRM_TEXT = (
    "Dear Jane, One more step is required before we can start working on your request. "
    "Please confirm your email by clicking on the button below.\n\n"
    "Confirm email (https://x-privacy.my.onetrust.com/privacyaccess/verify/tok123)\n"
)


def _pending_confirm(sender="noreply@m.onetrust.com", text=_CONFIRM_TEXT):
    from optout.db import InboxMessage, list_submissions

    _sent_request()
    with get_session() as s:
        sub = list_submissions(s)[0]
        sub.status = "awaiting_user_input"
        s.add(sub)
        s.add(
            InboxMessage(
                message_id="<cf1>",
                submission_id=sub.id,
                broker_slug="evil",
                from_addr=sender,
                subject="Your Privacy Request",
                kind="need-info",
                text=text,
                state="applied",
            )
        )
        s.commit()


def test_confirm_for_me_is_offered_for_known_senders_and_works(ui, monkeypatch):
    from optout.engine import confirm as cf

    _pending_confirm()
    _, body = ui.req("GET", "/")
    assert "Confirm for me" in body and "action='/act/confirm'" in body
    seen: list[str] = []
    monkeypatch.setattr(
        cf,
        "default_fetch",
        lambda url, hosts: (
            seen.append(url)
            or cf.Fetched(200, "Your email has been verified.", "x-privacy.my.onetrust.com")
        ),
    )
    # the form carries no address: even if one is supplied, it is ignored
    r, _ = ui.post("/act/confirm", {"slug": "evil", "url": "https://phish.example/x"})
    assert r.getheader("Location").startswith("/?m=conf_ok&n=")
    assert seen == ["https://x-privacy.my.onetrust.com/privacyaccess/verify/tok123"]
    with get_session() as s:
        from optout.db import list_submissions

        assert list_submissions(s)[0].status == "awaiting_broker_confirmation"
    _, body = ui.req("GET", "/")
    assert "id='attention-h'" not in body  # it's off the attention list


def test_an_unknown_sender_is_only_clicked_after_you_teach_it(ui, monkeypatch, tmp_path):
    from optout.engine import confirm as cf

    text = _CONFIRM_TEXT.replace("x-privacy.my.onetrust.com", "t.my.newplatform.example")
    _pending_confirm(sender="noreply@mail.newplatform.example", text=text)
    seen: list[str] = []
    monkeypatch.setattr(
        cf,
        "default_fetch",
        lambda url, hosts: (
            seen.append(url) or cf.Fetched(200, "Thank you, verified.", "t.my.newplatform.example")
        ),
    )
    _, body = ui.req("GET", "/")
    assert "Confirm &amp; remember" in body and "Confirm for me" not in body
    r, _ = ui.post("/act/confirm", {"slug": "evil"})  # no 'remember': refused
    assert r.getheader("Location") == "/?m=err" and seen == []
    r, _ = ui.post("/act/confirm", {"slug": "evil", "remember": "1"})
    assert r.getheader("Location").startswith("/?m=conf_ok&n=") and len(seen) == 1
    assert any(f.sender == "@mail.newplatform.example" for f in cf.load_flows())  # remembered


def test_confirm_setting_defaults_to_ask_and_is_saved(ui):
    from optout.config import load_config

    base = {
        "legal_name": "Jane Q Public",
        "dob": "1990-05-14",
        "street": "1 Main St",
        "city": "Richmond",
        "state": "VA",
        "zip": "23219",
        "daily_limit": "40",
        "send_delay_seconds": "5",
        "daily_send_time": "10:00",
    }
    _, body = ui.req("GET", "/")
    assert "name='confirm_links'" in body and load_config().confirm_links == "ask"
    ui.post("/act/settings", {**base, "confirm_links": "auto"})
    assert load_config().confirm_links == "auto"
    ui.post("/act/settings", base)  # unticked/absent goes back to asking
    assert load_config().confirm_links == "ask"


def test_top_bar_has_a_fetch_emails_button_only_on_the_dashboard():
    from optout.web.render import page

    on = page("t", "<p>x</p>", nav=False, updated="now", fetch_csrf="tok")
    assert "action='/act/inbox'" in on and "Fetch emails" in on and "tok" in on
    assert "Fetch emails" not in page("t", "<p>x</p>", nav=True)
    assert "Fetch emails" not in page("t", "<p>x</p>", nav=False, updated="now")


def test_form_said_removed_marks_the_request_as_their_word(ui):
    from optout.db import create_submission, get_session, list_submissions, update_status

    with get_session() as session:
        sub = create_submission(
            session,
            broker_slug="evil",
            method_used="email",
            legal_basis_cited=[],
            statutory_response_days=45,
        )
        update_status(session, sub, "awaiting_user_input")
    r, _ = ui.post("/act/row", {"slug": "evil", "action": "form-removed"})
    assert r.status == 303
    with get_session() as session:
        (got,) = list_submissions(session, broker_slug="evil")
        assert got.status == "confirmed"


def test_expiry_is_read_from_their_wording_and_promises_are_ignored():
    from datetime import UTC, datetime, timedelta

    from optout.web.data import _expires_in

    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    got = now - timedelta(days=1)
    five = "You must confirm your email address within five (5) days. Click Confirm Email."
    assert round(_expires_in(five, got, now) or 0) == 4
    day = _expires_in("This verification link expires in 24 hours.", got, now)
    assert day is not None and abs(day) < 0.01  # lapses about now
    assert _expires_in("We will process your request within 45 days.", got, now) is None
    blis = (
        "If we do not receive your MAID within 14 days, we will not be able to process it "
        "and it will be closed."
    )
    assert round(_expires_in(blis, got, now) or 0) == 13
    assert _expires_in("Thanks for writing to us.", got, now) is None
    fifteen = "This link will expire 15 minutes after verification was sent."
    assert (_expires_in(fifteen, got, now) or 0) < 0  # long gone: shown as Expired


def test_the_link_picked_is_their_form_not_a_policy_terms_or_drop_page():
    from optout.web.data import _best_link

    assert (
        _best_link(
            "Use our portal: https://app.example.com/ownyourdata. Agents: "
            "https://app.example.com/dsr-agent-api-terms. Policies: https://app.example.com/policies/"
        )
        == "https://app.example.com/ownyourdata"
    )
    assert (
        _best_link(
            "California residents may use DROP at https://consumer.drop.privacy.ca.gov/ or our "
            "form https://clients.example.com/public/optout/capture"
        )
        == "https://clients.example.com/public/optout/capture"
    )
    assert (
        _best_link(
            "See https://pages.example.io/privacy-policy and submit at https://preferences.example.io"
        )
        == "https://preferences.example.io"
    )
    assert _best_link("Follow us https://twitter.com/x") == ""


def _people_site(ui):
    """A never-contacted people-search site, so the plan has a prompt to copy."""
    import yaml as _yaml

    from optout.brokers import registry as reg

    d = reg._brokers_dir()
    (d / "findme.yml").write_text(
        _yaml.safe_dump(
            {
                "slug": "findme",
                "name": "FindMe",
                "domain": "findme.example",
                "category": "people_search",
                "method": "manual",
                "opt_out_url": "https://findme.example/optout",
                "priority": 1,
                "legal_basis": ["CCPA"],
                "statutory_response_days": 45,
            }
        )
    )
    reg._registry.clear()
    reg._loaded = False


def test_plan_prompt_comes_as_json_for_one_click_copy(ui):
    import json as _json

    _people_site(ui)
    r, body = ui.req("GET", "/prompt/people-search?format=json")
    got = _json.loads(body)
    assert r.status == 200 and got["count"] == 1 and "F1. broker: findme" in got["text"]
    _, page_ = ui.req("GET", "/")
    assert "data-copy-url='/prompt/people-search'" in page_


def test_copy_browser_prompt_puts_it_on_the_clipboard_in_one_click(ui):
    sync_api = pytest.importorskip("playwright.sync_api")
    _people_site(ui)
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:
        pytest.skip(f"no browser: {type(exc).__name__}")
    try:
        ctx = browser.new_context()
        ctx.grant_permissions(
            ["clipboard-read", "clipboard-write"], origin=f"http://127.0.0.1:{ui.port}"
        )
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{ui.port}/?t={ui.token}")
        btn = page.locator("button[data-copy-url='/prompt/people-search']")
        btn.click()
        page.locator("button[data-copy-url]", has_text="Copied").wait_for(timeout=10000)
        assert "F1. broker: findme" in page.evaluate("navigator.clipboard.readText()")
        assert page.url.endswith(f"?t={ui.token}") or "/prompt/" not in page.url  # stayed put
    finally:
        browser.close()
        pw.stop()


def test_reply_sender_links_to_their_latest_email_in_gmail(ui):
    from datetime import UTC, datetime, timedelta

    from optout.db import InboxMessage, list_submissions

    _sent_request()
    with get_session() as s:
        sub = list_submissions(s)[0]
        for mid, ago in (("<old@x>", 3), ("<new@x>", 1)):
            s.add(
                InboxMessage(
                    message_id=mid,
                    submission_id=sub.id,
                    broker_slug="evil",
                    from_addr="privacy@evil.example",
                    subject="Re: request",
                    kind="auto-reply",
                    text="Thanks.",
                    state="applied",
                    received_at=datetime.now(UTC) - timedelta(days=ago),
                )
            )
        s.commit()
    _, body = ui.req("GET", "/")
    row = body[body.index("↩ ") : body.index("</div>", body.index("↩ "))]
    assert "rfc822msgid%3Anew%40x" in row and ">privacy@evil.example</a>" in row
    assert "rel='noreferrer noopener'" in row


def test_a_reply_to_review_links_to_the_email_in_gmail(ui):
    from optout.db import InboxMessage

    with get_session() as s:
        s.add(
            InboxMessage(
                message_id="<rev1@mail.example>",
                from_addr="noreply@someplatform.example",
                subject="Your request",
                text="Something odd.",
                kind="unknown",
                state="review",
            )
        )
        s.commit()
    _, body = ui.req("GET", "/")
    replies = body[body.index("id='replies'") : body.index("id='requests'")]
    assert "Show the email" in replies
    assert "rfc822msgid%3Arev1%40mail.example" in replies
    assert "rel='noreferrer noopener'" in replies


def test_california_residents_are_pointed_to_drop_first(ui):
    from optout.config import load_config, save_config

    _, body = ui.req("GET", "/")
    assert "consumer.drop.privacy.ca.gov" not in body  # Virginia: not relevant
    cfg = load_config()
    cfg = cfg.model_copy(
        update={
            "profile": cfg.profile.model_copy(
                update={
                    "current_address": cfg.profile.current_address.model_copy(
                        update={"state": "CA"}
                    )
                }
            )
        }
    )
    save_config(cfg)
    _, body = ui.req("GET", "/")
    assert "California resident?" in body and "https://consumer.drop.privacy.ca.gov/" in body


def test_a_finished_batch_reads_replies_in_shortly_after_once(ui, monkeypatch):
    import time as _time

    from optout.web import server

    ran = []
    monkeypatch.setattr("optout.engine.inbox.gmail_ready", lambda c: None)  # Gmail is set up
    monkeypatch.setattr("optout.engine.inbox.run_sync", lambda c, **k: ran.append(1))
    app = ui.app_
    app.fetch_after_batch(delay=0)
    app.fetch_after_batch(delay=0)  # a second one while the first is queued: coalesced
    _time.sleep(0.5)
    assert ran == [1]
    assert app._fetch_pending is False
    assert server.App  # imported: the setting below is what turns it off

    from optout.config import load_config, save_config

    cfg = load_config()
    save_config(
        cfg.model_copy(update={"email": cfg.email.model_copy(update={"fetch_after_batch": False})})
    )
    app.fetch_after_batch(delay=0)
    _time.sleep(0.3)
    assert ran == [1]  # turned off in Settings: no second check


def test_settings_toggles_for_reading_replies_in_and_marking_them_read(ui):
    from optout.config import load_config

    _, body = ui.req("GET", "/")
    assert "name='fetch_after_batch'" in body and "name='mark_replies_read'" in body
    assert load_config().email.fetch_after_batch is True  # on by default


def test_link_picking_handles_mangled_links_and_prefers_opt_out_over_account_pages():
    from optout.web.data import _best_link

    # a mail client turned "https://" into "https:/", and a banner link came first
    assert (
        _best_link(
            "Notice https://aka.ms/LearnAboutSenderIdentification. Use our form "
            "https:/privacyportal.example.com/webform/abc123"
        )
        == "https://privacyportal.example.com/webform/abc123"
    )
    # an account-deletion page is not the opt-out form
    assert (
        _best_link(
            "Opt out: https://www.example.com/opt-out . Delete your account: "
            "https://www.example.com/dashboard/account/delete . Right to know: "
            "https://www.example.com/request-my-info"
        )
        == "https://www.example.com/opt-out"
    )
