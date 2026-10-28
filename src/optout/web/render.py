"""HTML for the report and the local UI.

Every value that did not come from this file is passed through ``e()``: broker
names and contact data originate in public registries and are untrusted. Pages
use no JavaScript, so there is nothing for injected markup to run.
"""

from __future__ import annotations

from html import escape as e
from urllib.parse import quote

from .data import Dashboard, Row

CSS = """
:root{--bg:#fff;--fg:#1c1f23;--muted:#5f6b76;--line:#d9dee3;--card:#f6f8fa;
--accent:#1259c3;--ok:#1a7f37;--warn:#9a6700;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--fg:#e8eaed;--muted:#9aa5b1;
--line:#2d343b;--card:#1b1f23;--accent:#6aa8ff;--ok:#56d364;--warn:#e3b341;--bad:#ff7b72}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:16px}
nav{display:flex;gap:16px;padding:12px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
nav a{color:var(--accent);text-decoration:none;font-weight:600}
h1{font-size:22px;margin:16px 0 8px}h2{font-size:17px;margin:24px 0 8px}
.cards{display:flex;gap:12px;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;min-width:130px}
.card b{display:block;font-size:24px}.card span{color:var(--muted);font-size:13px}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600}
a{color:var(--accent)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
.box{border:1px solid var(--line);border-radius:8px;padding:12px 16px;background:var(--card);margin:8px 0}
.attn li{margin:4px 0}
form.inline{display:inline}
input,select,button{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--fg)}
button{background:var(--accent);color:#fff;border-color:var(--accent);cursor:pointer}
button.secondary{background:var(--bg);color:var(--fg);border-color:var(--line)}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;
white-space:pre-wrap;overflow-wrap:anywhere}
"""

_TONE = {
    "Removed ✓ verified": "ok",
    "Broker says done": "ok",
    "Refused": "bad",
    "Rejected": "bad",
    "Failed": "bad",
}


def page(title: str, body: str, *, nav: bool, refresh: int | None = None) -> str:
    links = (
        "<nav><a href='/'>Dashboard</a><a href='/campaign'>Send a batch</a>"
        "<a href='/drafts'>Drafts</a></nav>"
        if nav
        else ""
    )
    meta = f"<meta http-equiv='refresh' content='{int(refresh)}'>" if refresh else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{meta}<title>{e(title)}</title><style>{CSS}</style></head>"
        f"<body>{links}<main>{body}</main></body></html>"
    )


def _row_link(row: Row, interactive: bool) -> str:
    if not interactive:
        return e(row.name)
    return f"<a href='/broker/{quote(row.slug, safe='')}'>{e(row.name)}</a>"


def _action_cell(row: Row) -> str:
    return f"<td><a href='/broker/{quote(row.slug, safe='')}'>Follow up →</a></td>"


def _days_cell(row: Row) -> str:
    if row.days_left is None:
        return "—"
    if row.overdue:
        return "<span class='bad'>OVERDUE</span>"
    cls = "warn" if row.days_left < 14 else ""
    return f"<span class='{cls}'>{row.days_left}</span>"


