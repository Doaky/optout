"""Top-level Typer app and all subcommands."""

from __future__ import annotations

from datetime import UTC

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(
    name="optout",
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
brokers_app = typer.Typer(help="Browse and inspect broker definitions.")
app.add_typer(brokers_app, name="brokers")

console = Console()


@app.callback()
def _main(ctx: typer.Context) -> None:
    """Self-hosted data broker opt-out tool.

    Start with `optout plan` to see what to do next; run with no command to open the dashboard.
    """
    if ctx.invoked_subcommand is not None:
        if ctx.invoked_subcommand == "forms":
            return  # its output is read by other programs: no banner
        console.print("\n[bold]OptOut[/bold]  [dim]·  self-hosted data-broker opt-out[/dim]\n")
        return
    # Plain `optout`: set up on first run, then open the dashboard.
    from .config import config_path
    from .web.server import serve

    if not config_path().exists():
        from .wizard import run_init_wizard

        run_init_wizard(open_dashboard=False)
        if not config_path().exists():
            raise typer.Exit(code=0)
    serve()


# ---------------------------------------------------------------------------
# optout init
# ---------------------------------------------------------------------------


@app.command()
def init() -> None:
    """Interactive wizard — creates ~/.config/optout/config.yml."""
    from .wizard import run_init_wizard

    run_init_wizard()


# ---------------------------------------------------------------------------
# Display helpers (brokers)
# ---------------------------------------------------------------------------


def _method_style(method: str) -> str:
    return {"web_form": "blue", "email": "green", "postal": "yellow", "manual": "dim"}.get(
        method, "white"
    )


def _bool_icon(val: bool) -> str:
    return "[green]✓[/green]" if val else "[dim]✗[/dim]"


def _status_style(status: str) -> str:
    return {
        "queued": "dim",
        "awaiting_user_input": "yellow",
        "submitted": "cyan",
        "awaiting_broker_confirmation": "cyan",
        "confirmed": "green",
        "removed": "bold green",
        "rejected": "red",
        "expired": "dim",
        "failed": "red",
    }.get(status, "white")


from .labels import STATUS_LABEL as _STATUS_LABEL  # noqa: E402


def _status_label(status: str) -> str:
    return _STATUS_LABEL.get(status, status)


def _fmt_date(dt) -> str:
    if dt is None:
        return "—"
    d = dt.date() if hasattr(dt, "date") else dt
    return d.strftime("%b %d").replace(" 0", "  ")  # "May 10", "Jun  4"


# ---------------------------------------------------------------------------
# optout brokers list / info
# ---------------------------------------------------------------------------


@brokers_app.command("list")
def brokers_list(
    category: str | None = typer.Option(None, "--category", "-c", help="Filter by category."),
    method: str | None = typer.Option(None, "--method", "-m", help="Filter by method."),
    disabled: bool = typer.Option(False, "--disabled", help="Only show disabled brokers."),
    show_all: bool = typer.Option(False, "--all", "-a", help="List every row, however many."),
) -> None:
    """Show all known brokers."""
    from collections import Counter

    from .brokers.registry import all_brokers, load_errors

    brokers = all_brokers()
    if category:
        brokers = [b for b in brokers if b.category == category]
    if method:
        brokers = [b for b in brokers if b.method == method]
    if disabled:
        brokers = [b for b in brokers if not b.enabled]
    if load_errors:
        console.print(
            f"[yellow]{len(load_errors)} broker file(s) skipped; first: {load_errors[0]}[/yellow]"
        )
    if len(brokers) > 40 and not show_all:
        by = Counter((b.method, "on" if b.enabled else "off") for b in brokers)
        console.print(f"[bold]{len(brokers)} brokers[/bold]")
        for (m, st), n in sorted(by.items()):
            console.print(f"  {m:<9} {st:<4} {n}")
        console.print(
            "[dim]Use --all to list them, or filter with --category/--method/--disabled.[/dim]"
        )
        raise typer.Exit(code=0)

    if not brokers:
        msg = (
            f"No brokers found in category '{category}'."
            if category
            else "No broker definitions found. Add YAML files to the brokers/ directory."
        )
        console.print(f"[yellow]{msg}[/yellow]")
        raise typer.Exit(code=0)

    table = Table(show_header=True, header_style="bold", box=box.SIMPLE_HEAD)
    table.add_column("On", justify="center")
    table.add_column("Name", style="bold", min_width=16)
    table.add_column("Slug", style="dim")
    table.add_column("Category")
    table.add_column("Method", min_width=8)
    table.add_column("Legal Basis")
    table.add_column("Days", justify="right")
    table.add_column("Last Verified")

    for b in brokers:
        ms = _method_style(b.method)
        table.add_row(
            "[green]✓[/green]" if b.enabled else "[red]✗[/red]",
            b.name,
            b.slug,
            b.category,
            f"[{ms}]{b.method}[/{ms}]",
            ", ".join(b.legal_basis),
            str(b.statutory_response_days),
            str(b.last_verified_at or "—"),
        )

    console.print(table)
    console.print(f"[dim]{len(brokers)} broker(s) listed.[/dim]")


@brokers_app.command("probe")
def brokers_probe(
    slug: str = typer.Argument(
        ..., metavar="BROKER", help="Broker ID, as shown by `optout brokers list` (e.g. spokeo)."
    ),
    channel: str = typer.Option(
        "config",
        "--channel",
        help="Browser: 'chrome', 'chromium' (bundled), or 'config' to use your config.",
    ),
    pause: bool = typer.Option(
        True, "--pause/--no-pause", help="Wait for Enter after each page load."
    ),
    settle: float = typer.Option(
        8.0, "--settle", help="With --no-pause, seconds to let each page render."
    ),
) -> None:
    """Open a broker's opt-out page and check our form-filling steps still match it.

    Never fills or submits.
    """
    import json
    from dataclasses import asdict
    from datetime import UTC, datetime

    from rich.markup import escape

    from .brokers.registry import get_broker
    from .config import PlaywrightConfig, config_dir, load_config, write_private
    from .db import get_session, init_db, record_health
    from .engine.browser import launch_browser
    from .engine.probe import blocked_reason, build_plan, run_checks, summarize

    try:
        broker = get_broker(slug)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    try:
        pw_cfg = load_config().playwright
    except FileNotFoundError:
        pw_cfg = PlaywrightConfig()
    pw_cfg = pw_cfg.model_copy(update={"headless": False})
    if channel != "config":
        pw_cfg.channel = None if channel == "chromium" else channel

    plan = build_plan(broker)
    console.print(
        f"[bold]Probing {broker.name}[/bold]: read-only, placeholder identity "
        "(John Smith, Virginia). Nothing is filled, clicked, or submitted."
    )

    with launch_browser(pw_cfg) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for probe_page in plan.pages:
            console.print(f"\n[cyan]{probe_page.source}[/cyan] → {probe_page.url}")
            try:
                page.goto(probe_page.url, timeout=45_000, wait_until="domcontentloaded")
            except Exception as exc:
                probe_page.load_error = str(exc).splitlines()[0][:200]
                console.print(f"  [red]load failed:[/red] {probe_page.load_error}")
                continue
            if pause:
                typer.prompt(
                    "  Clear any CAPTCHA/Cloudflare check in the browser (don't submit "
                    "anything), then press Enter",
                    default="",
                    show_default=False,
                )
            else:
                page.wait_for_timeout(settle * 1000)
            probe_page.final_url = page.url
            probe_page.title = page.title()
            run_checks(page, probe_page)
            if reason := blocked_reason(probe_page.title):
                console.print(
                    f"  [yellow]blocked:[/yellow] page title is {escape(repr(probe_page.title))} "
                    f"({reason}); results below are not about the broker's real page"
                )
            for c in probe_page.checks:
                colour = {"found": "green", "missing": "red", "error": "yellow"}[c.status]
                console.print(
                    f"  [{colour}]{c.status:<7}[/{colour}] {c.count:>3}  "
                    f"{c.role:<24} {escape(c.selector)}"
                    + (f"  ({escape(c.detail)})" if c.detail else "")
                )

    if plan.unreachable:
        console.print("\n[dim]Not checkable without interacting (after a click or prompt):[/dim]")
        for c in plan.unreachable:
            console.print(f"  [dim]{c.role:<24} {escape(c.selector)}[/dim]")

    ok, detail = summarize(plan)
    if ok is not None:
        init_db()
        with get_session() as session:
            record_health(session, broker.slug, "probe", ok, detail[:200])
    out_dir = config_dir() / "probes"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = out_dir / f"{broker.slug}-{stamp}.json"
    write_private(out, json.dumps(asdict(plan), indent=2))
    console.print(f"\nReport saved to {out}")


@brokers_app.command("import")
def brokers_import(
    csv_path: str | None = typer.Argument(
        None, help="Registry CSV file. Omit to download California's from cppa.ca.gov."
    ),
) -> None:
    """Load the list of registered data brokers (California registry). Sends nothing."""
    from pathlib import Path

    from .brokers.importer import CA_REGISTRY_URL, import_ca_registry
    from .brokers.registry import imported_dir

    if not csv_path:
        console.print(f"Downloading {CA_REGISTRY_URL}")
    try:
        r = import_ca_registry(Path(csv_path).expanduser() if csv_path else None)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"[green]Imported {r.imported} brokers[/green] to {imported_dir()}\n"
        f"  {r.disabled} disabled by default (FCRA/GLBA/HIPAA-regulated)\n"
        f"  {r.merged} more entities merged into a shared contact\n"
        f"  {r.skipped} skipped (no valid email)\n"
        "Nothing has been sent."
    )


def _toggle(slug: str, enabled: bool) -> None:
    from .brokers.registry import get_broker, set_enabled

    try:
        get_broker(slug)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    set_enabled(slug, enabled)
    console.print(f"{slug}: {'[green]enabled[/green]' if enabled else '[red]disabled[/red]'}")


@brokers_app.command("disable")
def brokers_disable(
    slug: str = typer.Argument(
        ..., metavar="BROKER", help="Broker ID to skip (see `optout brokers list`)."
    ),
) -> None:
    """Never contact this broker (e.g. a site you want to stay listed on)."""
    _toggle(slug, False)


@brokers_app.command("enable")
def brokers_enable(
    slug: str = typer.Argument(
        ..., metavar="BROKER", help="Broker ID to include (see `optout brokers list`)."
    ),
) -> None:
    """Include this broker again (also overrides the FCRA/GLBA default-off)."""
    _toggle(slug, True)


