"""The command-line commands, run end to end against a throwaway config and database.

Nothing here sends mail, opens a browser or touches the network: the commands that would
are run in preview mode, or with the one function that reaches out replaced.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from optout.cli import app
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

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures" / "brokers"
EMAIL = "test-email-broker"
WEB = "test-web-broker"


def _config(**email) -> UserConfig:
    return UserConfig(
        profile=Profile(
            legal_name="Jane Q Public",
            dob=date(1990, 5, 14),
            current_address=Address(street="1 Main St", city="Austin", state="TX", zip="78701"),
            phones=PhoneSet(current=["+15125550000"]),
            emails=EmailSet(current=["jane@example.invalid"]),
        ),
        email=EmailConfig(
            method="smtp",
            smtp=SmtpConfig(
                host="smtp.example.invalid", username="jane@example.invalid", password_env="PW"
            ),
            **email,
        ),
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Config, an empty database and the two fixture brokers."""
    from optout.brokers import registry as reg

    monkeypatch.setenv("OPTOUT_BROKERS_DIR", str(FIXTURES))
    monkeypatch.setenv("PW", "app-password")
    monkeypatch.setenv("COLUMNS", "200")
    reg._registry.clear()
    reg._loaded = False
    save_config(_config())
    init_db()
    yield tmp_path
    reg._registry.clear()
    reg._loaded = False


def _sent(slug=EMAIL, days=10, status="awaiting_broker_confirmation"):
    with get_session() as s:
        sub = create_submission(
            s,
            broker_slug=slug,
            method_used="email",
            legal_basis_cited=["TDPSA"],
            statutory_response_days=45,
        )
        when = datetime.now(UTC) - timedelta(days=days)
        mark_submitted(s, sub, {}, None, 45, submitted_at=when)
        if status != "submitted":
            update_status(s, sub, status)
        return sub.id


def run(*args, input=None):
    result = runner.invoke(app, list(args), input=input)
    assert result.exit_code == 0, result.output + repr(result.exception)
    return result.output


# --- read-only views -----------------------------------------------------------


def test_brokers_list_and_info(env):
    out = run("brokers", "list")
    assert "Test Email Broker" in out and "Test Web Broker" in out
    out = run("brokers", "info", EMAIL)
    assert "optout@example-email.com" in out
    bad = runner.invoke(app, ["brokers", "info", "nope"])
    assert bad.exit_code != 0


def test_disable_and_enable_a_broker(env):
    assert "isabled" in run("brokers", "disable", EMAIL)
    assert "nabled" in run("brokers", "enable", EMAIL)


def test_plan_status_history_and_monitor(env):
    _sent(days=60)  # overdue
    assert "Email" in run("plan")
    out = run("status")
    assert "Test Email Broker" in out or EMAIL in out
    assert EMAIL in run("history") or "Test Email Broker" in run("history")
    run("history", "--broker", EMAIL)


def test_statutes_list(env):
    out = run("statutes", "list")
    assert "Virginia" in out and "45 days" in out


def test_doctor_runs(env):
    result = runner.invoke(app, ["doctor"])
    assert "Python" in result.output or "python" in result.output.lower()


# --- recording things by hand --------------------------------------------------


def test_log_a_request_made_elsewhere_then_record_the_reply(env):
    out = run("log", EMAIL, "--date", "2026-10-01", "--method", "email", "--note", "ticket 42")
    assert "Test Email Broker" in out or EMAIL in out
    run("reply", EMAIL, "deleted", "--note", "confirmed by email")
    from sqlmodel import select

    from optout.db import Submission

    with get_session() as s:
        sub = s.exec(select(Submission).where(Submission.broker_slug == EMAIL)).one()
        assert sub.status == "confirmed"


def test_reply_to_an_unknown_broker_fails(env):
    assert runner.invoke(app, ["reply", "nope", "deleted"]).exit_code != 0


# --- sending, in preview only ----------------------------------------------------


def test_campaign_preview_sends_nothing(env, monkeypatch):
    monkeypatch.setattr(
        "smtplib.SMTP", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no sending"))
    )
    out = run("campaign", "--limit", "5")
    assert "Test Email Broker" in out or "preview" in out.lower()


def test_followups_preview(env):
    _sent(days=60)
    run("followups")


def test_queue_adds_a_broker(env):
    out = run("queue", "--broker", EMAIL)
    assert EMAIL in out or "Test Email Broker" in out or "queued" in out.lower()


# --- replies ---------------------------------------------------------------------


def test_inbox_reports_what_a_check_found(env, monkeypatch):
    from optout.engine import inbox

    rep = inbox.SyncReport()
    monkeypatch.setattr(inbox, "run_sync", lambda config, dry_run=False: rep)
    run("inbox")
    run("inbox", "--dry-run")


