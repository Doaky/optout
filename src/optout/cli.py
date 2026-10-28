"""Top-level Typer app and all subcommands."""

from __future__ import annotations

from datetime import UTC

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(name="optout", no_args_is_help=True)
brokers_app = typer.Typer(help="Browse and inspect broker definitions.")
app.add_typer(brokers_app, name="brokers")

console = Console()


@app.callback()
def _main(ctx: typer.Context) -> None:
    """Self-hosted data broker opt-out tool."""
    if ctx.invoked_subcommand is not None:
        console.print("\n[bold]OptOut[/bold]  [dim]·  self-hosted data-broker opt-out[/dim]\n")


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
        "rejected": "red",
        "expired": "dim",
        "failed": "red",
    }.get(status, "white")


_STATUS_LABEL: dict[str, str] = {
    "queued": "Queued",
    "awaiting_user_input": "⚡ Needs input",
    "submitted": "Submitted",
    "awaiting_broker_confirmation": "Submitted ✓",
    "confirmed": "Removed ✓",
    "rejected": "Rejected",
    "expired": "Expired",
    "failed": "Failed",
}


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
    slug: str = typer.Argument(..., help="Broker slug (e.g. spokeo)."),
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
    """Check a broker's selectors against the live site. Never fills or submits."""
    import json
    from dataclasses import asdict
    from datetime import UTC, datetime

    from rich.markup import escape

    from .brokers.registry import get_broker
    from .config import PlaywrightConfig, config_dir, load_config
    from .engine.browser import launch_browser
    from .engine.probe import blocked_reason, build_plan, run_checks

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

    out_dir = config_dir() / "probes"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = out_dir / f"{broker.slug}-{stamp}.json"
    out.write_text(json.dumps(asdict(plan), indent=2))
    console.print(f"\nReport saved to {out}")


_CA_REGISTRY_URL = "https://cppa.ca.gov/data_broker_registry/registry.csv"
_MAX_REGISTRY_BYTES = 20_000_000


@brokers_app.command("import")
def brokers_import(
    csv_path: str | None = typer.Argument(
        None, help="Registry CSV file. Omit to download California's from cppa.ca.gov."
    ),
) -> None:
    """Generate email-method broker definitions from the California registry CSV."""
    import urllib.request
    from datetime import date
    from pathlib import Path

    from .brokers.importer import parse_ca_registry, write_yaml_dir
    from .brokers.registry import imported_dir
    from .db import db_path

    if csv_path:
        path = Path(csv_path).expanduser()
        if not path.is_file():
            console.print(f"[red]No such file: {path}[/red]")
            raise typer.Exit(code=1)
    else:
        # Fixed allowlisted URL only; nothing from inside the registry is ever fetched.
        path = db_path().parent / "registry-cache" / "ca-registry.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        console.print(f"Downloading {_CA_REGISTRY_URL}")
        req = urllib.request.Request(_CA_REGISTRY_URL, headers={"User-Agent": "optout/0.2"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed https URL)
                data = resp.read(_MAX_REGISTRY_BYTES + 1)
        except OSError as exc:
            console.print(f"[red]Download failed: {exc}[/red]")
            raise typer.Exit(code=1)
        if len(data) > _MAX_REGISTRY_BYTES:
            console.print("[red]Registry file is unexpectedly large; aborting.[/red]")
            raise typer.Exit(code=1)
        path.write_bytes(data)

    result = parse_ca_registry(path)
    if not result.brokers:
        console.print("[red]No usable brokers found; is this a registry CSV?[/red]")
        raise typer.Exit(code=1)
    n = write_yaml_dir(result, imported_dir(), "ca-registry", date.today().isoformat())
    exempt = sum(1 for b in result.brokers if b.exemptions)
    merged = sum(len(b.covers) for b in result.brokers)
    console.print(
        f"[green]Imported {n} brokers[/green] to {imported_dir()}\n"
        f"  {exempt} disabled by default (FCRA/GLBA/HIPAA-regulated)\n"
        f"  {merged} more entities merged into a shared contact\n"
        f"  {len(result.skipped)} skipped (no valid email)\n"
        "Nothing has been sent. Next: [cyan]optout campaign[/cyan] previews what would go out."
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
def brokers_disable(slug: str = typer.Argument(..., help="Broker slug to skip.")) -> None:
    """Never contact this broker (e.g. a site you want to stay listed on)."""
    _toggle(slug, False)


@brokers_app.command("enable")
def brokers_enable(slug: str = typer.Argument(..., help="Broker slug to include.")) -> None:
    """Include this broker again (also overrides the FCRA/GLBA default-off)."""
    _toggle(slug, True)


@app.command()
def campaign(
    limit: int = typer.Option(25, "--limit", "-n", help="Max emails this run."),
    only: list[str] = typer.Option(None, "--only", help="Restrict to these broker slugs."),
    send: bool = typer.Option(False, "--send", help="Actually send. Default is preview only."),
) -> None:
    """Email deletion requests to every eligible broker, within daily limits."""
    from datetime import UTC, datetime

    from .brokers.registry import all_brokers
    from .config import load_config
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
        sel = select_brokers(all_brokers(), session, now=now, only=only or None)
        used = sent_today(session, now)
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
    console.print("Track them with [cyan]optout status[/cyan]. Sent does not mean removed.")


@brokers_app.command("info")
def brokers_info(
    slug: str = typer.Argument(..., help="Broker slug (e.g. whitepages)."),
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

    if submission.status == "confirmed":
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

    with get_session() as session:
        subs = list_submissions(session, broker_slug=broker, status=filter_status)

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
            f"     Consider filing a complaint at [link=https://cppa.ca.gov]cppa.ca.gov[/link] "
            f"or contacting your state attorney general."
        )
    if needs_input_slugs:
        console.print(
            f"\n  [yellow bold]⚠[/yellow bold]  "
            f"{len(needs_input_slugs)} submission(s) need your attention "
            f"({', '.join(needs_input_slugs)}).\n"
            f"     Run [cyan]optout submit[/cyan] to retry."
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
    """Run diagnostic checks and report any setup problems."""
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

    # ── 2. Playwright + Chromium ─────────────────────────────────────────────
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
        if Path(exe).exists():
            results.append(Check("Playwright / Chromium", "pass", exe))
        else:
            results.append(
                Check(
                    "Playwright / Chromium",
                    "fail",
                    f"Not found at {exe}",
                    "Run: playwright install chromium",
                )
            )
    except Exception as exc:
        results.append(
            Check(
                "Playwright / Chromium",
                "fail",
                str(exc),
                "Run: playwright install chromium",
            )
        )

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
                    "Run: optout queue  (the DB is created automatically)",
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
                f"Schema may be stale. Delete {_dp} and run: optout queue",
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


if __name__ == "__main__":
    app()
