"""HTML for the report and the local UI.

Every value that did not come from this file is passed through ``e()``: broker
names and contact data originate in public registries and are untrusted. Pages
use no JavaScript (the server's CSP forbids it); hover states are pure CSS and
chart tooltips are native SVG <title> elements.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from html import escape as e
from urllib.parse import quote

from ..labels import STATUS_LABEL
from .data import Attention, Dashboard, Row

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
.stat .sub{color:var(--muted);font-size:12px;margin-top:2px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none}
.muted{color:var(--muted)}.small{font-size:13px}
.ok{color:var(--good)}.bad{color:var(--critical)}
.flash{border-radius:10px;padding:10px 14px;margin:0 0 16px;border:1px solid var(--line);background:var(--surface)}
.flash.ok{border-color:color-mix(in srgb,var(--good) 40%,var(--line));color:var(--text)}
.flash.bad{border-color:color-mix(in srgb,var(--critical) 45%,var(--line));color:var(--text)}
.flash{display:flex;align-items:center;gap:10px;animation:rise .25s ease-out}
.flash::before{content:"";width:9px;height:9px;border-radius:50%;background:var(--good);flex:none}
.flash.bad::before{background:var(--critical)}
.flash.live::before,.live-dot{animation:glow 1.6s ease-in-out infinite}
.live-dot{width:9px;height:9px;border-radius:50%;background:var(--good);display:inline-block;
  margin-right:8px;vertical-align:middle}
@keyframes glow{0%,100%{opacity:.35;box-shadow:0 0 0 0 color-mix(in srgb,var(--good) 0%,transparent)}
  50%{opacity:1;box-shadow:0 0 0 5px color-mix(in srgb,var(--good) 28%,transparent)}}
@keyframes rise{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.notice{border-left:3px solid var(--warning)}
.notice.go{border-left-color:var(--accent)}
.pill{display:inline-flex;align-items:center;gap:6px;padding:2px 10px 2px 8px;border-radius:999px;
  font-size:12.5px;font-weight:600;line-height:20px;white-space:nowrap;color:var(--text);
  background:color-mix(in srgb,var(--tone) 14%,transparent);
  border:1px solid color-mix(in srgb,var(--tone) 35%,transparent)}
.pill .i{color:var(--tone);font-size:11px;line-height:1;display:inline-flex;align-items:center}
.pill .pd{width:7px;height:7px;border-radius:50%;background:var(--tone);flex:none}
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
button:active{transform:translateY(1px)}
button[disabled]{opacity:.7;cursor:progress}
button.big{padding:10px 18px;font-size:15px}
button.secondary{background:var(--surface);color:var(--text);border-color:var(--line)}
button.secondary:hover{border-color:var(--accent)}
form.inline{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap}
form.inline.rowform{flex-wrap:nowrap}
.rowform select{width:150px;padding:5px 8px;font-size:13px}.rowform button{padding:5px 12px;font-size:13px}
.search{display:flex;gap:6px}.search input{width:220px}
input.combo{width:260px}
.replies .rowform select{width:auto;max-width:280px}
.js .nojs{display:none}
.hdr-link{display:inline-flex;align-items:center;gap:6px;color:var(--text-2);text-decoration:none;
  font-size:14px;font-weight:600;padding:6px 10px;border-radius:8px;border:1px solid var(--line)}
.hdr-link:hover{color:var(--text);border-color:var(--accent)}
details.settings>summary{display:flex;align-items:baseline;gap:12px;cursor:pointer;list-style:none}
details.settings>summary::-webkit-details-marker{display:none}
details.settings>summary h2{margin:0}
details.settings>summary::after{content:"▸";margin-left:auto;color:var(--muted)}
details.settings[open]>summary::after{content:"▾"}
fieldset{border:1px solid var(--line);border-radius:10px;padding:12px 16px 16px;margin:16px 0}
legend{font-weight:700;padding:0 6px}
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px 16px}
.fld{display:flex;flex-direction:column;gap:4px;font-size:13px}
.fld>span{color:var(--text-2);font-weight:600}
.fld small{color:var(--muted)}
.fld.wide{margin-top:12px}
.check{display:flex;gap:8px;align-items:center;margin-top:12px;font-size:14px}
form.inline.pwform{align-items:flex-end;flex-wrap:nowrap}
.pwform .fld{flex:0 1 320px}
.pwform input,.pwform button{height:38px}
.pwform button{white-space:nowrap}
.how{margin:0 0 4px;color:var(--muted);font-size:13px}
.att{display:flex;gap:16px;justify-content:space-between;align-items:center;padding:12px 0;
  border-bottom:1px solid var(--line)}
.att:last-child{border-bottom:0}
.att-main{flex:1;min-width:0}
.att-head{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.att .quote{margin:4px 0 0;color:var(--text-2);font-size:14px;line-height:1.45;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.att p{margin:4px 0 0}
details.mini{margin-top:2px}
details.mini>summary{cursor:pointer;color:var(--accent);font-size:12.5px;width:max-content;
  list-style:none}
details.mini>summary::-webkit-details-marker{display:none}
details.mini[open]>summary{margin-bottom:6px}
.att:has(details.coverage[open]){align-items:flex-start}
.coverage{max-width:760px}
.coverage .checks{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));
  gap:2px 18px;margin:6px 0}
.coverage .check{margin:0;font-size:13px;align-items:flex-start}
.coverage .noterow{display:flex;gap:8px;margin-top:6px}
.coverage .noterow input{flex:1;min-width:0}
.coverage textarea{margin-top:8px}
.coverage form + form{margin-top:6px}
.att-actions{display:flex;gap:6px;align-items:center;flex:none}
.att-actions button{padding:6px 14px;font-size:13px}
a.ibtn{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;
  border-radius:8px;border:1px solid var(--line);background:var(--surface);color:var(--text-2)}
a.ibtn:hover{border-color:var(--accent);color:var(--text)}
@media (max-width:700px){.att{flex-direction:column;align-items:flex-start}}
a.btnlink{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border-radius:8px;
  border:1px solid var(--line);background:var(--surface);color:var(--text);text-decoration:none;
  font-weight:600;font-size:14px}
a.btnlink:hover{border-color:var(--accent)}
hr{border:0;border-top:1px solid var(--line);margin:18px 0}
body.busy main{opacity:.65;transition:opacity .15s}
body.busy{cursor:progress}
.js .search button{display:none}
.sendrow{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin-top:12px}
.sendrow label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--text-2)}
details.emails>summary,details.more>summary{cursor:pointer;font-weight:600;color:var(--accent);margin:8px 0}
details.email{margin:8px 0;border:1px solid var(--line);border-radius:10px;background:var(--surface-2)}
details.email>summary{cursor:pointer;padding:8px 12px;font-size:14px}
details.email>div{padding:0 12px 12px}
pre{background:var(--surface-2);border:1px solid var(--line);border-radius:10px;padding:12px 14px;
  white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;margin:8px 0}
details.email pre{background:var(--surface)}
.attn,.replies{list-style:none;margin:0;padding:0}
.reply{padding:12px 0;border-bottom:1px solid var(--line);display:grid;gap:6px}
.reply:last-child{border-bottom:0}
.reply-head{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.steps{padding-left:20px;display:grid;gap:10px}
.redacted{margin:6px 0 8px;padding:8px 10px;border-radius:8px;font-size:13px;
  background:color-mix(in srgb,var(--good) 10%,transparent);
  border:1px solid color-mix(in srgb,var(--good) 30%,transparent)}
details.peek{display:block;margin:8px 0 0}
details.peek>summary{cursor:pointer;color:var(--accent);font-size:13px;width:max-content}
details.peek pre{max-height:340px;overflow:auto}.steps li{line-height:1.6}
textarea{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;width:100%;padding:8px 10px;
  border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--text);
  margin:6px 0}
.hidden-copy{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}
.attn li{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;
  padding:10px 0;border-bottom:1px solid var(--line)}
.attn li:last-child{border-bottom:0}
.progress{height:8px;border-radius:999px;background:var(--surface-2);overflow:hidden;margin:8px 0 4px}
.progress>span{display:block;height:100%;background:var(--accent);border-radius:999px;
  transition:width .6s ease}
.bars{display:grid;gap:10px;margin:4px 0 8px}
.bar{display:grid;grid-template-columns:7.5em 1fr 3em;align-items:center;gap:10px;font-size:13px}
.bar .bl{color:var(--text-2)}.bar .bv{font-weight:700;font-variant-numeric:tabular-nums;text-align:right}
.bt{height:12px;background:var(--surface-2);border-radius:4px;overflow:hidden}
.bt>span{display:block;height:100%;border-radius:0 4px 4px 0}
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
@media (prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}
  .card,.stat{transition:box-shadow .15s,border-color .15s}}
@media (prefers-reduced-motion:reduce){.flash.live::before,.live-dot,.flash{animation:none}
  .progress>span{transition:none}}
@media (forced-colors:active){.pill,.fpill{border:1px solid CanvasText}}
"""