@app.command()
def campaign(
    limit: int = typer.Option(25, "--limit", "-n", help="Max emails this run."),
    only: list[str] = typer.Option(None, "--only", help="Restrict to these broker slugs."),
    send: bool = typer.Option(False, "--send", help="Actually send. Default is preview only."),
    tier: str = typer.Option(
        None, "--group", "--tier", help="Only one group from `optout plan`: email or email-confirm."
    ),
) -> None:
    """Email deletion requests to every eligible broker, within daily limits."""
    from datetime import UTC, datetime

    from .brokers.registry import all_brokers
    from .config import load_config, user_tz
    from .db import get_session, init_db
    from .engine.campaign import run_campaign, select_brokers, sent_today
    from .engine.methods.email import _contact_email, _render_body
    from .wizard import assert_own_identity

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    assert_own_identity(config.profile.legal_name)
    init_db()
    now = datetime.now(UTC)

    with get_session() as session:
        sel = select_brokers(
            all_brokers(),
            session,
            now=now,
            only=only or None,
            my_groups=config.my_groups,
            resend_after_days=config.email.resend_after_days,
        )
        if tier:
            from .plan import tier as tier_of

            sel.eligible = [b for b in sel.eligible if tier_of(b) == tier]
        used = sent_today(session, now, user_tz(config))
        quota = max(0, config.email.daily_limit - used)
        batch = sel.eligible[: min(limit, quota)]

        console.print(
            f"[bold]{len(sel.eligible)}[/bold] brokers eligible, "
            f"{len(sel.skipped)} skipped. Daily quota: {used}/{config.email.daily_limit} used."
        )
        if not batch:
            console.print("[yellow]Nothing to send right now.[/yellow]")
            raise typer.Exit(code=0)

        sender = config.email.from_address or (
            config.email.smtp.username if config.email.smtp else "?"
        )
        console.print(f"This run would email [bold]{len(batch)}[/bold] brokers from {sender}:")
        console.print(
            "  " + ", ".join(b.slug for b in batch[:15]) + (" …" if len(batch) > 15 else "")
        )

        if not send:
            sample = batch[0]
            minimal = bool(sample.source) and config.email.identifiers == "minimal"
            body = _render_body(
                sample,
                config.profile,
                contact_email=_contact_email(config, sample, config.profile),
                minimal=minimal,
            )
            console.print(f"\n[dim]Sample, to {sample.opt_out_email}:[/dim]\n{body}")
            console.print("[yellow]Preview only.[/yellow] Re-run with [cyan]--send[/cyan] to send.")
            raise typer.Exit(code=0)

        typed = typer.prompt(f"Type SEND {len(batch)} to send these emails now")
        if typed.strip() != f"SEND {len(batch)}":
            console.print("Cancelled.")
            raise typer.Exit(code=1)

        def progress(slug: str, state: str) -> None:
            colour = "green" if state == "sent" else "red"
            console.print(f"  [{colour}]{state:<6}[/{colour}] {slug}")

        report = run_campaign(session, config, batch, limit=limit, on_progress=progress)

    console.print(f"\n[bold]{len(report.sent)} sent, {len(report.failed)} failed.[/bold]")
    if report.stopped_reason:
        console.print(f"[yellow]Stopped early: {report.stopped_reason}[/yellow]")
    _read_replies_after_batch(config, len(report.sent))
    console.print("Track them with [cyan]optout status[/cyan]. Sent does not mean removed.")


def _read_replies_after_batch(config, sent: int) -> None:
    """After a batch from the command line: wait a moment for auto-replies, then read them in."""
    import time

    from .engine.inbox import gmail_ready, run_sync

    if not sent or not config.email.fetch_after_batch or gmail_ready(config) is not None:
        return
    console.print("[dim]Waiting 30 seconds for auto-replies, then reading replies in…[/dim]")
    time.sleep(30)
    try:
        rep = run_sync(config)
        console.print(f"Replies: {len(rep.applied)} applied, {rep.review} to review.")
    except ValueError as exc:
        console.print(f"[yellow]{exc}[/yellow]")


@brokers_app.command("info")
def brokers_info(
    slug: str = typer.Argument(
        ...,
        metavar="BROKER",
        help="Broker ID, as shown by `optout brokers list` (e.g. whitepages).",
    ),
) -> None:
    """Show a broker's definition and your submission status with it."""
    from .brokers.registry import get_broker
    from .db import db_path, get_session, list_submissions

    try:
        broker = get_broker(slug)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    # ── Overview panel ────────────────────────────────────────────────────
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="dim", justify="right")
    grid.add_column()

    ms = _method_style(broker.method)
    rows: list[tuple[str, str]] = [
        ("Slug:", broker.slug),
        ("Domain:", broker.domain),
        ("Category:", broker.category),
        ("Method:", f"[{ms}]{broker.method}[/{ms}]"),
    ]
    if broker.opt_out_url:
        rows.append(("Opt-out URL:", broker.opt_out_url))
    if broker.opt_out_email:
        rows.append(("Opt-out email:", broker.opt_out_email))
    if broker.opt_out_postal_address:
        rows.append(("Postal address:", broker.opt_out_postal_address))
    if broker.parent_company:
        rows.append(("Parent company:", broker.parent_company))
    rows += [
        ("Legal basis:", ", ".join(broker.legal_basis)),
        ("Statutory response:", f"{broker.statutory_response_days} days"),
        (
            "Re-add window:",
            f"{broker.re_add_window_days} days" if broker.re_add_window_days else "—",
        ),
        ("Last verified:", str(broker.last_verified_at or "unknown")),
    ]
    for label, value in rows:
        grid.add_row(label, value)

    console.print(Panel(grid, title=f"[bold]{broker.name}[/bold]", expand=False))

    # ── Requirements ──────────────────────────────────────────────────────
    console.print("\n[bold]Requirements[/bold]")
    reqs = [
        ("Listing URL", broker.requires_listing_url),
        ("Phone verification", broker.requires_phone_verification),
        ("Email verification", broker.requires_email_verification),
        ("ID upload", broker.requires_id_upload),
        ("Notarization", broker.requires_notarization),
    ]
    for label, val in reqs:
        console.print(f"  {_bool_icon(val)} {label}")

    if broker.required_fields:
        console.print(f"  [dim]Required fields:[/dim] {', '.join(broker.required_fields)}")
    if broker.optional_fields:
        console.print(f"  [dim]Optional fields:[/dim] {', '.join(broker.optional_fields)}")

    # ── Notes ─────────────────────────────────────────────────────────────
    if broker.notes:
        console.print("\n[bold]Notes[/bold]")
        for line in broker.notes.strip().splitlines():
            console.print(f"  [dim]{line}[/dim]")

    # ── Steps (web_form only) ─────────────────────────────────────────────
    if broker.steps:
        console.print(f"\n[bold]Steps ({len(broker.steps)})[/bold]")
        steps_table = Table(show_header=True, header_style="bold", box=box.SIMPLE)
        steps_table.add_column("#", justify="right", style="dim", width=3)
        steps_table.add_column("ID")
        steps_table.add_column("Type", style="cyan")
        for i, step in enumerate(broker.steps, 1):
            steps_table.add_row(str(i), step.id, step.type)
        console.print(steps_table)

    # ── Submission status ─────────────────────────────────────────────────
    console.print("\n[bold]Your submissions[/bold]")
    if not db_path().exists():
        console.print("  [dim]Database not initialised — run [bold]optout init[/bold] first.[/dim]")
        return

    with get_session() as session:
        subs = list_submissions(session, broker_slug=slug)

    if not subs:
        console.print(
            f"  [dim]No submissions yet. Run [bold]optout submit --broker {slug}[/bold].[/dim]"
        )
        return

    sub_table = Table(show_header=True, header_style="bold", box=box.SIMPLE)
    sub_table.add_column("ID", style="dim", max_width=12)
    sub_table.add_column("Status")
    sub_table.add_column("Submitted")
    sub_table.add_column("Deadline")
    for s in subs:
        ss = _status_style(s.status)
        sub_table.add_row(
            str(s.id)[:8] + "…",
            f"[{ss}]{s.status}[/{ss}]",
            str(s.submitted_at.date()) if s.submitted_at else "—",
            str(s.deadline_at.date()) if s.deadline_at else "—",
        )
    console.print(sub_table)


# ---------------------------------------------------------------------------
# optout scan
# ---------------------------------------------------------------------------


@app.command()
def scan(
    broker: str | None = typer.Option(None, "--broker", "-b", help="Limit to one broker slug."),
) -> None:
    """Check which brokers currently list you (no submissions made)."""
    import json as _json

    from rich.progress import Progress, SpinnerColumn, TextColumn

    from .brokers.registry import all_brokers, get_broker
    from .config import load_config
    from .db import get_session, init_db, record_scan
    from .scan.scanner import scan_broker
    from .wizard import assert_own_identity

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    assert_own_identity(config.profile.legal_name)
    init_db()

    try:
        targets = [get_broker(broker)] if broker else all_brokers()
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    no_scan_cfg = [b for b in targets if not b.scan]
    scannable = [b for b in targets if b.scan]

    if no_scan_cfg:
        console.print(
            f"[dim]{len(no_scan_cfg)} broker(s) have no scan config and will be skipped: "
            f"{', '.join(b.slug for b in no_scan_cfg)}[/dim]\n"
        )

    if not scannable:
        console.print("[yellow]No brokers with scan config found.[/yellow]")
        raise typer.Exit(code=0)

    table = Table(show_header=True, header_style="bold", box=box.SIMPLE_HEAD)
    table.add_column("Broker", style="bold", min_width=16)
    table.add_column("Result", min_width=14)
    table.add_column("Matches", justify="right")
    table.add_column("Detail", style="dim", no_wrap=False)

    found_count = clean_count = inconclusive_count = 0

    for b in scannable:
        with Progress(
            SpinnerColumn(),
            TextColumn(f"Scanning [bold]{b.name}[/bold]…"),
            transient=True,
            console=console,
        ) as progress:
            progress.add_task("", total=None)
            outcome = scan_broker(
                b,
                config.profile,
                headless=config.playwright.headless,
                slow_mo_ms=config.playwright.slow_mo_ms,
            )

        with get_session() as session:
            record_scan(
                session,
                b.slug,
                outcome.result,
                matches_json=(
                    _json.dumps([{"text": m.text, "url": m.url} for m in outcome.matches])
                    if outcome.matches
                    else None
                ),
            )

        if outcome.result == "found":
            result_cell = "[red bold]FOUND[/red bold]"
            found_count += 1
        elif outcome.result == "clean":
            result_cell = "[green]clean[/green]"
            clean_count += 1
        else:
            result_cell = "[yellow]inconclusive[/yellow]"
            inconclusive_count += 1

        if outcome.matches:
            snippet = outcome.matches[0].text[:70].replace("\n", " ")
            detail = snippet + ("…" if len(outcome.matches[0].text) > 70 else "")
        else:
            detail = outcome.error or ""

        table.add_row(b.name, result_cell, str(len(outcome.matches)), detail)

    console.print(table)

    parts: list[str] = []
    if found_count:
        parts.append(f"[red bold]{found_count} found[/red bold]")
    if clean_count:
        parts.append(f"[green]{clean_count} clean[/green]")
    if inconclusive_count:
        parts.append(f"[yellow]{inconclusive_count} inconclusive[/yellow]")
    console.print("  " + "  ·  ".join(parts))

    if found_count:
        console.print(
            f"\n  [red bold]⚠[/red bold]  Your data was found on {found_count} broker(s).\n"
            "     Run [cyan]optout queue[/cyan] and [cyan]optout submit[/cyan] to request removal."
        )
        console.print(
            "\n  [yellow bold]ℹ[/yellow bold]  [yellow]Scan matches by name only"
            " — results may include other people\n"
            "     with the same name. Check the Detail column"
            " before submitting.[/yellow]"
        )


