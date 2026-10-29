"""The local UI: authentication, request forgery defences, escaping, and the send flow."""

from __future__ import annotations

import http.client
import threading
from datetime import date
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
from optout.db import get_session, init_db, list_submissions
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
        "optout.web.server.App.start_campaign", lambda self, n: started.append(n) or "started"
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
    assert "Broker says done</span></td>" in body
    assert "Removed (verified)</span></td>" not in body
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
    assert "/act/row" in body and "Automated reply only" in body

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
        "optout.web.server.App.start_campaign", lambda self, n: started.append(n) or "started"
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

    def fake_start(self, n):
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
        assert page.locator("form.rowform button:visible").count() == 0  # no Go buttons

        page.fill("#q", "other")  # filters as you type, no Enter
        assert page.locator("tr[data-name]:visible").count() == 1
        page.fill("#q", "zzz")
        assert page.locator("#nomatch").is_visible()
        page.fill("#q", "")

        # choosing from the menu applies at once; a false "done" can be moved back
        evil = page.locator("tr[data-name*='evil'] select")
        evil.select_option("status:confirmed")
        page.locator("tr[data-name*='evil'] .pill", has_text="Broker says done").wait_for()
        page.locator("tr[data-name*='evil'] select").select_option(
            "status:awaiting_broker_confirmation"
        )
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
        "gmail_label": "OPTOUT",
        "reply_label": "OPTOUT Response",
        "from_address": "",
        "alias_pattern": "",
    }
    r, _ = ui.post("/act/settings", form)
    assert r.getheader("Location") == "/?m=settings_ok#settings"
    cfg = load_config()
    assert cfg.email.daily_limit == 80 and cfg.email.reply_label == "OPTOUT Response"
    assert cfg.profile.current_address.state == "VA"
    assert cfg.profile.current_address.street2 == "Apt 2"
    assert cfg.profile.previous_addresses[0].city == "Norfolk"
    assert cfg.profile.aliases == ["Janie", "J Public"]

    for bad in (
        {"daily_limit": "5000"},
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
    monkeypatch.setattr("optout.web.server.sent_today", lambda session, now: 40)
    _, body = ui.req("GET", "/")
    assert "sent today's limit" in body and "href='#settings'" in body


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
    attn = body[body.index("id='attn-h'") : body.index("</section>", body.index("id='attn-h'"))]
    assert "via our web form here" in attn  # the sentence that matters
    assert "Open their link ↗" in attn and "forms.evil.example" in attn
    assert "rel='noreferrer noopener'" in attn  # never opened for you, no referrer
    assert "I&#x27;ve done it" in attn and ">Close<" in attn

    r, _ = ui.post("/act/row", {"slug": "evil", "action": "status:closed"})
    assert r.getheader("Location") == "/?m=toggle"
    _, body = ui.req("GET", "/")
    assert "id='attn-h'" not in body  # closed: no longer nagging
    assert "Closed</span>" in body
