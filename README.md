<p align="center">
  <img src="docs/logo.svg" width="72" height="72" alt="">
</p>
<h1 align="center">optout</h1>
<p align="center">
  <b>Get your personal data deleted from data brokers, from your own computer.</b>
</p>
<p align="center">
  <a href="https://github.com/Doaky/optout/actions/workflows/ci.yml"><img src="https://github.com/Doaky/optout/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue" alt="License: AGPL-3.0"></a>
  <img src="https://img.shields.io/badge/python-3.12%20%7C%203.13-blue" alt="Python 3.12 | 3.13">
  <img src="https://img.shields.io/badge/runs-locally-2a78d6" alt="Runs locally">
</p>

optout emails deletion requests to the ~580 data brokers registered with the State of
California, keeps track of who you contacted and when each legal deadline falls, and helps you
follow up with the ones that ignore you. It runs entirely on your machine: your details never
go to any server except the brokers you choose to email.

It's for one person: you. Each friend who wants to use it runs their own copy.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/dashboard-dark.png">
  <img src="docs/dashboard.png" alt="The optout dashboard: request totals, a progress chart, the send panel, and brokers needing attention">
</picture>

<sub>Sample data shown.</sub>

---

## Setup (about 5 minutes)

Tested on Linux and Windows (WSL); macOS should work the same way. You need a terminal and a Gmail account (other
providers work too, with one extra step).

**1. Install uv**, the tool that installs optout and the right Python for it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then close and reopen your terminal.

**2. Install optout:**

```bash
git clone https://github.com/Doaky/optout.git
cd optout
uv tool install --editable .
```

**3. Run it:**

```bash
optout
```

The first time, a short setup asks for:

| Step | What it asks | Notes |
|---|---|---|
| 1. About you | Name, address, date of birth; optionally old addresses, phone, relatives | Stays in a private file on your computer. Bulk emails only include your name, address, and email. |
| 2. Your email | Your email address, then an **app password** | Gmail and iCloud are detected and configured automatically: no server names to type. |
| 3. Broker list | Whether to download California's public broker list | Nothing is sent. |

Then your dashboard opens in your browser. From then on, `optout` just opens the dashboard.

### About the app password (Gmail)

Gmail won't let apps use your normal password. You create a separate 16-letter **app password**
just for optout. Setup links you straight to the right pages and checks it works before saving it:

1. Turn on 2-Step Verification: <https://myaccount.google.com/signinoptions/twosv>
2. Create an app password named `optout`: <https://myaccount.google.com/apppasswords>
3. Copy the 16 letters Google shows (like `abcd efgh ijkl mnop`) and paste them when asked.

If the app passwords page says it's unavailable, 2-Step Verification is off, or a work/school
administrator has disabled app passwords. Changed it later? Run `optout smtp-password`.

The password is stored in `~/.config/optout/smtp_password`, readable only by you.

---

## Using it

Everything happens on the dashboard (`optout`):

- **Send a batch.** Expand *Preview* to read every email exactly as it will be sent, then click
  **Send emails**. Up to 40 a day, a few seconds apart, so your account isn't flagged as a
  bulk mailer. About two weeks covers every broker.
- **Track requests.** Each broker has one row with a status, when you sent it, and how long
  until its legal deadline.
- **When a broker replies**, use the row's menu: *They say it's deleted*, *They refused*,
  *They need more info*, or *Automated reply only*.
- **Follow up** from the same menu: it writes a clarification, follow-up, appeal, or Attorney
  General complaint draft for you to copy into an email. Overdue requests show up under
  *Needs your attention*.

Other commands, if you prefer a terminal:

| Command | Does |
|---|---|
| `optout status` / `optout history` | Who you contacted, when, and what happened |
| `optout campaign --send` | Send a batch from the terminal |
| `optout log <company>` | Record a request you made yourself (e.g. a spammer) |
| `optout reply <broker> deleted` | Record a broker's answer |
| `optout escalate` | Write drafts for overdue or refused requests |
| `optout label` | Add your Gmail label to past sent emails and replies |
| `optout init` | Redo setup |

### Settings worth knowing

Your settings live in `~/.config/optout/config.yml`. Defaults are fine for most people:

```yaml
email:
  gmail_label: OPTOUT        # Gmail label put on sent requests (setup asks)
  daily_limit: 40            # emails per day
  send_delay_seconds: 5      # pause between emails
  alias_pattern: "optout-{slug}@yourdomain.com"   # optional: a different address per broker
```

With `alias_pattern` (needs your own domain with a catch-all), each broker gets its own
address, so spam to `optout-acme@…` tells you exactly who sold your data.

---

## Where to start: the plan

The dashboard's **Your plan** (or `optout plan`) groups every broker by what it takes, in the
order worth doing them:

1. **People-search sites**: they show your address to anyone. Their own forms, done with a
   browser agent (Claude in Chrome): the dashboard gives you the prompt, you paste its answer back.
2. **Email only**: a plain email is enough. One click sends a batch.
3. **Email + confirm link**: an email, then they send a link to click (clicked for you when
   "Confirm links" is set to automatic).