# Progressive enhancement for the dashboard. The server's CSP allows exactly this script
# (by hash); without it every page still works through plain forms and links.
#  - forms post in the background and the page body is swapped in place (no reloads)
#  - dropdowns apply as soon as you choose; search and status pills filter instantly
#  - batch progress updates live; copy buttons copy
LIVE_JS = """(() => {
const d = document;
d.documentElement.classList.add('js');
const strip = ['m', 'a', 'r', 's', 'live', 'draft', 'ds', 'dk', 'settings'];

const filter = () => {
  const box = d.getElementById('q');
  const q = box ? box.value.trim().toLowerCase() : '';
  const on = d.querySelector('a.fpill[data-filter][aria-current=page]');
  const status = on ? on.dataset.filter : '';
  let shown = 0;
  d.querySelectorAll('tr[data-name]').forEach((tr) => {
    const ok = (!status || tr.dataset.status === status) && (!q || tr.dataset.name.includes(q));
    tr.hidden = !ok;
    if (ok) shown++;
  });
  const none = d.getElementById('nomatch');
  if (none) none.hidden = shown > 0;
};

const swap = async (res) => {
  const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
  const fresh = doc.querySelector('main');
  if (!fresh) { location.href = res.url; return; }
  const keepQ = (d.getElementById('q') || {}).value || '';
  const y = window.scrollY;
  d.querySelector('main').replaceWith(fresh);
  d.title = doc.title;
  const url = new URL(res.url);
  strip.forEach((k) => url.searchParams.delete(k));
  history.replaceState(null, '', url.pathname + url.search);
  const box = d.getElementById('q');
  if (box && keepQ) box.value = keepQ;
  filter();
  fillZone();
  const draft = d.getElementById('draft');
  const settings = d.getElementById('settings');
  if (draft) draft.scrollIntoView({ block: 'start' });
  else if (settings && settings.open) settings.scrollIntoView({ block: 'start' });
  else window.scrollTo(0, y);
  start();
};

const post = async (form) => {
  d.body.classList.add('busy');
  const btn = form.querySelector('button[type=submit]:not(.nojs)');
  let dots = null;
  try {
    // Read the fields first: disabled fields are left out of a submission.
    const body = new URLSearchParams(new FormData(form));
    if (btn) {
      btn.style.width = btn.offsetWidth + 'px';  // keep the size while the label changes
      let n = 0;
      const show = () => { btn.textContent = 'Working' + '.'.repeat((n++ % 3) + 1); };
      show();
      dots = setInterval(show, 400);
    }
    form.querySelectorAll('button, select, input').forEach((el) => { el.disabled = true; });
    // getAttribute, not form.action: a field named "action" shadows that property.
    const res = await fetch(form.getAttribute('action'), { method: 'POST', body });
    if (res.ok) await swap(res); else location.reload();
  } catch (_) {
    location.reload();
  } finally {
    if (dots) clearInterval(dots);
    d.body.classList.remove('busy');
  }
};

d.addEventListener('submit', (e) => {
  const f = e.target;
  if (f.getAttribute('role') === 'search') { e.preventDefault(); filter(); return; }
  if ((f.getAttribute('method') || 'get').toLowerCase() !== 'post') return;
  e.preventDefault();
  post(f);
});

d.addEventListener('change', (e) => {
  const el = e.target;
  if (el.matches('form.auto select') && el.value && el.form.reportValidity()) post(el.form);
});

d.addEventListener('input', (e) => { if (e.target.id === 'q') filter(); });

const openSettings = () => {
  const panel = d.getElementById('settings');
  if (panel) { panel.open = true; panel.scrollIntoView({ block: 'start' }); }
};
if (location.hash === '#settings') openSettings();

d.addEventListener('click', (e) => {
  if (e.target.closest('a[href$="#settings"]')) { e.preventDefault(); openSettings(); return; }
  const pill = e.target.closest('a.fpill[data-filter]');
  if (pill) {
    e.preventDefault();
    d.querySelectorAll('a.fpill[data-filter]').forEach((p) => p.removeAttribute('aria-current'));
    pill.setAttribute('aria-current', 'page');
    filter();
    return;
  }
  const btn = e.target.closest('[data-copy]');
  if (!btn) return;
  const src = d.getElementById(btn.dataset.copy);
  if (!src) return;
  const label = btn.textContent;
  const done = () => { btn.textContent = 'Copied ✓'; setTimeout(() => { btn.textContent = label; }, 1600); };
  if (navigator.clipboard) navigator.clipboard.writeText(src.value).then(done, () => src.select());
  else { src.select(); d.execCommand('copy'); done(); }
});

let polling = false;
const tick = async () => {
  try {
    const res = await fetch('/api/job', { cache: 'no-store' });
    if (res.ok) {
      const j = await res.json();
      const p = d.getElementById('send');
      const pct = Math.round((100 * (j.sent + j.failed)) / Math.max(j.total, 1));
      const bar = p && p.querySelector('.progress>span');
      const txt = p && p.querySelector('[data-live]');
      if (bar) bar.style.width = pct + '%';
      if (txt) txt.textContent = j.sent + ' sent, ' + j.failed + ' failed of ' + j.total;
      if (j.state !== 'running') {
        polling = false;
        await swap(await fetch('/?m=finished'));
        return;
      }
    }
  } catch (_) { /* try again */ }
  setTimeout(tick, 1500);
};
const start = () => {
  const p = d.getElementById('send');
  if (!polling && p && p.dataset.state === 'running') { polling = true; setTimeout(tick, 1000); }
};

const fillZone = () => {
  const tz = d.getElementById('tz');
  if (tz && !tz.value) {
    try { tz.value = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (_) {}
  }
};

filter();
start();
fillZone();
})();"""

