"""Replay a recorded form recipe in a visible browser, pausing for the human parts.

A recipe (see ``forms.py``) holds field names and visible labels. Here each step is found on
the page by its label, filled from the user's own profile, and the submit is pressed. When a
CAPTCHA is showing the run stops and waits for the user to solve it, then carries on. Nothing
here defeats a CAPTCHA or hides that the browser is automated.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout

from ..forms import Recipe, Step

STEP_MS = 8_000
SUCCESS_MS = 20_000
CAPTCHA_WAIT_S = 300

# Recipes we won't drive: they need something the user must do away from this page.
# Steps where the user swears something: they confirm each one, we don't tick it for them.
ATTEST = re.compile(r"certif|perjury|penalt|truthful|accura|i agree|^agree$|consent", re.I)

REFUSED = {"id_upload": "wants an ID uploaded"}

# A final page asking for a code that was emailed or texted: the user types it, not us.
CODE_PAGE = re.compile(r"code|sms|authenticat|one-time|verification", re.I)

_CAPTCHA_FRAMES = ("recaptcha", "hcaptcha", "turnstile", "challenges.cloudflare.com")
_TOKEN_FIELDS = (
    "textarea[name='g-recaptcha-response']",
    "textarea[name='h-captcha-response']",
    "input[name='cf-turnstile-response']",
)


@dataclass
class Result:
    status: str  # done | stuck | refused
    detail: str = ""
    waited_for_human: bool = False
    # What happened, for fixing the recipe afterwards. Never holds anything you typed.
    steps: list[dict] = field(default_factory=list)
    final_url: str = ""  # no query string
    title: str = ""
    inventory: list[str] = field(default_factory=list)  # the page's labels/buttons, on failure
    failed_requests: list[str] = field(default_factory=list)  # blocked or failed resources
    seconds: float = 0.0
    screenshot: bytes | None = None  # on failure; shows your details, so keep it local

    def record(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "screenshot"}
        d["has_screenshot"] = self.screenshot is not None
        return d


def captcha_showing(page: Page) -> bool:
    """A CAPTCHA widget is on the page and hasn't produced an answer token yet."""
    widget = any(any(k in (f.url or "") for k in _CAPTCHA_FRAMES) for f in page.frames)
    if not widget:
        return False
    for sel in _TOKEN_FIELDS:
        for el in page.locator(sel).all():
            try:
                if el.input_value():
                    return False
            except Exception:
                continue
    return True


def wait_for_captcha(
    page: Page, say: Callable[[str], None], timeout_s: int = CAPTCHA_WAIT_S
) -> bool:
    """Block until the user has solved the CAPTCHA (True) or time runs out (False)."""
    if not captcha_showing(page):
        return False
    say("  CAPTCHA: solve it in the browser window; I'll carry on when it's done.")
    page.bring_to_front()
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if not captcha_showing(page):
            return True
        page.wait_for_timeout(1000)
    raise PWTimeout("the CAPTCHA wasn't solved in time")


_INVENTORY_JS = """() => {
  const out = [];
  const text = (e) => (e.innerText || e.getAttribute('aria-label') || e.placeholder || '')
    .replace(/\\s+/g, ' ').trim().slice(0, 60);
  const sel = 'input,select,textarea,button,a[role=button],[role=checkbox]';
  for (const e of document.querySelectorAll(sel)) {
    if (!e.offsetParent && e.type !== 'hidden') continue;
    if (e.type === 'hidden' || e.type === 'password') continue;
    const lab = (e.labels && e.labels[0] ? e.labels[0].innerText : '')
      || text(e) || e.name || e.id || '';
    const kind = e.tagName.toLowerCase() + (e.type ? ':' + e.type : '');
    out.push(`${kind} ${lab.replace(/\\s+/g, ' ').trim().slice(0, 60)}`);
  }
  return out.slice(0, 60);
}"""


def snapshot(page: Page) -> tuple[list[str], str]:
    """The labels, buttons and field types currently on the page (no values), and its title."""
    try:
        frames = [f for f in page.frames if not any(k in (f.url or "") for k in _CAPTCHA_FRAMES)]
        found: list[str] = []
        for f in frames[:4]:
            try:
                found += f.evaluate(_INVENTORY_JS)
            except Exception:
                continue
        return found[:80], page.title()[:100]
    except Exception:
        return [], ""


def _locate(page: Page, step: Step, kinds: tuple[str, ...]):
    """The element for this step, found by its visible label, trying a few ways."""
    name = step.label or step.text
    tries = []
    if name:
        tries.append(page.get_by_label(name, exact=False))
        for kind in kinds:
            tries.append(page.get_by_role(kind, name=re.compile(re.escape(name[:60]), re.I)))
        tries.append(page.get_by_placeholder(name, exact=False))
    for loc in tries:
        if loc.count():
            return loc.first
    raise LookupError(f"couldn't find “{name}” on the page")


def _value(step: Step, values: dict[str, str]) -> str:
    if step.field:
        v = values.get(step.field, "")
        if not v:
            raise LookupError(f"no {step.field} in your profile")
        return v
    return step.text


