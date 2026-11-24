# Design Notes

This document explains the non-obvious technical decisions in OptOut — the ones where I picked one approach over a reasonable alternative and want to be able to explain why.

---

## Broker definitions are YAML, not Python

The core question when designing the broker layer was: what does it cost to add a new broker?

If broker logic lives in Python, adding a broker means writing code, running tests, reviewing a diff, and shipping a release. That's fine for five brokers. It doesn't scale to fifty, and it raises the barrier for outside contributors who know how to read a website's HTML but don't want to learn the codebase internals.

The YAML approach inverts this. Each broker is a declarative list of steps — navigate, fill, click, wait, prompt the user, capture a screenshot. Adding a broker is a data change, not a code change. The schema (`brokers/schema.py`) is a Pydantic discriminated union that validates every YAML at load time, so malformed broker definitions are caught before they run. CI validates all YAMLs on every push.

The tradeoff is expressiveness. Anything that doesn't fit the step vocabulary requires either a new step type (a Python change) or a workaround. So far the vocabulary has been sufficient for five real brokers with meaningfully different flows. When it isn't, adding a step type is a single-place Python change that immediately unlocks the new capability for every broker.

---

## Persistent Chrome profile instead of stealth plugins

The obvious approach for browser automation that needs to pass bot detection is to add a stealth library — `playwright-stealth`, undetected-chromedriver, or similar. I deliberately didn't do this.

OptOut uses `launch_persistent_context` with a real Chrome installation (`channel="chrome"`) and a persistent user data directory at `~/.config/optout/browser_profile/`. When a broker serves a Cloudflare Turnstile challenge or reCAPTCHA, the user solves it once in the headed browser. The resulting cookies and browser state are saved to the profile and reused on every subsequent run. Most brokers stop challenging after the first pass.

There are two reasons I went this route instead of stealth plugins:

**Legal posture.** OptOut submits real CCPA/CPRA opt-out requests on behalf of the person running it. Spoofing browser fingerprints to circumvent bot detection would add legal ambiguity to what is otherwise straightforwardly a legitimate privacy rights exercise. The tool is meant to be defensible — both technically and legally.

**Reliability.** Stealth plugins fight a cat-and-mouse battle with detection systems that gets updated constantly. A persistent real Chrome profile with legitimate cookies is just... a returning user. It doesn't require maintenance as detection techniques evolve.

The cost is that the tool only runs headed and requires the user to be present for the first run on each broker.

---

## Three ways to fill a form field

Playwright's `fill()` works on most inputs, but real-world opt-out forms include enough edge cases that the engine needs three distinct fill strategies, controlled per-field in the broker YAML.

**Standard (`fill()`)** — works for any visible input where the browser's native value setter is enough. This is the default.

**`force: true`** — some SPAs render inputs in the DOM before they're visually revealed, with `display: none` or zero dimensions. Playwright's default `fill()` waits for the element to be visible and times out. `force: true` waits for `state="attached"` instead and passes `force=True` to the fill call.

**`use_js: true`** — this one came from debugging Spokeo's opt-out form. Spokeo uses React controlled inputs (`value=""` set explicitly in the rendered HTML). Playwright's `fill()` and even `keyboard.type()` both set the DOM value, but React's virtual DOM treats the input as still empty because its internal state was never updated. On the next render cycle, React resets the field to `""`.

The fix is to bypass the DOM setter entirely and use the native `HTMLInputElement.prototype.value` setter, which React can't intercept, then dispatch a real `input` and `change` event so React's synthetic event system picks up the new value:

```javascript
const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value'
).set;
setter.call(element, value);
element.dispatchEvent(new Event('input', { bubbles: true }));
element.dispatchEvent(new Event('change', { bubbles: true }));
```

There was also a timing issue specific to Spokeo: reCAPTCHA's async initialization was triggering a React re-render that cleared the fields after the JS fill ran. The fix was to prompt the user to solve the reCAPTCHA *before* filling the fields, so the form is stable by the time automation touches it.

---

## Self-operated model, not authorized agent

CCPA and CPRA define two ways to submit opt-out requests: the consumer submits directly, or an "authorized agent" submits on their behalf. Authorized agents face additional verification requirements — brokers can demand proof of authorization, notarized forms, or signed permission letters before honoring a request.

OptOut is self-operated: the person running the tool is submitting requests for themselves, not on behalf of someone else. This sidesteps the authorized-agent verification path entirely. The legal basis is simpler, the flow is more reliable, and there's no identity verification friction.

Multi-profile support (running opt-outs for family members from one installation) would push the tool into authorized-agent territory. That's explicitly out of scope.

---

## Broker drift detection

Broker opt-out pages change. A selector that worked in April might break in June because the site shipped a redesign. Without active checking, the tool silently stops working and the user doesn't find out until they try to use it.

`optout drift-check` loads each scripted web-form broker's page and reports the ones whose steps no longer match (`optout brokers probe BROKER` checks one). It can send an alert to your webhook, and the dashboard marks a broker whose form may have changed. There is no scheduled workflow for this in the repository; run it yourself or from your own scheduler.

The point isn't to guarantee the tool always works — it can't, since opt-out pages change on their own schedule. The point is to make breakage visible quickly so it can be fixed before a user gets an error six steps into a form.
