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
from ..brokers.registry import all_brokers, imported_dir, reload_if_changed, set_enabled
from ..config import load_config, user_tz
from ..db import (
    get_session,
    init_db,
    list_all_events,
    list_submissions,
    record_event,
    update_status,
)
from ..engine.campaign import run_campaign, select_brokers, sent_today
from ..engine.methods.email import read_smtp_password
from ..labels import STATUS_LABEL
from . import render
from .data import collect

COOKIE = "optout_session"
MAX_BODY = 65_536
FLASH = {
    "reply": "Reply recorded.",
    "toggle": "Saved.",
    "set_awaiting_broker_confirmation": "Moved to Awaiting reply: a new wait for their answer "
    "starts now.",
    "set_confirmed": "Marked as Broker says done.",
    "set_awaiting_user_input": "Marked as Waiting on you. It's listed under Needs you.",
    "set_rejected": "Marked as Broker said no. You can write an appeal from Needs you.",
    "set_failed": "Marked as Failed to send.",
    "set_closed": "Closed. It counts as done and leaves Needs you.",
    "form_resubmitted": "Recorded: you resubmitted their form. Now waiting for their "
    "confirmation email.",
    "form_removed": "Recorded: their form said your data was removed (Broker says done).",
    "stopped": "Stopped: this broker won't be emailed again (no resends or follow-ups). Its row "
    "shows Stopped; the same menu can undo it.",
    "started_again": "This broker can be emailed again.",
    "sent": "Marked as sent. It leaves Needs you while you wait for their answer.",
    "drafts": "Checked. New drafts are listed under “Saved drafts”.",
    "started": "Sending started. This page updates as each email goes out.",
    "cancelling": "Stopping after the email in progress. Everything sent so far is recorded.",
    "backup_ok": "Backup written.",
    "finished": "Batch finished.",
    "imported": "Broker list loaded.",
    "inboxerr": "Couldn't read your replies. Check your app password and internet connection.",
    "reviewed": "Saved. That reply left the list and its request was updated.",
    "conf_ok": "Confirmed. They'll carry on with your request; it's back to awaiting a reply.",
    "conf_unclear": "I opened the link, but the page didn't clearly say it worked. Check your "
    "inbox, or open the link yourself.",
    "conf_failed": "Couldn't use that confirm link (it may have expired). Open it yourself.",
    "cov_ok": "Coverage saved. Brokers that only cover other groups are skipped once you've "
    "set your groups in Settings.",
    "settings_ok": "Settings saved.",
    "pw_ok": "Signed in and saved your app password.",
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
        self.inbox_lock = threading.Lock()
        self._fetch_pending = False  # a reply check is already queued after a batch
        self.report: tuple[str, bool, list[str]] | None = None  # (title, ok, lines), shown once
        self.last_error = ""  # shown once after a failed settings save (never from the URL)
        self.settings_draft: dict[str, str] | None = None  # the form as typed, after an error
        try:  # a send cut off by closing the app last time must not block that broker
            from ..db import get_session as _gs
            from ..engine.campaign import recover_interrupted

            with _gs() as session:
                recover_interrupted(session, datetime.now(UTC))
        except Exception:
            _log_error("recovering interrupted campaigns")

    def schedule_forever(self, every: float = 60, first: float = 20) -> None:
        """Daily sending and backups, whenever they're due. Checked shortly after start and
        then every minute, so a missed day (the app wasn't running) is caught up as soon as
        you open it."""
        import time

        from .. import backup
        from ..config import user_tz
        from ..engine.campaign import auto_send_due, last_auto_run, mark_auto_run

        time.sleep(first)
        while True:
            try:
                config = load_config()
                now_local = datetime.now(UTC).astimezone(user_tz(config))
                if config.email.allow_unattended and auto_send_due(
                    now_local, config.email.daily_send_time, last_auto_run()
                ):
                    mark_auto_run(now_local.date())  # once per day, even if nothing is eligible
                    if self.job.get("state") != "running":
                        self.start_daily(config.email.daily_limit)
                    actions.write_escalation_drafts(config, datetime.now(UTC))
                if backup.due(config, now_local.date()):
                    backup.run(config)
            except Exception:
                _log_error("scheduled work")
            time.sleep(every)

    def fetch_after_batch(self, delay: float = 30) -> None:
        """Read replies in shortly after a batch: auto-replies arrive within seconds to
        minutes, so they're sorted without a click. Runs in the background after the job is
        marked done, never overlaps another reply check, and is skipped if Gmail isn't set
        up or you've turned it off."""
        from ..engine.inbox import gmail_ready

        try:
            config = load_config()
            if not config.email.fetch_after_batch or gmail_ready(config) is not None:
                return
        except Exception:
            return
        with self.job_lock:
            if self._fetch_pending:
                return
            self._fetch_pending = True
        threading.Thread(target=self._fetch_later, args=(delay,), daemon=True).start()

    def _fetch_later(self, delay: float) -> None:
        import time

        from ..engine.inbox import run_sync

        try:
            time.sleep(delay)
            if self.inbox_lock.acquire(blocking=False):
                try:
                    run_sync(load_config())
                finally:
                    self.inbox_lock.release()
        except Exception:
            _log_error("reading replies after a batch")
        finally:
            self._fetch_pending = False

    def start_daily(self, count: int) -> str:
        """The day's batch, then any follow-ups that are due, as one background job."""
        with self.job_lock:
            if self.job.get("state") == "running":
                return "busy"
            self.job = {"state": "running", "sent": [], "failed": [], "total": count}

        def both() -> None:
            job = self.job
            self._run_campaign(count, fetch=False)
            if not job.get("cancel"):
                # Same job, so the progress bar covers both; follow-ups add to the counts.
                total, failed = job.get("total", 0), list(job.get("failed", []))
                job["state"] = "running"
                self._run_followups(fetch=False)
                job["total"] = total + job.get("total", 0)
                job["failed"] = failed + list(job.get("failed", []))
            self.fetch_after_batch()  # once, after both

        threading.Thread(target=both, daemon=True).start()
        return "started"

    def poll_inbox_forever(self, every: float = 900, first: float = 5) -> None:
        """Check for replies shortly after start, then every 15 minutes."""
        import time

        from ..engine.inbox import gmail_ready, run_sync

        time.sleep(first)
        while True:
            try:
                config = load_config()
                if gmail_ready(config) is None and self.inbox_lock.acquire(blocking=False):
                    try:
                        run_sync(config)
                    finally:
                        self.inbox_lock.release()
            except Exception:
                pass  # recorded by run_sync; the dashboard shows the last attempt
            time.sleep(self._next_check_delay(every))

    def _next_check_delay(self, every: float) -> float:
        """Sooner while a web form is waiting on its confirmation email."""
        try:
            from ..db import get_session
            from ..forms import waiting_for_email

            with get_session() as session:
                if waiting_for_email(session):
                    return min(every, load_config().email.form_check_seconds)
        except Exception:
            pass
        return every

    # -- campaign job ----------------------------------------------------
    def start_campaign(
        self, count: int, only: str | None = None, force: bool = False, tier: str | None = None
    ) -> str:
        """Send up to *count* emails in the background; *only* limits it to one broker
        (used to resend a failed one). *force* sends to that broker even if it refused, was
        closed, or was emailed recently: only ever from an explicit "Send again"."""
        with self.job_lock:
            if self.job.get("state") == "running":
                return "busy"
            self.job = {"state": "running", "sent": [], "failed": [], "total": count}
        threading.Thread(
            target=self._run_campaign, args=(count, only, force, tier), daemon=True
        ).start()
        return "started"

    def start_resend(self, slug: str, address: str) -> str:
        with self.job_lock:
            if self.job.get("state") == "running":
                return "busy"
            self.job = {"state": "running", "sent": [], "failed": [], "total": 1}
        threading.Thread(target=self._run_resend, args=(slug, address), daemon=True).start()
        return "started"

    def start_followups(self) -> str:
        with self.job_lock:
            if self.job.get("state") == "running":
                return "busy"
            self.job = {"state": "running", "sent": [], "failed": [], "total": 0}
        threading.Thread(target=self._run_followups, daemon=True).start()
        return "started"

    def _run_followups(self, fetch: bool = True) -> None:
        from ..engine import followups

        job = self.job
        try:
            config = load_config()
            reload_if_changed()
            with get_session() as session:
                items = followups.due(
                    session, {b.slug: b for b in all_brokers()}, datetime.now(UTC)
                )
                job["total"] = len(items)

                def progress(slug: str, state: str) -> None:
                    (job["sent"] if state == "sent" else job["failed"]).append(
                        slug
                        if state == "sent"
                        else (slug, "couldn't send; see its row in Requests")
                    )

                report = followups.send(
                    session,
                    config,
                    items,
                    limit=len(items),
                    on_progress=progress,
                    should_stop=lambda: bool(job.get("cancel")),
                )
                job["failed"] = report.failed
                job["stopped"] = report.stopped_reason
        except Exception as exc:  # never leak a traceback into the page
            job["stopped"] = f"{type(exc).__name__}"
        finally:
            job["state"] = "done"
            if fetch:
                self.fetch_after_batch()

    def _run_resend(self, slug: str, address: str) -> None:
        from ..engine.campaign import resend_to

        job = self.job
        try:
            config = load_config()
            reload_if_changed()
            broker = next(b for b in all_brokers() if b.slug == slug)
            with get_session() as session:
                report = resend_to(session, config, broker, address)
            job["sent"], job["failed"], job["stopped"] = (
                report.sent,
                report.failed,
                report.stopped_reason,
            )
        except Exception as exc:  # never leak a traceback into the page
            job["stopped"] = f"{type(exc).__name__}"
        finally:
            job["state"] = "done"
            self.fetch_after_batch()

    def _run_campaign(
        self,
        count: int,
        only: str | None = None,
        force: bool = False,
        tier: str | None = None,
        fetch: bool = True,
    ) -> None:
        job = self.job
        try:
            config = load_config()
            reload_if_changed()
            now = datetime.now(UTC)
            with get_session() as session:
                sel = select_brokers(
                    all_brokers(),
                    session,
                    now=now,
                    only=[only] if only else None,
                    my_groups=config.my_groups,
                    resend_after_days=config.email.resend_after_days,
                    force=force and only is not None,
                )
                if tier:
                    from ..plan import tier as tier_of

                    sel.eligible = [b for b in sel.eligible if tier_of(b) == tier]
                batch = sel.eligible[:count]
                job["total"] = len(batch)

                def progress(slug: str, state: str) -> None:
                    (job["sent"] if state == "sent" else job["failed"]).append(
                        slug
                        if state == "sent"
                        else (slug, "couldn't send; see its row in Requests")
                    )

                report = run_campaign(
                    session,
                    config,
                    batch,
                    limit=count,
                    on_progress=progress,
                    should_stop=lambda: bool(job.get("cancel")),
                )
                job["failed"] = report.failed
                job["stopped"] = report.stopped_reason
        except Exception as exc:  # never leak a traceback into the page
            job["stopped"] = f"{type(exc).__name__}"
        finally:
            job["state"] = "done"
            if fetch:
                self.fetch_after_batch()


# A browser that closes the connection early (navigating away, a cancelled fetch) is
# normal, not an error.
_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def _log_error(where: str) -> None:
    """One short traceback in the terminal for a real bug (never the request URL: it can
    carry the session token)."""
    import sys
    import traceback

    print(f"optout: error handling {where}:", file=sys.stderr)
    traceback.print_exc(limit=4, file=sys.stderr)


def _make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "optout"
        sys_version = ""

        def log_message(self, *args) -> None:  # URLs can carry the token; log nothing
            pass

        def handle(self) -> None:
            try:
                super().handle()
            except _GONE:
                pass  # connection closed by the browser mid-response

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
                f"script-src '{render.LIVE_JS_HASH}'; connect-src 'self'; "
                "form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, data: dict) -> None:
            import json

            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
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
            nums = {k: _int(query.get(k, ["0"])[0]) for k in ("a", "r", "s")}
            code = query.get("m", [""])[0]
            if code == "inbox":
                flash = render.message_box(
                    f"Checked replies: {nums['a']} applied, {nums['r']} to review."
                )
            elif code == "claude":
                flash = render.message_box(
                    f"Applied {nums['a']} of Claude's answers"
                    + (f", skipped {nums['s']}." if nums["s"] else "."),
                    ok=nums["a"] > 0,
                )
            elif code == "report" and app.report:
                title, ok_, rep_lines = app.report
                app.report = None
                flash = render.report_box(title, rep_lines, ok_)
            elif code in ("forms_ok", "forms_bad"):
                flash = render.message_box(
                    ("Applied: " if code == "forms_ok" else "Couldn't apply: ")
                    + (app.last_error or "nothing to apply"),
                    ok=code == "forms_ok",
                )
            elif code in ("settings_err", "pw_bad", "backup_bad"):
                what = app.last_error or "something didn't validate"
                lead = {
                    "settings_err": "Couldn't save: ",
                    "pw_bad": "That password didn't work: ",
                    "backup_bad": "Couldn't back up: ",
                }[code]
                flash = render.message_box(lead + what, ok=False)
            if code in FLASH:
                text = FLASH[code]
                if code.startswith("conf_") and (said := query.get("n", [""])[0][:200]):
                    text += f" The page said: “{said}”"
                bad = code in ("err", "busy", "nocfg", "inboxerr", "conf_unclear", "conf_failed")
                if code == "drafts":
                    a = nums["a"]
                    text = (
                        f"Wrote {a} draft{'s' if a != 1 else ''}. They're listed under "
                        "Saved drafts; nothing has been sent."
                        if a
                        else "No drafts needed: nothing is overdue or refused right now."
                    )
                if code == "finished":
                    sent, failed = len(app.job.get("sent", [])), len(app.job.get("failed", []))
                    text = f"Batch finished: {sent} sent" + (
                        f", {failed} couldn't be sent. See below." if failed else "."
                    )
                    bad = failed > 0 and sent == 0
                flash = render.message_box(
                    text,
                    ok=not bad,
                    live=code == "started" and app.job.get("state") == "running",
                )

            try:
                self._route_get(url.path, query, flash)
            except _GONE:
                return  # the browser stopped listening (navigated away, cancelled): fine
            except Exception:
                _log_error(f"GET {url.path}")
                try:
                    self._fail(500, "Something went wrong. Check the terminal running optout.")
                except _GONE:
                    pass

        def _route_get(self, path: str, query: dict, flash: str) -> None:
            init_db()
            reload_if_changed()
            now = datetime.now(UTC)
            if path == "/api/job":
                job = dict(app.job)
                return self._send_json(
                    {
                        "state": job.get("state", "idle"),
                        "sent": len(job.get("sent", [])),
                        "failed": len(job.get("failed", [])),
                        "total": job.get("total", 0),
                    }
                )

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
                    needs_import=not any(imported_dir().glob("*.yml")),
                    password_missing=config is not None
                    and config.email.smtp is not None
                    and not read_smtp_password(config.email.smtp),
                )
                if (want := query.get("draft", [""])[0]) and (
                    match := next((p for p in draft_files if p.name == want), None)
                ):
                    file_slug, file_kind = actions.draft_info(match.name)
                    kind = query.get("dk", [""])[0] or file_kind
                    ui.open_draft = (
                        match.name,
                        match.read_text(),
                        query.get("ds", [""])[0][:_BROKER_MAX] or file_slug,
                        kind if kind in actions.ESCALATION_KINDS else "",
                    )
                ui.draft_titles = {
                    p.name: actions.draft_title(p.name, {b.slug: b.name for b in all_brokers()})
                    for p in draft_files
                }
                with get_session() as session:
                    d = collect(session, config, now, drafts=len(draft_files))
                    if config is not None:
                        _fill_replies(ui, session, config)
                if config is not None:
                    ui.settings = _settings_values(config)
                    if query.get("m", [""])[0] == "settings_err" and app.settings_draft:
                        ui.settings.update(app.settings_draft)  # what you typed, to fix
                    app.settings_draft = None
                    ui.settings_open = bool(query.get("settings")) or code_opens_settings(query)
                    if config is not None:
                        sel = select_brokers(
                            all_brokers(),
                            session,
                            now=now,
                            my_groups=config.my_groups,
                            resend_after_days=config.email.resend_after_days,
                        )
                        ui.used = sent_today(session, now, user_tz(config))
                        ui.limit = config.email.daily_limit
                        room = max(0, ui.limit - ui.used)
                        batch = sel.eligible[:room]  # a batch can use the whole day's room
                        ui.waiting = len(sel.eligible)
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
                    sel = select_brokers(
                        all_brokers(),
                        session,
                        now=now,
                        my_groups=config.my_groups,
                        resend_after_days=config.email.resend_after_days,
                    )
                    used = sent_today(session, now, user_tz(config))
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
                    sel = select_brokers(
                        all_brokers(),
                        session,
                        now=now,
                        my_groups=config.my_groups,
                        resend_after_days=config.email.resend_after_days,
                    )
                    used = sent_today(session, now, user_tz(config))
                room = max(0, config.email.daily_limit - used)
                batch = sel.eligible[: max(0, min(want, room))]
                items = [(b.name, b.opt_out_email or "", *_email_for(config, b)) for b in batch]
                sender = config.email.from_address or (
                    config.email.smtp.username if config.email.smtp else "your account"
                )
                return self._send(200, render.preview_page(items, sender, app.csrf, flash))

            if path == "/drafts":
                names = [p.name for p in actions.list_drafts()]
                return self._send(200, render.drafts_page(names, app.csrf, flash))

            if path.startswith("/coverage-prompt/"):
                from sqlmodel import select

                from ..audiences import coverage_prompt
                from ..db import InboxMessage
                from ..engine.assist import redact

                config = load_config()
                slug = unquote(path[len("/coverage-prompt/") :])
                broker = next((b for b in all_brokers() if b.slug == slug), None)
                if broker is None or not config.contributor_tools:
                    return self._fail(404, "No such broker.")
                with get_session() as session:
                    rows = session.exec(
                        select(InboxMessage)
                        .where(InboxMessage.broker_slug == slug, InboxMessage.state != "ignored")
                        .order_by(InboxMessage.received_at.desc())  # type: ignore[union-attr]
                    ).all()
                row = next((r for r in rows if r.kind in ("need-info", "denied")), None) or (
                    rows[0] if rows else None
                )
                text = (
                    coverage_prompt(slug, broker.name, redact(row.text, config.profile))
                    if row
                    else ""
                )
                return self._send_json(
                    {"text": text, "count": 1, "msg": "Copied. Paste into Claude"}
                )

            if path.startswith("/prompt/"):
                from .. import forms
                from ..plan import TIERS

                tier = unquote(path[len("/prompt/") :])
                titles = {k: t for k, t, _ in TIERS}
                if tier not in ("people-search", "form", "form-plus"):
                    return self._fail(404, "No such group.")
                config = load_config()
                with get_session() as session:
                    queue = forms.todo(session, config, tier=tier)[:10]
                text = forms.build_prompt(config, queue) if queue else ""
                if query.get("format", [""])[0] == "json":  # the dashboard's one-click copy
                    return self._send_json({"text": text, "count": len(queue)})
                return self._send(200, render.prompt_page(titles[tier], text, len(queue), app.csrf))

            if path.startswith("/draft/"):
                name = unquote(path[len("/draft/") :])
                match = next((p for p in actions.list_drafts() if p.name == name), None)
                if match is None:
                    return self._fail(404, "No such draft.")
                title = actions.draft_title(match.name, {b.slug: b.name for b in all_brokers()})
                return self._send(200, render.draft_page(title, match.read_text()))

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
            except _GONE:
                return
            except Exception:
                _log_error(f"POST {urlsplit(self.path).path}")
                try:
                    self._redirect("/?m=err")
                except _GONE:
                    pass

        def _row_action(self, slug: str, action: str) -> None:
            kind, _, arg = action.partition(":")
            if kind == "reply":
                with get_session() as session:
                    actions.record_reply(session, slug, arg, None)
                return self._redirect("/?m=reply")
            if kind == "send-again":
                # By hand only: email a broker that refused, was closed, or was emailed recently.
                broker = next((b for b in all_brokers() if b.slug == slug), None)
                if broker is None or broker.method != "email" or not broker.opt_out_email:
                    raise ValueError("this broker isn't contacted by email")
                return self._redirect(f"/?m={app.start_campaign(1, only=slug, force=True)}#send")
            if kind == "form-submitted":
                from ..forms import mark_outcome

                with get_session() as session:
                    mark_outcome(session, slug, "submitted", "Resubmitted.", expects_email=True)
                return self._redirect("/?m=form_resubmitted")
            if kind == "form-removed":
                # Their web form said the data was removed: record it as their word.
                with get_session() as session:
                    actions.record_reply(session, slug, "deleted", "Their web form said removed.")
                return self._redirect("/?m=form_removed")
            if kind == "draft":
                arg = actions.auto_draft_kind(slug) if arg == "auto" else arg
                out = actions.write_draft_now(load_config(), slug, arg, datetime.now(UTC))
                return self._redirect(
                    f"/?draft={quote(out.name, safe='')}&ds={quote(slug, safe='')}"
                    f"&dk={quote(arg, safe='')}#draft"
                )
            if kind == "sent":
                with get_session() as session:
                    actions.record_escalation_sent(session, slug, arg)
                actions.file_sent_drafts(slug, arg)
                return self._redirect("/?m=sent")
            if kind == "status":
                # A person correcting a status. Never "removed": that needs a re-search.
                allowed = {
                    "awaiting_broker_confirmation",
                    "confirmed",
                    "awaiting_user_input",
                    "rejected",
                    "failed",
                    "closed",
                }
                if arg not in allowed:
                    raise ValueError("status not allowed")
                as_reply = {
                    "confirmed": "deleted",
                    "rejected": "denied",
                    "awaiting_user_input": "need-info",
                }
                with get_session() as session:
                    subs = list_submissions(session, broker_slug=slug)
                    if not subs:
                        raise ValueError("no request")
                    sub = max(subs, key=lambda x: x.created_at)
                    before = sub.status
                    if arg in as_reply:
                        actions.record_reply(session, slug, as_reply[arg], None)
                    else:
                        update_status(session, sub, arg)
                    record_event(session, sub, "status_set", {"from": before, "to": arg})
                return self._redirect(f"/?m=set_{arg}")
            if action in ("toggle:off", "toggle:on"):
                if slug not in {b.slug for b in all_brokers()}:
                    raise ValueError("unknown broker")
                set_enabled(slug, action == "toggle:on")
                return self._redirect(
                    "/?m=" + ("started_again" if action == "toggle:on" else "stopped")
                )
            raise ValueError("unknown action")

        def _route_post(self, path: str, form: dict[str, str]) -> None:
            init_db()
            reload_if_changed()
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
                if path == "/act/inbox":
                    from ..engine.inbox import run_sync

                    try:
                        with app.inbox_lock:
                            rep_ = run_sync(load_config())
                    except ValueError:
                        return self._redirect("/?m=inboxerr#replies")
                    return self._redirect(
                        f"/?m=inbox&a={len(rep_.applied)}&r={rep_.review}#replies"
                    )
                if path == "/act/review":
                    import uuid as _uuid

                    from ..db import InboxMessage
                    from ..engine.assist import resolve

                    with get_session() as session:
                        row = session.get(InboxMessage, _uuid.UUID(form.get("id", "")))
                        if row is None or row.state != "review":
                            raise ValueError("unknown reply")
                        resolve(
                            session,
                            row,
                            form.get("outcome", ""),
                            "",
                            "user_reported",
                            broker=form.get("broker") or None,
                        )
                    return self._redirect("/?m=reviewed#replies")
                if path == "/act/claude-apply":
                    from sqlmodel import select

                    from ..db import Submission
                    from ..engine.assist import parse_answer, resolve, review_items

                    applied = skipped = 0
                    lines: list[str] = []
                    names = {b.slug: b.name for b in all_brokers()}
                    with get_session() as session:
                        refs = {it.ref: it for it in review_items(session)}
                        slugs = {
                            s.broker_slug
                            for s in session.exec(select(Submission))
                            if s.submitted_at
                        }
                        verdicts, problems = parse_answer(form.get("answer", ""), set(refs), slugs)
                        skipped = len(problems)
                        for v in verdicts:
                            row = refs[v.ref].row
                            who = names.get(v.broker or row.broker_slug or "", v.ref)
                            try:
                                resolve(session, row, v.outcome, v.note, "claude", broker=v.broker)
                                applied += 1
                                said = _OUTCOME_WORDS.get(v.outcome, v.outcome)
                                lines.append(f"✓ {who}: {said}")
                            except ValueError as exc:
                                skipped += 1
                                lines.append(f"✗ {who}: {exc}")
                    lines += [f"✗ {p}" for p in problems]
                    if not verdicts and not problems:
                        lines.append("✗ Nothing in the pasted text looked like an answer.")
                    if not verdicts:
                        title = (
                            "Couldn't read Claude's answer. Paste the whole JSON reply it gave, "
                            "including the [ ] brackets."
                        )
                    else:
                        title = (
                            f"Applied {applied} of Claude's answers"
                            + (f"; {skipped} couldn't be applied" if skipped else "")
                            + ". Applied ones left the review list."
                        )
                    app.report = (title, applied > 0, lines)
                    return self._redirect("/?m=report")
                if path == "/act/settings":
                    from ..config import save_config
                    from ..wizard import _CELEBRITY_NAMES
                    from .settings import apply_form

                    try:
                        new = apply_form(load_config(), form)
                        if new.profile.legal_name.strip().lower() in _CELEBRITY_NAMES:
                            raise ValueError("name (this tool is only for your own name)")
                    except ValueError as exc:
                        app.last_error = str(exc)[:120]
                        app.settings_draft = _unsaved(form)  # shown again, not thrown away
                        return self._redirect("/?m=settings_err#settings")
                    app.settings_draft = None
                    if form.get("backup_key", "").strip():
                        from ..backup import save_key

                        try:
                            save_key(form["backup_key"])
                        except ValueError as exc:
                            app.last_error = str(exc)[:120]
                            return self._redirect("/?m=settings_err#settings")
                    save_config(new)
                    return self._redirect("/?m=settings_ok#settings")
                if path == "/act/backup":
                    from .. import backup

                    try:
                        backup.run(load_config())
                    except ValueError as exc:
                        app.last_error = str(exc)[:120]
                        return self._redirect("/?m=backup_bad#settings")
                    return self._redirect("/?m=backup_ok#settings")
                if path == "/act/password":
                    from ..engine.methods.email import check_login, save_smtp_password

                    cfg = load_config()
                    pw = form.get("password", "").strip()
                    if cfg.email.smtp is None or not pw:
                        app.last_error = "no password given"
                        return self._redirect("/?m=pw_bad#settings")
                    problem = check_login(cfg.email.smtp, pw)
                    if problem:
                        app.last_error = problem
                        return self._redirect("/?m=pw_bad#settings")
                    save_smtp_password(pw)
                    return self._redirect("/?m=pw_ok#settings")
                if path == "/act/coverage":
                    from ..audiences import GROUPS, save_local

                    picked = [k for k in (*GROUPS, "everyone") if form.get(f"aud_{k}") == "on"]
                    save_local(slug, picked, form.get("note", ""))
                    return self._redirect("/?m=cov_ok")
                if path == "/act/coverage-claude":
                    from ..audiences import parse_coverage_answer, save_local

                    groups, note = parse_coverage_answer(form.get("answer", ""), slug)
                    save_local(slug, groups, note)
                    return self._redirect("/?m=cov_ok")
                if path == "/act/resend-to":
                    from sqlmodel import select

                    from ..db import InboxMessage
                    from .data import alternate_addresses

                    address = form.get("address", "").strip().lower()
                    broker = next((b for b in all_brokers() if b.slug == slug), None)
                    if broker is None:
                        raise ValueError("unknown broker")
                    # Only an address that the broker's own stored reply names, on its own
                    # domain. The form value is never trusted by itself.
                    from .data import sent_to

                    with get_session() as session:
                        replies = session.exec(
                            select(InboxMessage).where(InboxMessage.broker_slug == slug)
                        ).all()
                        subs = list_submissions(session, broker_slug=slug)
                    went_to = (
                        sent_to(max(subs, key=lambda x: x.created_at), lambda s_: broker)
                        if subs
                        else ""
                    )
                    allowed = {
                        a
                        for r in replies
                        for a in alternate_addresses(r.text, r.from_addr, went_to)
                    }
                    if address not in allowed:
                        raise ValueError("that address isn't in their reply")
                    return self._redirect(f"/?m={app.start_resend(slug, address)}#send")
                if path == "/act/confirm":
                    from ..engine import confirm as cf
                    from ..engine.assist import _deciding_sentence

                    config = load_config()
                    remember = form.get("remember") == "1"
                    with get_session() as session:
                        rows = cf.confirmable_rows(session, slug)
                        flows = cf.load_flows()
                        # The link is re-derived here from the stored reply; nothing from the
                        # form is ever used as an address.
                        row = next(
                            (
                                r
                                for r in rows
                                if cf.flow_for(r.from_addr, r.text, flows)
                                or (remember and cf.propose_flow(r.from_addr, r.text))
                            ),
                            None,
                        )
                        if row is None:
                            raise ValueError("nothing to confirm")
                        if cf.flow_for(row.from_addr, row.text, flows) is None:
                            sender, link_host, _url = cf.propose_flow(row.from_addr, row.text)  # type: ignore[misc]
                            phrase = _deciding_sentence(row.text, "need-info", config.profile)
                            if len(phrase) < 12:
                                phrase = cf.teach_phrase(row.text)
                            cf.save_local_flow(
                                sender, phrase, link_host, "Taught from a reply you confirmed."
                            )
                            from ..forms import attach_followup

                            attach_followup(slug, sender, phrase, link_host, config)
                            flows = cf.load_flows()
                        page_said: list[str] = []
                        result, _host = cf.confirm_reply(
                            session, row, flows=flows, fetch=cf.default_fetch, said=page_said
                        )
                    code = {
                        "confirmed": "conf_ok",
                        "already": "conf_ok",
                        "unclear": "conf_unclear",
                        "failed": "conf_failed",
                    }.get(result)
                    if code is None:
                        raise ValueError(result)
                    seen = f"&n={quote(page_said[0][:160], safe='')}" if page_said else ""
                    return self._redirect(f"/?m={code}{seen}")
                if path == "/act/retry":
                    broker = next((b for b in all_brokers() if b.slug == slug), None)
                    if broker is None or broker.method != "email":
                        raise ValueError("can't resend this one")
                    with get_session() as session:
                        subs = list_submissions(session, broker_slug=slug)
                    if not any(s.status == "failed" and s.submitted_at is None for s in subs):
                        raise ValueError("nothing failed to send")
                    return self._redirect(f"/?m={app.start_campaign(1, only=slug)}#send")
                if path == "/act/import":
                    from ..brokers.importer import import_ca_registry

                    import_ca_registry()
                    return self._redirect("/?m=imported")
                if path == "/act/make-drafts":
                    n = len(actions.write_escalation_drafts(load_config(), datetime.now(UTC)))
                    return self._redirect(f"/?m=drafts&a={n}")
                if path == "/act/cancel":
                    # Stops after the email in progress; everything sent so far is recorded.
                    if app.job.get("state") == "running":
                        app.job["cancel"] = True
                    return self._redirect("/?m=cancelling#send")
                if path == "/act/followups":
                    return self._redirect(f"/?m={app.start_followups()}#send")
                if path == "/act/campaign":
                    try:
                        config = load_config()
                    except FileNotFoundError:
                        return self._redirect("/?m=nocfg#send")
                    count = int(form.get("count", "0"))
                    if not 1 <= count <= config.email.daily_limit:
                        raise ValueError("count out of range")
                    tier = form.get("tier") or None
                    if tier not in (None, "email", "email-confirm"):
                        raise ValueError("not an email group")
                    return self._redirect(f"/?m={app.start_campaign(count, tier=tier)}#send")
                if path == "/act/forms-apply":
                    import json as _json
                    import re as _re

                    from .. import forms

                    raw = form.get("answer", "")
                    fenced = _re.search(r"```(?:json)?\s*(.*?)```", raw, _re.S)
                    try:
                        results = _json.loads(fenced.group(1) if fenced else raw)
                        if not isinstance(results, list):
                            raise ValueError("expected a list")
                    except ValueError:
                        app.last_error = "that isn't the JSON answer the prompt asks for"
                        return self._redirect("/?m=forms_bad")
                    with get_session() as session:
                        lines = forms.apply_results(
                            session, load_config(), [r for r in results if isinstance(r, dict)]
                        )
                    done = sum(1 for ln in lines if ": left alone" not in ln and ": " in ln)
                    title = (
                        f"Browser agent's answer: {done} recorded, {len(lines) - done} left alone"
                    )
                    app.report = (title, done > 0, [f"• {ln}" for ln in lines])
                    return self._redirect("/?m=report")
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


