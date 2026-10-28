"""HTML for the report and the local UI.

Every value that did not come from this file is passed through ``e()``: broker
names and contact data originate in public registries and are untrusted. Pages
use no JavaScript (the server's CSP forbids it); hover states are pure CSS and
chart tooltips are native SVG <title> elements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from html import escape as e
from urllib.parse import quote

from ..labels import STATUS_LABEL
from .data import Dashboard, Row

_STATUS_LABELS = set(STATUS_LABEL.values())

# --- brand -------------------------------------------------------------------

_LOGO = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64' aria-hidden='true'>"
    "<rect width='64' height='64' rx='14' fill='#2a78d6'/>"
    "<path d='M32 11l17 6.5v12.5c0 11.5-7.3 19.6-17 23-9.7-3.4-17-11.5-17-23V17.5z' "
    "fill='#fff'/>"
    "<path d='M24 32.5l6 6 11.5-12.5' fill='none' stroke='#2a78d6' stroke-width='5.5' "
    "stroke-linecap='round' stroke-linejoin='round'/></svg>"
)
FAVICON = "data:image/svg+xml," + quote(_LOGO, safe="/:=' ")

CSS = """
:root{
  --bg:#f5f6f8;--surface:#ffffff;--surface-2:#f0f2f5;--line:#e2e5e9;
  --text:#15181c;--text-2:#46505a;--muted:#6a7480;
  --accent:#2a78d6;--accent-hover:#2167bd;--accent-ink:#fff;--ring:rgba(42,120,214,.45);
  --good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;--info:#2a78d6;--neutral:#8a939d;
  --chart-surface:#fcfcfb;--grid:#e8e9e6;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;
  --shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.07);
  --radius:12px;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#111214;--surface:#1a1a19;--surface-2:#232422;--line:#30312e;
  --text:#f0efec;--text-2:#c3c2b7;--muted:#9a998f;
  --accent:#3987e5;--accent-hover:#5598e7;--ring:rgba(57,135,229,.5);
  --chart-surface:#1a1a19;--grid:#2c2d2a;--s1:#3987e5;--s2:#d95926;--s3:#199e70;
  --shadow:0 1px 2px rgba(0,0,0,.4);
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:0 16px}
.skip{position:absolute;left:-999px;top:8px}.skip:focus{left:16px;z-index:10;
  background:var(--surface);padding:8px 12px;border-radius:8px}
a{color:var(--accent);text-underline-offset:2px}
:focus-visible{outline:3px solid var(--ring);outline-offset:2px;border-radius:6px}
.topbar{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--surface) 88%,transparent);
  backdrop-filter:saturate(1.4) blur(8px);border-bottom:1px solid var(--line)}
.topbar .wrap{display:flex;align-items:center;gap:12px;min-height:56px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;color:var(--text);text-decoration:none;
  font-weight:700;font-size:17px;letter-spacing:-.01em}
.brand svg{width:28px;height:28px}
.topbar .sub{color:var(--muted);font-size:13px}
.topbar .updated{margin-left:auto;color:var(--muted);font-size:13px}
main{padding:20px 0 48px}
h1{font-size:24px;letter-spacing:-.02em;margin:4px 0 16px}
h2{font-size:16px;margin:0 0 12px;letter-spacing:-.01em}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:18px 20px;margin:0 0 16px}
.grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:16px;margin:0 0 16px}
@media (max-width:880px){.grid{grid-template-columns:1fr}}
.grid>.card{margin:0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:0 0 16px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:14px 16px}
.stat .n{font-size:28px;font-weight:700;letter-spacing:-.02em;line-height:1.1;font-variant-numeric:tabular-nums}
.stat .l{color:var(--text-2);font-size:13px;display:flex;align-items:center;gap:6px;margin-top:4px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none}
.muted{color:var(--muted)}.small{font-size:13px}
.ok{color:var(--good)}.bad{color:var(--critical)}
.flash{border-radius:10px;padding:10px 14px;margin:0 0 16px;border:1px solid var(--line);background:var(--surface)}
.flash.ok{border-color:color-mix(in srgb,var(--good) 40%,var(--line));color:var(--text)}
.flash.bad{border-color:color-mix(in srgb,var(--critical) 45%,var(--line));color:var(--text)}
.flash::before{content:"● ";color:var(--good)}.flash.bad::before{color:var(--critical)}
.pill{display:inline-flex;align-items:center;gap:6px;padding:2px 10px 2px 8px;border-radius:999px;
  font-size:12.5px;font-weight:600;line-height:20px;white-space:nowrap;color:var(--text);
  background:color-mix(in srgb,var(--tone) 14%,transparent);
  border:1px solid color-mix(in srgb,var(--tone) 35%,transparent)}