LIVE_JS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(LIVE_JS.encode()).digest()).decode()


def page(title: str, body: str, *, nav: bool, refresh: int | None = None, updated: str = "") -> str:
    # No-script fallback. The target has no #fragment: a refresh to the same URL with a
    # fragment is treated as an in-page jump and never reloads.
    meta = (
        f"<noscript><meta http-equiv='refresh' content='{int(refresh)};url=/?live=1'></noscript>"
        if refresh
        else ""
    )
    back = "<a href='/' class='small'>← Dashboard</a>" if nav else ""
    upd = f"<span class='updated'>Updated {e(updated)}</span>" if updated else ""
    settings_link = (
        "<a class='hdr-link' href='/#settings' aria-label='Settings'>"
        "<svg viewBox='0 0 24 24' width='18' height='18' aria-hidden='true'><path fill='currentColor' "
        "d='M19.4 13a7.5 7.5 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.4 7.4 0 0 0-1.7-1L15 3h-4l-.4 2.9"
        "a7.4 7.4 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.5 7.5 0 0 0 0 2l-2.1 1.6 2 3.5 2.5-1a7.4 7.4 0 0 0 "
        "1.7 1l.4 2.9h4l.4-2.9a7.4 7.4 0 0 0 1.7-1l2.5 1 2-3.5zM12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 "
        "0 0 1 0 7z'/></svg><span>Settings</span></a>"
        if updated
        else ""
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='light dark'>"
        "<meta name='robots' content='noindex,nofollow'>"
        "<meta name='referrer' content='same-origin'>"
        "<meta name='description' content='Your data-broker opt-out requests'>"
        f'<link rel="icon" type="image/svg+xml" href="{FAVICON}">'
        f"{meta}<title>{e(title)}</title><style>{CSS}</style></head><body>"
        "<a class='skip' href='#main'>Skip to content</a>"
        "<header class='topbar'><div class='wrap'>"
        f"<a class='brand' href='/' aria-label='optout home'>{_LOGO}<span>optout</span></a>"
        f"<span class='sub'>Data-broker opt-outs</span>{back}{upd}"
        f"{'' if nav else settings_link}</div></header>"
        f"<main id='main' class='wrap'>{body}</main><script>{LIVE_JS}</script></body></html>"
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
    "closed": ("neutral", "○"),
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


def _icon(icon: str) -> str:
    # A text "●" sits on the font baseline and looks off-centre; draw dots with CSS.
    if icon == "●":
        return "<span class='pd' aria-hidden='true'></span>"
    return f"<span class='i' aria-hidden='true'>{icon}</span>"


def pill(status: str, label: str) -> str:
    tone, icon = _PILL.get(status, ("neutral", "●"))
    text = _clean(label, status) if status in _SHORT and label in _STATUS_LABELS else _clean(label)
    return f"<span class='pill t-{tone}'>{_icon(icon)}{e(text)}</span>"


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
        # One day of data: a line needs two points, so compare the three totals as bars.
        peak = max(last[1], 1)
        bars = "".join(
            f"<div class='bar'><span class='bl'>{name}</span>"
            f"<span class='bt'><span style='width:{max(v / peak * 100, 0.8 if v else 0):.1f}%;"
            f"background:var({var})'></span></span><span class='bv'>{v}</span></div>"
            for (name, var), v in zip(_SERIES, last[1:], strict=True)
        )
        return (
            f"<div class='bars' role='img' aria-label='{e(summary)}'>{bars}</div>"
            "<p class='muted small'>The line over time starts after your second day.</p>"
            f"{table}"
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
    needs_import: bool = False  # no broker list loaded yet
    password_missing: bool = False
    inbox_status: str = ""  # "Checked 4 min ago" or why replies can't be read
    inbox_ok: bool = False
    # (id, ref, broker name or None, from, subject, date, guess, text)
    replies: list[tuple[str, str, str | None, str, str, str, str, str]] = field(
        default_factory=list
    )
    contacted: list[tuple[str, str]] = field(default_factory=list)  # (slug, name)
    claude_prompt: str = ""
    redactions: str = ""  # e.g. "your name ×6 · your email address ×2"
    learned_yaml: str = ""
    learned_count: int = 0
    settings: dict[str, str] = field(default_factory=dict)
    settings_open: bool = False


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
    (
        "Move to",
        [
            ("status:awaiting_broker_confirmation", "Awaiting reply"),
            ("status:confirmed", "Broker says done"),
            ("status:awaiting_user_input", "Needs input"),
            ("status:rejected", "Refused"),
            ("status:failed", "Failed"),
        ],
    ),
    (
        "Other",
        [
            ("status:closed", "Close (I'm done with this one)"),
            ("toggle:off", "Never contact this broker"),
        ],
    ),
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
        "<form method='post' action='/act/row' class='inline rowform auto'>"
        f"{_hidden(csrf, slug=slug)}"
        f"<select name='action' required aria-label='Action for {e(name or slug)}'>{opts}"
        "</select><button type='submit' class='nojs'>Go</button></form>"
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
            f"<p>{'<span class=live-dot></span>' if job.get('state') == 'running' else ''}"
            f"<b>{'Sending…' if job.get('state') == 'running' else 'Batch finished'}</b> "
            f"<span class='muted' data-live>{len(job.get('sent', []))} sent, "
            f"{len(job.get('failed', []))} failed of {job.get('total', 0)}</span></p>"
            f"<div class='progress' role='progressbar' aria-label='Sending progress' "
            f"aria-valuemin='0' aria-valuemax='100' aria-valuenow='{pct}'>"
            f"<span style='width:{pct}%'></span></div>{stopped}"
            f"{('<ul class=small>' + failed + '</ul>') if failed else ''}"
        )
    n = len(ui.previews)
    if job.get("state") == "running":
        inner = "<p class='muted small'>This updates by itself; you can keep using the page.</p>"
    elif not ui.configured:
        inner = "<p class='bad'>No config found. Run <code>optout init</code> first.</p>"
    elif n == 0 and ui.limit and ui.used >= ui.limit:
        inner = (
            f"<p>You've sent today's limit of <b>{ui.limit}</b>.</p><p class='muted small'>"
            "To send another batch now, raise <b>Emails per day</b> in "
            "<a href='#settings'>Settings</a>.</p>"
        )
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
        f"<section class='card' aria-labelledby='send-h' id='send' "
        f"data-state='{e(str(job.get('state', 'idle')))}'>"
        f"<h2 id='send-h'>Send a batch</h2>{status}{inner}</section>"
    )