def _fill_replies(ui, session, config) -> None:
    from sqlmodel import select

    from ..db import Submission
    from ..engine import inbox
    from ..engine.assist import (
        build_prompt_report,
        describe_redactions,
        learned_entries,
        review_items,
    )

    problem = inbox.gmail_ready(config)
    ui.inbox_ok = problem is None
    if problem:
        ui.inbox_status = f"Not reading replies: {problem}."
    else:
        err = inbox.last_error()
        when = inbox.last_checked()
        if err:
            first = f"The last check for replies failed ({_ago(when)})."
        elif when is None:
            first = "Haven't checked for replies yet."
        else:
            first = f"Last checked for replies {_ago(when)}."
        ui.inbox_status = (
            first + " Checks again by itself every 15 minutes while optout is running."
        )
    names = {b.slug: b.name for b in all_brokers()}
    contacted = {
        s.broker_slug: names.get(s.broker_slug, s.broker_slug)
        for s in session.exec(select(Submission))
        if s.submitted_at
    }
    ui.contacted = sorted(contacted.items(), key=lambda kv: kv[1].lower())
    items = review_items(session)
    from .data import gmail_url

    ui.replies = [
        (
            str(it.row.id),
            it.ref,
            names.get(it.row.broker_slug, it.row.broker_slug) if it.row.broker_slug else None,
            it.row.from_addr,
            it.row.subject,
            it.row.received_at.strftime("%b %d") if it.row.received_at else "",
            it.row.kind or "unknown",
            it.row.text,
            gmail_url(it.row.message_id),
        )
        for it in items
    ]
    if items:
        ui.claude_prompt, counts = build_prompt_report(items, config.profile, contacted)
        ui.redactions = describe_redactions(counts)
    learned = learned_entries(session, config.profile)
    from ..engine.assist import learned_coverage

    coverage = learned_coverage()
    from ..brokers.contacts import learned_contacts

    contacts = learned_contacts()
    from ..brokers.traits import learned_traits

    traits = learned_traits()
    if learned or coverage or contacts or traits:
        import yaml

        parts = []
        if learned:
            parts.append(
                "# add to data/known_replies.yml\n"
                + yaml.safe_dump(learned, sort_keys=False, allow_unicode=True)
            )
        if coverage:
            parts.append(
                "# add to data/broker_audiences.yml\n"
                + yaml.safe_dump(coverage, sort_keys=False, allow_unicode=True)
            )
        if contacts:
            parts.append(
                "# add to data/contact_overrides.yml\n"
                + yaml.safe_dump(contacts, sort_keys=False, allow_unicode=True)
            )
        if traits:
            parts.append(
                "# add to data/broker_traits.yml\n"
                + yaml.safe_dump(traits, sort_keys=False, allow_unicode=True)
            )
        ui.learned_yaml = "\n".join(parts)
        ui.learned_count = len(learned) + len(coverage) + len(contacts) + len(traits)