def dashboard(d: Dashboard, *, interactive: bool, status_filter: str | None = None) -> str:
    cards = "".join(
        f"<div class='card'><b>{n}</b><span>{e(label)}</span></div>"
        for label, n in sorted(d.counts.items())
    )
    if interactive and d.daily_limit:
        cards += (
            f"<div class='card'><b>{d.sent_today}/{d.daily_limit}</b><span>sent today</span></div>"
            f"<div class='card'><b>{d.eligible}</b><span>ready to email</span></div>"
        )

    attn = ""
    if d.attention:
        items = "".join(
            f"<li><b>{e(name)}</b>: {e(why)}</li>" for _slug, name, why in d.attention[:40]
        )
        attn = f"<h2>Needs your attention</h2><div class='box'><ul class='attn'>{items}</ul></div>"
    elif d.rows:
        attn = "<div class='box ok'>Nothing needs attention right now.</div>"

    if d.drafts and interactive:
        attn += (
            f"<div class='box'>{d.drafts} follow-up draft(s) are waiting. "
            "<a href='/drafts'>Review them</a>.</div>"
        )

    rows = [r for r in d.rows if not status_filter or r.label == status_filter]
    shown = rows[:300]
    trs = "".join(
        f"<tr><td>{_row_link(r, interactive)}</td>"
        f"<td class='{_TONE.get(r.label, '')}'>{e(r.label)}</td>"
        f"<td>{e(r.method)}</td><td>{e(r.submitted)}</td><td>{e(r.deadline)}</td>"
        f"<td>{_days_cell(r)}</td><td>{e(r.last_check)}"
        f"{' <span class=bad>⚠ drift</span>' if r.drifted else ''}</td>"
        f"{_action_cell(r) if interactive else ''}</tr>"
        for r in shown
    )
    more = (
        f"<p class='muted'>Showing {len(shown)} of {len(rows)}.</p>"
        if len(rows) > len(shown)
        else ""
    )
    table = (
        "<div class='table-wrap'><table><thead><tr><th>Broker</th><th>Status</th><th>Method</th>"
        "<th>Sent</th><th>Due</th><th>Days left</th><th>Last check</th>"
        f"{'<th></th>' if interactive else ''}</tr></thead>"
        f"<tbody>{trs}</tbody></table></div>{more}"
        if rows
        else "<p class='muted'>No requests yet.</p>"
    )

    filters = ""
    if interactive and d.counts:
        links = " · ".join(
            f"<a href='/?status={quote(label, safe='')}'>{e(label)}</a>"
            for label in sorted(d.counts)
        )
        filters = f"<p class='muted'>Filter: <a href='/'>all</a> · {links}</p>"

    note = (
        "<p class='muted'>“Broker says done” is the broker's word. Only "
        "“Removed ✓ verified” means a re-search confirmed the listing is gone.</p>"
    )
    return page(
        "optout",
        f"<h1>Opt-out status</h1><p class='muted'>Updated {e(d.generated)}</p>"
        f"<div class='cards'>{cards}</div>{attn}<h2>Requests</h2>{filters}{table}{note}",
        nav=interactive,
    )


def message_box(text: str, ok: bool = True) -> str:
    return f"<div class='box {'ok' if ok else 'bad'}'>{e(text)}</div>"


def broker_page(
    slug: str,
    name: str,
    enabled: bool,
    events: list[tuple[str, str, str]],
    csrf: str,
    flash: str = "",
) -> str:
    ev = "".join(
        f"<tr><td>{e(when)}</td><td>{e(kind)}</td><td>{e(note)}</td></tr>"
        for when, kind, note in events
    )
    hidden = f"<input type='hidden' name='csrf' value='{e(csrf)}'><input type='hidden' name='slug' value='{e(slug)}'>"
    body = (
        f"<h1>{e(name)}</h1><p class='muted'>{e(slug)}</p>{flash}"
        "<h2>Record their reply</h2><div class='box'>"
        f"<form method='post' action='/act/reply'>{hidden}"
        "<select name='outcome'><option value='deleted'>They say it's deleted</option>"
        "<option value='denied'>They refused</option>"
        "<option value='need-info'>They need more info</option>"
        "<option value='auto-reply'>Automated reply only (deadline keeps running)</option>"
        "</select> "
        "<input name='note' maxlength='200' placeholder='short note, no personal data'> "
        "<button>Save</button></form></div>"
        "<h2>Write a follow-up</h2><div class='box'>"
        f"<form method='post' action='/act/draft'>{hidden}"
        "<select name='kind'>"
        "<option value='clarify'>Clarify: reply to an automated answer</option>"
        "<option value='followup'>Follow up: no response yet</option>"
        "<option value='appeal'>Appeal: they refused</option>"
        "<option value='complaint'>Attorney General complaint draft</option>"
        "</select> <button>Write draft</button></form>"
        "<p class='muted'>Writes text for you to copy. Nothing is sent.</p></div>"
        "<h2>Include or skip this broker</h2><div class='box'>"
        f"<form method='post' action='/act/toggle'>{hidden}"
        f"<input type='hidden' name='enabled' value='{0 if enabled else 1}'>"
        f"<button class='secondary'>{'Disable (never contact)' if enabled else 'Enable'}</button>"
        "</form></div>"
        "<h2>I sent a follow-up, appeal, or complaint</h2><div class='box'>"
        f"<form method='post' action='/act/escalation-sent'>{hidden}"
        "<select name='kind'><option>clarify</option><option>followup</option>"
        "<option>appeal</option><option>complaint</option></select> "
        "<button class='secondary'>Mark as sent</button>"
        "</form></div>"
        f"<h2>History</h2><div class='table-wrap'><table><tbody>{ev or '<tr><td>None</td></tr>'}"
        "</tbody></table></div>"
    )
    return page(name, body, nav=True)