_GUESS = {
    "deleted": ("good", "✓", "Says deleted"),
    "no-data": ("good", "✓", "Says no data"),
    "denied": ("critical", "✕", "Refused"),
    "need-info": ("warning", "!", "Wants something"),
    "auto-reply": ("info", "●", "Automated reply"),
    "bounced": ("serious", "!", "Bounced"),
    "unknown": ("neutral", "?", "Not sure"),
}
_REVIEW_OPTIONS = [
    ("deleted", "They deleted it / have no data"),
    ("denied", "They refused"),
    ("need-info", "They want me to do something"),
    ("auto-reply", "Automated reply only"),
    ("close", "Doesn't apply to me (close it)"),
    ("ignore", "Not about my request"),
]


def _guess_pill(kind: str) -> str:
    tone, icon, text = _GUESS.get(kind, _GUESS["unknown"])
    return f"<span class='pill t-{tone}'>{_icon(icon)}{e(text)}</span>"


def _replies_panel(ui: UI) -> str:
    head = (
        "<div class='toolbar'><span class='muted small'>"
        f"{e(ui.inbox_status)}</span>"
        + (
            "<form method='post' action='/act/inbox' class='inline'>"
            f"{_hidden(ui.csrf)}<button type='submit' class='secondary'>Check for replies"
            "</button></form>"
            if ui.inbox_ok
            else ""
        )
        + "</div>"
    )
    if not ui.replies:
        body = "<p class='muted'>No replies need a look. Clear answers are applied for you.</p>"
    else:
        # A searchable list: type part of a name, pick from the matches (no script needed).
        datalist = (
            "<datalist id='contacted-brokers'>"
            + "".join(
                f"<option value='{e(s)}' label='{e(n)}'>{e(n)}</option>" for s, n in ui.contacted
            )
            + "</datalist>"
        )
        items = []
        for rid, ref, name, sender, subject, when, guess, text in ui.replies:
            pick = (
                ""
                if name
                else "<input name='broker' list='contacted-brokers' required autocomplete='off' "
                "placeholder='Which broker? (type to search)' aria-label='Which broker sent this' "
                "class='combo'>"
            )
            opts = "".join(f"<option value='{v}'>{t}</option>" for v, t in _REVIEW_OPTIONS)
            items.append(
                "<li class='reply'>"
                f"<div class='reply-head'><span class='muted small'>{e(ref)}</span> "
                f"<b>{e(name or 'Unmatched sender')}</b> {_guess_pill(guess)}"
                f"<span class='muted small'>{e(when)}</span></div>"
                f"<div class='small'><span class='muted'>{e(sender)}</span> · {e(subject)}</div>"
                f"<details class='email'><summary>Show the email</summary><div><pre>{e(text)}"
                "</pre></div></details>"
                f"<form method='post' action='/act/review' class='inline rowform auto'>"
                f"{_hidden(ui.csrf, id=rid)}{pick}"
                "<select name='outcome' required aria-label='What did they say'>"
                f"<option value='' disabled selected>What did they say?</option>{opts}</select>"
                "<button type='submit' class='nojs'>Save</button></form></li>"
            )
        n = len(ui.replies)
        claude = (
            "<details class='more'><summary>"
            + (
                "Ask Claude to sort this reply"
                if n == 1
                else f"Ask Claude to sort these {n} replies"
            )
            + "</summary>"
            "<ol class='steps'><li>Copy the prompt. Your personal details are replaced with "
            "placeholders like [MY NAME] first."
            + (
                f"<div class='redacted'><b>Removed before copying:</b> {e(ui.redactions)}</div>"
                if ui.redactions
                else ""
            )
            + "<button type='button' class='secondary' data-copy='claude-prompt'>Copy prompt"
            "</button> <details class='peek'><summary>See exactly what will be copied</summary>"
            f"<pre>{e(ui.claude_prompt)}</pre></details></li>"
            "<li>Paste it into <a href='https://claude.ai/new' target='_blank' "
            "rel='noreferrer'>Claude</a> and send it.</li>"
            "<li>Paste Claude's answer here:"
            "<form method='post' action='/act/claude-apply'>"
            f"{_hidden(ui.csrf)}<textarea name='answer' rows='5' required "
            "placeholder='Paste the JSON Claude gives you' aria-label='Answer from Claude'>"
            "</textarea><button type='submit'>Apply Claude's answer</button></form></li></ol>"
            f"<textarea id='claude-prompt' class='hidden-copy' readonly aria-hidden='true' "
            f"tabindex='-1'>{e(ui.claude_prompt)}</textarea></details>"
        )
        body = (
            f"<p>{n} repl{'y' if n == 1 else 'ies'} the tool wasn't sure about:</p>"
            f"<ul class='replies'>{''.join(items)}</ul>{datalist}{claude}"
        )
    share = ""
    if ui.learned_yaml:
        share = (
            f"<details class='more'><summary>Share what you've learned "
            f"({ui.learned_count} reply pattern{'s' if ui.learned_count != 1 else ''})</summary>"
            "<p class='small'>Patterns from replies you've sorted, with your personal details "
            "removed. Contributing them means the next person's replies sort themselves. Check "
            "there's nothing personal, then "
            "<a href='https://github.com/Doaky/optout/issues/new?title=Known%20broker%20replies' "
            "target='_blank' rel='noreferrer'>open an issue</a> and paste them.</p>"
            "<button type='button' class='secondary' data-copy='learned'>Copy patterns</button>"
            f"<pre>{e(ui.learned_yaml)}</pre>"
            f"<textarea id='learned' class='hidden-copy' readonly aria-hidden='true' "
            f"tabindex='-1'>{e(ui.learned_yaml)}</textarea></details>"
        )
    return (
        "<section class='card' aria-labelledby='replies-h' id='replies'>"
        f"<h2 id='replies-h'>Broker replies</h2>{head}{body}{share}</section>"
    )


