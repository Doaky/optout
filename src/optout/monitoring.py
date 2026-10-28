"""One-shot monitor: re-scan all brokers, re-queue if data reappeared."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .brokers.schema import BrokerDef
from .config import UserConfig
from .db import (
    create_submission,
    get_last_scan,
    get_session,
    init_db,
    list_submissions,
    record_scan,
)
from .scan.scanner import ScanOutcome, scan_broker

_ACTIVE_STATUSES = frozenset(
    {
        "queued",
        "awaiting_user_input",
        "submitted",
        "awaiting_broker_confirmation",
    }
)


@dataclass
class BrokerScanReport:
    slug: str
    name: str
    result: str  # "clean" | "found" | "inconclusive" | "skipped"
    skipped_reason: str | None = None
    requeued: bool = False
    matches: list[dict] = field(default_factory=list)


@dataclass
class MonitorSummary:
    reports: list[BrokerScanReport] = field(default_factory=list)

    @property
    def scanned(self) -> int:
        return sum(1 for r in self.reports if r.result != "skipped")

    @property
    def found(self) -> int:
        return sum(1 for r in self.reports if r.result == "found")

    @property
    def clean(self) -> int:
        return sum(1 for r in self.reports if r.result == "clean")

    @property
    def inconclusive(self) -> int:
        return sum(1 for r in self.reports if r.result == "inconclusive")

    @property
    def requeued(self) -> int:
        return sum(1 for r in self.reports if r.requeued)


def _due_for_rescan(last_scan, interval_days: int) -> bool:
    if last_scan is None:
        return True
    now = datetime.now(UTC)
    scanned_at = last_scan.scanned_at
    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=UTC)
    return (now - scanned_at) >= timedelta(days=interval_days)


def run_monitor(
    config: UserConfig,
    brokers: list[BrokerDef] | None = None,
    *,
    _page_factory=None,
) -> MonitorSummary:
    """One-shot monitor pass.

    _page_factory(broker) → Playwright page — used in tests to inject mock pages.
    """
    from .brokers.registry import all_brokers as _all_brokers

    init_db()
    interval = config.monitoring.rescan_interval_days
    targets = [b for b in (brokers if brokers is not None else _all_brokers()) if b.enabled]
    summary = MonitorSummary()

    for broker in targets:
        with get_session() as session:
            last_scan = get_last_scan(session, broker.slug)

        if not _due_for_rescan(last_scan, interval):
            summary.reports.append(
                BrokerScanReport(
                    slug=broker.slug,
                    name=broker.name,
                    result="skipped",
                    skipped_reason=f"scanned within the last {interval} day(s)",
                )
            )
            continue

        if not broker.scan:
            with get_session() as session:
                record_scan(session, broker.slug, "inconclusive")
            summary.reports.append(
                BrokerScanReport(
                    slug=broker.slug,
                    name=broker.name,
                    result="inconclusive",
                    skipped_reason="no scan config",
                )
            )
            continue

        page = _page_factory(broker) if _page_factory else None
        outcome: ScanOutcome = scan_broker(
            broker,
            config.profile,
            headless=config.playwright.headless,
            slow_mo_ms=config.playwright.slow_mo_ms,
            _page=page,
        )

        matches_json = (
            json.dumps([{"url": m.url} for m in outcome.matches])  # URLs only: result text
            # can contain other people's names and addresses
            if outcome.matches
            else None
        )

        requeued = False
        triggered_id = None

        if outcome.result == "found":
            with get_session() as session:
                existing = list_submissions(session, broker_slug=broker.slug)
                if not any(s.status in _ACTIVE_STATUSES for s in existing):
                    new_sub = create_submission(
                        session,
                        broker_slug=broker.slug,
                        method_used=broker.method,
                        legal_basis_cited=broker.legal_basis,
                        statutory_response_days=broker.statutory_response_days,
                    )
                    triggered_id = new_sub.id
                    requeued = True

        with get_session() as session:
            record_scan(
                session,
                broker.slug,
                outcome.result,
                matches_json=matches_json,
                triggered_resubmission_id=triggered_id,
            )

        summary.reports.append(
            BrokerScanReport(
                slug=broker.slug,
                name=broker.name,
                result=outcome.result,
                requeued=requeued,
                matches=[{"text": m.text, "url": m.url} for m in outcome.matches],
            )
        )

    return summary
