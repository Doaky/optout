# optout: self-hosted data-broker opt-out (for one person)

Sends deletion requests to data brokers on your behalf, remembers who you've
contacted and when, tracks the legal deadline, re-checks whether your listing is
actually gone, and drafts follow-ups when a broker ignores you. Personal use only:
one profile, no acting as an agent for anyone else.

Derived from [Blake104/OptOut](https://github.com/Blake104/OptOut) (AGPL-3.0). See `NOTICE`.

## What it does

| | |
|---|---|
| `optout brokers import` | Builds ~580 email-method broker definitions from California's public registry. Disabled by default for FCRA/GLBA/HIPAA-regulated entities. |
| `optout campaign` | Previews, then (with `--send` and a typed confirmation) emails brokers. Capped per day, paced, stops on repeated failures. |
| `optout log <company>` | Records a request you made yourself (spammers, phone calls, one-off emails). |
| `optout history` / `optout status` | Who you contacted and when; deadline countdown; last verification; drift flags. |
| `optout verify` | Re-searches people-search sites 7 days after a request. **The only way anything becomes "removed".** |
| `optout escalate` | Writes follow-up, appeal, and Attorney General complaint *drafts*. You send/file them. |
| `optout daily` | One cron entry point: capped sends (if you allow it), escalation drafts, webhook summary. |
| `optout brokers probe/enable/disable` | Check selectors against the live site; skip brokers you want to stay listed on. |

## Quick start

```bash
uv sync --all-extras
uv run playwright install chromium        # only for the browser lane
uv run optout init                        # or copy examples/config.example.yml to ~/.config/optout/config.yml
export OPTOUT_SMTP_PASSWORD=...           # an app password, not your main one
uv run optout brokers import              # downloads the California registry CSV
uv run optout campaign                    # PREVIEW: shows who would be emailed and a sample
uv run optout campaign --send --limit 10  # start small; type the confirmation
uv run optout status
```

Config goes in `~/.config/optout/config.yml` (mode 0600). Useful email settings:

```yaml
email:
  alias_pattern: "optout-{slug}@yourdomain.com"   # per-broker alias via a catch-all
  from_address: optout@yourdomain.com             # otherwise From is your SMTP username
  identifiers: minimal                            # name + address + alias only (default)
  daily_limit: 40
  send_delay_seconds: 30
  allow_unattended: false                         # true lets `optout daily` send on its own
notify:
  webhook_url: https://ntfy.sh/your-private-topic  # counts and slugs only, never PII
```

With an alias per broker, a spam email sent to `optout-acme@yourdomain.com` tells you
which broker leaked or resold your address.

## Two lanes

- **Unattended (email):** `optout daily` from cron/systemd (or the container below). Deadline
  tracking, escalation drafts, webhook summary.
- **Attended (browser):** web forms with CAPTCHA or email-link verification, plus
  `verify`. Run on a machine with a real browser. CAPTCHAs and verification are done
  by you, in the window; there is no CAPTCHA-solving service and no fingerprint spoofing.

**Sharing state.** Both lanes use one SQLite file (`~/.local/share/optout/optout.db`).
The simplest correct setup is one machine owning it. Start by running everything from
the desktop (cron or Task Scheduler for `optout daily`). Don't put the file on a sync
service; SQLite and file sync corrupt each other. If you later want the homelab to own it,
run the browser commands over SSH with X forwarding or add an export/import step (not built).

## Container (unattended lane only; untested)

`Dockerfile` and `docker-compose.yml` run `optout daily` once a day. The author had no
Docker available when writing them, so build and check them before relying on them.

## Honest limitations

- **Sent is not removed.** Email requests to registry brokers can't be re-searched, so they stay
  "Submitted, awaiting reply" until the broker answers. Only people-search sites with a `scan`
  config can reach "Removed ✓ verified".
- **The legal footing is thin in Virginia.** Requests cite the Virginia Consumer Data Protection
  Act, Va. Code § 59.1-577. It excludes some data and some businesses, and California's CCPA
  deadlines don't bind brokers about a Virginia resident. Many brokers honor requests anyway;
  some will refuse. California's DROP platform is for California residents.
- **Registry emails go stale.** Expect bounces. Contacts come from brokers' own filings and
  aren't verified.
- **Broker sites change.** Selectors decay and Cloudflare blocks automation; `drift-check`
  tells you when. Radaris's domain was seized by court order (2026-08); it's marked manual.
- **Data comes back.** Brokers re-acquire it. Re-run `campaign` every 90 days (it skips anyone
  contacted more recently) and `verify` weekly.
- **It won't stop spam from breaches or marketing lists you signed up for.** Per-broker aliases
  help you find out where it came from.

## Safety rules the code enforces

- Nothing is sent without `--send` plus a typed confirmation (or `allow_unattended`).
- A request becomes `removed` only via `mark_removed()` after a stored, later, clean re-search.
  Block pages, timeouts, and layout changes are "unverifiable", never "clear".
- No SSN, ID, or payment data is ever asked for or stored.
- Registry-supplied URLs are stored as text and never opened; email addresses are validated strictly.
- `tests/test_no_pii_in_repo.py` fails if your config values or SSN-shaped strings appear in tracked files.