def _field(
    label: str, name: str, value: str, *, kind: str = "text", hint: str = "", attrs: str = ""
) -> str:
    return (
        f"<label class='fld'><span>{e(label)}</span>"
        f"<input type='{kind}' name='{name}' value='{e(value)}' {attrs}>"
        f"{f'<small>{e(hint)}</small>' if hint else ''}</label>"
    )


_COMMON_TZ = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "UTC",
]
_TZ_LIST = (
    "<datalist id='tz-list'>"
    + "".join(f"<option value='{z}'></option>" for z in _COMMON_TZ)
    + "</datalist>"
)


def _groups_fieldset(v: dict[str, str]) -> str:
    from ..audiences import GROUPS

    mine = set(v.get("my_groups", "").split(",")) if v.get("groups_set") else set()
    boxes = "".join(
        f"<label class='check'><input type='checkbox' name='group_{k}'"
        f"{' checked' if k in mine else ''}> {e(text)}</label>"
        for k, text in GROUPS.items()
    )
    status = (
        "Brokers that only cover groups you're not in are left out of batches."
        if v.get("groups_set")
        else "Not set yet: every broker is included. Save once (even with nothing ticked) to "
        "start skipping brokers that only cover other groups."
    )
    return (
        "<fieldset><legend>Groups you belong to</legend>"
        "<p class='muted small'>A few brokers only hold data on a specific group, like "
        f"educators or financial advisors. {e(status)}</p>"
        f"<input type='hidden' name='groups_present' value='1'>{boxes}</fieldset>"
    )