def test_inbox_recheck(env):
    _sent()
    run("inbox", "--recheck")


def test_review_prompt_and_apply(env, tmp_path):
    sub = _sent()
    with get_session() as s:
        s.add(
            InboxMessage(
                message_id="<r1@x>",
                from_addr="who@else.example",
                subject="Re: your request",
                received_at=datetime.now(UTC),
                text="We need more information before we can continue.",
                kind="unknown",
                state="review",
            )
        )
        s.commit()
    assert sub
    out = run("review")
    assert "who@else.example" in out or "1" in out
    prompt = run("review", "--prompt")
    assert "JSON" in prompt or "json" in prompt
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps([]))
    run("review", "--apply", str(answer))


def test_replies_senders_and_share(env):
    run("replies", "senders")
    run("replies", "share")


# --- forms -------------------------------------------------------------------------


def test_forms_views(env):
    run("forms", "todo")
    run("forms", "status")
    run("forms", "profile")
    run("forms", "prompt")
    run("forms", "share")


# --- state, report, escalation --------------------------------------------------------


def test_state_export_and_import_round_trip(env, tmp_path, monkeypatch):
    monkeypatch.setenv("OPTOUT_STATE_KEY", "a long test passphrase")
    _sent()
    path = tmp_path / "state.bin"
    run("state", "export", str(path))
    assert path.exists() and path.stat().st_size > 0
    run("state", "import", str(path))


def test_report_writes_html_without_opening_it(env, tmp_path):
    _sent()
    out_file = tmp_path / "report.html"
    run("report", "--out", str(out_file), "--no-open")
    html = out_file.read_text()
    assert "<html" in html.lower() and "Test Email Broker" in html


def test_escalate_lists_and_drafts(env):
    _sent(days=60)
    run("escalate")


# --- commands that reach Gmail, a browser or the network (each replaced by a fake) ----


class _FakeLabeler:
    calls: list = []

    def __init__(self, user, password, label, reply_label=None):
        _FakeLabeler.calls.append(("open", user, label))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def label_all_sent(self):
        return 3

    def label_replies(self, addresses, *, since):
        _FakeLabeler.calls.append(("replies", sorted(addresses), since))
        return 2


def test_label_tags_sent_mail_and_replies_from_expected_addresses(env, monkeypatch):
    save_config(
        _config(gmail_label="OPTOUT").model_copy(
            update={
                "email": _config(gmail_label="OPTOUT").email.model_copy(
                    update={
                        "smtp": SmtpConfig(
                            host="smtp.gmail.com", username="jane@gmail.com", password_env="PW"
                        )
                    }
                )
            }
        )
    )
    _sent(days=10)
    _FakeLabeler.calls = []
    monkeypatch.setattr("optout.gmail_label.GmailLabeler", _FakeLabeler)
    out = run("label")
    assert "Labeled 3 sent email(s) and 2 repl(ies) as OPTOUT" in " ".join(out.split())
    kind, addresses, since = _FakeLabeler.calls[-1]
    assert kind == "replies" and "optout@example-email.com" in addresses
    assert since == (datetime.now(UTC) - timedelta(days=11)).date()  # a day before the first send


def test_label_needs_a_gmail_account(env):
    result = runner.invoke(app, ["label"])
    assert result.exit_code == 1 and "Gmail only" in result.output


def test_daily_without_unattended_sending_only_says_what_is_ready(env, monkeypatch):
    from optout.engine import inbox

    monkeypatch.setattr(inbox, "gmail_ready", lambda c: "not set up")
    out = run("daily")
    assert "ready. Run: optout campaign --send" in out


def test_daily_with_unattended_sending_sends_and_reads_replies(env, monkeypatch):
    from optout.engine import campaign, inbox

    save_config(_config(allow_unattended=True))
    sent = []

    def fake_campaign(session, config, brokers, limit):
        sent.extend(b.slug for b in brokers[:limit])
        return campaign.CampaignReport(sent=[b.slug for b in brokers[:limit]])

    monkeypatch.setattr(campaign, "run_campaign", fake_campaign)
    rep = inbox.SyncReport()
    rep.review = 1
    monkeypatch.setattr(inbox, "gmail_ready", lambda c: None)
    monkeypatch.setattr(inbox, "run_sync", lambda c: rep)
    out = run("daily")
    assert sent == [EMAIL]
    assert "Emails sent: 1, failed: 0" in out and "Replies: 0 applied, 1 to review" in out


def test_verify_with_nothing_sent(env):
    assert "Nothing sent yet" in run("verify")