# ---------------------------------------------------------------------------
# optout queue
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES = frozenset(
    {
        "queued",
        "awaiting_user_input",
        "submitted",
        "awaiting_broker_confirmation",
    }
)


@app.command()
def queue(
    broker: str | None = typer.Option(None, "--broker", "-b", help="Broker slug to queue."),
) -> None:
    """Add brokers to the submission queue."""
    from .brokers.registry import all_brokers, get_broker
    from .config import load_config
    from .db import create_submission, get_session, init_db, list_submissions
    from .wizard import assert_own_identity

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    assert_own_identity(config.profile.legal_name)
    init_db()

    try:
        brokers_to_queue = (
            [get_broker(broker)]
            if broker
            else [b for b in all_brokers() if b.enabled and not b.source]
        )
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    if not brokers_to_queue:
        console.print(
            "[yellow]No brokers found. Add YAML files to the brokers/ directory.[/yellow]"
        )
        raise typer.Exit(code=0)

    queued = skipped = 0
    with get_session() as session:
        for b in brokers_to_queue:
            existing = list_submissions(session, broker_slug=b.slug)
            if any(s.status in _ACTIVE_STATUSES for s in existing):
                console.print(f"  [dim]Skip {b.name} — active submission already exists.[/dim]")
                skipped += 1
                continue

            create_submission(
                session,
                broker_slug=b.slug,
                method_used=b.method,
                legal_basis_cited=b.legal_basis,
                statutory_response_days=b.statutory_response_days,
            )
            ms = _method_style(b.method)
            console.print(f"  [green]Queued:[/green] {b.name}  [{ms}]{b.method}[/{ms}]")
            queued += 1

    console.print(f"\n[bold]{queued}[/bold] broker(s) queued, {skipped} skipped.")
    if queued:
        console.print("Run [cyan]optout submit[/cyan] to process the queue.")


# ---------------------------------------------------------------------------
# optout submit
# ---------------------------------------------------------------------------


@app.command()
def submit(
    broker: str | None = typer.Option(None, "--broker", "-b", help="Limit to one broker slug."),
    slow: bool = typer.Option(
        False, "--slow", help="Add 600ms delay between browser actions so you can follow along."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show debug logs (selectors, timing) in the terminal."
    ),
) -> None:
    """Process queued opt-out submissions."""
    from rich.progress import Progress, SpinnerColumn, TextColumn

    from .brokers.registry import get_broker
    from .config import load_config
    from .db import get_session, init_db, list_submissions
    from .engine.dispatcher import run_submission
    from .logging import configure_logging
    from .wizard import assert_own_identity

    configure_logging(verbose=verbose)

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    if slow:
        config.playwright.slow_mo_ms = 600

    assert_own_identity(config.profile.legal_name)
    init_db()

    with get_session() as session:
        subs = list_submissions(
            session, broker_slug=broker, status=["queued", "awaiting_user_input"]
        )

        if not subs:
            console.print("[yellow]No queued submissions found.[/yellow]")
            hint = f"--broker {broker}" if broker else ""
            console.print(f"  Run: [bold]optout queue {hint}[/bold]".rstrip())
            raise typer.Exit(code=0)

        console.print(f"\nProcessing [bold]{len(subs)}[/bold] queued submission(s)…\n")

        succeeded = failed = needs_input = 0
        for sub in subs:
            try:
                broker_def = get_broker(sub.broker_slug)
            except KeyError:
                console.print(f"  [red]Unknown broker slug '{sub.broker_slug}' — skipping.[/red]")
                failed += 1
                continue

            ms = _method_style(broker_def.method)
            retry_tag = "  [yellow](retry)[/yellow]" if sub.status == "awaiting_user_input" else ""
            console.print(
                f"[bold]{broker_def.name}[/bold]  [{ms}]{broker_def.method}[/{ms}]{retry_tag}"
            )

            with Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                transient=True,
                console=console,
            ) as progress:
                task = progress.add_task("Submitting…", total=None)
                run_submission(session, sub, broker_def, config.profile, config)
                progress.update(task, completed=True)

            ss = _status_style(sub.status)
            console.print(f"  Status: [{ss}]{_status_label(sub.status)}[/{ss}]")
            if sub.failure_reason:
                console.print(f"  [dim]{sub.failure_reason}[/dim]")
            if sub.artifact_path:
                console.print(f"  [dim]Artifact: {sub.artifact_path}[/dim]")
            console.print()

            if sub.status == "awaiting_broker_confirmation":
                succeeded += 1
            elif sub.status == "awaiting_user_input":
                needs_input += 1
            else:
                failed += 1

    parts = []
    if succeeded:
        parts.append(f"[green]{succeeded} submitted[/green]")
    if needs_input:
        parts.append(f"[yellow]{needs_input} need user input[/yellow]")
    if failed:
        parts.append(f"[red]{failed} failed[/red]")
    console.print("Summary: " + ", ".join(parts))
    console.print("Run [cyan]optout status[/cyan] to see the full picture.")


# ---------------------------------------------------------------------------
# Display helpers (status command)
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset({"confirmed", "rejected", "failed", "expired"})


def _days_left_cell(submission) -> str:
    """Return a Rich-markup string for the Days Left column."""
    from datetime import datetime

    if submission.status in ("confirmed", "removed"):
        return "[green]✓[/green]"
    if submission.status in _TERMINAL_STATUSES:
        return "—"
    if submission.deadline_at is None:
        return "—"

    now = datetime.now(UTC)
    # SQLite stores datetimes as naive strings; ensure we compare correctly
    dl = submission.deadline_at
    if dl.tzinfo is None:
        dl = dl.replace(tzinfo=UTC)
    days = (dl - now).days

    if days < 0:
        return "[red bold]OVERDUE[/red bold]"
    if days < 7:
        return f"[red]{days}[/red]"
    if days < 14:
        return f"[yellow]{days}[/yellow]"
    return f"[green]{days}[/green]"


def _deadline_cell(submission) -> str:
    if submission.deadline_at is None:
        return "—"
    dl = submission.deadline_at
    date_str = dl.date() if hasattr(dl, "date") else str(dl)[:10]
    return str(date_str)


# ---------------------------------------------------------------------------
# optout status
# ---------------------------------------------------------------------------


@app.command("log")
def log_cmd(
    broker: str = typer.Argument(..., help="Broker slug, or any name for an unlisted company."),
    on: str = typer.Option(
        None, "--date", "-d", help="Date you contacted them (YYYY-MM-DD). Default: today."
    ),
    method: str = typer.Option(
        "email", "--method", "-m", help="email, web_form, phone, postal, or manual."
    ),
    days: int = typer.Option(
        None, "--days", help="Response deadline in days (default: broker's, or 45)."
    ),
    note: str = typer.Option(
        None, "--note", "-n", help="Short note, e.g. a ticket number. Don't put PII here."
    ),
) -> None:
    """Record an opt-out request you made yourself, so its deadline is tracked."""
    from datetime import UTC, date, datetime

    from .brokers.registry import get_broker
    from .db import get_session, init_db, log_outreach

    if method not in ("email", "web_form", "phone", "postal", "manual"):
        console.print(f"[red]Unknown method '{method}'.[/red]")
        raise typer.Exit(code=1)
    try:
        when = date.fromisoformat(on) if on else date.today()
    except ValueError:
        console.print(f"[red]Bad date '{on}', expected YYYY-MM-DD.[/red]")
        raise typer.Exit(code=1)
    if when > date.today():
        console.print("[red]Date is in the future.[/red]")
        raise typer.Exit(code=1)

    try:
        b = get_broker(broker)
        slug, legal, default_days = b.slug, b.legal_basis, b.statutory_response_days
    except KeyError:
        slug, legal, default_days = broker.strip().lower(), [], 45
        console.print(f"[dim]'{slug}' isn't a known broker; logging it as unlisted.[/dim]")

    init_db()
    with get_session() as session:
        sub = log_outreach(
            session,
            broker_slug=slug,
            method_used=method,
            legal_basis_cited=legal,
            statutory_response_days=days or default_days,
            submitted_at=datetime(when.year, when.month, when.day, 12, tzinfo=UTC),
            note=note,
        )
    console.print(
        f"[green]Logged[/green] {slug} ({method}) on {when}. Deadline {sub.deadline_at:%Y-%m-%d}."
    )


@app.command("history")
def history_cmd(
    broker: str | None = typer.Option(None, "--broker", "-b", help="Filter by broker slug."),
) -> None:
    """Timeline of every request and event, oldest first."""
    import json

    from .db import db_path, get_session, list_all_events

    if not db_path().exists():
        console.print("[yellow]No history yet.[/yellow]")
        raise typer.Exit(code=0)
    with get_session() as session:
        rows = list_all_events(session, broker_slug=broker)
    if not rows:
        console.print("[yellow]No history yet.[/yellow]")
        raise typer.Exit(code=0)

    table = Table(show_header=True, header_style="bold", box=box.SIMPLE_HEAD)
    table.add_column("When")
    table.add_column("Broker", style="bold")
    table.add_column("Event")
    table.add_column("Method")
    table.add_column("Note", style="dim")
    for sub, ev in rows:
        # A logged request's event time is when it was recorded; show the real date.
        payload = json.loads(ev.payload_json or "{}")
        when = sub.submitted_at if ev.event_type == "submitted" else ev.created_at
        table.add_row(
            _fmt_date(when),
            sub.broker_slug,
            ev.event_type.replace("_", " "),
            sub.method_used,
            payload.get("note", "") if payload.get("source") == "manual_log" else "",
        )
    console.print(table)