def _settings_panel(ui: UI) -> str:
    v = ui.settings
    if not v:
        return ""
    about = (
        "<fieldset><legend>About you</legend><div class='fgrid'>"
        + _field("Full legal name", "legal_name", v["legal_name"], attrs="required")
        + _field(
            "Other names", "aliases", v["aliases"], hint="Comma-separated: maiden name, nickname"
        )
        + _field(
            "Date of birth",
            "dob",
            v["dob"],
            kind="date",
            attrs="required",
            hint="Only some people-search forms use it; never put in emails",
        )
        + _field("Street", "street", v["street"], attrs="required autocomplete='address-line1'")
        + _field(
            "Apt / Suite / Unit", "street2", v["street2"], attrs="autocomplete='address-line2'"
        )
        + _field("City", "city", v["city"], attrs="required autocomplete='address-level2'")
        + _field(
            "State",
            "state",
            v["state"],
            attrs="required maxlength='2' autocomplete='address-level1' style='width:5em'",
        )
        + _field("ZIP", "zip", v["zip"], attrs="required autocomplete='postal-code'")
        + _field("Phone numbers", "phones", v["phones"], hint="Comma-separated; optional")
        + _field("Relatives on your listings", "relatives", v["relatives"], hint="Optional")
        + "</div><label class='fld wide'><span>Previous addresses</span>"
        f"<textarea name='previous' rows='3' placeholder='12 Oak St, Richmond, VA 23219'>"
        f"{e(v['previous'])}</textarea><small>One per line: street, city, ST ZIP. "
        "Your last one or two is plenty.</small></label></fieldset>"
    )
    sending = (
        "<fieldset><legend>Sending</legend><div class='fgrid'>"
        + _field(
            "Emails per day",
            "daily_limit",
            v["daily_limit"],
            kind="number",
            attrs="min='1' max='200' required",
            hint="Raise this to send another batch today. Gmail allows ~500; 40–100 is safer.",
        )
        + _field(
            "Pause between emails (seconds)",
            "send_delay_seconds",
            v["send_delay_seconds"],
            kind="number",
            attrs="min='0' max='120' required",
        )
        + _field(
            "Label for sent requests",
            "gmail_label",
            v["gmail_label"],
            hint="Gmail label, e.g. OPTOUT. Blank to skip.",
        )
        + _field(
            "Label for broker replies",
            "reply_label",
            v["reply_label"],
            hint="Replies get this label and are read automatically.",
        )
        + _field(
            "Send as (optional)",
            "from_address",
            v["from_address"],
            kind="email",
            hint="A 'Send mail as' address set up in Gmail. Blank = your account.",
        )
        + _field(
            "Per-broker alias (optional)",
            "alias_pattern",
            v["alias_pattern"],
            hint="Needs your own domain, e.g. optout-{slug}@yourdomain.com",
        )
        + _field(
            "Time zone",
            "timezone",
            v["timezone"],
            attrs="list='tz-list' id='tz' autocomplete='off' placeholder='e.g. America/New_York'",
            hint="For dates and the send time. Filled in from your browser if blank.",
        )
        + _field(
            "Send automatically at",
            "daily_send_time",
            v["daily_send_time"],
            kind="time",
            attrs="required",
            hint="Used when the box below is ticked, while optout is running.",
        )
        + "</div><label class='check'><input type='checkbox' name='allow_unattended'"
        f"{' checked' if v['allow_unattended'] == 'on' else ''}> Send each day's batch "
        "automatically at that time (no clicking Send)</label>"
        + _TZ_LIST
        + f"<p class='muted small'>Sending from <b>{e(v['sender'])}</b> via {e(v['host'])}. "
        "To change the account, run <code>optout init</code>.</p></fieldset>"
    )
    password = (
        "<form method='post' action='/act/password' class='inline pwform'>"
        f"{_hidden(ui.csrf)}<label class='fld'><span>App password</span>"
        "<input type='password' name='password' autocomplete='new-password' required "
        "placeholder='New app password'></label>"
        "<button type='submit' class='secondary'>Test &amp; save</button></form>"
        "<p class='muted small'>Signs in to check it before saving; it's never shown again. "
        "Gmail: <a href='https://myaccount.google.com/apppasswords' target='_blank' "
        "rel='noreferrer'>create an app password</a>.</p>"
    )
    return (
        f"<details class='card settings' id='settings'{' open' if ui.settings_open else ''}>"
        "<summary><h2>Settings</h2><span class='muted small'>Your details, sending, and "
        "Gmail</span></summary>"
        f"<form method='post' action='/act/settings'>{_hidden(ui.csrf)}{about}{sending}"
        f"{_groups_fieldset(v)}"
        "<fieldset><legend>Contributor tools</legend><label class='check'>"
        "<input type='checkbox' name='contributor_tools'"
        f"{' checked' if v.get('contributor_tools') == 'on' else ''}> Show coverage tools on "
        "&ldquo;Needs your attention&rdquo;</label><p class='muted small'>Adds a panel to each "
        "item for judging who a broker covers (educators only, financial advisors only, "
        "&hellip;), with a prompt you can paste into Claude. What you save can be shared back "
        "so other people skip brokers that don't apply to them. Limits: Claude can be wrong, "
        "so check its answer; a broker's own reply is the best evidence.</p></fieldset>"
        "<button type='submit' class='big'>Save settings</button></form>"
        f"<hr>{password}</details>"
    )


# Email click-tracking redirectors: the link works, but opening it tells the sender you did.
_TRACKERS = re.compile(
    r"(pstmrk\.it|ct\.sendgrid\.net|list-manage\.com|hubspotlinks\.com|mandrillapp\.com|"
    r"mailgun|links\.\w+\.com|click\.|track\.)",
    re.IGNORECASE,
)


def _link_note(host: str) -> str:
    if _TRACKERS.search(host):
        return (
            f" Their link goes through a click-tracking service (<b>{e(host)}</b>), so "
            "opening it tells them you clicked."
        )
    return f" Their link goes to <b>{e(host)}</b>."


_GMAIL_ICON = (
    "<svg viewBox='0 0 24 24' width='18' height='18' aria-hidden='true'>"
    "<path fill='#4285F4' d='M2 7v11.5c0 .55.45 1 1 1h3V10.2z'/>"
    "<path fill='#34A853' d='M18 10.2v9.3h3c.55 0 1-.45 1-1V7z'/>"
    "<path fill='#FBBC04' d='M18 5.2v5l4-3.2V6c0-1.3-1.5-2-2.5-1.2z'/>"
    "<path fill='#EA4335' d='M6 10.2V5.2l6 4.5 6-4.5v5l-6 4.5z'/>"
    "<path fill='#C5221F' d='M2 6v1l4 3.2v-5L4.5 4.8C3.5 4 2 4.7 2 6z'/></svg>"
)
_LINK_ICON = (
    "<svg viewBox='0 0 24 24' width='17' height='17' aria-hidden='true' fill='none' "
    "stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
    "<path d='M14 4h6v6'/><path d='M20 4l-9 9'/><path d='M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7"
    "a1 1 0 0 1 1-1h5'/></svg>"
)
_URL_IN_TEXT = re.compile(r"\s*[<(]?https?://[^\s>)]+[>)]?")


