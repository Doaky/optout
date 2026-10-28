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
    _section("Step 1 of 3 · About you")
    console.print(
        "Brokers need enough to find your records. Everything you enter stays in a\n"
        f"private file on this computer ([dim]{config_path()}[/dim]).\n"
    )

    legal_name = Prompt.ask("Full legal name [dim](as it would appear on records)[/dim]").strip()

    # Celebrity guard — refuse immediately
    if legal_name.lower() in _CELEBRITY_NAMES:
        err.print(
            f"\n[red bold]Error:[/red bold] '{legal_name}' matches a known public figure. "
            "This tool operates only on behalf of the person running it."
        )
        raise typer.Exit(code=1)

    attested = Confirm.ask(
        f"Is [bold]{legal_name}[/bold] your own name, and are these requests for yourself?",
        default=False,
    )
    if not attested:
        console.print(
            "\n[yellow]Stopped. This tool only sends requests for the person running it.[/yellow]"
        )
        raise typer.Exit(code=0)

    aliases = _ask_list("Other names you go by [dim](maiden name, nickname)[/dim]")
    dob = _ask_date(
        "Date of birth [dim](only some people-search forms use it; it's never put in emails)[/dim]"
    )
    current_address = _collect_address("Current home address")
    console.print(
        "[dim]Old addresses help brokers find older listings. Your last one or two is plenty.[/dim]"
    )
    previous_addresses = _collect_previous_addresses()

    console.print()
    phones = _ask_list(
        "Phone number(s) [dim](optional; used by people-search forms, not in emails)[/dim]"
    )
    relatives = _ask_list(
        "Relatives who appear on your listings [dim](optional, helps matching)[/dim]"
    )

    return Profile(
        legal_name=legal_name,
        aliases=aliases,
        dob=dob,
        current_address=current_address,
        previous_addresses=previous_addresses,
        phones=PhoneSet(current=phones),
        emails=EmailSet(),  # filled in from the sending address in step 2
        relatives=relatives,
    )


# Known providers: the user never has to type a server name for these.
_PROVIDERS = {
    "gmail": {
        "name": "Gmail",
        "host": "smtp.gmail.com",
        "port": 587,
        "steps": [
            (
                "Turn on 2-Step Verification (Google requires it first)",
                "https://myaccount.google.com/signinoptions/twosv",
            ),
            (
                "Create an app password: name it [bold]optout[/bold], click Create",
                "https://myaccount.google.com/apppasswords",
            ),
        ],
        "shape": "Google shows 16 letters in four groups, like [bold]abcd efgh ijkl mnop[/bold].",
    },
    "icloud": {
        "name": "iCloud Mail",
        "host": "smtp.mail.me.com",
        "port": 587,
        "steps": [
            (
                "Sign in, open Sign-In and Security → App-Specific Passwords, create one",
                "https://account.apple.com",
            ),
        ],
        "shape": "Apple shows a password like [bold]abcd-efgh-ijkl-mnop[/bold].",
    },
}


def _detect_provider(address: str) -> str | None:
    domain = address.rsplit("@", 1)[-1].lower()
    if domain in ("gmail.com", "googlemail.com"):
        return "gmail"
    if domain in ("icloud.com", "me.com", "mac.com"):
        return "icloud"
    return None


def _looks_like_app_password(provider: str | None, pw: str) -> bool:
    raw = pw.replace(" ", "").replace("-", "")
    if provider == "gmail":
        return len(raw) == 16 and raw.isalpha()
    if provider == "icloud":
        return len(raw) == 16 and raw.isalnum()
    return bool(raw)


