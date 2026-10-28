"""Interactive init wizard — builds ~/.config/optout/config.yml."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import TypeVar

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from .config import (
    Address,
    EmailConfig,
    EmailSet,
    MonitoringConfig,
    PhoneSet,
    PlaywrightConfig,
    PreviousAddress,
    Profile,
    SmtpConfig,
    UserConfig,
    config_path,
    save_config,
)
from .db import init_db

console = Console()
err = Console(stderr=True)

# Small guard — correct tone, not comprehensive security
_CELEBRITY_NAMES: frozenset[str] = frozenset(
    {
        "taylor swift",
        "elon musk",
        "donald trump",
        "joe biden",
        "barack obama",
        "kamala harris",
        "kim kardashian",
        "beyonce knowles",
        "lebron james",
        "jeff bezos",
        "mark zuckerberg",
        "bill gates",
        "oprah winfrey",
        "michelle obama",
        "george washington",
        "abraham lincoln",
    }
)


def assert_own_identity(legal_name: str) -> None:
    """Raise if name looks like a celebrity. Call at startup for non-init commands too."""
    if legal_name.strip().lower() in _CELEBRITY_NAMES:
        err.print(
            f"[red bold]Error:[/red bold] The configured name '{legal_name}' matches "
            "a well-known public figure. OptOut only operates on behalf of the person "
            "running it. Please re-run [bold]optout init[/bold] with your own name."
        )
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Low-level prompt helpers
# ---------------------------------------------------------------------------

T = TypeVar("T")


def _section(title: str) -> None:
    console.print()
    console.print(Panel(f"[bold]{title}[/bold]", expand=False))


def _retry[T](prompt_fn: Callable[[], T]) -> T:
    """Keep calling prompt_fn until it doesn't raise ValueError."""
    while True:
        try:
            return prompt_fn()
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")


def _ask_date(prompt: str) -> date:
    def _try() -> date:
        raw = Prompt.ask(prompt + " [dim](YYYY-MM-DD)[/dim]")
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise ValueError("Enter a date in YYYY-MM-DD format, e.g. 1990-05-14.")

    return _retry(_try)


def _ask_year(prompt: str, required: bool = True) -> int | None:
    def _try() -> int | None:
        suffix = "" if required else " [dim](blank to skip)[/dim]"
        raw = Prompt.ask(prompt + suffix, default="").strip()
        if not raw:
            if required:
                raise ValueError("Year is required.")
            return None
        try:
            year = int(raw)
        except ValueError:
            raise ValueError("Enter a 4-digit year, e.g. 2015.")
        current = date.today().year
        if not (1900 <= year <= current):
            raise ValueError(f"Year must be between 1900 and {current}.")
        return year

    return _retry(_try)


def _ask_list(prompt: str, *, required: bool = False) -> list[str]:
    hint = (
        "[dim](comma-separated)[/dim]"
        if required
        else "[dim](comma-separated, or blank to skip)[/dim]"
    )
    while True:
        raw = Prompt.ask(f"{prompt} {hint}", default="")
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if required and not items:
            console.print("[red]At least one value is required.[/red]")
            continue
        return items


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _collect_address(label: str) -> Address:
    console.print(f"\n[italic]{label}[/italic]")
    return Address(
        street=Prompt.ask("  Street"),
        street2=Prompt.ask("  Apt / Suite / Unit [dim](optional)[/dim]", default="").strip(),
        city=Prompt.ask("  City"),
        state=Prompt.ask("  State [dim](2-letter)[/dim]").strip().upper(),
        zip=Prompt.ask("  ZIP code"),
    )


def _collect_previous_addresses() -> list[PreviousAddress]:
    addrs: list[PreviousAddress] = []
    if not Confirm.ask("  Add a previous address?", default=False):
        return addrs
    while True:
        console.print(f"\n  [italic]Previous address #{len(addrs) + 1}[/italic]")
        from_year = _ask_year("    Moved in (year)", required=False)
        to_year = _ask_year("    Moved out (year)", required=False)
        addrs.append(
            PreviousAddress(
                street=Prompt.ask("    Street"),
                street2=Prompt.ask(
                    "    Apt / Suite / Unit [dim](optional)[/dim]", default=""
                ).strip(),
                city=Prompt.ask("    City"),
                state=Prompt.ask("    State [dim](2-letter)[/dim]").strip().upper(),
                zip=Prompt.ask("    ZIP code"),
                **{"from": from_year, "to": to_year},
            )
        )
        if not Confirm.ask("  Add another previous address?", default=False):
            break
    return addrs


def _collect_profile() -> Profile:
    _section("Your identity")
    console.print(
        "[dim]This information stays on your machine and is never sent to a server.[/dim]"
    )

    legal_name = Prompt.ask("\nFull legal name")

    # Celebrity guard — refuse immediately
    if legal_name.strip().lower() in _CELEBRITY_NAMES:
        err.print(
            f"\n[red bold]Error:[/red bold] '{legal_name}' matches a known public figure. "
            "This tool operates only on behalf of the person running it."
        )
        raise typer.Exit(code=1)

    # Attestation
    console.print()
    attested = Confirm.ask(
        f"I confirm that [bold]{legal_name}[/bold] is my own legal name and that I am "
        "submitting opt-out requests on my own behalf",
        default=False,
    )
    if not attested:
        console.print("\n[yellow]Aborted. OptOut only submits on behalf of its operator.[/yellow]")
        raise typer.Exit(code=0)

    aliases = _ask_list("Other names you appear under (maiden name, nickname, etc.)")
    dob = _ask_date("Date of birth")
    current_address = _collect_address("Current address")
    previous_addresses = _collect_previous_addresses()

    console.print()
    current_phones = _ask_list("Current phone number(s)", required=True)
    previous_phones = _ask_list("Previous phone number(s)")
    current_emails = _ask_list("Current email address(es)", required=True)
    previous_emails = _ask_list("Previous email address(es)")
    relatives = _ask_list(
        "Names of relatives who may appear on the same listings [dim](helps with matching)[/dim]"
    )

    return Profile(
        legal_name=legal_name,
        aliases=aliases,
        dob=dob,
        current_address=current_address,
        previous_addresses=previous_addresses,
        phones=PhoneSet(current=current_phones, previous=previous_phones),
        emails=EmailSet(current=current_emails, previous=previous_emails),
        relatives=relatives,
    )


