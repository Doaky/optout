# Contributing to optout

Most of what makes optout work is data, not code: which address a broker really answers,
what its replies mean, how its web form is filled in. You can contribute all of that without
writing Python.

---

## Share what the tool learned from your replies (easiest)

As you use optout, it learns from the replies you sort and the forms you fill. Print what it
learned, with your personal details removed, and post it as a new GitHub issue:

| What | How to get it |
|---|---|
| What broker replies mean, a broker's real contact address (from its own reply), what its process requires, who it covers | Dashboard: *Broker replies → Share what you've learned* (or `optout replies share` for reply patterns only) |
| Web-form steps (field names and labels only) | `optout forms share` |

Each block it prints says which file under `src/optout/data/` it belongs in. To judge who a
broker covers (educators only, doctors only...), turn on Settings → *Help improve optout*.

Read it before you post it. Each data file starts with a comment explaining its format, if
you'd rather open a pull request.

Where brokers come from: most are generated from California's data broker registry
(`optout brokers import`), plus `extra_brokers.yml` and `people_search.yml`. Don't hand-edit
the generated ones; corrections go in `contact_overrides.yml`, `broker_traits.yml` and the
other data files. The broker YAML files described below are only for the few people-search
sites with scripted web-form steps.

---

## Adding a scripted web-form broker

### 1. Verify the opt-out process manually first

Before writing any YAML, go through the broker's opt-out flow yourself in a browser. Note:

- Is the opt-out a web form or an email address?
- If a web form: what fields does it have? What selectors?
- Does it require finding your listing URL first?
- Does it send a verification email or SMS?
- Does it show a CAPTCHA?
- What does the confirmation screen look like?

Do not guess at selectors. Inspect the live page.

### 2. Copy the template

```bash
cp src/optout/data/brokers/whitepages.yml src/optout/data/brokers/your-broker.yml   # web-form with listing search
# or
cp src/optout/data/brokers/spokeo.yml src/optout/data/brokers/your-broker.yml       # simple web form
```

### 3. Fill in the top-level fields

```yaml
slug: your-broker           # MUST match the filename (your-broker.yml)
name: Your Broker           # human-readable name shown in the CLI
domain: yourbroker.com
category: people_search     # people_search | marketing | background_check | other
parent_company: null        # or "Acme Data Corp" if applicable
method: web_form            # web_form | email | postal | manual

# Exactly one of these:
opt_out_url: "https://yourbroker.com/optout"
opt_out_email: null
opt_out_postal_address: null
```

### 4. Set the requirements flags

```yaml
required_fields: [full_name, current_address, email_current]
optional_fields: [dob, aliases, previous_addresses]

requires_listing_url: true      # true if user must find their listing URL first
requires_phone_verification: false
requires_email_verification: true
requires_id_upload: false
requires_notarization: false
```

These flags drive the CLI's pre-flight checklist. Set them exactly — if the broker requires phone verification, `requires_phone_verification` must be `true` and the steps must include an `sms_verification_prompt` step.

### 5. Set the legal metadata

```yaml
legal_basis: [CCPA, CPRA]  # from: CCPA, CPRA, GDPR, VCDPA, CPA, CTDPA, UCPA
statutory_response_days: 45
re_add_window_days: 60     # how soon data typically reappears; null if unknown

last_verified_at: 2026-04-20   # today's date — update when you re-verify
notes: |
  Free-form notes about quirks, caveats, and anything a future contributor
  should know. Mention if selectors are fragile, if the broker frequently
  rejects or ignores requests, etc.
```

### 6. Add the scan block

The scan block lets `optout scan` check whether your data appears on this broker:

```yaml
scan:
  search_url_template: "https://yourbroker.com/search?q={name_slug}&state={state}"
  result_selector: ".person-card, .search-result"
  match_fields: [full_name, current_address]
```

Template variables available in `search_url_template`:
- `{name_slug}` — URL-encoded full name (e.g. `Jane-Public`)
- `{state}` — two-letter address state code (e.g. `TX`)

### 7. Write the steps (web_form brokers only)

Steps are executed in order by the Playwright interpreter in `engine/methods/web_form.py`. Email-method brokers leave `steps: []`.

#### Step types

**`navigate`** — load a URL

```yaml
- id: open_optout_form
  type: navigate
  url: "https://yourbroker.com/optout"
```

**`search`** — search for the user's listing and prompt them to pick one

```yaml
- id: find_listing
  type: search
  search_url_template: "https://yourbroker.com/search?q={name_slug}&state={state}"
  result_selector: ".person-card"
  pick_strategy: prompt_user     # always prompt_user — never auto-pick
```

Required when `requires_listing_url: true`. The selected URL is stored as `{state.found_listing_url}` for use in later steps.

**`form_fill`** — fill one or more fields