def _collect_email_config() -> tuple[EmailConfig, bool]:
    """Returns the email config and whether a working password was saved."""
    from .engine.methods.email import check_login, save_smtp_password
    from .web.server import open_in_browser

    _section("Step 2 of 3 · The email account requests are sent from")
    console.print(
        "Requests go out [bold]from your own email account[/bold], so brokers reply to you.\n"
        "Gmail is the easiest: everything is filled in for you.\n"
    )

    def _address() -> str:
        raw = Prompt.ask("Your email address").strip()
        if "@" not in raw or "." not in raw.rsplit("@", 1)[-1] or " " in raw:
            raise ValueError("That doesn't look like an email address.")
        return raw

    address = _retry(_address)
    provider = _detect_provider(address)
    if provider is None and Confirm.ask(
        "Is this a Google Workspace account (your work or school email runs on Gmail)?",
        default=False,
    ):
        provider = "gmail"

    if provider:
        p = _PROVIDERS[provider]
        host, port = str(p["host"]), int(p["port"])  # type: ignore[call-overload]
        console.print(
            f"\n[green]✓[/green] Using {p['name']}'s mail server ([dim]{host}, port {port}[/dim]). "
            "Nothing to change here."
        )
    else:
        console.print(
            "\n[dim]Your provider's outgoing (SMTP) server is in its help pages: search for\n"
            f'"{address.rsplit("@", 1)[-1]} SMTP settings". Port 587 is the usual choice.[/dim]'
        )
        host = Prompt.ask("Outgoing mail server (SMTP)").strip()
        port = int(Prompt.ask("Port", default="587"))

    smtp = SmtpConfig(host=host, port=port, username=address)

    # --- app password ---------------------------------------------------
    console.print()
    if provider:
        p = _PROVIDERS[provider]
        console.print(
            f"{p['name']} needs an [bold]app password[/bold]: a separate password just for this "
            "tool.\nYour normal password won't work.\n"
        )
        for i, (text, url) in enumerate(p["steps"], 1):  # type: ignore[arg-type]
            console.print(f"  {i}. {text}\n     [link={url}]{url}[/link]")
        console.print(f"\n  {p['shape']} Copy it.\n")
        last_url = p["steps"][-1][1]  # type: ignore[index]
        if Confirm.ask("Open that page in your browser now?", default=True):
            if not open_in_browser(last_url):
                console.print(f"[dim]Couldn't open a browser; go to {last_url}[/dim]")
    else:
        console.print(
            "Use your email password. Many providers require an [bold]app password[/bold]\n"
            "for apps like this; check your account's security settings.\n"
        )

    saved = False
    while True:
        pw = Prompt.ask("Paste the app password [dim](typing is hidden)[/dim]", password=True)
        if not pw.strip():
            console.print("[yellow]Skipped. Add it later with: optout smtp-password[/yellow]")
            break
        if not _looks_like_app_password(provider, pw) and not Confirm.ask(
            "[yellow]That doesn't look like an app password[/yellow] (a regular password will be "
            "rejected). Try it anyway?",
            default=False,
        ):
            continue
        with console.status("Checking it by signing in (nothing is sent)…"):
            problem = check_login(smtp, pw.strip())
        if problem is None:
            path = save_smtp_password(pw)
            console.print(
                f"[green]✓ Signed in.[/green] Password saved to {path} (only you can read it)."
            )
            saved = True
            break
        console.print(f"[red]✗ Didn't work:[/red] {problem}.")
        if provider == "gmail":
            console.print(
                "[dim]Check you copied the app password (not your Gmail password). Work or school\n"
                "accounts may have app passwords turned off by an administrator.[/dim]"
            )
        if not Confirm.ask("Try again?", default=True):
            console.print("[yellow]Add it later with: optout smtp-password[/yellow]")
            break

    label = None
    if provider == "gmail":
        console.print()
        if Confirm.ask(
            "Put a Gmail label [bold]OPTOUT[/bold] on the emails this tool sends, so they're easy "
            "to find?",
            default=True,
        ):
            label = "OPTOUT"

    return EmailConfig(method="smtp", smtp=smtp, gmail_label=label), saved


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_init_wizard(open_dashboard: bool = True) -> None:
    console.print(
        Panel(
            "[bold]Welcome to optout[/bold]\n"
            "Three short steps, about 3 minutes. Nothing is sent to anyone during setup.",
            expand=False,
        )
    )

    path = config_path()
    if path.exists():
        console.print(f"\n[yellow]You're already set up ({path}).[/yellow]")
        if not Confirm.ask("Start over and replace your settings?", default=False):
            console.print("Keeping your settings.")
            raise typer.Exit(code=0)

    try:
        profile = _collect_profile()
        email_cfg, password_ok = _collect_email_config()
    except KeyboardInterrupt:
        console.print("\n\n[yellow]Stopped. Nothing was saved.[/yellow]")
        raise typer.Exit(code=0)

    assert email_cfg.smtp is not None
    sender = email_cfg.smtp.username
    profile.emails = EmailSet(current=[sender])
    cfg = UserConfig(
        profile=profile,
        email=email_cfg,
        playwright=PlaywrightConfig(),
        monitoring=MonitoringConfig(),
    )
    save_config(cfg)
    init_db()

    _section("Step 3 of 3 · The broker list")
    console.print(
        "optout emails the data brokers registered with the State of California\n"
        "(about 580 companies). Loading the list downloads that public file; nothing is sent.\n"
    )
    if Confirm.ask("Load it now?", default=True):
        from .brokers.importer import import_ca_registry

        try:
            with console.status("Downloading the registry…"):
                r = import_ca_registry()
            console.print(
                f"[green]✓ {r.imported} brokers loaded[/green] "
                f"[dim]({r.disabled} credit/health/finance-regulated ones are off by default)[/dim]"
            )
        except ValueError as exc:
            console.print(f"[yellow]{exc}. You can load it later from the dashboard.[/yellow]")

    console.print(
        Panel(
            "[bold green]You're set up.[/bold green]\n\n"
            "From now on, just run [bold cyan]optout[/bold cyan] to open your dashboard.\n"
            "There you can preview the emails, send a batch, and track replies.\n"
            + (
                ""
                if password_ok
                else "\n[yellow]Before sending, save your app password: "
                "optout smtp-password[/yellow]\n"
            )
            + f"\n[dim]Settings: {path}  ·  change them with: optout init[/dim]",
            expand=False,
        )
    )
    if open_dashboard and Confirm.ask("Open the dashboard now?", default=True):
        from .web.server import serve

        serve()
