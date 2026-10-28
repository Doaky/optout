"""Local web UI. Binds to 127.0.0.1 only and defends against other sites in your browser.

Threat model: the data is personal and actions send real email, so the risk is
*another website you visit* making your browser talk to this server, or another
program on the machine reading it. Defences:

- listens on 127.0.0.1 only (not configurable)
- random per-run token; the link printed in the terminal sets a session cookie
  (HttpOnly, SameSite=Strict) and is then redirected away from the URL
- Host header must be 127.0.0.1/localhost:<port> (blocks DNS rebinding)
- POSTs also need a matching Origin (when sent), Sec-Fetch-Site same-origin,
  a per-run CSRF token, and a form content type
- GETs never change state; no JavaScript; strict CSP; no caching; no request logging
- all dynamic text is HTML-escaped (see render.py)

Residual risk: any process running as your user can read the token from this
process's memory or the terminal. That's the same exposure as ~/.config/optout.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import webbrowser
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .. import actions
from ..brokers.registry import all_brokers, load_registry, set_enabled
from ..config import load_config
from ..db import get_session, init_db, list_all_events, list_submissions
from ..engine.campaign import run_campaign, select_brokers, sent_today
from ..labels import STATUS_LABEL
from . import render
from .data import collect

COOKIE = "optout_session"
MAX_BODY = 16_384
FLASH = {
    "reply": "Reply recorded.",
    "toggle": "Saved.",
    "sent": "Marked as sent.",
    "drafts": "Checked. New drafts are listed under “Saved drafts”.",
    "started": "Batch started.",
    "err": "That didn't work. Check the details and try again.",
    "busy": "A batch is already running.",
    "nocfg": "No config found. Run optout init first.",
}
_BROKER_MAX = 80


class App:
    def __init__(self, port: int) -> None:
        self.port = port
        self.token = secrets.token_urlsafe(32)
        self.csrf = hmac.new(self.token.encode(), b"csrf", "sha256").hexdigest()
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        self.job: dict = {"state": "idle"}
        self.job_lock = threading.Lock()

    # -- campaign job ----------------------------------------------------
    def start_campaign(self, count: int) -> str:
        with self.job_lock:
            if self.job.get("state") == "running":
                return "busy"
            self.job = {"state": "running", "sent": [], "failed": [], "total": count}
        threading.Thread(target=self._run_campaign, args=(count,), daemon=True).start()
        return "started"

    def _run_campaign(self, count: int) -> None:
        job = self.job
        try:
            config = load_config()
            load_registry()
            now = datetime.now(UTC)
            with get_session() as session:
                sel = select_brokers(all_brokers(), session, now=now)
                batch = sel.eligible[:count]
                job["total"] = len(batch)

                def progress(slug: str, state: str) -> None:
                    (job["sent"] if state == "sent" else job["failed"]).append(
                        slug if state == "sent" else (slug, "see optout status")
                    )

                report = run_campaign(session, config, batch, limit=count, on_progress=progress)
                job["failed"] = report.failed
                job["stopped"] = report.stopped_reason
        except Exception as exc:  # never leak a traceback into the page
            job["stopped"] = f"{type(exc).__name__}"
        finally:
            job["state"] = "done"


def _make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "optout"
        sys_version = ""

        def log_message(self, *args) -> None:  # URLs can carry the token; log nothing
            pass

        # -- plumbing ----------------------------------------------------
        def _send(self, code: int, html: str, headers: dict[str, str] | None = None) -> None:
            body = html.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                "form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, where: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _fail(self, code: int, text: str) -> None:
            self._send(code, render.error_page(code, text))

        def _authed(self) -> bool:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = jar.get(COOKIE)
            if morsel is None:
                return False
            return hmac.compare_digest(morsel.value.encode(), app.token.encode())

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in app.hosts

        # -- GET ---------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._fail(403, "Unexpected host.")
            url = urlsplit(self.path)
            query = parse_qs(url.query)

            if url.path == "/" and "t" in query:
                if hmac.compare_digest(query["t"][0].encode(), app.token.encode()):
                    cookie = f"{COOKIE}={app.token}; Path=/; HttpOnly; SameSite=Strict"
                    return self._redirect("/", {"Set-Cookie": cookie})
                return self._fail(403, "Bad link.")
            if not self._authed():
                return self._fail(403, "Open the link printed by `optout ui` in your terminal.")

            flash = ""
            if (code := query.get("m", [""])[0]) in FLASH:
                flash = render.message_box(
                    FLASH[code], ok=code not in ("err", "busy", "confirm", "nocfg")
                )

            try:
                self._route_get(url.path, query, flash)
            except Exception:
                self._fail(500, "Something went wrong. Check the terminal running optout ui.")

        def _route_get(self, path: str, query: dict, flash: str) -> None:
            init_db()
            load_registry()
            now = datetime.now(UTC)
            if path == "/":
                try:
                    config = load_config()
                except FileNotFoundError:
                    config = None
                status = query.get("status", [None])[0]
                status = status if status in STATUS_LABEL.values() else None
                draft_files = actions.list_drafts()
                ui = render.UI(
                    csrf=app.csrf,
                    drafts=[p.name for p in draft_files],
                    job=dict(app.job),
                    flash=flash,
                    query=query.get("q", [""])[0][:80],
                    configured=config is not None,
                )
                if (want := query.get("draft", [""])[0]) and (
                    match := next((p for p in draft_files if p.name == want), None)
                ):
                    kind = query.get("dk", [""])[0]
                    ui.open_draft = (
                        match.name,
                        match.read_text(),
                        query.get("ds", [""])[0][:_BROKER_MAX],
                        kind if kind in actions.ESCALATION_KINDS else "",
                    )
                with get_session() as session:
                    d = collect(session, config, now, drafts=len(draft_files))
                    if config is not None:
                        sel = select_brokers(all_brokers(), session, now=now)
                        ui.used = sent_today(session, now)
                        ui.limit = config.email.daily_limit
                        room = max(0, ui.limit - ui.used)
                        batch = sel.eligible[: min(room, 40)]
                        ui.previews = [
                            (b.name, b.opt_out_email or "", *_email_for(config, b)) for b in batch
                        ]
                        ui.sender = config.email.from_address or (
                            config.email.smtp.username if config.email.smtp else "your account"
                        )
                return self._send(
                    200, render.dashboard(d, interactive=True, status_filter=status, ui=ui)
                )

            if path == "/campaign":
                try:
                    config = load_config()
                except FileNotFoundError:
                    return self._send(
                        200,
                        render.page(
                            "Send a batch", render.message_box(FLASH["nocfg"], False), nav=True
                        ),
                    )
                with get_session() as session:
                    sel = select_brokers(all_brokers(), session, now=now)
                    used = sent_today(session, now)
                eligible = [(b.slug, b.name) for b in sel.eligible]
                return self._send(
                    200,
                    render.campaign_page(
                        eligible,
                        len(sel.skipped),
                        used,
                        config.email.daily_limit,
                        app.csrf,
                        dict(app.job),
                        flash,
                    ),
                )

            if path == "/preview":
                try:
                    config = load_config()
                except FileNotFoundError:
                    return self._fail(404, FLASH["nocfg"])
                try:
                    want = int(query.get("count", ["40"])[0])
                except ValueError:
                    want = 40
                with get_session() as session:
                    sel = select_brokers(all_brokers(), session, now=now)
                    used = sent_today(session, now)
                room = max(0, config.email.daily_limit - used)
                batch = sel.eligible[: max(0, min(want, room, 40))]
                items = [(b.name, b.opt_out_email or "", *_email_for(config, b)) for b in batch]
                sender = config.email.from_address or (
                    config.email.smtp.username if config.email.smtp else "your account"
                )
                return self._send(200, render.preview_page(items, sender, app.csrf, flash))

            if path == "/drafts":
                names = [p.name for p in actions.list_drafts()]
                return self._send(200, render.drafts_page(names, app.csrf, flash))

            if path.startswith("/draft/"):
                name = unquote(path[len("/draft/") :])
                match = next((p for p in actions.list_drafts() if p.name == name), None)
                if match is None:
                    return self._fail(404, "No such draft.")
                return self._send(200, render.draft_page(match.name, match.read_text()))

            if path.startswith("/broker/"):
                slug = unquote(path[len("/broker/") :])
                if not slug or len(slug) > _BROKER_MAX or any(ord(c) < 32 for c in slug):
                    return self._fail(404, "No such broker.")
                by_slug = {b.slug: b for b in all_brokers()}
                with get_session() as session:
                    subs = list_submissions(session, broker_slug=slug)
                    events = [
                        (
                            ev.created_at.strftime("%Y-%m-%d"),
                            ev.event_type.replace("_", " "),
                            _note(ev.payload_json),
                        )
                        for _sub, ev in list_all_events(session, broker_slug=slug)
                    ]
                if slug not in by_slug and not subs:
                    return self._fail(404, "No such broker.")
                broker = by_slug.get(slug)
                return self._send(
                    200,
                    render.broker_page(
                        slug,
                        broker.name if broker else slug,
                        broker.enabled if broker else True,
                        events,
                        app.csrf,
                        flash,
                    ),
                )
            self._fail(404, "Not found.")

        # -- POST --------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok() or not self._authed():
                return self._fail(403, "Forbidden.")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in app.origins:
                return self._fail(403, "Forbidden.")
            if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                return self._fail(403, "Forbidden.")
            if not self.headers.get("Content-Type", "").startswith(
                "application/x-www-form-urlencoded"
            ):
                return self._fail(415, "Unsupported content type.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._fail(400, "Bad request.")
            if not 0 <= length <= MAX_BODY:
                return self._fail(413, "Too large.")
            form = {
                k: v[0]
                for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()
            }
            if not hmac.compare_digest(form.get("csrf", "").encode(), app.csrf.encode()):
                return self._fail(403, "Forbidden.")
            try:
                self._route_post(urlsplit(self.path).path, form)
            except Exception:
                self._redirect("/?m=err")

        def _row_action(self, slug: str, action: str) -> None:
            kind, _, arg = action.partition(":")
            if kind == "reply":
                with get_session() as session:
                    actions.record_reply(session, slug, arg, None)
                return self._redirect("/?m=reply")
            if kind == "draft":
                out = actions.write_draft_now(load_config(), slug, arg, datetime.now(UTC))
                return self._redirect(
                    f"/?draft={quote(out.name, safe='')}&ds={quote(slug, safe='')}"
                    f"&dk={quote(arg, safe='')}#draft"
                )
            if kind == "sent":
                with get_session() as session:
                    actions.record_escalation_sent(session, slug, arg)
                return self._redirect("/?m=sent")
            if action == "toggle:off":
                if slug not in {b.slug for b in all_brokers()}:
                    raise ValueError("unknown broker")
                set_enabled(slug, False)
                return self._redirect("/?m=toggle")
            raise ValueError("unknown action")

        def _route_post(self, path: str, form: dict[str, str]) -> None:
            init_db()
            load_registry()
            slug = form.get("slug", "")[:_BROKER_MAX]
            back = f"/broker/{quote(slug, safe='')}"
            try:
                if path == "/act/row":
                    return self._row_action(slug, form.get("action", ""))
                if path == "/act/reply":
                    with get_session() as session:
                        actions.record_reply(
                            session, slug, form.get("outcome", ""), form.get("note")
                        )
                    return self._redirect(f"{back}?m=reply")
                if path == "/act/toggle":
                    if slug not in {b.slug for b in all_brokers()}:
                        raise ValueError("unknown broker")
                    set_enabled(slug, form.get("enabled") == "1")
                    return self._redirect(f"{back}?m=toggle")
                if path == "/act/escalation-sent":
                    with get_session() as session:
                        actions.record_escalation_sent(session, slug, form.get("kind", ""))
                    return self._redirect(f"{back}?m=sent")
                if path == "/act/draft":
                    out = actions.write_draft_now(
                        load_config(), slug, form.get("kind", ""), datetime.now(UTC)
                    )
                    return self._redirect(f"/draft/{quote(out.name, safe='')}")
                if path == "/act/make-drafts":
                    actions.write_escalation_drafts(load_config(), datetime.now(UTC))
                    return self._redirect("/?m=drafts")
                if path == "/act/campaign":
                    try:
                        config = load_config()
                    except FileNotFoundError:
                        return self._redirect("/?m=nocfg#send")
                    count = int(form.get("count", "0"))
                    if not 1 <= count <= min(40, config.email.daily_limit):
                        raise ValueError("count out of range")
                    return self._redirect(f"/?m={app.start_campaign(count)}#send")
            except ValueError:
                return self._redirect("/?m=err")
            self._fail(404, "Not found.")

    return Handler


def _email_for(config, broker) -> tuple[str, str]:
    """(subject, body) exactly as the email handler would send for this broker."""
    from ..engine.methods.email import _contact_email, _render_body

    profile = config.profile
    body = _render_body(
        broker,
        profile,
        contact_email=_contact_email(config, broker, profile),
        minimal=bool(broker.source) and config.email.identifiers == "minimal",
    )
    return f"Data Deletion Request — {profile.legal_name}", body


def _note(payload_json: str) -> str:
    import json

    try:
        data = json.loads(payload_json or "{}")
    except ValueError:
        return ""
    return str(data.get("note") or data.get("kind") or data.get("outcome") or "")[:200]


def make_server(port: int = 8765) -> tuple[ThreadingHTTPServer, App]:
    """Bind to 127.0.0.1 (port 0 picks a free one). Tries a few ports if busy."""
    last: OSError | None = None
    for p in [port] if port == 0 else range(port, port + 10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), BaseHTTPRequestHandler)
        except OSError as exc:
            last = exc
            continue
        app = App(httpd.server_address[1])
        httpd.RequestHandlerClass = _make_handler(app)
        return httpd, app
    raise OSError(f"no free port near {port}: {last}")


def _is_wsl() -> bool:
    try:
        return "microsoft" in open("/proc/version").read().lower()
    except OSError:
        return False


def open_in_browser(url: str) -> bool:
    """Open *url* in the user's browser, including from WSL (xdg-open has no browser there)."""
    import shutil
    import subprocess

    devnull = subprocess.DEVNULL
    if _is_wsl():
        if shutil.which("wslview"):
            done = subprocess.run(["wslview", url], stdout=devnull, stderr=devnull)
            return done.returncode == 0
        ps = (
            shutil.which("powershell.exe")
            or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        # The URL is ours (127.0.0.1 + url-safe token); quote it for PowerShell anyway.
        safe = url.replace("'", "''")
        try:
            done = subprocess.run(
                [ps, "-NoProfile", "-Command", f"Start-Process '{safe}'"],
                timeout=20,
                stdout=devnull,
                stderr=devnull,
            )
            return done.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return webbrowser.open(url)


def serve(port: int = 8765, open_browser: bool = True) -> None:
    httpd, app = make_server(port)
    url = f"http://127.0.0.1:{app.port}/?t={app.token}"
    print(
        f"optout UI running. Open this link (it contains a one-time key):\n\n  {url}\n",
        flush=True,
    )
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        open_in_browser(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
