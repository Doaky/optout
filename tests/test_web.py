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
        "a=" + "x" * 20000,
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
    assert "1990" not in body and "5125550000" not in body


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