def code_opens_settings(query: dict) -> bool:
    return query.get("m", [""])[0] in (
        "settings_ok",
        "settings_err",
        "pw_ok",
        "pw_bad",
        "backup_ok",
        "backup_bad",
    )


def _backup_status(config) -> str:
    from .. import backup

    last = backup.last_backup()
    if why := backup.problem(config):
        return f"Off: {why}."
    if last is None:
        return "Set up; the first backup is made within a minute."
    return f"Last backup {last[0].isoformat()}: {last[1]}"


def _settings_values(config) -> dict[str, str]:
    from .settings import previous_text

    p, em, a = config.profile, config.email, config.profile.current_address
    return {
        "legal_name": p.legal_name,
        "aliases": ", ".join(p.aliases),
        "dob": p.dob.isoformat(),
        "street": a.street,
        "street2": a.street2,
        "city": a.city,
        "state": a.state,
        "zip": a.zip,
        "phones": ", ".join(p.phones.current),
        "relatives": ", ".join(p.relatives),
        "previous": previous_text(config),
        "daily_limit": str(em.daily_limit),
        "resend_after_days": str(em.resend_after_days),
        "send_delay_seconds": str(em.send_delay_seconds),
        "is_gmail": "1" if _uses_gmail(config) else "",
        "gmail_label": em.gmail_label or "",
        "reply_label": em.reply_label or "",
        "from_address": em.from_address or "",
        "alias_pattern": em.alias_pattern or "",
        "allow_unattended": "on" if em.allow_unattended else "",
        "mark_replies_read": "on" if em.mark_replies_read else "",
        "fetch_after_batch": "on" if em.fetch_after_batch else "",
        "daily_send_time": em.daily_send_time,
        "my_groups": ",".join(config.my_groups) if config.my_groups is not None else "",
        "groups_set": "1" if config.my_groups is not None else "",
        "contributor_tools": "on" if config.contributor_tools else "",
        "confirm_links": config.confirm_links,
        "backup_dir": config.backup_dir or "",
        "backup_every_days": str(config.backup_every_days),
        "backup_status": _backup_status(config),
        "timezone": config.timezone or "",
        "sender": em.smtp.username if em.smtp else "",
        "host": em.smtp.host if em.smtp else "",
    }