def _collect_email_config() -> EmailConfig:
    _section("Email configuration")
    console.print(
        "Some data brokers only accept opt-out requests sent by email.\n"
        "This tool sends those emails [bold]from your own inbox[/bold] so brokers\n"
        "can't ignore them as coming from a third-party service.\n\n"
        "[dim]Your password is never stored in the config file — only the name\n"
        "of an environment variable that you set separately.[/dim]"
    )

    console.print()
    host = Prompt.ask("  Your email provider's outgoing mail server", default="smtp.gmail.com")
    port = int(Prompt.ask("  Port", default="587"))
    username = Prompt.ask("  Your email address")

    if "gmail.com" in host:
        console.print(
            "\n[bold yellow]Gmail requires an App Password — "
            "not your regular password.[/bold yellow]\n"
            "\n"
            "To generate one:\n"
            "  1. Go to [link=https://myaccount.google.com/security]myaccount.google.com/security[/link]\n"
            "  2. Make sure [bold]2-Step Verification[/bold] is turned on\n"
            "  3. Search for [bold]App Passwords[/bold] in the search bar\n"
            "  4. Click App Passwords → create one (name it anything, e.g. 'optout')\n"
            "  5. Google shows a 16-character password — copy it\n"
            "\n"
            "You'll set that password in the next step after the wizard finishes.\n"
        )
    else:
        console.print(
            "\n[dim]Use the password you'd use to log in to your email provider's\n"
            "webmail. Some providers require an 'app password' or 'third-party\n"
            "access' to be enabled in your account settings.[/dim]\n"
        )

    smtp = SmtpConfig(
        host=host,
        port=port,
        username=username,
        password_env="OPTOUT_SMTP_PASSWORD",
    )

    return EmailConfig(method="smtp", smtp=smtp)


def _collect_playwright_config() -> PlaywrightConfig:
    _section("Browser settings")
    console.print(
        "[dim]Headed mode (default) lets you solve CAPTCHAs yourself — "
        "this is intentional and keeps opt-outs legitimate.[/dim]"
    )
    headless = Confirm.ask("\nRun browser in headless mode?", default=False)
    return PlaywrightConfig(headless=headless, slow_mo_ms=0)


def _collect_monitoring_config() -> MonitoringConfig:
    _section("Monitoring")

    def _try() -> MonitoringConfig:
        raw = Prompt.ask("How often to re-scan brokers [dim](days)[/dim]", default="30")
        try:
            days = int(raw)
        except ValueError:
            raise ValueError("Enter a whole number of days.")
        if days < 1:
            raise ValueError("Interval must be at least 1 day.")
        return MonitoringConfig(rescan_interval_days=days)

    return _retry(_try)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_init_wizard() -> None:
    console.print(
        Panel(
            "[bold]OptOut Setup Wizard[/bold]\n"
            "[dim]Builds your personal config at "
            f"{config_path()}[/dim]",
            expand=False,
        )
    )

    # Warn if config already exists
    path = config_path()
    if path.exists():
        console.print(f"\n[yellow]A config already exists at {path}.[/yellow]")
        if not Confirm.ask("Overwrite it?", default=False):
            console.print("Keeping existing config. Done.")
            raise typer.Exit(code=0)

    try:
        profile = _collect_profile()
        email_cfg = _collect_email_config()
        playwright_cfg = _collect_playwright_config()
        monitoring_cfg = _collect_monitoring_config()
    except KeyboardInterrupt:
        console.print("\n\n[yellow]Interrupted. No config was written.[/yellow]")
        raise typer.Exit(code=0)

    cfg = UserConfig(
        profile=profile,
        email=email_cfg,
        playwright=playwright_cfg,
        monitoring=monitoring_cfg,
    )

    # Write config
    save_config(cfg)
    console.print(f"\n[green]Config written to {path} (permissions: 600)[/green]")

    # Initialise the database
    init_db()
    console.print("[green]SQLite database initialised.[/green]")

    smtp_username = cfg.email.smtp.username if cfg.email.smtp else ""
    console.print(
        "\n[bold]Almost done.[/bold] One last step — save your email password so the\n"
        "tool can send emails on your behalf. Run this command in your terminal:\n"
    )
    console.print(
        "  [bold cyan]echo 'export OPTOUT_SMTP_PASSWORD=\"your-password-here\"'"
        " >> ~/.zshrc && source ~/.zshrc[/bold cyan]\n"
    )
    if cfg.email.smtp and "gmail" in smtp_username:
        console.print(
            "  [dim]Replace your-password-here with the 16-character App Password\n"
            "  you generated in Google (not your regular Gmail password).[/dim]\n"
        )
    else:
        console.print("  [dim]Replace your-password-here with your email password.[/dim]\n")
    console.print(
        "[bold]Then you're ready:[/bold]\n"
        "  [cyan]optout brokers list[/cyan]   — see which brokers are supported\n"
        "  [cyan]optout queue[/cyan]           — add brokers to the submission queue\n"
        "  [cyan]optout submit[/cyan]          — submit opt-out requests\n"
        "  [cyan]optout status[/cyan]          — check submission status\n"
    )