4. **Web forms**: brokers that only act on their own form. Learned from their replies ("we don't
   accept email, use our portal"), so brokers move here as they answer.
5. **Forms + more**: a form plus something only you can do: an SMS or phone code, an account,
   payment.
6. **Asks too much**: skipped (see below).

## Brokers that ask too much

These are off by default. You can still turn one on from its page if you decide it's worth it.

| Broker | Site | What it wants |
|---|---|---|
| Blis Global | blis.com | only your phone's advertising ID or a browser cookie, not your name |
| Clearview AI | clearview.ai | a photo of your face |
| FaceCheck.ID | facecheck.id | an ID or a photo of your face |
| Kargo Global | kargo.com | only your phone's advertising ID or a browser cookie, not your name |
| Madhive | madhive.com | only an IP address or a browser cookie, not your name |
| Matchbook Data | matchbookdata.com | only your phone's advertising ID, not your name |
| PimEyes | pimeyes.com | an ID image |

For the advertising-ID ones, the better move is on your phone: reset its advertising ID and turn
on *Limit ad tracking* (iOS: Settings → Privacy → Tracking; Android: Settings → Privacy → Ads).
The list comes from `src/optout/data/broker_traits.yml` and `people_search.yml`.

---

## Run it on a schedule with GitHub Actions

`.github/workflows/scheduled.yml` runs `optout daily` once a month (send the next batch,
read replies, draft follow-ups) and refreshes California's broker list. It's off until you
turn it on, and it only runs in a **private** repository.

1. Make a private copy of this repo (GitHub: *Import repository*, or push a clone to a new
   private repo).
2. Pick a long passphrase and save your history to it, so nobody gets emailed twice:
   ```
   export OPTOUT_STATE_KEY='a long passphrase you keep somewhere safe'
   optout state push        # encrypts your history onto the optout-state branch
   ```
3. Add repository **secrets**: `OPTOUT_STATE_KEY` (the same passphrase),
   `OPTOUT_SMTP_PASSWORD` (your Gmail app password), and `OPTOUT_CONFIG_B64`
   (`base64 -w0 ~/.config/optout/config.yml`, with `email.allow_unattended: true` set).
4. Add the repository **variable** `OPTOUT_SCHEDULE` = `on`. Run it once from the Actions tab
   to check.

The workflow restores your history, runs, and saves it back. Before using the dashboard on
your computer, `optout state pull` to see what it did, and `optout state push` afterwards, so
both sides stay in step. The history is encrypted (AES-256, never your profile or password),
and the branch keeps only the latest copy. Change the `cron:` line for weekly runs.

---

## What to expect (honestly)

- **Sent isn't deleted.** Many brokers reply with an automated message, ask for more
  information, or don't answer. The tool tracks deadlines so you know when to follow up.
- **"Broker says done" is their word.** Only people-search sites the tool can re-check
  reach "Removed (verified)".
- **The legal footing depends on your state.** Emails cite your state's privacy law for
  Virginia, California, Colorado, Connecticut, and Utah, and use general wording elsewhere.
  Only the Virginia citation has been checked against current statute text in this project.
  California residents should also use the state's free DROP (Delete Request and Opt-out
  Platform), which reaches every registered broker at once.
- **Some brokers are exempt.** Credit, finance, and health-regulated brokers are off by
  default because state deletion laws usually exclude that data.
- **Data comes back.** Brokers re-acquire information. Re-running every few months
  (it skips anyone you contacted in the last 90 days) keeps it down.
- **It won't stop spam you already get** from breaches or lists you joined; it reduces how much
  your details are bought and sold.

## Privacy and safety

- Runs locally. No telemetry. Settings and the database are private to your user account.
- The dashboard only listens on `127.0.0.1` and needs the one-time link optout opens for you.
  It checks every request's origin, uses a strict Content-Security-Policy, and escapes all
  broker data.
- Nothing is sent without you clicking Send (or explicitly enabling unattended sending).
- A request is only marked removed after a later re-search shows the listing gone.
- It never asks for an SSN, ID, or payment details.

## For developers

```bash
uv sync --all-extras
uv run pytest            # tests (a few drive a real browser if Playwright's Chromium is installed)
uv run ruff check src tests && uv run mypy src/optout/
```

Advanced pieces not covered above: per-broker web-form automation for people-search sites
(`optout queue` / `optout submit`, needs `uv run playwright install chromium`),
`optout verify` (re-search to confirm removal), `optout daily` (cron entry point),
`optout drift-check` (detect broken broker forms), and a `Dockerfile` for an unattended
email-only lane (untested).

## License and credits

AGPL-3.0. Derived from [Blake104/OptOut](https://github.com/Blake104/OptOut); ideas from
[auto-identity-remove](https://github.com/stephenlthorn/auto-identity-remove) and
[Privotron](https://github.com/kevinl95/Privotron) (both MIT). Extra brokers and form
links from [Eraser](https://github.com/digisamroc/eraser) (MIT); people-search priorities
checked against [BADBOOL](https://github.com/yaelwrites/Big-Ass-Data-Broker-Opt-Out-List).
See `NOTICE`.