def test_verify_reports_each_result(env, monkeypatch):
    from contextlib import contextmanager

    from optout.engine import browser, verify

    _sent(slug=EMAIL, days=30)

    @contextmanager
    def no_browser(cfg):
        yield type("Ctx", (), {"new_page": lambda self: None})()

    monkeypatch.setattr(browser, "launch_browser", no_browser)
    monkeypatch.setattr(
        verify,
        "run_verification",
        lambda *a, **k: verify.VerifyReport(
            removed=[EMAIL], still_listed=["other"], unverifiable=[("third", "captcha")]
        ),
    )
    out = run("verify", "--force")
    assert "removed" in out and "still listed" in out and "third: captcha" in out


def test_drift_check_reports_changed_pages(env, monkeypatch):
    from contextlib import contextmanager

    from optout.engine import browser, probe

    class Page:
        def goto(self, *a, **k): ...
        def wait_for_timeout(self, ms): ...
        def title(self):
            return "Opt out"

        def close(self): ...

    @contextmanager
    def fake_browser(cfg):
        yield type("Ctx", (), {"new_page": lambda self: Page()})()

    monkeypatch.setattr(browser, "launch_browser", fake_browser)
    monkeypatch.setattr(probe, "run_checks", lambda page, pp: None)
    monkeypatch.setattr(probe, "summarize", lambda plan: (False, "form missing"))
    for _ in range(3):  # three failed checks in a row count as drift
        out = run("drift-check", "--no-notify")
    assert "broken" in out and WEB in out
    assert "Pages that changed" in out

    monkeypatch.setattr(probe, "summarize", lambda plan: (None, "challenge page"))
    out = run("drift-check", "--no-notify")
    assert "could not be read" in out


def test_escalate_draft_and_mark_sent(env):
    _sent(days=60)
    out = run("escalate", "--draft", f"{EMAIL}:followup")
    assert "Draft written" in out
    assert "Recorded: followup" in run("escalate", "--mark-sent", f"{EMAIL}:followup")
    bad = runner.invoke(app, ["escalate", "--mark-sent", f"{EMAIL}:nonsense"])
    assert bad.exit_code == 1
    none = runner.invoke(app, ["escalate", "--mark-sent", "nobody:appeal"])
    assert none.exit_code == 1 and "No request on record" in none.output


class _FakeMailbox:
    """A mailbox holding one broker reply and one unrelated message."""

    def __init__(self, replies):
        self.replies = replies
        self.calls: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def label_replies(self, addresses, *, since):
        self.calls.append(("label", sorted(addresses)))
        return 0

    def fetch_replies(self, seen, *, since):
        return [raw for mid, raw in self.replies if mid not in seen]

    def display_names(self, ids):
        return {}

    def fetch_messages(self, ids):
        self.calls.append(("reread", sorted(ids)))
        return {}

    def mark_read_message_ids(self, ids):
        return 0


def _mail(mid, sender, subject, body, when):
    from email.message import EmailMessage
    from email.utils import format_datetime

    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "jane@example.invalid", subject
    m["Message-ID"], m["Date"] = mid, format_datetime(when)
    m.set_content(body)
    return mid, m.as_bytes()


def test_a_reply_check_reads_mail_and_updates_the_request(env, monkeypatch):
    """The whole check, end to end, with only the mailbox faked."""
    from sqlmodel import select

    from optout.db import Submission
    from optout.engine import inbox

    _sent(days=10)
    now = datetime.now(UTC)
    box = _FakeMailbox(
        [
            _mail(
                "<r1@example-email.com>",
                "optout@example-email.com",
                "Re: Data Deletion Request",
                "We have deleted the personal information we held about you.",
                now - timedelta(days=2),
            ),
            # Older than our first request: unrelated mail, never applied.
            _mail("<old@x>", "news@example-email.com", "News", "Hi", now - timedelta(days=90)),
        ]
    )
    monkeypatch.setattr(inbox, "gmail_ready", lambda c: None)
    monkeypatch.setattr(inbox, "open_mailbox", lambda config, **k: box)

    out = run("inbox")
    assert "1 new reply applied" in " ".join(out.split())
    assert ("label", ["optout@example-email.com"]) in box.calls
    with get_session() as s:
        sub = s.exec(select(Submission).where(Submission.broker_slug == EMAIL)).one()
        assert sub.status == "confirmed"
        saved = s.exec(select(InboxMessage)).all()
        assert [m.message_id for m in saved] == ["<r1@example-email.com>"]

    # The next check finds nothing new; a parser update re-reads what's saved, in one batch.
    monkeypatch.setattr(inbox, "parser_version_seen", lambda: 0)
    out = run("inbox")
    assert "0 new replies applied" in " ".join(out.split())
    assert ("reread", ["<r1@example-email.com>"]) in box.calls


def test_a_reply_check_before_anything_was_sent(env, monkeypatch):
    from optout.engine import inbox

    monkeypatch.setattr(inbox, "gmail_ready", lambda c: None)
    result = runner.invoke(app, ["inbox"])
    assert result.exit_code == 1 and "nothing has been sent yet" in result.output