_OUTCOME_WORDS = {
    "deleted": "they deleted it or have no data",
    "no-data": "they have no data on you",
    "denied": "they said no",
    "need-info": "they want you to do something",
    "auto-reply": "only an automated reply",
    "close": "closed: doesn't apply to you",
    "ignore": "not about your request",
}

_CHECKBOXES = ("allow_unattended", "mark_replies_read", "fetch_after_batch", "contributor_tools")
_SECRET_FIELDS = {"csrf", "backup_key", "password"}


def _unsaved(form: dict[str, str]) -> dict[str, str]:
    """The settings form as typed, to refill the page after a failed save (never secrets)."""
    out = {k: v for k, v in form.items() if k not in _SECRET_FIELDS and not k.startswith("group_")}
    for box in _CHECKBOXES:
        out[box] = "on" if form.get(box) == "on" else ""
    if form.get("groups_present") == "1":
        out["my_groups"] = ",".join(
            k.removeprefix("group_")
            for k, v in form.items()
            if k.startswith("group_") and v == "on"
        )
        out["groups_set"] = "1"
    return out


def _int(text: str) -> int:
    try:
        return max(0, min(int(text), 100_000))
    except ValueError:
        return 0


def _uses_gmail(config) -> bool:
    from ..engine.inbox import uses_gmail

    return uses_gmail(config)


def _ago(when) -> str:
    if when is None:
        return "never"
    mins = int((datetime.now(UTC) - when).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    return f"{mins // 60} h ago" if mins < 1440 else f"{mins // 1440} d ago"


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
    threading.Thread(target=app.poll_inbox_forever, daemon=True).start()
    threading.Thread(target=app.schedule_forever, daemon=True).start()
    if open_browser:
        open_in_browser(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