def _coverage_panel(a: Attention, csrf: str) -> str:
    from ..audiences import GROUPS

    boxes = "".join(
        f"<label class='check'><input type='checkbox' name='aud_{k}'"
        f"{' checked' if k in a.coverage_groups else ''}> {e(text)}</label>"
        for k, text in GROUPS.items()
    )
    everyone = (
        "<label class='check'><input type='checkbox' name='aud_everyone'"
        f"{' checked' if 'everyone' in a.coverage_groups else ''}> Everyone: the general public"
        "</label>"
    )
    return (
        "<details class='mini coverage'><summary>Coverage</summary>"
        "<p class='muted small'>Does this broker only hold data on a specific group? "
        "People outside it can skip it. Judge from their own reply; if you can't tell, "
        "leave it as Everyone.</p>"
        f"<form method='post' action='/act/coverage'>{_hidden(csrf, slug=a.slug)}"
        f"<div class='checks'>{boxes}{everyone}</div>"
        "<div class='noterow'>"
        f"<input name='note' maxlength='240' value='{e(a.coverage_note)}' "
        "placeholder='One line on who they cover (no personal details)' aria-label='Note'>"
        "<button type='submit' class='secondary'>Save</button></div></form>"
        + (
            "<p class='small'><b>Or ask Claude:</b> copy the prompt, paste it into "
            "<a href='https://claude.ai/new' target='_blank' rel='noreferrer'>Claude</a>, "
            "then paste its answer below. Your details are replaced with placeholders.</p>"
            f"<button type='button' class='secondary' data-copy='cov-{e(a.slug)}'>Copy prompt"
            "</button>"
            f"<form method='post' action='/act/coverage-claude'>{_hidden(csrf, slug=a.slug)}"
            "<textarea name='answer' rows='4' required placeholder='Paste Claude&#x27;s answer'"
            " aria-label='Answer from Claude'></textarea>"
            "<button type='submit' class='secondary'>Apply Claude&#x27;s answer</button></form>"
            f"<textarea id='cov-{e(a.slug)}' class='hidden-copy' readonly aria-hidden='true' "
            f"tabindex='-1'>{e(a.coverage_prompt)}</textarea>"
            if a.coverage_prompt
            else ""
        )
        + "</details>"
    )


def _attention_item(a: Attention, csrf: str, contributor: bool = False) -> str:
    def act(action: str, label: str, cls: str = "secondary") -> str:
        return (
            "<form method='post' action='/act/row' class='inline'>"
            f"{_hidden(csrf, slug=a.slug, action=action)}"
            f"<button type='submit' class='{cls}'>{e(label)}</button></form>"
        )

    icons: list[str] = []
    if a.gmail_url:
        icons.append(
            f"<a class='ibtn' href='{e(a.gmail_url)}' target='_blank' rel='noreferrer noopener' "
            "title='Open this email in Gmail' aria-label='Open this email in Gmail'>"
            f"{_GMAIL_ICON}</a>"
        )
    if a.link and a.kind == "need-info":
        where = (
            f"{a.link_host}: a click-tracking link (opening it tells them you clicked)"
            if _TRACKERS.search(a.link_host)
            else a.link_host
        )
        icons.append(
            f"<a class='ibtn' href='{e(a.link)}' target='_blank' rel='noreferrer noopener' "
            f"title='Open their link: {e(where)}' aria-label='Open their link ({e(a.link_host)})'>"
            f"{_LINK_ICON}</a>"
        )
    buttons: list[str] = []
    if a.kind == "need-info":
        buttons.append(act("status:awaiting_broker_confirmation", "Done", cls=""))
    elif a.kind == "refused":
        buttons.append(act("draft:appeal", "Appeal", cls=""))
    elif a.kind == "overdue":
        buttons.append(act("draft:followup", "Follow up", cls=""))
    if a.kind in ("need-info", "refused", "overdue"):
        buttons.append(act("status:closed", "Close"))

    quote = _URL_IN_TEXT.sub("", a.quote).replace("  ", " ").strip()
    quote_html = (
        f"<p class='quote'>“{e(quote)}”</p>" if quote else f"<p class='muted'>{e(a.hint)}</p>"
    )
    email = (
        f"<details class='mini'><summary>Show email</summary><pre>{e(a.email)}</pre></details>"
        if a.email
        else ""
    )
    tone = {"need-info": "warning", "refused": "critical", "overdue": "critical"}.get(
        a.kind, "serious"
    )
    return (
        "<li class='att'><div class='att-main'>"
        f"<div class='att-head'><b>{e(a.name)}</b> <span class='pill t-{tone}'>{e(a.why)}</span>"
        f"</div>{quote_html}{email}"
        f"{_coverage_panel(a, csrf) if contributor else ''}</div>"
        f"<div class='att-actions'>{''.join(icons)}{''.join(buttons)}</div></li>"
    )


def _notices(ui: UI) -> str:
    out = []
    if not ui.configured:
        out.append(
            "<section class='card notice go'><h2>Finish setting up</h2><p>Run "
            "<code>optout</code> (or <code>optout init</code>) in your terminal. It asks a few "
            "questions, checks your email login, and loads the broker list.</p></section>"
        )
        return "".join(out)
    if ui.password_missing:
        out.append(
            "<section class='card notice'><h2>Add your email app password</h2>"
            "<p>Sending needs it. In your terminal run <code>optout smtp-password</code> and "
            "paste the app password (for Gmail: "
            "<a href='https://myaccount.google.com/apppasswords' rel='noreferrer' "
            "target='_blank'>create one here</a>).</p></section>"
        )
    if ui.needs_import:
        out.append(
            "<section class='card notice go'><h2>Load the broker list</h2>"
            "<p>Downloads the public list of data brokers registered with California "
            "(about 580). Nothing is sent.</p>"
            f"<form method='post' action='/act/import'>{_hidden(ui.csrf)}"
            "<button type='submit' class='big'>Load broker list</button></form></section>"
        )
    return "".join(out)


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