```yaml
- id: fill_form
  type: form_fill
  fields:
    listing_url:
      selector: "input[name='url'], input[placeholder*='yourbroker.com']"
      value: "{state.found_listing_url}"
    email:
      selector: "input[name='email'], input[type='email']"
      value: "{profile.emails.current[0]}"
```

Field name (e.g. `listing_url`, `email`) is a label for logging only. The `selector` is a CSS selector. Use comma-separated alternatives for resilience.

**`prompt_user_if_present`** — pause and prompt the user when a selector is visible

```yaml
- id: handle_captcha
  type: prompt_user_if_present
  selector: ".g-recaptcha, iframe[src*='recaptcha'], .recaptcha-checkbox"
  message: "Please solve the CAPTCHA in the browser window, then press Enter."
```

Use this for: CAPTCHAs, listing-selection UI, email-verification instructions. The step is a no-op if the selector is not found on the current page.

**`click`** — click a button or element

```yaml
- id: submit
  type: click
  selector: "button[type='submit'], button.optout-submit"
```

**`sms_verification_prompt`** — enter an SMS code

```yaml
- id: phone_verify
  type: sms_verification_prompt
  code_selector: "input[name='code'], input[type='tel']"
  message: "Enter the SMS code YourBroker sent to the phone number on the listing:"
```

Required when `requires_phone_verification: true`.

**`capture`** — take a confirmation screenshot (always the last step)

```yaml
- id: capture_confirmation
  type: capture
  method: screenshot
  save_as: "{submission.id}-your-broker-confirmation.png"
```

#### Template variables in step values

| Variable | Resolves to |
|---|---|
| `{name_slug}` | URL-encoded full name |
| `{state}` | Two-letter address state code OR step state object (context-dependent) |
| `{state.found_listing_url}` | URL picked during a `search` step |
| `{profile.legal_name}` | User's full legal name |
| `{profile.emails.current[0]}` | User's primary email address |
| `{profile.phones.current[0]}` | User's primary phone number |
| `{profile.current_address.city}` | City from the user's current address |
| `{submission.id}` | UUID of the current submission record |
| `{broker.slug}` | The broker's slug |

### 8. Validate before opening a PR

```bash
# Install dev dependencies (requires uv)
uv sync --extra dev

# Validate your new file against the schema
uv run python -c "
from pathlib import Path
from optout.brokers.loader import load_broker
b = load_broker(Path('src/optout/data/brokers/your-broker.yml'))
print(f'OK: {b.slug} ({b.method})')
"

# Run the full broker test suite
uv run pytest tests/test_production_brokers.py -v
```

The CI pipeline runs `validate-brokers` on every push and PR, so any schema error will be caught there too.

### 9. Verification checklist

Before committing a new or updated broker YAML, confirm:

- [ ] `slug` exactly matches the filename stem (`your-broker.yml` → `slug: your-broker`)
- [ ] `method` is consistent with `opt_out_url` / `opt_out_email`
- [ ] `requires_listing_url: true` ↔ there is a `search` step
- [ ] `requires_phone_verification: true` ↔ there is an `sms_verification_prompt` step
- [ ] All step `id` values are unique within the broker
- [ ] Web-form brokers end with a `capture` step
- [ ] `last_verified_at` is set to today's date
- [ ] You personally ran through the opt-out flow in a browser and confirmed the selectors match the live page
- [ ] Notes explain any known quirks (fragile selectors, re-add behavior, response time)
- [ ] `uv run pytest tests/test_production_brokers.py` passes locally

---

## Updating an existing broker

When a broker changes their opt-out flow:

1. Go through the new flow manually.
2. Update the affected `steps` and any flags.
3. Update `last_verified_at` to today.
4. Add a note in the `notes` block describing what changed and when.
5. Run the verification checklist above.

---

## Code contributions

For anything beyond broker YAMLs, open an issue first to discuss the approach. The project has a deliberate scope:

- No SaaS, no multi-tenancy, no accounts.
- No programmatic CAPTCHA solving.
- No submission on behalf of users other than the operator.
- No bot-detection evasion (stealth plugins, proxy rotation, fingerprint spoofing).

These are not gaps to fill — they are the project's legal foundation.

### Dev setup

```bash
git clone https://github.com/Doaky/optout.git
cd optout
uv sync --extra dev
uv run playwright install chromium   # only for the few tests that drive a browser
uv run pytest
uv run ruff check src/ tests/
uv run mypy src/optout/
```

To run the app against throwaway data instead of your own, point it at empty folders:
`XDG_CONFIG_HOME=/tmp/o/cfg XDG_DATA_HOME=/tmp/o/data uv run optout`.

### Running CI checks locally

```bash
uv run ruff check src/ tests/            # lint
uv run ruff format --check src/ tests/   # format
uv run mypy src/optout/                  # types
uv run pytest                            # tests
```

---

## Conduct

Be straightforward. The project exists to help people remove their personal data from commercial databases. Keep contributions focused on that.
