"""The order to work through brokers in, grouped by what each one takes.

A first run is easier as a series of steps than as one list of 900 brokers:

1. people-search  sites that show your address to anyone: do these first (forms)
2. email          a plain email is enough: one click sends them
3. email-confirm  an email, then they send a confirm link (clicked for you when allowed)
   follow-up      sent as a plain email, but the broker replied asking for something more; kept
                  apart so "email only" stays email only
4. form           their own web form (the AI browser helper fills it in)
5. form-plus      a form plus something only you can do: an SMS or phone code, an account,
                  payment, several verification steps
6. too-much       an ID upload, notarisation, or only a phone's ad ID: off by default,
                  listed in the README

A broker's group follows what's known about it, including what's been learned (a confirm link
that worked, a form that wanted an SMS code), so it can move as the tool learns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from .brokers.schema import BrokerDef
from .db import Submission

TIERS: list[tuple[str, str, str]] = [
    ("people-search", "People-search sites", "Show your address to anyone. Do these first."),
    ("email", "Email only", "A plain email is enough."),
    ("email-confirm", "Email + confirm link", "They email back a link to click."),
    (
        "follow-up",
        "They replied and need more from you",
        "They answered but want something from you.",
    ),
    ("form", "Web forms", "Their own form; the AI browser helper fills it in."),
    (
        "form-plus",
        "Forms needing a code, account or payment",
        "Also an SMS or phone code, an account, or payment.",
    ),
    ("too-much", "Asks too much", "ID upload, notarisation, or only a phone's ad ID."),
]
DONE = {"confirmed", "removed", "closed"}
UNDER_WAY = {"queued", "submitted", "awaiting_broker_confirmation"}


def asks_too_much(b: BrokerDef) -> bool:
    return b.requires_id_upload or b.requires_notarization or b.requires_device_ids


def tier(b: BrokerDef) -> str:
    if asks_too_much(b):
        return "too-much"
    if b.category == "people_search":
        return "people-search"
    if b.method in ("manual", "web_form") or b.form_only:
        extra = b.requires_phone_verification or b.requires_account or b.requires_payment
        return "form-plus" if extra else "form"
    if b.requires_email_verification:
        return "email-confirm"
    return "email"


@dataclass
class TierSummary:
    key: str
    title: str
    hint: str
    total: int = 0
    done: int = 0
    under_way: int = 0  # sent; waiting on the broker
    needs_you: int = 0  # waiting on you (their form, a confirm, an answer)
    refused: int = 0  # they said no; the tool won't resend by itself
    todo: list[str] = field(default_factory=list)  # never contacted, enabled


def summarise(session: Session, brokers: list[BrokerDef]) -> list[TierSummary]:
    latest: dict[str, Submission] = {}
    for sub in session.exec(select(Submission)).all():
        cur = latest.get(sub.broker_slug)
        if cur is None or sub.created_at > cur.created_at:
            latest[sub.broker_slug] = sub
    out = {k: TierSummary(k, t, h) for k, t, h in TIERS}
    for b in sorted(brokers, key=lambda x: (x.priority, x.name.lower())):
        if not b.enabled and not asks_too_much(b):
            continue
        got = latest.get(b.slug)
        key = tier(b)
        if key == "email" and got is not None and got.status == "awaiting_user_input":
            key = "follow-up"
        t = out[key]
        t.total += 1
        if got is not None and got.status in DONE:
            t.done += 1
        elif got is not None and got.status in UNDER_WAY:
            t.under_way += 1
        elif got is not None and got.status == "awaiting_user_input":
            if key != "too-much":  # those are skipped on purpose: nothing for you to do
                t.needs_you += 1
        elif got is not None and got.status == "rejected":
            t.refused += 1
        elif b.enabled:
            t.todo.append(b.slug)
    return [out[k] for k, _, _ in TIERS]