@app.command()
def verify(
    broker: str | None = typer.Option(None, "--broker", "-b", help="Limit to one broker slug."),
    force: bool = typer.Option(False, "--force", help="Check now even if not yet due."),
) -> None:
    """Re-search brokers to confirm listings are gone. Only this can mark a request removed."""
    from datetime import UTC, datetime

    from .brokers.registry import get_broker, load_registry
    from .config import load_config
    from .db import get_session, init_db, list_submissions
    from .engine.browser import launch_browser
    from .engine.verify import VERIFIABLE_STATUSES, is_due, run_verification

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    load_registry()
    init_db()
    now = datetime.now(UTC)

    with get_session() as session:
        subs = [
            s
            for s in list_submissions(session, broker_slug=broker)
            if s.status in VERIFIABLE_STATUSES
        ]
        if not subs:
            console.print("[yellow]Nothing sent yet that could be verified.[/yellow]")
            raise typer.Exit(code=0)
        due = [s for s in subs if force or is_due(session, s, now)]
        console.print(f"{len(subs)} request(s) awaiting verification, {len(due)} due now.")
        if not due:
            console.print("First check runs 7 days after sending, then weekly.")
            raise typer.Exit(code=0)

        with launch_browser(config.playwright) as ctx:
            report = run_verification(
                session, due, get_broker, config.profile, lambda b: ctx.new_page(), force=True
            )

    for slug in report.removed:
        console.print(
            f"  [bold green]removed[/bold green]       {slug}  (re-search shows no listing)"
        )
    for slug in report.still_listed:
        console.print(f"  [red]still listed[/red]  {slug}")
    for slug, why in report.unverifiable:
        console.print(f"  [yellow]unverified[/yellow]   {slug}: {why}")
    console.print("\nNothing is marked removed unless a re-search rendered without your listing.")


@app.command()
def label(
    replies: bool = typer.Option(
        True, "--replies/--no-replies", help="Also label inbox replies from contacted brokers."
    ),
) -> None:
    """Catch up: add your Gmail label to opt-out emails already sent, and to broker replies."""
    from .brokers.registry import get_broker, load_registry
    from .config import load_config
    from .db import get_session, init_db, list_submissions
    from .engine.methods.email import read_smtp_password
    from .gmail_label import GmailLabeler

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    name = config.email.gmail_label
    smtp = config.email.smtp
    if not name or smtp is None or not smtp.host.endswith("gmail.com"):
        console.print(
            "Set [bold]gmail_label: OPTOUT[/bold] under email: in your config (Gmail only)."
        )
        raise typer.Exit(code=1)
    load_registry()
    init_db()
    domains: set[str] = set()
    with get_session() as session:
        for sub in list_submissions(session):
            try:
                b = get_broker(sub.broker_slug)
            except KeyError:
                continue
            domains.add(b.domain)
            if b.opt_out_email:
                domains.add(b.opt_out_email.rsplit("@", 1)[1])
    try:
        with GmailLabeler(
            smtp.username, read_smtp_password(smtp), name, reply_label=config.email.reply_label
        ) as gmail:
            sent = gmail.label_all_sent()
            got = gmail.label_replies(domains) if replies else 0
    except Exception as exc:
        console.print(f"[red]Could not reach Gmail over IMAP: {type(exc).__name__}[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"Labeled [bold]{sent}[/bold] sent email(s) and [bold]{got}[/bold] repl(ies) as {name}."
    )


replies_app = typer.Typer(help="Known broker replies (shared patterns).")
app.add_typer(replies_app, name="replies")


@app.command()
def inbox(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would happen; change nothing."
    ),
    recheck_saved: bool = typer.Option(
        False, "--recheck", help="Re-sort saved replies with the current rules (no Gmail)."
    ),
) -> None:
    """Read broker replies from Gmail and update requests where the answer is clear."""
    from .config import load_config
    from .db import init_db
    from .engine.inbox import run_sync

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    init_db()
    if recheck_saved:
        from sqlmodel import select

        from .brokers.registry import all_brokers, load_registry
        from .db import Submission, get_session
        from .engine.inbox import build_index, mark_rules_version, recheck, reply_senders

        load_registry()
        with get_session() as session:
            subs = list(session.exec(select(Submission)))
            idx = build_index(
                subs,
                {b.slug: b for b in all_brokers()},
                config.email.alias_pattern,
                reply_senders(session),
            )
            n = recheck(session, idx)
        mark_rules_version()
        console.print(f"Re-checked saved replies: {n} changed.")
        return
    try:
        report = run_sync(config, dry_run=dry_run)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    for slug, kind in report.applied:
        console.print(f"  [green]{kind:<12}[/green] {slug}")
    console.print(
        f"\n{len(report.applied)} new repl{'y' if len(report.applied) == 1 else 'ies'}"
        f"{' (dry run)' if dry_run else ' applied'}, {report.review} to review."
        + (f" {report.confirmed} confirm link(s) opened." if report.confirmed else "")
        + (f" {report.closed} closed (broker doesn't cover you)." if report.closed else "")
    )
    if report.review and not dry_run:
        console.print("Review them on the dashboard, or: [cyan]optout review[/cyan]")


@app.command()
def review(
    prompt: bool = typer.Option(False, "--prompt", help="Print a prompt to paste into Claude."),
    apply: str = typer.Option(
        None, "--apply", help="Apply Claude's JSON answer from a file, or - for stdin."
    ),
) -> None:
    """Replies the tool wasn't sure about. Sort them yourself or with Claude's help."""
    import sys

    from .brokers.registry import all_brokers
    from .config import load_config
    from .db import Submission, get_session, init_db
    from .engine.assist import build_prompt, parse_answer, resolve, review_items

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    init_db()
    names = {b.slug: b.name for b in all_brokers()}
    with get_session() as session:
        items = review_items(session)
        from sqlmodel import select

        contacted = {
            s.broker_slug: names.get(s.broker_slug, s.broker_slug)
            for s in session.exec(select(Submission))
            if s.submitted_at
        }
        if apply:
            text = sys.stdin.read() if apply == "-" else open(apply, encoding="utf-8").read()
            refs = {it.ref: it for it in items}
            verdicts, problems = parse_answer(text, set(refs), set(contacted))
            for v in verdicts:
                try:
                    msg = resolve(
                        session, refs[v.ref].row, v.outcome, v.note, "claude", broker=v.broker
                    )
                    console.print(f"  [green]{v.ref}[/green] {msg}")
                except ValueError as exc:
                    console.print(f"  [yellow]{v.ref}[/yellow] {exc}")
            for p in problems:
                console.print(f"  [yellow]{p}[/yellow]")
            return
        if not items:
            console.print("[green]Nothing to review.[/green]")
            return
        if prompt:
            print(build_prompt(items, config.profile, contacted))
            return
        for it in items:
            r = it.row
            who = names.get(r.broker_slug or "", r.broker_slug or "unmatched")
            console.print(
                f"[bold]{it.ref}[/bold] {who}  [dim]{r.from_addr} · {r.subject[:70]}[/dim]  "
                f"guess: {r.kind}"
            )
        console.print(
            "\nSort them on the dashboard, or ask Claude: [cyan]optout review --prompt[/cyan]"
        )


@replies_app.command("senders")
def replies_senders() -> None:
    """Which addresses each broker has replied from."""
    from .brokers.registry import all_brokers
    from .db import get_session, init_db
    from .engine.inbox import senders_by_broker

    init_db()
    names = {b.slug: b.name for b in all_brokers()}
    with get_session() as session:
        found = senders_by_broker(session)
    if not found:
        console.print("No replies recorded yet. Run: optout inbox")
        return
    table = Table(show_header=True, header_style="bold", box=box.SIMPLE_HEAD)
    table.add_column("Broker", style="bold")
    table.add_column("Replied from")
    for slug, addrs in sorted(found.items(), key=lambda kv: names.get(kv[0], kv[0]).lower()):
        table.add_row(names.get(slug, slug), "\n".join(addrs))
    console.print(table)


@replies_app.command("share")
def replies_share() -> None:
    """Print known-reply entries learned from your sorted replies, without personal details."""
    import yaml

    from .config import load_config
    from .db import get_session, init_db
    from .engine.assist import learned_entries

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    init_db()
    with get_session() as session:
        entries = learned_entries(session, config.profile)
    if not entries:
        console.print("Nothing learned yet. Sort some replies first.")
        return
    print(yaml.safe_dump(entries, sort_keys=False, allow_unicode=True))
    console.print(
        "[dim]Check it has nothing personal, then contribute it: add it to "
        "src/optout/data/known_replies.yml, or open an issue at "
        "https://github.com/Doaky/optout/issues[/dim]"
    )


forms_app = typer.Typer(
    help="Web forms. Normal flow: `forms prompt` -> paste it into your AI browser helper "
    "(Claude in "
    "Chrome) -> paste its answer into `forms apply`. Once a form has saved steps, `forms run` "
    "fills it for you and you only solve the CAPTCHA. `forms status` shows which are waiting on "
    "a confirmation email."
)
app.add_typer(
    forms_app,
    name="forms",
    help="Web forms: get a prompt for your AI browser helper, apply its answer, "
    "fill them again from saved steps.",
)


def _forms_config():
    from .config import load_config

    try:
        return load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


@forms_app.command("todo")
def forms_todo() -> None:
    """For agents: JSON of requests waiting on a web form. Includes broker email text."""
    import json

    from . import forms
    from .db import get_session, init_db

    config = _forms_config()
    init_db()
    with get_session() as session:
        print(json.dumps(forms.todo(session, config), indent=2))


@forms_app.command("profile")
def forms_profile() -> None:
    """For agents: JSON of your name, address, phone and so on. Contains your own details."""
    import json
    import sys

    from . import forms

    if sys.stdout.isatty():
        console.print(
            "[yellow]This prints your own details; don't paste it anywhere public.[/yellow]"
        )

    print(json.dumps(forms.profile_fields(_forms_config()), indent=2))


