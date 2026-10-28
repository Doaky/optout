"""Email-based opt-out handler (SMTP / Gmail OAuth)."""

from __future__ import annotations

import os
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import jinja2

from ...brokers.schema import BrokerDef
from ...config import SmtpConfig, UserConfig, load_config
from ...db import Submission, artifacts_dir
from ...utils.statutes import get_statutes
from .. import SubmissionFailed, SubmissionResult, UserInputRequired

# Jinja2 environment — templates live in src/optout/templates/
_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"
_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(_TEMPLATES_DIR),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


# ---------------------------------------------------------------------------
# Public entry point (matches the dispatcher's handler(broker, profile, sub) signature)
# ---------------------------------------------------------------------------


def run(
    broker: BrokerDef,
    profile: object,
    submission: Submission,
    *,
    _config: UserConfig | None = None,
) -> SubmissionResult:
    """Send an opt-out email for *broker* on behalf of *profile*."""
    if not broker.opt_out_email:
        raise SubmissionFailed(
            f"Broker '{broker.slug}' has no opt_out_email configured. Cannot use the email handler."
        )

    config = _config or load_config()
    smtp_cfg = config.email.smtp
    if smtp_cfg is None:
        raise SubmissionFailed("No SMTP configuration found. Run `optout init` to configure email.")

    subject = f"Data Deletion Request — {profile.legal_name}"  # type: ignore[attr-defined]
    contact = _contact_email(config, broker, profile)
    body = _render_body(
        broker,
        profile,
        contact_email=contact,
        minimal=bool(broker.source) and config.email.identifiers == "minimal",
    )
    msg = _build_message(
        from_addr=config.email.from_address or smtp_cfg.username,
        reply_to=contact if config.email.alias_pattern else None,
        to_addr=broker.opt_out_email,
        subject=subject,
        body=body,
        submission_id=submission.id,
    )

    _send(smtp_cfg, msg)

    artifact_path = _save_eml(submission.id, msg)

    return SubmissionResult(
        payload={
            "to": broker.opt_out_email,
            "from": msg["From"],
            "subject": subject,
            "message_id": msg["Message-ID"],
        },
        artifact_path=str(artifact_path),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _contact_email(config: UserConfig, broker: BrokerDef, profile: object) -> str:
    """The address the broker should write back to: a per-broker alias if configured."""
    if config.email.alias_pattern:
        return config.email.alias_pattern.format(slug=broker.slug)
    emails = profile.emails.current  # type: ignore[attr-defined]
    return emails[0] if emails else (config.email.from_address or "")


def _render_body(
    broker: BrokerDef,
    profile: object,
    *,
    contact_email: str | None = None,
    minimal: bool = False,
) -> str:
    statutes = get_statutes(broker.legal_basis)
    name = "opt_out_email_minimal.txt.j2" if minimal else "opt_out_email.txt.j2"
    return _env.get_template(name).render(
        broker=broker,
        profile=profile,
        statutes=statutes,
        contact_email=contact_email or "",
    )


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def _build_message(
    *,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    submission_id: uuid.UUID,
    reply_to: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
    msg["X-OptOut-Submission-ID"] = str(submission_id)
    msg.set_content(body, charset="utf-8")
    return msg


# ---------------------------------------------------------------------------
# SMTP transport
# ---------------------------------------------------------------------------


def _send(smtp_cfg: SmtpConfig, msg: EmailMessage) -> None:
    password = os.environ.get(smtp_cfg.password_env, "")
    if not password and smtp_cfg.host != "localhost" and not smtp_cfg.host.startswith("127."):
        raise UserInputRequired(
            f"Environment variable {smtp_cfg.password_env} is not set, so there is nothing to "
            f"log in to {smtp_cfg.host} with. "
            f"Run: export {smtp_cfg.password_env}='your app password'"
        )

    try:
        if smtp_cfg.port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_cfg.host, smtp_cfg.port, context=ctx) as smtp:
                if password:
                    smtp.login(smtp_cfg.username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(smtp_cfg.host, smtp_cfg.port) as smtp:
                smtp.ehlo()
                if smtp.has_extn("STARTTLS"):
                    smtp.starttls()
                    smtp.ehlo()
                if password and smtp.has_extn("AUTH"):
                    smtp.login(smtp_cfg.username, password)
                smtp.send_message(msg)
    except smtplib.SMTPRecipientsRefused as exc:
        raise SubmissionFailed(f"SMTP server refused recipient {msg['To']}: {exc}") from exc
    except smtplib.SMTPServerDisconnected as exc:
        raise SubmissionFailed(
            f"{smtp_cfg.host} closed the connection ({exc}). For Gmail this usually means the "
            "app password is wrong or missing, or the account blocked the sign-in."
        ) from exc
    except smtplib.SMTPAuthenticationError as exc:
        raise UserInputRequired(
            f"SMTP authentication failed. Check that {smtp_cfg.password_env!r} "
            f"is set correctly. Server said: {exc}"
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise SubmissionFailed(f"SMTP error: {exc}") from exc


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------


def _save_eml(submission_id: uuid.UUID, msg: EmailMessage) -> Path:
    path = artifacts_dir() / f"{submission_id}.eml"
    path.write_bytes(msg.as_bytes())
    return path