.pill .i{color:var(--tone);font-size:11px;line-height:1}
.t-good{--tone:var(--good)}.t-warning{--tone:var(--warning)}.t-serious{--tone:var(--serious)}
.t-critical{--tone:var(--critical)}.t-info{--tone:var(--info)}.t-neutral{--tone:var(--neutral)}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
a.fpill{text-decoration:none;display:inline-flex;align-items:center;gap:6px;padding:5px 12px;
  border-radius:999px;border:1px solid var(--line);background:var(--surface);color:var(--text-2);
  font-size:13px;font-weight:600}
a.fpill:hover{border-color:var(--accent);color:var(--text)}
a.fpill[aria-current=page]{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
a.fpill[aria-current=page] .dot{box-shadow:0 0 0 2px var(--accent-ink)}
.fpill .c{font-variant-numeric:tabular-nums;opacity:.75;font-weight:500}
.toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;justify-content:space-between;margin:0 0 12px}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap;vertical-align:middle}
thead th{position:sticky;top:0;background:var(--surface-2);color:var(--text-2);font-size:12px;
  font-weight:600;text-transform:uppercase;letter-spacing:.04em}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--surface-2)}
td.name{font-weight:600;white-space:normal;min-width:160px}
td.num{font-variant-numeric:tabular-nums}
input,select,button{font:inherit;font-size:14px;border-radius:8px}
input,select{padding:7px 10px;border:1px solid var(--line);background:var(--surface);color:var(--text)}
input:hover,select:hover{border-color:color-mix(in srgb,var(--accent) 50%,var(--line))}
button{padding:7px 14px;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);
  font-weight:600;cursor:pointer}
button:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
button.big{padding:10px 18px;font-size:15px}
button.secondary{background:var(--surface);color:var(--text);border-color:var(--line)}
button.secondary:hover{border-color:var(--accent)}
form.inline{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap}
form.inline.rowform{flex-wrap:nowrap}
.rowform select{width:150px;padding:5px 8px;font-size:13px}.rowform button{padding:5px 12px;font-size:13px}
.search{display:flex;gap:6px}.search input{width:220px}
.sendrow{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin-top:12px}
.sendrow label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--text-2)}
details.emails>summary,details.more>summary{cursor:pointer;font-weight:600;color:var(--accent);margin:8px 0}
details.email{margin:8px 0;border:1px solid var(--line);border-radius:10px;background:var(--surface-2)}
details.email>summary{cursor:pointer;padding:8px 12px;font-size:14px}
details.email>div{padding:0 12px 12px}
pre{background:var(--surface-2);border:1px solid var(--line);border-radius:10px;padding:12px 14px;
  white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;margin:8px 0}
details.email pre{background:var(--surface)}
.attn{list-style:none;margin:0;padding:0}
.attn li{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;
  padding:10px 0;border-bottom:1px solid var(--line)}