def run_step(
    page: Page,
    step: Step,
    values: dict[str, str],
    say: Callable[[str], None],
    ask: Callable[[str], str] = lambda _m: "y",
    manual_captcha: bool = False,
) -> bool:
    """Do one step. Returns True if the user had to solve a CAPTCHA along the way."""
    waited = False
    page.set_default_timeout(STEP_MS)
    name = step.label or step.text
    if step.action in ("click", "check") and ATTEST.search(name):
        if (
            ask(f"  You are being asked to confirm: “{name}”. Tick it? [y/N] ").strip().lower()
            != "y"
        ):
            raise LookupError(f"you didn't confirm “{name}”")
    if step.action == "fill":
        _locate(page, step, ("textbox", "combobox")).fill(_value(step, values))
    elif step.action == "select":
        loc = _locate(page, step, ("combobox", "listbox"))
        want = _value(step, values)
        for opt in ({"value": want}, {"label": want}, {"label": step.text}):
            try:
                loc.select_option(**opt)
                break
            except Exception:
                continue
        else:
            raise LookupError(f"no option “{want}” for “{step.label}”")
    elif step.action == "check":
        _locate(page, step, ("checkbox", "radio")).check()
    elif step.action == "click":
        try:
            btn = _locate(page, step, ("button", "link"))
        except LookupError:
            btn = page.get_by_text(step.text, exact=False).first
        if re.search(r"submit|continue|review", name, re.I):
            waited = wait_for_captcha(page, say)
            if not waited and manual_captcha:  # a text CAPTCHA built into the form
                ask("  Solve the CAPTCHA in the browser window, then press Enter here. ")
                waited = True
        btn.click()
    elif step.action == "wait_text":
        waited = wait_for_captcha(page, say)
        page.get_by_text(step.text, exact=False).first.wait_for(timeout=SUCCESS_MS)
    return waited


def replay(
    page: Page,
    recipe: Recipe,
    values: dict[str, str],
    say: Callable[[str], None] = lambda _m: None,
    ask: Callable[[str], str] = lambda _m: "y",
) -> Result:
    """Walk the recipe on *page*. Never raises for page problems: returns what happened."""
    for kind, why in REFUSED.items():
        if kind in recipe.needs_human:
            return Result("refused", f"this form {why}; do it by hand")
    started = time.monotonic()
    failed: list[str] = []
    page.on(
        "requestfailed",
        lambda r: failed.append(f"{urlsplit(r.url).hostname}: {(r.failure or '')[:40]}"),
    )
    steps: list[dict] = []
    waited = False

    def finish(status: str, detail: str = "") -> Result:
        res = Result(
            status,
            detail,
            waited,
            steps,
            f"{urlsplit(page.url).scheme}://{urlsplit(page.url).netloc}{urlsplit(page.url).path}"
            if page.url
            else "",
            seconds=round(time.monotonic() - started, 1),
            failed_requests=sorted(set(failed))[:10],
        )
        if status == "stuck":
            res.inventory, res.title = snapshot(page)
            try:
                res.screenshot = page.screenshot(full_page=True)
            except Exception:
                pass
        return res

    try:
        page.goto(recipe.url, wait_until="domcontentloaded", timeout=30_000)
        if urlsplit(page.url).hostname != urlsplit(recipe.url).hostname:
            return finish("stuck", f"sent to {urlsplit(page.url).hostname}, not the recipe's site")
        last_submit = max(
            (
                n
                for n, st in enumerate(recipe.steps, 1)
                if st.action == "click" and re.search(r"submit", st.label or st.text, re.I)
            ),
            default=0,
        )
        for i, step in enumerate(recipe.steps, 1):
            say(f"  {i}/{len(recipe.steps)} {step.action} {step.label or step.text}".rstrip())
            t0 = time.monotonic()
            rec = {"n": i, "action": step.action, "target": step.label or step.text}
            steps.append(rec)
            try:
                waited |= run_step(
                    page,
                    step,
                    values,
                    say,
                    ask,
                    "captcha" in recipe.needs_human and i == last_submit,
                )
                rec.update(ok=True, seconds=round(time.monotonic() - t0, 1))
            except LookupError as exc:
                rec.update(ok=False, error=str(exc)[:120])
                return finish("stuck", f"step {i}: {exc}")
            except PWTimeout:
                rec.update(ok=False, error="timed out")
                return finish(
                    "stuck", f"step {i}: timed out ({step.action} “{step.label or step.text}”)"
                )
        if recipe.success_text and not page.get_by_text(recipe.success_text, exact=False).count():
            return finish("stuck", f"never saw “{recipe.success_text}”")
        if {"email_verify", "phone_verify"} & set(recipe.needs_human) and CODE_PAGE.search(
            recipe.success_text
        ):
            ask(
                "  They sent you a code (email or text). Type it into the page and submit it, "
                "then press Enter here. "
            )
            waited = True
    except PWTimeout as exc:
        return finish("stuck", f"timed out: {str(exc).splitlines()[0][:80]}")
    except Exception as exc:  # a closed window, a crashed page: report, don't traceback
        return finish("stuck", f"{type(exc).__name__}: {str(exc).splitlines()[0][:80]}")
    return finish("done", recipe.success_text)