@forms_app.command("save")
def forms_save(
    file: str = typer.Argument("-", help="Saved steps as JSON, or - for stdin."),
) -> None:
    """Save recorded form steps (field names and labels only; refused if they hold your details)."""
    import json
    import sys

    from pydantic import ValidationError

    from . import forms

    config = _forms_config()
    raw = sys.stdin.read() if file == "-" else open(file).read()
    try:
        recipe = forms.Recipe.model_validate(json.loads(raw))
        saved = forms.save_local(recipe, config)
    except (ValueError, ValidationError) as exc:
        console.print(f"[red]Not saved:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(f"Saved form steps for [bold]{saved.broker}[/bold] ({len(saved.steps)} steps).")


@forms_app.command("prompt")
def forms_prompt(
    limit: int = typer.Option(10, "--limit", "-n", help="How many forms per prompt."),
    slugs: list[str] = typer.Argument(None, metavar="[BROKER]...", help="Only these brokers."),
    recheck: bool = typer.Option(
        False, "--recheck", help="Instead: check whether listings are gone (or came back)."
    ),
    tier: str = typer.Option(
        None,
        "--group",
        "--tier",
        help="Only one group from `optout plan` (people-search, form, form-plus), "
        "or `urgent` for time-sensitive items.",
    ),
) -> None:
    """Print one prompt to give an AI browser helper (Claude in Chrome, etc.) for the next forms."""
    from . import forms
    from .db import get_session, init_db

    config = _forms_config()
    init_db()
    if recheck:
        with get_session() as session:
            due = [i for i in forms.recheck_items(session) if not slugs or i["broker"] in slugs]
        if not due:
            console.print("No re-checks are due.", style="dim")
            raise typer.Exit(code=0)
        print(forms.build_recheck_prompt(config, due[:limit]))
        return
    with get_session() as session:
        if tier == "urgent":  # not a plan group: whatever has a clock on it
            items = [i for i in forms.todo(session, config) if i.get("urgent")]
        else:
            items = forms.todo(session, config, tier=tier)
        items = [i for i in items if not slugs or i["broker"] in slugs]
    if not items:
        console.print("Nothing is waiting on a form.", style="dim")
        raise typer.Exit(code=0)
    print(forms.build_prompt(config, items[:limit]))


@forms_app.command("apply")
def forms_apply(
    file: str = typer.Argument("-", help="The agent's JSON answer, or - for stdin."),
) -> None:
    """Apply your AI browser helper's answer: record each outcome and save the steps it used."""
    import json
    import re
    import sys

    from . import forms
    from .db import get_session, init_db

    config = _forms_config()
    raw = sys.stdin.read() if file == "-" else open(file).read()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    try:
        results = json.loads(fenced.group(1) if fenced else raw)
        if not isinstance(results, list):
            raise ValueError("expected a JSON array")
    except ValueError as exc:
        console.print(f"[red]Couldn't read that:[/red] {exc}")
        raise typer.Exit(code=1)
    init_db()
    with get_session() as session:
        for line in forms.apply_results(
            session, config, [r for r in results if isinstance(r, dict)]
        ):
            console.print(line)


@forms_app.command("run")
def forms_run(
    slugs: list[str] = typer.Argument(
        None,
        metavar="[BROKER]...",
        help="Only these brokers (default: every one with saved steps).",
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="How many forms in this run."),
) -> None:
    """Fill queued forms in a browser window, using steps saved earlier; you only solve CAPTCHAs."""
    from . import forms
    from .db import get_session, init_db
    from .engine.browser import launch_browser
    from .engine.form_replay import replay

    config = _forms_config()
    init_db()
    recipes = forms.load_recipes()
    with get_session() as session:
        queue = [
            i["broker"]
            for i in forms.todo(session, config)
            if i["broker"] in recipes and (not slugs or i["broker"] in slugs)
        ][:limit]
        if not queue:
            console.print(
                "No queued forms have saved steps yet. Run `optout forms prompt` first.",
                style="dim",
            )
            raise typer.Exit(code=0)
        console.print(f"Replaying {len(queue)}: {', '.join(queue)}")
        values = forms.profile_fields(config)
        pw = config.playwright.model_copy(update={"headless": False})  # a person must see it
        with launch_browser(pw) as ctx:
            for n, slug in enumerate(queue, 1):
                recipe = recipes[slug]
                console.print(f"[bold]{n}/{len(queue)} {slug}[/bold] {recipe.url}")
                page = ctx.new_page()
                result = replay(page, recipe, values, say=console.print, ask=input)
                saved = forms.save_run(slug, result)
                if result.status == "done":
                    expects = "email_verify" in recipe.needs_human or recipe.then is not None
                    done_note = result.detail or "Form submitted."
                    lines = forms.apply_results(
                        session,
                        config,
                        [
                            {
                                "broker": slug,
                                "outcome": "submitted",
                                "note": f"Replayed: {done_note}",
                                "expects_email": expects,
                            }
                        ],
                    )
                    console.print("  " + lines[0])
                else:
                    console.print(
                        f"  [yellow]{result.status}[/yellow]: {result.detail}"
                        f"  (recorded as {saved})"
                    )
                try:
                    page.close()
                except Exception:
                    pass
        _watch_for_form_emails(config, session)


def _watch_for_form_emails(config, session, minutes: int = 15) -> None:
    """After a run: look for the confirmation emails the forms promised, every minute, until
    they've all come or the time is up. They're clicked for you only if confirm_links is auto."""
    import time

    from . import forms
    from .engine.inbox import gmail_ready, run_sync

    if not forms.waiting_for_email(session) or gmail_ready(config) is not None:
        return
    mode = "and confirming them" if config.confirm_links == "auto" else "(confirm_links is 'ask')"
    console.print(f"Watching your inbox for confirmation emails {mode}. Ctrl-C to stop.")
    end = time.monotonic() + minutes * 60
    try:
        while time.monotonic() < end:
            time.sleep(config.email.form_check_seconds)
            try:
                report = run_sync(config)
            except ValueError as exc:
                console.print(f"[yellow]{exc}[/yellow]")
                return
            session.expire_all()
            left = forms.waiting_for_email(session)
            console.print(
                f"  checked: {left} still waiting"
                + (f", confirmed {report.confirmed}" if report.confirmed else "")
            )
            if not left:
                return
    except KeyboardInterrupt:
        pass


@forms_app.command("status")
def forms_status(
    all_: bool = typer.Option(
        False, "--all", help="Also list forms that never promised a confirmation email."
    ),
) -> None:
    """Web forms you've submitted and whether their confirmation email arrived.

    (`status` covers every request.)
    """
    from . import forms
    from .db import get_session, init_db

    init_db()
    with get_session() as session:
        rows = forms.stats(session)
    if not rows:
        console.print("No forms recorded yet.", style="dim")
        return
    from .brokers.registry import all_brokers

    names = {b.slug: b.name for b in all_brokers()}
    submitted = sum(r["forms"] for r in rows)
    promised = sum(r["expected_email"] for r in rows)
    arrived = sum(r["followups"] for r in rows)
    missing = [r for r in rows if r["waiting"]]
    shown = [r for r in rows if all_ or r["expected_email"] or r["waiting"]]
    console.print(
        "[dim]Submitted = times the form was sent. Email promised = the form said a "
        "confirmation email would follow. Email arrived = we matched it in your inbox.[/dim]"
    )
    table = Table(box=box.SIMPLE)
    for col in ("Broker", "Submitted", "Email promised", "Email arrived", "Next"):
        table.add_column(col)
    for r in shown:
        table.add_row(
            names.get(r["broker"], r["broker"]) + (f" [dim]({r['broker']})[/dim]" if all_ else ""),
            str(r["forms"]),
            str(r["expected_email"]),
            str(r["followups"]),
            "check inbox" if r["waiting"] else "",
        )
    console.print(table)
    console.print(
        f"{submitted} forms submitted to {len(rows)} brokers; {promised} promised a "
        f"confirmation email; {arrived} arrived; {len(missing)} still missing"
        + (" (rows marked 'check inbox')." if missing else ".")
    )
    hidden = [r for r in rows if r not in shown]
    if hidden:
        console.print(
            f"{len(hidden)} brokers ({sum(r['forms'] for r in hidden)} forms) that promised no "
            "email are hidden; --all shows them.",
            style="dim",
        )


@forms_app.command("stats", hidden=True)  # the old name
def forms_stats() -> None:
    """Old name for `forms status`."""
    forms_status(all_=False)


@forms_app.command("show")
def forms_show(
    slug: str = typer.Argument(..., metavar="BROKER", help="Broker ID, e.g. spycloud."),
) -> None:
    """Print the saved form steps for one broker."""
    import yaml

    from . import forms

    recipe = forms.load_recipes().get(slug)
    if recipe is None:
        console.print(f"No saved steps for '{slug}'.")
        raise typer.Exit(code=1)
    print(yaml.safe_dump(recipe.model_dump(), sort_keys=False, allow_unicode=True))


@forms_app.command("done")
def forms_done(
    slug: str = typer.Argument(
        ..., metavar="BROKER", help="Broker ID, e.g. spycloud (see `optout brokers list`)."
    ),
    outcome: str = typer.Argument(..., help="removed (the page said so) or submitted"),
    note: str = typer.Option("", "--note", help="What the page said, for your records."),
) -> None:
    """Record by hand how a form went, so the request leaves Needs you."""
    from . import forms
    from .db import get_session, init_db

    init_db()
    try:
        with get_session() as session:
            console.print(forms.mark_outcome(session, slug, outcome, note))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


@forms_app.command("share")
def forms_share() -> None:
    """Print your saved form steps that the shared list lacks, ready to contribute."""
    import yaml

    from . import forms

    entries = forms.learned_recipes()
    if not entries:
        console.print("No new form steps to share.")
        return
    print(yaml.safe_dump(entries, sort_keys=False, allow_unicode=True))
    console.print("[dim]Add to src/optout/data/form_recipes.yml, or open an issue.[/dim]")


state_app = typer.Typer(
    help="Encrypted copy of your history, to sync with a scheduled GitHub Actions run."
)
app.add_typer(state_app, name="state")


def _state_call(fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


@state_app.command("push")
def state_push(
    remote: str = typer.Option("origin", help="Git remote of your own (private) repo."),
) -> None:
    """Encrypt your state (needs OPTOUT_STATE_KEY) and put it on the optout-state branch."""
    from pathlib import Path

    from . import state

    size = _state_call(state.push_state, Path.cwd(), remote)
    console.print(f"Pushed the encrypted state ({size // 1024} KB) to {remote}/{state.BRANCH}.")


@state_app.command("pull")
def state_pull(
    remote: str = typer.Option("origin", help="Git remote of your own (private) repo."),
    allow_empty: bool = typer.Option(
        False, "--allow-empty", help="No saved state yet is fine (start fresh)."
    ),
) -> None:
    """Replace your local state with the one on the optout-state branch."""
    from pathlib import Path

    from . import state

    restored = _state_call(state.pull_state, Path.cwd(), remote)
    if restored is None:
        if allow_empty:
            console.print("No saved state yet; starting fresh.")
            return
        console.print(
            f"[red]No {state.BRANCH} branch on {remote}.[/red] Run `optout state push` from "
            "the computer you've been using first, so nobody is emailed twice."
        )
        raise typer.Exit(code=1)
    console.print(f"Restored: {', '.join(restored)}")


@state_app.command("export")
def state_export(path: str) -> None:
    """Write the encrypted state to a file (needs OPTOUT_STATE_KEY)."""
    from pathlib import Path

    from . import state

    size = _state_call(state.export_state, Path(path).expanduser())
    console.print(f"Wrote {path} ({size // 1024} KB, encrypted).")


@state_app.command("import")
def state_import(path: str) -> None:
    """Replace your local state with an encrypted state file."""
    from pathlib import Path

    from . import state

    restored = _state_call(state.import_state, Path(path).expanduser())
    console.print(f"Restored: {', '.join(restored)}")


@app.command()
def report(
    out: str = typer.Option(None, "--out", "-o", help="Where to write the HTML file."),
    open_it: bool = typer.Option(True, "--open/--no-open", help="Open it in your browser."),
) -> None:
    """Write a static status page (no server). Contains broker names and dates, not your details."""
    import os
    from datetime import UTC, datetime
    from pathlib import Path

    from .actions import list_drafts
    from .brokers.registry import load_registry
    from .config import load_config
    from .db import db_path, get_session, init_db
    from .web import render
    from .web.data import collect

    try:
        config = load_config()
    except FileNotFoundError:
        config = None
    load_registry()
    init_db()
    with get_session() as session:
        d = collect(session, config, datetime.now(UTC), drafts=len(list_drafts()))
    path = Path(out).expanduser() if out else db_path().parent / "report.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(render.dashboard(d, interactive=False))
    console.print(f"Report written: {path}")
    if open_it:
        from .web.server import open_in_browser

        if not open_in_browser(path.as_uri()):
            console.print("[dim]Open that file in your browser.[/dim]")


@app.command()
def ui(
    port: int = typer.Option(8765, "--port", help="Port on 127.0.0.1."),
    open_it: bool = typer.Option(True, "--open/--no-open", help="Open your browser."),
) -> None:
    """Start the local web interface (127.0.0.1 only; open the link it prints)."""
    from .web.server import serve

    serve(port=port, open_browser=open_it)


@app.command()
def reply(
    broker: str = typer.Argument(..., help="Broker slug."),
    outcome: str = typer.Argument(..., help="deleted | denied | need-info | auto-reply"),
    note: str = typer.Option(None, "--note", "-n", help="Short note. Don't put PII here."),
) -> None:
    """Record what a broker answered. 'deleted' is the broker's word, not verified removal."""
    from .actions import record_reply
    from .db import get_session, init_db

    init_db()
    try:
        with get_session() as session:
            msg = record_reply(session, broker, outcome, note)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]{broker}[/green]: {msg}")


@app.command("smtp-password")
def smtp_password() -> None:
    """Save your email app password to a private file (typed hidden, never on a command line)."""
    from .engine.methods.email import save_smtp_password

    first = typer.prompt("App password", hide_input=True)
    again = typer.prompt("Again", hide_input=True)
    if not first.strip() or first != again:
        console.print("[red]Empty or mismatched; nothing saved.[/red]")
        raise typer.Exit(code=1)
    path = save_smtp_password(first)
    console.print(f"[green]Saved[/green] to {path} (readable only by you).")


@app.command()
def daily() -> None:
    """Cron entry point: send the day's capped emails (if allowed), draft escalations, notify."""
    from datetime import UTC, datetime

    from .brokers.registry import all_brokers, load_registry
    from .config import load_config, user_tz
    from .db import get_session, init_db
    from .engine.campaign import run_campaign, select_brokers, sent_today
    from .notify import send_webhook

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    load_registry()
    init_db()
    now = datetime.now(UTC)
    lines: list[str] = []

    with get_session() as session:
        sel = select_brokers(
            all_brokers(),
            session,
            now=now,
            my_groups=config.my_groups,
            resend_after_days=config.email.resend_after_days,
        )
        quota = max(0, config.email.daily_limit - sent_today(session, now, user_tz(config)))
        if config.email.allow_unattended:
            report = run_campaign(session, config, sel.eligible, limit=quota)
            lines.append(f"Emails sent: {len(report.sent)}, failed: {len(report.failed)}")
            if report.stopped_reason:
                lines.append(f"Stopped: {report.stopped_reason}")
        elif sel.eligible:
            lines.append(f"{len(sel.eligible)} brokers ready. Run: optout campaign --send")

        from .engine import followups

        overdue = followups.due(session, {b.slug: b for b in all_brokers()}, now)
        if overdue and config.email.allow_unattended:
            fr = followups.send(session, config, overdue, limit=len(overdue))
            lines.append(f"Follow-ups sent: {len(fr.sent)}, failed: {len(fr.failed)}")
            if fr.stopped_reason:
                lines.append(f"Follow-ups stopped: {fr.stopped_reason}")
        elif overdue:
            lines.append(f"{len(overdue)} follow-up(s) due. Run: optout followups --send")

    from .engine.inbox import gmail_ready, run_sync

    if gmail_ready(config) is None:
        try:
            rep = run_sync(config)
            if rep.applied or rep.review:
                lines.append(f"Replies: {len(rep.applied)} applied, {rep.review} to review")
        except ValueError as exc:
            lines.append(str(exc))

    drafts = _write_escalation_drafts(config, now)
    if drafts:
        lines.append(f"{len(drafts)} escalation draft(s) waiting: optout escalate")
    message = "\n".join(lines) or "Nothing to do today."
    console.print(message)
    if config.notify.webhook_url and lines:
        send_webhook(config.notify.webhook_url, "optout daily", message)


statutes_app = typer.Typer(help="The state laws requests cite, checked against official text.")
app.add_typer(statutes_app, name="statutes")


@statutes_app.command("list")
def statutes_list() -> None:
    """Which states' laws are cited, and when each was last checked."""
    from .utils.statute_check import entries

    table = Table(box=box.SIMPLE)
    for col in ("State", "Law", "Citation", "Deadline", "Checked"):
        table.add_column(col)
    for e in entries():
        table.add_row(
            e["state"],
            e["name"],
            e["citation"],
            f"{e['response_days']} days" if e.get("response_days") else "not claimed",
            str(e.get("verified", "no")),
        )
    console.print(table)
    console.print("[dim]Other states get general wording, never an unchecked citation.[/dim]")


@statutes_app.command("verify")
def statutes_verify() -> None:
    """Fetch each state's official text and confirm every quote is there word for word."""
    from .utils.statute_check import verify

    bad = 0
    for r in verify():
        if r.problems:
            bad += 1
            console.print(f"[red]✗ {r.state} {r.key}[/red]")
            for p in r.problems:
                console.print(f"    {p}")
        else:
            console.print(f"[green]✓[/green] {r.state} {r.key}")
    if bad:
        console.print(
            f"[red]{bad} law(s) didn't match their official text. Fix data/statutes.yml.[/red]"
        )
        raise typer.Exit(code=1)


@app.command()
def plan() -> None:
    """The order to work through brokers in, by what each one takes, with progress."""
    from .brokers.registry import all_brokers, load_registry
    from .db import get_session, init_db
    from .plan import summarise

    load_registry()
    init_db()
    with get_session() as session:
        tiers = summarise(session, all_brokers())
    table = Table(box=box.SIMPLE)
    for col in ("", "Group", "Done", "Awaiting reply", "Needs you", "Not started", "How"):
        table.add_column(col, overflow="fold" if col == "How" else "ellipsis")
    how = {
        "people-search": "optout forms prompt --group people-search",
        "email": "optout campaign --group email --send",
        "email-confirm": "optout campaign --group email-confirm --send",
        "follow-up": "optout review (answer what they asked)",
        "form": "optout forms prompt --group form",
        "form-plus": "optout forms prompt --group form-plus",
        "too-much": "nothing to run: they ask for ID or device IDs",
    }
    for n, t in enumerate(tiers, 1):
        table.add_row(
            str(n),
            f"{t.title}\n[dim]{t.hint}[/dim]",
            f"{t.done}/{t.total}",
            str(t.under_way),
            str(len(t.todo)),
            how[t.key],
        )
    console.print(table)


@app.command()
def followups(
    send: bool = typer.Option(False, "--send", help="Actually send. Default is a preview."),
) -> None:
    """Re-email every broker that is past its deadline with no answer, in one batch."""
    from datetime import UTC, datetime

    from .brokers.registry import all_brokers, load_registry
    from .config import load_config
    from .db import get_session, init_db
    from .engine import followups as fu

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    load_registry()
    init_db()
    with get_session() as session:
        items = fu.due(session, {b.slug: b for b in all_brokers()}, datetime.now(UTC))
        if not items:
            console.print("No follow-ups are due: nobody is past their deadline without an answer.")
            return
        for sub, broker in items:
            console.print(f"  {broker.name}  [dim]→ {broker.opt_out_email}[/dim]")
        if not send:
            first = fu.build(config, *items[0])
            console.print(f"\n[bold]{len(items)}[/bold] due. The first one reads:\n")
            console.print(f"Subject: {first['Subject']}\n\n{first.get_content()}", markup=False)
            console.print("[dim]Send them with: optout followups --send[/dim]")
            return
        report = fu.send(session, config, items, limit=len(items))
    console.print(f"Sent {len(report.sent)}, failed {len(report.failed)}.")
    if report.stopped_reason:
        console.print(f"[yellow]Stopped:[/yellow] {report.stopped_reason}")
    _read_replies_after_batch(config, len(report.sent))


@app.command("drift-check")
def drift_check(
    notify: bool = typer.Option(
        True, "--notify/--no-notify", help="Send the alert webhook for broken pages."
    ),
) -> None:
    """Check every web-form broker's page still matches our steps; report the ones that broke."""
    from .brokers.registry import all_brokers, load_registry
    from .config import PlaywrightConfig, load_config
    from .db import get_session, init_db, is_drifted, record_health
    from .engine.browser import launch_browser
    from .engine.probe import build_plan, run_checks, summarize
    from .notify import send_webhook

    try:
        config = load_config()
        pw = config.playwright
        hook = config.notify.webhook_url
    except FileNotFoundError:
        pw, hook = PlaywrightConfig(), None
    load_registry()
    init_db()
    targets = [b for b in all_brokers() if b.enabled and b.method == "web_form"]

    drifted: list[str] = []
    unknown: list[str] = []
    with launch_browser(pw) as ctx, get_session() as session:
        for b in targets:
            plan = build_plan(b)
            page = ctx.new_page()
            try:
                for pp in plan.pages:
                    try:
                        page.goto(pp.url, timeout=45_000, wait_until="domcontentloaded")
                        page.wait_for_timeout(6000)
                        pp.title = page.title()
                    except Exception as exc:
                        pp.load_error = str(exc).splitlines()[0][:200]
                        continue
                    run_checks(page, pp)
            finally:
                page.close()
            ok, detail = summarize(plan)
            if ok is None:
                unknown.append(b.slug)
                console.print(f"  [yellow]unknown[/yellow]  {b.slug}: {detail}")
                continue
            record_health(session, b.slug, "probe", ok, detail[:200])
            console.print(f"  {'[green]ok[/green]     ' if ok else '[red]broken[/red] '}  {b.slug}")
            if is_drifted(session, b.slug):
                drifted.append(b.slug)

    if drifted:
        console.print(
            "\n[red bold]Pages that changed (failed 3 checks in a row):[/red bold] "
            + ", ".join(drifted)
        )
        if notify and hook:
            sent = send_webhook(
                hook, "optout: broker drift", "Drifted brokers: " + ", ".join(drifted)
            )
            console.print("Webhook sent." if sent else "[yellow]Webhook failed.[/yellow]")
    else:
        console.print("\nNo broker page has changed.")
    if unknown:
        console.print(
            f"[dim]{len(unknown)} could not be read (challenge pages); not counted.[/dim]"
        )


def _write_escalation_drafts(config, now) -> list[tuple[str, str, str]]:
    from .actions import write_escalation_drafts

    return write_escalation_drafts(config, now)


@app.command()
def escalate(
    mark_sent: str | None = typer.Option(
        None, "--mark-sent", help="Record that you sent/filed this draft: SLUG:KIND."
    ),
    draft: str | None = typer.Option(
        None, "--draft", help="Write a draft now: SLUG:followup|appeal|complaint|clarify."
    ),
) -> None:
    """Write follow-up / appeal / complaint drafts for unanswered requests. Never sends."""
    from datetime import UTC, datetime

    from .brokers.registry import load_registry
    from .config import load_config
    from .db import get_session, init_db, list_submissions, record_event

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    load_registry()
    init_db()
    now = datetime.now(UTC)

    if draft:
        from .actions import write_draft_now

        slug, _, kind = draft.partition(":")
        try:
            path = write_draft_now(config, slug, kind, now)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
        console.print(f"Draft written: {path}")
        raise typer.Exit(code=0)

    if mark_sent:
        slug, _, kind = mark_sent.partition(":")
        if kind not in ("followup", "appeal", "complaint", "clarify"):
            console.print("[red]Use --mark-sent SLUG:followup|appeal|complaint|clarify[/red]")
            raise typer.Exit(code=1)
        with get_session() as session:
            subs = list_submissions(session, broker_slug=slug)
            if not subs:
                console.print(f"[red]No request on record for {slug}.[/red]")
                raise typer.Exit(code=1)
            latest = max(subs, key=lambda x: x.created_at)
            record_event(session, latest, "escalation_sent", {"kind": kind})
        console.print(f"Recorded: {kind} for {slug}.")
        raise typer.Exit(code=0)

    written = _write_escalation_drafts(config, now)
    count = len(written)
    for kind, name, path in written:
        console.print(f"  [yellow]{kind:<9}[/yellow] {name}  →  {path}")
    if not count:
        console.print("[green]No request is overdue or told no right now.[/green]")
    else:
        console.print(
            f"\n{count} draft(s) written. Review, send/file them yourself, then run "
            "[cyan]optout escalate --mark-sent SLUG:KIND[/cyan]."
        )


@app.command()
def status(
    broker: str | None = typer.Option(None, "--broker", "-b", help="Filter by broker slug."),
    filter_status: str | None = typer.Option(
        None, "--status", "-s", help="Filter by status value."
    ),
) -> None:
    """Show submission statuses, deadlines, and confirmations."""
    from collections import Counter

    from .brokers.registry import load_registry
    from .db import db_path, get_session, list_submissions

    if not db_path().exists():
        console.print("[yellow]No database found.[/yellow] Run [bold]optout init[/bold] first.")
        raise typer.Exit(code=0)

    # Load registry for name lookup — best-effort, fall back to slug
    try:
        load_registry()
        from .brokers.registry import get_broker as _get_broker
    except Exception:
        _get_broker = None  # type: ignore[assignment]

    from .db import is_drifted, latest_verification

    verif: dict = {}
    drift: set[str] = set()
    with get_session() as session:
        subs = list_submissions(session, broker_slug=broker, status=filter_status)
        for sub in subs:
            v = latest_verification(session, sub.id)
            if v:
                verif[sub.id] = v
            if sub.broker_slug not in drift and is_drifted(session, sub.broker_slug):
                drift.add(sub.broker_slug)

    if not subs:
        parts = []
        if broker:
            parts.append(f"broker '{broker}'")
        if filter_status:
            parts.append(f"status '{filter_status}'")
        qualifier = " for " + " and ".join(parts) if parts else ""
        console.print(f"[yellow]No submissions found{qualifier}.[/yellow]")
        console.print("Run [cyan]optout queue[/cyan] to add brokers to the queue.")
        raise typer.Exit(code=0)

    # ── Build table ───────────────────────────────────────────────────────
    table = Table(
        show_header=True,
        header_style="bold",
        box=box.SIMPLE_HEAD,
        show_lines=False,
    )
    table.add_column("Broker", style="bold", min_width=16)
    table.add_column("Status", min_width=16)
    table.add_column("Submitted", min_width=8)
    table.add_column("Deadline", min_width=8)
    table.add_column("Days Left", justify="right")
    table.add_column("Last check")

    counts: Counter[str] = Counter()
    overdue_slugs: list[str] = []
    needs_input_slugs: list[str] = []
    failure_rows: list[tuple[str, str]] = []

    for sub in sorted(subs, key=lambda s: s.created_at):
        # Broker name / method
        broker_name = sub.broker_slug
        if _get_broker is not None:
            try:
                b = _get_broker(sub.broker_slug)
                broker_name = b.name
            except KeyError:
                pass

        ss = _status_style(sub.status)
        days_cell = _days_left_cell(sub)

        table.add_row(
            broker_name,
            f"[{ss}]{_status_label(sub.status)}[/{ss}]",
            _fmt_date(sub.submitted_at),
            _fmt_date(sub.deadline_at),
            days_cell,
            (
                f"{verif[sub.id].outcome.replace('_', ' ')} {_fmt_date(verif[sub.id].checked_at)}"
                if sub.id in verif
                else "—"
            )
            + ("  [red]⚠ drift[/red]" if sub.broker_slug in drift else ""),
        )

        counts[sub.status] += 1

        if "OVERDUE" in days_cell:
            overdue_slugs.append(broker_name)
        if sub.status == "awaiting_user_input":
            needs_input_slugs.append(broker_name)
        if sub.status == "failed" and sub.failure_reason:
            failure_rows.append((broker_name, sub.failure_reason))

    console.print(table)

    # ── Summary line ──────────────────────────────────────────────────────
    summary_parts: list[str] = []
    for st, count in sorted(counts.items()):
        style = _status_style(st)
        summary_parts.append(f"[{style}]{count} {_status_label(st)}[/{style}]")
    console.print("  " + "  ·  ".join(summary_parts))

    # ── Callouts ──────────────────────────────────────────────────────────
    if overdue_slugs:
        console.print(
            f"\n  [red bold]⚠[/red bold]  "
            f"{len(overdue_slugs)} submission(s) past their statutory deadline "
            f"({', '.join(overdue_slugs)}).\n"
            f"     Run [cyan]optout followups[/cyan] to email them again. If they still don't\n"
            f"     answer, you can complain to your state attorney general (in California:\n"
            f"     [link=https://cppa.ca.gov]cppa.ca.gov[/link])."
        )
    if needs_input_slugs:
        console.print(
            f"\n  [yellow bold]⚠[/yellow bold]  "
            f"{len(needs_input_slugs)} submission(s) need you "
            f"({', '.join(needs_input_slugs)}).\n"
            "     Open [cyan]optout ui[/cyan] and look under Needs you to see what each one\n"
            "     asked for."
        )
    if failure_rows:
        console.print("\n  [bold]Failure details:[/bold]")
        for name, reason in failure_rows:
            console.print(f"    [dim]{name}:[/dim] {reason}")


# ---------------------------------------------------------------------------
# optout monitor
# ---------------------------------------------------------------------------


@app.command()
def monitor() -> None:
    """One-shot re-scan; safe to cron."""
    from .config import load_config
    from .monitoring import run_monitor
    from .wizard import assert_own_identity

    try:
        config = load_config()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    assert_own_identity(config.profile.legal_name)

    console.print("[bold]Running monitor scan…[/bold]\n")
    summary = run_monitor(config)

    table = Table(show_header=True, header_style="bold", box=box.SIMPLE_HEAD)
    table.add_column("Broker", style="bold", min_width=16)
    table.add_column("Result", min_width=14)
    table.add_column("Re-queued", justify="center")

    for report in summary.reports:
        if report.result == "skipped":
            result_cell = f"[dim]skipped — {report.skipped_reason}[/dim]"
        elif report.result == "found":
            result_cell = "[red bold]FOUND[/red bold]"
        elif report.result == "clean":
            result_cell = "[green]clean[/green]"
        else:
            result_cell = "[yellow]inconclusive[/yellow]"

        requeued_cell = "[green]✓[/green]" if report.requeued else ""
        table.add_row(report.name, result_cell, requeued_cell)

    console.print(table)

    parts: list[str] = []
    if summary.scanned:
        parts.append(f"[bold]{summary.scanned}[/bold] scanned")
    if summary.found:
        parts.append(f"[red]{summary.found} found[/red]")
    if summary.clean:
        parts.append(f"[green]{summary.clean} clean[/green]")
    if summary.inconclusive:
        parts.append(f"[yellow]{summary.inconclusive} inconclusive[/yellow]")
    if summary.requeued:
        parts.append(f"[cyan]{summary.requeued} re-queued[/cyan]")
    console.print("  " + "  ·  ".join(parts))

    if summary.requeued:
        console.print(
            f"\n  [cyan]→[/cyan]  {summary.requeued} broker(s) re-queued for resubmission.\n"
            "     Run [cyan]optout submit[/cyan] to process them."
        )


# ---------------------------------------------------------------------------
# optout doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor() -> None:
    """Check your setup and whether each broker site is reachable (uses the network, slow)."""
    import sys
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Literal

    @dataclass
    class Check:
        name: str
        status: Literal["pass", "warn", "fail"]
        detail: str = ""
        hint: str = ""

    results: list[Check] = []

    # ── 1. Python version ────────────────────────────────────────────────────
    vi = sys.version_info
    vs = f"{vi.major}.{vi.minor}.{vi.micro}"
    if vi >= (3, 12):
        results.append(Check("Python ≥ 3.12", "pass", vs))
    else:
        results.append(
            Check(
                "Python ≥ 3.12",
                "fail",
                vs,
                "Install Python 3.12+ — see python.org or use pyenv.",
            )
        )

    # ── 2. Browser for automatic form filling (optional) ──────────────────────
    _form_hint = (
        "Only needed to fill web forms automatically. Run: playwright install chromium "
        "(or install Google Chrome)."
    )
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
        if Path(exe).exists():
            results.append(Check("Form-filling browser", "pass", exe))
        else:
            results.append(Check("Form-filling browser", "warn", f"Not found at {exe}", _form_hint))
    except Exception as exc:
        results.append(Check("Form-filling browser", "warn", str(exc)[:80], _form_hint))

    # ── 3. Config file ───────────────────────────────────────────────────────
    from .config import config_path

    _cp = config_path()
    try:
        from .config import load_config

        load_config()
        results.append(Check("Config file", "pass", str(_cp)))
    except FileNotFoundError:
        results.append(
            Check(
                "Config file",
                "fail",
                f"Not found: {_cp}",
                "Run: optout init",
            )
        )
    except Exception as exc:
        results.append(
            Check(
                "Config file",
                "fail",
                str(exc)[:80],
                "Run: optout init",
            )
        )

    # ── 3b. Email login (sends nothing) ──────────────────────────────────────
    try:
        from .config import load_config as _load
        from .engine.methods.email import check_login, read_smtp_password

        _cfg = _load()
        _smtp = _cfg.email.smtp
        if _cfg.email.method == "smtp" and _smtp is not None:
            _pw = read_smtp_password(_smtp)
            if not _pw:
                results.append(
                    Check(
                        "Email login",
                        "warn",
                        "No app password saved yet",
                        "Run: optout smtp-password  (or save it in Settings in optout ui)",
                    )
                )
            elif (_why := check_login(_smtp, _pw)) is None:
                results.append(Check("Email login", "pass", f"{_smtp.username} @ {_smtp.host}"))
            else:
                results.append(
                    Check(
                        "Email login",
                        "fail",
                        _why,
                        "Check the app password: optout smtp-password",
                    )
                )
    except FileNotFoundError:
        pass  # the config check above already says what to do
    except Exception as exc:
        results.append(Check("Email login", "fail", str(exc)[:80], "Run: optout smtp-password"))

    # ── 4. Database schema ───────────────────────────────────────────────────
    from .db import db_path as _db_path

    _dp = _db_path()
    try:
        if not _dp.exists():
            results.append(
                Check(
                    "Database",
                    "warn",
                    f"Not yet created — {_dp}",
                    "It's created the first time you run optout plan or optout ui.",
                )
            )
        else:
            from sqlmodel import Session, select

            from .db import Submission, get_engine

            _eng = get_engine(_dp)
            with Session(_eng) as _s:
                _s.exec(select(Submission).limit(1))
            results.append(Check("Database schema", "pass", str(_dp)))
    except Exception as exc:
        results.append(
            Check(
                "Database schema",
                "fail",
                str(exc)[:80],
                "Schema may be stale. Back it up (optout state export), then run optout ui to "
                "upgrade it.",
            )
        )

    # ── 5. Browser profile dir ───────────────────────────────────────────────
    from .config import config_dir

    _pd = config_dir() / "browser_profile"
    try:
        _pd.mkdir(parents=True, exist_ok=True)
        _probe = _pd / ".doctor_probe"
        _probe.write_text("ok")
        _probe.unlink()
        results.append(Check("Browser profile dir", "pass", str(_pd)))
    except Exception as exc:
        results.append(
            Check(
                "Browser profile dir",
                "fail",
                str(exc),
                f"Check permissions on {_pd}",
            )
        )

    # ── 6. Artifacts dir ─────────────────────────────────────────────────────
    from .db import artifacts_dir as _artifacts_dir

    _ad = _artifacts_dir()
    try:
        _probe2 = _ad / ".doctor_probe"
        _probe2.write_text("ok")
        _probe2.unlink()
        results.append(Check("Artifacts dir", "pass", str(_ad)))
    except Exception as exc:
        results.append(
            Check(
                "Artifacts dir",
                "fail",
                str(exc),
                f"Check permissions on {_ad}",
            )
        )

    # ── 7. Broker YAMLs ──────────────────────────────────────────────────────
    from .brokers.loader import load_broker
    from .brokers.registry import _brokers_dir

    _bd = _brokers_dir()
    try:
        _yamls = sorted(_bd.glob("*.yml"))
        _errors: list[str] = []
        for _ypath in _yamls:
            try:
                load_broker(_ypath)
            except Exception as exc:
                _errors.append(f"{_ypath.stem}: {exc}")
        _n = len(_yamls)
        if _n == 0:
            results.append(Check("Broker YAMLs", "warn", f"No YAMLs found in {_bd}"))
        elif _errors:
            results.append(
                Check(
                    f"Broker YAMLs ({_n})",
                    "fail",
                    "; ".join(_errors),
                )
            )
        else:
            results.append(Check(f"Broker YAMLs ({_n})", "pass", "all valid"))
    except Exception as exc:
        results.append(Check("Broker YAMLs", "fail", str(exc)))

    # ── 8. Network reachability ───────────────────────────────────────────────
    try:
        import httpx

        from .brokers.registry import all_brokers

        _net_brokers = [b for b in all_brokers() if b.opt_out_url]
        console.print(
            f"  [dim]Checking network reachability for {len(_net_brokers)} broker(s)…[/dim]"
        )
        for _nb in _net_brokers:
            try:
                _r = httpx.head(
                    _nb.opt_out_url,
                    timeout=5,
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (compatible)"},
                )
                if _r.status_code < 500:
                    results.append(Check(f"{_nb.name} reachable", "pass", f"HTTP {_r.status_code}"))
                else:
                    results.append(
                        Check(
                            f"{_nb.name} reachable",
                            "warn",
                            f"HTTP {_r.status_code}",
                            "Site returned server error — check manually.",
                        )
                    )
            except Exception as exc:
                results.append(
                    Check(
                        f"{_nb.name} reachable",
                        "warn",
                        str(exc)[:60],
                        "Network or DNS issue — check your connection.",
                    )
                )
    except Exception:
        pass

    # ── Render results ────────────────────────────────────────────────────────
    _ICON = {
        "pass": "[green]✅[/green]",
        "warn": "[yellow]⚠️ [/yellow]",
        "fail": "[red]❌[/red]",
    }

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold", padding=(0, 1))
    tbl.add_column("", width=4, no_wrap=True)
    tbl.add_column("Check", style="bold", min_width=28)
    tbl.add_column("Detail", overflow="fold")

    for _res in results:
        _detail_markup = f"[dim]{_res.detail}[/dim]" if _res.detail else ""
        tbl.add_row(_ICON[_res.status], _res.name, _detail_markup)

    console.print()
    console.print(tbl)

    _hints = [(r.name, r.hint) for r in results if r.hint and r.status != "pass"]
    if _hints:
        console.print("[bold]Remediation:[/bold]")
        for _hname, _hint in _hints:
            console.print(f"  [dim]{_hname}[/dim]  →  {_hint}")
        console.print()

    _any_fail = any(r.status == "fail" for r in results)
    _any_warn = any(r.status == "warn" for r in results)

    if _any_fail:
        console.print("[red bold]Some checks failed — see remediation hints above.[/red bold]")
        raise typer.Exit(code=1)
    elif _any_warn:
        console.print("[yellow]All critical checks passed (see warnings above).[/yellow]")
    else:
        console.print("[green bold]All checks passed.[/green bold]")


_PANELS = {
    "Start here": ["plan", "campaign", "inbox", "review", "followups", "status", "history", "ui"],
    "Setup": ["init", "smtp-password", "doctor"],
    "Advanced": [
        "scan",
        "verify",
        "monitor",
        "escalate",
        "log",
        "reply",
        "report",
        "label",
        "daily",
        "queue",
        "submit",
        "state",
    ],
    "Maintainers": ["drift-check", "statutes"],
}
_FORMS_ORDER = ["prompt", "apply", "run", "status", "done"]


def _command_name(info) -> str:
    return info.name or info.callback.__name__.replace("_", "-")


def _organise() -> None:
    """Group the top-level help, and list the forms commands in the order you'd use them."""
    panel_of = {n: p for p, names in _PANELS.items() for n in names}
    order = {n: i for i, n in enumerate(n for names in _PANELS.values() for n in names)}
    for info in app.registered_commands:
        info.rich_help_panel = panel_of.get(_command_name(info), "Advanced")
    app.registered_commands.sort(key=lambda c: order.get(_command_name(c), 999))
    for g in app.registered_groups:
        g.rich_help_panel = "Start here" if g.name == "forms" else "Advanced"
    for info in forms_app.registered_commands:
        info.rich_help_panel = (
            None if _command_name(info) in _FORMS_ORDER else "Plumbing (for agents and sharing)"
        )
    forms_app.registered_commands.sort(
        key=lambda c: (
            _FORMS_ORDER.index(_command_name(c)) if _command_name(c) in _FORMS_ORDER else 99
        )
    )


_organise()


if __name__ == "__main__":
    app()