.attn li:last-child{border-bottom:0}
.progress{height:8px;border-radius:999px;background:var(--surface-2);overflow:hidden;margin:8px 0 4px}
.progress>span{display:block;height:100%;background:var(--accent);border-radius:999px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--text-2);margin:0 0 6px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{width:14px;height:3px;border-radius:2px;display:inline-block}
svg.chart{width:100%;height:auto;display:block;background:var(--chart-surface);border-radius:8px}
.chart .grid line{stroke:var(--grid);stroke-width:1}
.chart .axis{fill:var(--muted);font-size:11px}
.chart .lbl{fill:var(--text);font-size:12px;font-weight:600}
.chart .col .xh{stroke:var(--muted);stroke-width:1;opacity:0}
.chart .col:hover .xh{opacity:.6}
.chart .col rect{fill:transparent}
@media (prefers-reduced-motion:no-preference){a.fpill,button,tbody tr{transition:background-color .12s,border-color .12s}}
@media (forced-colors:active){.pill,.fpill{border:1px solid CanvasText}}
"""


def page(title: str, body: str, *, nav: bool, refresh: int | None = None, updated: str = "") -> str:
    meta = f"<meta http-equiv='refresh' content='{int(refresh)}'>" if refresh else ""
    back = "<a href='/' class='small'>← Dashboard</a>" if nav else ""
    upd = f"<span class='updated'>Updated {e(updated)}</span>" if updated else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='light dark'>"
        "<meta name='robots' content='noindex,nofollow'>"
        "<meta name='referrer' content='no-referrer'>"
        "<meta name='description' content='Your data-broker opt-out requests'>"
        f'<link rel="icon" type="image/svg+xml" href="{FAVICON}">'
        f"{meta}<title>{e(title)}</title><style>{CSS}</style></head><body>"
        "<a class='skip' href='#main'>Skip to content</a>"
        "<header class='topbar'><div class='wrap'>"
        f"<a class='brand' href='/' aria-label='optout home'>{_LOGO}<span>optout</span></a>"
        f"<span class='sub'>Data-broker opt-outs</span>{back}{upd}</div></header>"
        f"<main id='main' class='wrap'>{body}</main></body></html>"
    )


# --- status pills ------------------------------------------------------------

# status code -> (tone, icon). Color never carries meaning alone: icon + text label.
_PILL = {
    "removed": ("good", "✓"),
    "confirmed": ("good", "✓"),
    "awaiting_broker_confirmation": ("info", "●"),
    "submitted": ("info", "●"),
    "awaiting_user_input": ("warning", "!"),
    "rejected": ("critical", "✕"),
    "failed": ("serious", "!"),
    "queued": ("neutral", "○"),
    "expired": ("neutral", "○"),
}


_SHORT = {
    "awaiting_broker_confirmation": "Awaiting reply",
    "submitted": "Sent",
    "confirmed": "Broker says done",
    "removed": "Removed (verified)",
    "awaiting_user_input": "Needs input",
    "rejected": "Refused",
}


def _clean(label: str, status: str = "") -> str:
    return _SHORT.get(status) or label.replace("✓", "").replace("⚡", "").strip()


def pill(status: str, label: str) -> str:
    tone, icon = _PILL.get(status, ("neutral", "●"))
    text = _clean(label, status) if status in _SHORT and label in _STATUS_LABELS else _clean(label)
    return (
        f"<span class='pill t-{tone}'><span class='i' aria-hidden='true'>{icon}</span>"
        f"{e(text)}</span>"
    )


def _tone_var(status: str) -> str:
    return f"var(--{_PILL.get(status, ('neutral', ''))[0]})"


# --- chart -------------------------------------------------------------------

_SERIES = (("Sent", "--s1"), ("Replied", "--s2"), ("Removed", "--s3"))


def _nice_max(v: int) -> tuple[int, int]:
    """Upper bound and tick step giving about four clean ticks."""
    if v <= 4:
        return 4, 1
    raw = v / 4
    mag = 10 ** (len(str(int(raw))) - 1)
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    step = max(1, int(step))
    return int(-(-v // step) * step), step


def _fmt_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%b ") + str(d.day)


def progress_chart(series: list[tuple[str, int, int, int]]) -> str:
    if not series:
        return "<p class='muted'>Progress appears here once you've sent requests.</p>"
    legend = (
        "<div class='legend' aria-hidden='true'>"
        + "".join(
            f"<span><i style='background:var({var})'></i>{name}</span>" for name, var in _SERIES
        )
        + "</div>"
    )
    table_rows = "".join(
        f"<tr><td>{e(_fmt_day(d))}</td><td class='num'>{a}</td><td class='num'>{b}</td>"
        f"<td class='num'>{c}</td></tr>"
        for d, a, b, c in series
    )
    table = (
        "<details class='more'><summary>Show as table</summary><div class='table-wrap'>"
        "<table><thead><tr><th>Day</th><th>Sent</th><th>Replied</th><th>Removed (verified)</th>"
        f"</tr></thead><tbody>{table_rows}</tbody></table></div></details>"
    )
    last = series[-1]
    summary = (
        f"Progress to {_fmt_day(last[0])}: {last[1]} requests sent, {last[2]} brokers replied, "
        f"{last[3]} verified removed."
    )
    if len(series) < 2:
        return (
            f"{legend}<p class='muted small'>{e(summary)} The line chart fills in from your "
            f"second day of activity.</p>{table}"
        )

    w, h = 720, 260
    ml, mr, mt, mb = 40, 112, 14, 30
    pw, ph = w - ml - mr, h - mt - mb
    top, step = _nice_max(max(r[1] for r in series))
    n = len(series)

    def x(i: int) -> float:
        return ml + pw * i / (n - 1)

    def y(v: int) -> float:
        return mt + ph - ph * v / top

    ticks = range(0, top + 1, step)
    grid = "".join(
        f"<line x1='{ml}' x2='{ml + pw}' y1='{y(t):.1f}' y2='{y(t):.1f}'/>" for t in ticks
    )
    yl = "".join(
        f"<text class='axis' x='{ml - 8}' y='{y(t) + 4:.1f}' text-anchor='end'>{t}</text>"
        for t in ticks
    )
    xi = sorted({0, n - 1, (n - 1) // 2} if n > 4 else set(range(n)))
    xl = "".join(
        f"<text class='axis' x='{x(i):.1f}' y='{h - 8}' "
        f"text-anchor='{'start' if i == 0 else 'end' if i == n - 1 else 'middle'}'>"
        f"{e(_fmt_day(series[i][0]))}</text>"
        for i in xi
    )
    values = [(r[1], r[2], r[3]) for r in series]
    lines: list[str] = []
    dots: list[str] = []
    ends: list[tuple[float, str, int, str]] = []
    for k, (name, var) in enumerate(_SERIES):
        pts = " ".join(f"{x(i):.1f},{y(v[k]):.1f}" for i, v in enumerate(values))
        lines.append(
            f"<polyline points='{pts}' fill='none' stroke='var({var})' stroke-width='2' "
            "stroke-linejoin='round' stroke-linecap='round'/>"
        )
        lv = values[-1][k]
        dots.append(
            f"<circle cx='{x(n - 1):.1f}' cy='{y(lv):.1f}' r='4' fill='var({var})' "
            "stroke='var(--chart-surface)' stroke-width='2'/>"
        )
        ends.append((y(lv), name, lv, var))
    # Direct labels at the line ends, nudged apart so they never collide.
    ends.sort(key=lambda t: t[0])
    placed: list[float] = []
    for end in ends:
        placed.append(max(end[0], placed[-1] + 16) if placed else end[0])
    shift = max(0.0, placed[-1] - (mt + ph + 4))
    labels = "".join(
        f"<g><circle cx='{x(n - 1) + 14:.1f}' cy='{ly - shift:.1f}' r='3.5' "
        f"fill='var({var})'/><text class='lbl' x='{x(n - 1) + 22:.1f}' "
        f"y='{ly - shift + 4:.1f}'>{e(name)} {v}</text></g>"
        for ly, (_, name, v, var) in zip(placed, ends, strict=True)
    )
    colw = pw / (n - 1)
    cols = "".join(
        f"<g class='col'><rect x='{x(i) - colw / 2:.1f}' y='{mt}' width='{colw:.1f}' "
        f"height='{ph}'/><line class='xh' x1='{x(i):.1f}' x2='{x(i):.1f}' y1='{mt}' "
        f"y2='{mt + ph}'/><title>{e(_fmt_day(r[0]))}: {r[1]} sent · {r[2]} replied · "
        f"{r[3]} verified removed</title></g>"
        for i, r in enumerate(series)
    )
    svg = (
        f"<svg class='chart' viewBox='0 0 {w} {h}' role='img' aria-labelledby='ct'>"
        f"<title id='ct'>{e(summary)}</title><g class='grid'>{grid}</g>{yl}{xl}"
        f"{''.join(lines)}{''.join(dots)}{labels}{cols}</svg>"
    )
    return f"{legend}{svg}{table}"


# --- interactive main page ---------------------------------------------------


@dataclass
class UI:
    """Everything the interactive main page needs besides the dashboard data."""

    csrf: str
    sender: str = ""
    previews: list[tuple[str, str, str, str]] = field(default_factory=list)  # name,to,subj,body
    used: int = 0
    limit: int = 0
    job: dict = field(default_factory=dict)
    drafts: list[str] = field(default_factory=list)
    open_draft: tuple[str, str, str, str] | None = None  # (file name, text, slug, kind)
    flash: str = ""
    query: str = ""
    configured: bool = True


ROW_ACTIONS = [
    (
        "Record their reply",
        [
            ("reply:deleted", "They say it's deleted"),
            ("reply:denied", "They refused"),
            ("reply:need-info", "They need more info"),
            ("reply:auto-reply", "Automated reply only"),
        ],
    ),
    (
        "Write a follow-up",
        [
            ("draft:clarify", "Clarify (answer to an auto-reply)"),
            ("draft:followup", "Follow up (no response)"),
            ("draft:appeal", "Appeal (they refused)"),
            ("draft:complaint", "AG complaint draft"),
        ],
    ),
    (
        "I sent one",
        [
            ("sent:clarify", "Sent: clarify"),
            ("sent:followup", "Sent: follow-up"),
            ("sent:appeal", "Sent: appeal"),
            ("sent:complaint", "Sent: complaint"),
        ],
    ),
    ("Other", [("toggle:off", "Never contact this broker")]),
]


def _hidden(csrf: str, **fields: str) -> str:
    out = f"<input type='hidden' name='csrf' value='{e(csrf)}'>"
    return out + "".join(
        f"<input type='hidden' name='{e(k)}' value='{e(v)}'>" for k, v in fields.items()
    )


def row_form(slug: str, csrf: str, name: str = "") -> str:
    opts = "<option value='' disabled selected>Choose…</option>" + "".join(
        f"<optgroup label='{e(group)}'>"
        + "".join(f"<option value='{e(v)}'>{e(t)}</option>" for v, t in items)
        + "</optgroup>"
        for group, items in ROW_ACTIONS
    )
    return (
        "<form method='post' action='/act/row' class='inline rowform'>"
        f"{_hidden(csrf, slug=slug)}"
        f"<select name='action' required aria-label='Action for {e(name or slug)}'>{opts}"
        "</select><button type='submit'>Go</button></form>"
    )


def _send_panel(ui: UI) -> str:
    job = ui.job
    status = ""
    if job.get("state") in ("running", "done"):
        total = max(1, job.get("total", 0))
        done = len(job.get("sent", [])) + len(job.get("failed", []))
        pct = int(100 * done / total)
        failed = "".join(f"<li>{e(s)}: {e(why)}</li>" for s, why in job.get("failed", []))
        stopped = f"<p class='bad'>Stopped: {e(job['stopped'])}</p>" if job.get("stopped") else ""
        status = (
            f"<p><b>{'Sending…' if job.get('state') == 'running' else 'Batch finished'}</b> "
            f"<span class='muted'>{len(job.get('sent', []))} sent, "
            f"{len(job.get('failed', []))} failed of {job.get('total', 0)}</span></p>"
            f"<div class='progress' role='progressbar' aria-label='Sending progress' "
            f"aria-valuemin='0' aria-valuemax='100' aria-valuenow='{pct}'>"
            f"<span style='width:{pct}%'></span></div>{stopped}"
            f"{('<ul class=small>' + failed + '</ul>') if failed else ''}"
        )
    n = len(ui.previews)
    if job.get("state") == "running":
        inner = "<p class='muted small'>This page refreshes by itself while sending.</p>"
    elif not ui.configured:
        inner = "<p class='bad'>No config found. Run <code>optout init</code> first.</p>"
    elif n == 0:
        inner = (
            f"<p class='muted'>Nothing to send right now. {ui.used}/{ui.limit} used today; "
            "brokers contacted in the last 90 days are skipped.</p>"
        )
    else:
        emails = "".join(
            f"<details class='email'><summary><b>{e(name)}</b> "
            f"<span class='muted'>→ {e(to)}</span></summary><div>"
            f"<p class='muted small'>Subject: {e(subject)}</p><pre>{e(body)}</pre></div></details>"
            for name, to, subject, body in ui.previews
        )
        inner = (
            f"<p><b>{n}</b> emails ready to go from <b>{e(ui.sender)}</b>.<br>"
            f"<span class='muted small'>{ui.used}/{ui.limit} sent today · paced, stops on "
            "repeated errors</span></p>"
            f"<details class='emails'><summary>Preview all {n} emails</summary>{emails}</details>"
            "<form method='post' action='/act/campaign' class='sendrow'>"
            f"{_hidden(ui.csrf)}"
            f"<label>How many<input name='count' type='number' min='1' max='{n}' value='{n}' "
            "required style='width:6em' inputmode='numeric'></label>"
            f"<button type='submit' class='big'>Send emails</button></form>"
        )
    return (
        "<section class='card' aria-labelledby='send-h' id='send'>"
        f"<h2 id='send-h'>Send a batch</h2>{status}{inner}</section>"
    )


def _draft_panel(ui: UI) -> str:
    if not ui.open_draft:
        return ""
    name, text, slug, kind = ui.open_draft
    sent = (
        "<form method='post' action='/act/row' class='inline'>"
        f"{_hidden(ui.csrf, slug=slug, action=f'sent:{kind}')}"
        "<button type='submit'>I sent it</button></form>"
        if slug and kind
        else ""
    )
    return (
        "<section class='card' aria-labelledby='draft-h' id='draft'>"
        f"<h2 id='draft-h'>Draft · {e(name)}</h2>"
        "<p class='muted small'>Copy this into a reply in Gmail. It contains your name and "
        "address and has not been sent.</p>"
        f"<pre>{e(text)}</pre><div class='filters'>{sent}"
        "<a href='/' class='fpill'>Close</a></div></section>"
    )


def _drafts_line(ui: UI) -> str:
    make = (
        "<form method='post' action='/act/make-drafts' class='inline'>"
        f"{_hidden(ui.csrf)}<button type='submit' class='secondary'>"
        "Write drafts for overdue requests</button></form>"
    )
    saved = ""
    if ui.drafts:
        saved = "<span class='muted small'>Saved drafts:</span> " + " ".join(
            f"<a class='fpill' href='/?draft={quote(n, safe='')}#draft'>{e(n)}</a>"
            for n in ui.drafts[-8:]
        )
    return f"<div class='filters' style='margin-top:12px'>{make}{saved}</div>"


def _short(iso: str) -> str:
    try:
        return _fmt_day(iso)
    except ValueError:
        return "—"


def _due(row: Row) -> str:
    return f"due {e(_short(row.deadline))}" if row.days_left is not None else ""


def _days_cell(row: Row) -> str:
    if row.days_left is None:
        return "<span class='muted'>—</span>"
    if row.overdue:
        return pill("rejected", f"Overdue {-row.days_left}d")
    if row.days_left < 14:
        return f"<span class='pill t-warning'>{row.days_left} days</span>"
    return f"{row.days_left} days"


def _stats(d: Dashboard, ui: UI | None) -> str:
    by: dict[str, int] = {}
    for r in d.rows:
        by[r.status] = by.get(r.status, 0) + 1
    sent = sum(1 for r in d.rows if r.submitted != "—")
    tiles: list[tuple[object, str, str]] = [
        (sent, "Requests sent", "var(--s1)"),
        (
            by.get("awaiting_broker_confirmation", 0) + by.get("submitted", 0),
            "Awaiting reply",
            _tone_var("submitted"),
        ),
        (by.get("confirmed", 0), "Broker says done", _tone_var("confirmed")),
        (by.get("removed", 0), "Verified removed", "var(--s3)"),
        (len(d.attention), "Need your attention", _tone_var("awaiting_user_input")),
    ]
    if ui and ui.limit:
        tiles.append((f"{ui.used}/{ui.limit}", "Sent today", "var(--neutral)"))
    return (
        "<section class='stats' aria-label='Summary'>"
        + "".join(
            f"<div class='stat'><div class='n'>{e(str(n))}</div>"
            f"<div class='l'><span class='dot' style='background:{c}'></span>{e(label)}</div></div>"
            for n, label, c in tiles
        )
        + "</section>"
    )


def _filters(d: Dashboard, status_filter: str | None, query: str) -> str:
    codes: dict[str, tuple[str, int]] = {}
    for r in d.rows:
        label, n = codes.get(r.status, (r.label, 0))
        codes[r.status] = (label, n + 1)
    q = f"&q={quote(query, safe='')}" if query else ""
    cur = " aria-current='page'" if not status_filter else ""
    pills = [
        f"<a class='fpill' href='/{('?q=' + quote(query, safe='')) if query else ''}'{cur}>"
        f"All <span class='c'>{len(d.rows)}</span></a>"
    ]
    for code, (label, n) in sorted(codes.items(), key=lambda kv: -kv[1][1]):
        active = " aria-current='page'" if status_filter == label else ""
        pills.append(
            f"<a class='fpill' href='/?status={quote(label, safe='')}{q}'{active}>"
            f"<span class='dot' style='background:{_tone_var(code)}'></span>{e(_clean(label, code))} "
            f"<span class='c'>{n}</span></a>"
        )
    return "<nav class='filters' aria-label='Filter by status'>" + "".join(pills) + "</nav>"


def dashboard(
    d: Dashboard,
    *,
    interactive: bool,
    status_filter: str | None = None,
    ui: UI | None = None,
) -> str:
    act = ui if interactive else None  # the interactive UI, when there is one
    with_actions = act is not None
    csrf = act.csrf if act else ""
    query = (ui.query if ui else "").strip()
    needle = query.lower()
    rows = [
        r
        for r in d.rows
        if (not status_filter or r.label == status_filter)
        and (not needle or needle in r.name.lower() or needle in r.slug.lower())
    ]
    shown = rows[:300]

    attn = ""
    if d.attention:
        items = "".join(
            f"<li><span><b>{e(name)}</b> <span class='muted'>· {e(why)}</span></span>"
            f"{row_form(slug, csrf, name) if with_actions else ''}</li>"
            for slug, name, why in d.attention[:40]
        )
        attn = (
            "<section class='card' aria-labelledby='attn-h'>"
            f"<h2 id='attn-h'>Needs your attention</h2><ul class='attn'>{items}</ul></section>"
        )

    trs = "".join(
        f"<tr><td class='name'>{e(r.name)}</td><td>{pill(r.status, r.label)}</td>"
        f"<td class='num'>{e(_short(r.submitted))}</td>"
        f"<td>{_days_cell(r)}<div class='muted small'>{_due(r)}</div></td>"
        f"<td class='small'>{e(r.last_check)}"
        f"{' ' + pill('failed', 'Form changed') if r.drifted else ''}</td>"
        f"{('<td>' + row_form(r.slug, csrf, r.name) + '</td>') if with_actions else ''}</tr>"
        for r in shown
    )
    more = (
        f"<p class='muted small'>Showing {len(shown)} of {len(rows)}. Search to narrow.</p>"
        if len(rows) > len(shown)
        else ""
    )
    if rows:
        table = (
            "<div class='table-wrap'><table><thead><tr><th scope='col'>Broker</th>"
            "<th scope='col'>Status</th><th scope='col'>Sent</th>"
            "<th scope='col'>Time left</th><th scope='col'>Last check</th>"
            f"{'<th scope=col>Action</th>' if with_actions else ''}</tr></thead>"
            f"<tbody>{trs}</tbody></table></div>{more}"
        )
    elif d.rows:
        table = "<p class='muted'>No requests match.</p>"
    else:
        table = "<p class='muted'>No requests yet. Send your first batch above.</p>"

    search = ""
    if interactive:
        keep = (
            f"<input type='hidden' name='status' value='{e(status_filter)}'>"
            if status_filter
            else ""
        )
        search = (
            "<form method='get' action='/' class='search' role='search'>"
            f"{keep}<input type='search' name='q' value='{e(query)}' "
            "placeholder='Search brokers' aria-label='Search brokers'>"
            "<button type='submit' class='secondary'>Search</button></form>"
        )
    toolbar = (
        f"<div class='toolbar'>{_filters(d, status_filter, query)}{search}</div>" if d.rows else ""
    )

    chart = (
        "<section class='card' aria-labelledby='chart-h'>"
        f"<h2 id='chart-h'>Progress over time</h2>{progress_chart(d.series)}</section>"
    )
    if act is not None:
        top = act.flash + f"<div class='grid'>{chart}{_send_panel(act)}</div>" + _draft_panel(act)
    else:
        top = chart

    note = (
        "<p class='muted small'>“Broker says done” is the broker's word. Only “Removed (verified)” "
        "means a later re-search confirmed the listing is gone.</p>"
    )
    requests = (
        "<section class='card' aria-labelledby='req-h'><h2 id='req-h'>Requests</h2>"
        f"{toolbar}{table}{note}"
        f"{_drafts_line(act) if act is not None else ''}</section>"
    )
    refresh = 3 if ui and ui.job.get("state") == "running" else None
    return page(
        "optout · dashboard",
        f"<h1>Your opt-out requests</h1>{_stats(d, ui)}{top}{attn}{requests}",
        nav=False,
        refresh=refresh,
        updated=d.generated,
    )


# --- secondary pages (kept for direct links) ---------------------------------


def message_box(text: str, ok: bool = True) -> str:
    return f"<div class='flash {'ok' if ok else 'bad'}' role='status'>{e(text)}</div>"


def broker_page(
    slug: str,
    name: str,
    enabled: bool,
    events: list[tuple[str, str, str]],
    csrf: str,
    flash: str = "",
) -> str:
    ev = "".join(
        f"<tr><td class='num'>{e(when)}</td><td>{e(kind)}</td><td>{e(note)}</td></tr>"
        for when, kind, note in events
    )
    hidden = _hidden(csrf, slug=slug)
    body = (
        f"<h1>{e(name)}</h1><p class='muted'>{e(slug)}</p>{flash}"
        "<section class='card'><h2>Record their reply</h2>"
        f"<form method='post' action='/act/reply' class='inline'>{hidden}"
        "<select name='outcome' aria-label='Reply'>"
        "<option value='deleted'>They say it's deleted</option>"
        "<option value='denied'>They refused</option>"
        "<option value='need-info'>They need more info</option>"
        "<option value='auto-reply'>Automated reply only (deadline keeps running)</option>"
        "</select><input name='note' maxlength='200' placeholder='Short note, no personal data' "
        "aria-label='Note'><button type='submit'>Save</button></form></section>"
        "<section class='card'><h2>Write a follow-up</h2>"
        f"<form method='post' action='/act/draft' class='inline'>{hidden}"
        "<select name='kind' aria-label='Draft type'>"
        "<option value='clarify'>Clarify: reply to an automated answer</option>"
        "<option value='followup'>Follow up: no response yet</option>"
        "<option value='appeal'>Appeal: they refused</option>"
        "<option value='complaint'>Attorney General complaint draft</option>"
        "</select><button type='submit'>Write draft</button></form>"
        "<p class='muted small'>Writes text for you to copy. Nothing is sent.</p></section>"
        "<section class='card'><h2>Include or skip this broker</h2>"
        f"<form method='post' action='/act/toggle' class='inline'>{hidden}"
        f"<input type='hidden' name='enabled' value='{0 if enabled else 1}'>"
        "<button type='submit' class='secondary'>"
        f"{'Disable (never contact)' if enabled else 'Enable'}</button></form></section>"
        "<section class='card'><h2>I sent a follow-up, appeal, or complaint</h2>"
        f"<form method='post' action='/act/escalation-sent' class='inline'>{hidden}"
        "<select name='kind' aria-label='Kind'><option>clarify</option><option>followup</option>"
        "<option>appeal</option><option>complaint</option></select>"
        "<button type='submit' class='secondary'>Mark as sent</button></form></section>"
        "<section class='card'><h2>History</h2><div class='table-wrap'><table><tbody>"
        f"{ev or '<tr><td>None</td></tr>'}</tbody></table></div></section>"
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
    batch = min(max(0, limit - used), len(eligible), 40)
    lines = "".join(f"<li>{e(name)}</li>" for _slug, name in eligible[:25])
    status = (
        _send_panel(UI(csrf=csrf, used=used, limit=limit, job=job))
        if job.get("state") in ("running", "done")
        else ""
    )
    if running:
        form = "<p class='muted'>A batch is running. This page refreshes by itself.</p>"
    elif batch == 0:
        form = f"<p class='muted'>Nothing to send right now ({used}/{limit} used today).</p>"
    else:
        form = (
            "<section class='card'><p>Review exactly what will be emailed before anything is "
            f"sent.</p><a class='fpill' href='/preview?count={batch}'>Preview {batch} emails</a>"
            "</section>"
        )
    body = (
        f"<h1>Send a batch</h1>{flash}"
        f"<p>{len(eligible)} brokers are ready, {skipped} skipped. "
        f"Daily limit: {used}/{limit} used.</p>{status}{form}"
        f"<section class='card'><h2>Next in line</h2><ul>{lines}</ul></section>"
    )
    return page("Send a batch", body, nav=True, refresh=3 if running else None)


def preview_page(
    items: list[tuple[str, str, str, str]], sender: str, csrf: str, flash: str = ""
) -> str:
    """items: (broker name, to address, subject, body)."""
    n = len(items)
    blocks = "".join(
        f"<details class='email'><summary><b>{e(name)}</b> <span class='muted'>→ {e(to)}"
        f"</span></summary><div><p class='muted small'>Subject: {e(subject)}</p>"
        f"<pre>{e(body)}</pre></div></details>"
        for name, to, subject, body in items
    )
    form = (
        "<section class='card'><p>Sending happens one email at a time with a short pause "
        "between them, and stops on repeated errors.</p>"
        f"<form method='post' action='/act/campaign' class='sendrow'>"
        f"{_hidden(csrf, count=str(n))}"
        f"<button type='submit' class='big'>Send these {n} emails</button> "
        "<a href='/'>Cancel</a></form></section>"
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
        "yourself, then mark it as sent.</p>"
        f"<form method='post' action='/act/make-drafts'>{_hidden(csrf)}"
        "<button type='submit' class='secondary'>Check for overdue requests and write drafts"
        f"</button></form><ul>{items}</ul>"
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