def campaign_page(
    eligible: list[tuple[str, str]],
    skipped: int,
    used: int,
    limit: int,
    csrf: str,
    job: dict,
    flash: str = "",
) -> str:
    running = job.get("state") == "running"
    remaining = max(0, limit - used)
    batch = min(remaining, len(eligible), 40)
    lines = "".join(f"<li>{e(name)}</li>" for _slug, name in eligible[:25])
    job_box = ""
    if job.get("state") in ("running", "done"):
        failed = "".join(f"<li>{e(s)}: {e(why)}</li>" for s, why in job.get("failed", []))
        job_box = (
            f"<div class='box'><b>{'Sending…' if running else 'Finished'}</b>: "
            f"{len(job.get('sent', []))} sent, {len(job.get('failed', []))} failed"
            f" of {job.get('total', 0)}."
            f"{('<br>Stopped: ' + e(job['stopped'])) if job.get('stopped') else ''}"
            f"{('<ul>' + failed + '</ul>') if failed else ''}</div>"
        )
    if running:
        form = "<p class='muted'>A batch is running. This page refreshes by itself.</p>"
    elif batch == 0:
        form = f"<p class='muted'>Nothing to send right now ({used}/{limit} used today).</p>"
    else:
        form = (
            "<div class='box'><p>Review exactly what will be emailed before anything is sent.</p>"
            f"<a href='/preview?count={batch}'><button type='button'>"
            f"Preview {batch} emails</button></a></div>"
        )
    body = (
        f"<h1>Send a batch</h1>{flash}"
        f"<p>{len(eligible)} brokers are ready, {skipped} skipped. "
        f"Daily limit: {used}/{limit} used.</p>{job_box}{form}"
        f"<h2>Next in line</h2><ul>{lines}</ul>"
    )
    return page("Send a batch", body, nav=True, refresh=3 if running else None)


def preview_page(
    items: list[tuple[str, str, str, str]], sender: str, csrf: str, flash: str = ""
) -> str:
    """items: (broker name, to address, subject, body)."""
    n = len(items)
    blocks = "".join(
        f"<details><summary><b>{e(name)}</b> → {e(to)}</summary>"
        f"<p class='muted'>Subject: {e(subject)}</p><pre>{e(body)}</pre></details>"
        for name, to, subject, body in items
    )
    form = (
        "<div class='box'><p>Sending happens one email at a time with a pause between them, "
        "and stops on repeated errors. Nothing is sent until you type the confirmation.</p>"
        "<form method='post' action='/act/campaign'>"
        f"<input type='hidden' name='csrf' value='{e(csrf)}'>"
        f"<input type='hidden' name='count' value='{n}'>"
        f"<label>Type <b>SEND {n}</b> to send these {n} emails: "
        "<input name='confirm' autocomplete='off' required></label> "
        "<button>Send</button> <a href='/campaign'>Cancel</a></form></div>"
        if n
        else "<p class='muted'>Nothing to send.</p>"
    )
    body = (
        f"<h1>Preview: {n} emails</h1>{flash}"
        f"<p>From <b>{e(sender)}</b>. Click a broker to read the full email.</p>"
        f"{form}{blocks}"
    )
    return page("Preview", body, nav=True)


def drafts_page(names: list[str], csrf: str, flash: str = "") -> str:
    items = (
        "".join(f"<li><a href='/draft/{quote(n, safe='')}'>{e(n)}</a></li>" for n in names)
        or "<li class='muted'>No drafts waiting.</li>"
    )
    body = (
        f"<h1>Drafts</h1>{flash}<p>Drafts are written, never sent. Copy one, send or file it "
        "yourself, then mark it as sent on the broker's page.</p>"
        "<form method='post' action='/act/make-drafts'>"
        f"<input type='hidden' name='csrf' value='{e(csrf)}'>"
        "<button class='secondary'>Check for overdue requests and write drafts</button></form>"
        f"<ul>{items}</ul>"
    )
    return page("Drafts", body, nav=True)


def draft_page(name: str, text: str) -> str:
    return page(
        name,
        f"<h1>{e(name)}</h1><p class='muted'>Contains your name and address; "
        f"it is shown only on this page.</p><pre>{e(text)}</pre>",
        nav=True,
    )


def error_page(code: int, text: str) -> str:
    return page(f"Error {code}", f"<h1>{code}</h1><p>{e(text)}</p>", nav=False)