def _covers_tag(row: Row) -> str:
    if not row.only_covers:
        return ""
    return (
        f"<div class='muted small' title='This broker only holds data on this group'>"
        f"Only covers: {e(row.only_covers)}</div>"
    )


def _reply_from(row: Row) -> str:
    if not row.reply_from:
        return ""
    shown = ", ".join(e(a) for a in row.reply_from[:2])
    more = f" +{len(row.reply_from) - 2}" if len(row.reply_from) > 2 else ""
    return f"<div class='muted small' title='Replies came from'>↩ {shown}{more}</div>"


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
    # (number, label, colour, one-line explanation)
    tiles: list[tuple[object, str, str, str]] = [
        (sent, "Requests sent", "var(--s1)", "Deletion requests that went out"),
        (
            by.get("awaiting_broker_confirmation", 0) + by.get("submitted", 0),
            "Awaiting reply",
            _tone_var("submitted"),
            "Sent; no answer yet",
        ),
        (
            by.get("confirmed", 0),
            "Broker says done",
            _tone_var("confirmed"),
            "They told you it's deleted",
        ),
    ]
    if d.verifiable or by.get("removed", 0):
        # Only meaningful for sites the tool can search itself; hidden otherwise so a
        # permanent 0 doesn't look like failure.
        tiles.append(
            (by.get("removed", 0), "Verified removed", "var(--s3)", "We searched; you're gone")
        )
    tiles.append(
        (
            len(d.attention),
            "Need your attention",
            _tone_var("awaiting_user_input"),
            "Something for you to do",
        )
    )
    if ui and ui.limit:
        tiles.append((f"{ui.used}/{ui.limit}", "Sent today", "var(--neutral)", "Daily limit"))
    return (
        "<section class='stats' aria-label='Summary'>"
        + "".join(
            f"<div class='stat'><div class='n'>{e(str(n))}</div>"
            f"<div class='l'><span class='dot' style='background:{c}'></span>{e(label)}</div>"
            f"<div class='sub'>{e(hint)}</div></div>"
            for n, label, c, hint in tiles
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
        f"<a class='fpill' data-filter='' "
        f"href='/{('?q=' + quote(query, safe='')) if query else ''}'{cur}>"
        f"All <span class='c'>{len(d.rows)}</span></a>"
    ]
    for code, (label, n) in sorted(codes.items(), key=lambda kv: -kv[1][1]):
        active = " aria-current='page'" if status_filter == label else ""
        pills.append(
            f"<a class='fpill' data-filter='{e(label)}' "
            f"href='/?status={quote(label, safe='')}{q}'{active}>"
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
    shown = rows[:3000]

    attn = ""
    if d.attention:
        if with_actions:
            items = "".join(
                _attention_item(a, csrf, contributor=d.contributor_tools) for a in d.attention[:40]
            )
        else:
            items = "".join(
                f"<li><span><b>{e(a.name)}</b> <span class='muted'>· {e(a.why)}</span></span></li>"
                for a in d.attention[:40]
            )
        how = (
            "<p class='how'>Do what they ask (their link or the email in Gmail), then click "
            "<b>Done</b>. Doesn't apply to you? <b>Close</b> it.</p>"
            if with_actions
            else ""
        )
        attn = (
            "<section class='card' aria-labelledby='attn-h'>"
            f"<h2 id='attn-h'>Needs your attention</h2>{how}<ul class='attn'>{items}</ul>"
            "</section>"
        )

    trs = "".join(
        f"<tr data-name='{e((r.name + ' ' + r.slug).lower())}' data-status='{e(r.label)}'>"
        f"<td class='name'>{e(r.name)}{_reply_from(r)}{_covers_tag(r)}</td>"
        f"<td>{pill(r.status, r.label)}</td>"
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
            f"<tbody>{trs}<tr id='nomatch' hidden><td colspan='7' class='muted'>No requests "
            "match.</td></tr></tbody></table></div>"
            f"{more}"
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
            f"{keep}<input type='search' id='q' name='q' value='{e(query)}' "
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
        top = (
            act.flash
            + _notices(act)
            + f"<div class='grid'>{chart}{_send_panel(act)}</div>"
            + _draft_panel(act)
        )
    else:
        top = chart

    note = (
        "<p class='muted small'>“Broker says done” is the broker's word. Only “Removed "
        "(verified)” means a later re-search confirmed the listing is gone. That's possible "
        "only for people-search sites with a public search page (<code>optout verify</code>). "
        "Brokers you emailed have no public search, so “Broker says done” is as far as they "
        "can go.</p>"
    )
    replies = _replies_panel(act) if act is not None else ""
    settings = _settings_panel(act) if act is not None else ""
    requests = (
        "<section class='card' aria-labelledby='req-h'><h2 id='req-h'>Requests</h2>"
        f"{toolbar}{table}{note}"
        f"{_drafts_line(act) if act is not None else ''}</section>"
    )
    refresh = 3 if ui and ui.job.get("state") == "running" else None
    return page(
        "optout · dashboard",
        f"<h1>Your opt-out requests</h1>{_stats(d, ui)}{top}{attn}{replies}{requests}{settings}",
        nav=False,
        refresh=refresh,
        updated=d.generated,
    )


# --- secondary pages (kept for direct links) ---------------------------------


def message_box(text: str, ok: bool = True, live: bool = False) -> str:
    cls = ("ok" if ok else "bad") + (" live" if live else "")
    return f"<div class='flash {cls}' role='status'>{e(text)}</div>"


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
