# Legal basis and compliance

This page explains which laws OptOut's requests cite, how those citations are kept accurate, and what the law does and doesn't make a broker do. It describes the tool's behaviour; it is not legal advice. The statute quotes are checked word for word against each state's official text, but they have not been reviewed by a lawyer.

---

## You send every request yourself

OptOut runs on your computer and sends each request from your own email account, in your own name. There is no service in between, and every email says so: *"I am making this request myself, not through an agent or a service."*

This matters because privacy laws treat a request sent by someone acting for you differently from one you send yourself. California's rules, for example, let a business ask an authorized agent for proof of your signed permission, or ask you to confirm the agent's authority directly ([Cal. Code Regs. tit. 11, § 7063](https://law.cornell.edu/regulations/california/11-CCR-7063)). Brokers use steps like these to slow down requests from paid removal services. A request you send yourself avoids them.

The AGPL-3.0 licence supports this model: anyone who runs a modified OptOut as a service for other people must publish their changes.

---

## Which law your requests cite

Almost every broker OptOut contacts comes from California's public data broker registry. For these brokers, the email cites the deletion right of **the state you live in**, taken from your profile, but only if that state's law is in the verified list below. That list lives in [`src/optout/data/statutes.yml`](src/optout/data/statutes.yml).

- **Your state is listed:** the email quotes the law's deletion right word for word, with its citation.
- **Your state isn't listed:** the email says *"I make this request under the consumer privacy laws that apply to you"* and cites nothing. OptOut never cites a law whose wording it hasn't checked.
- **You live in California:** start with the state's own Delete Request and Opt-out Platform, DROP ([consumer.drop.privacy.ca.gov](https://consumer.drop.privacy.ca.gov/)). One request there reaches every registered broker. Since August 1, 2026, registered brokers have had to collect DROP requests at least every 45 days and act on them ([CalPrivacy: processing DROP requests](https://privacy.ca.gov/drop-for-data-brokers/process-drop-requests/)). The app's plan points California residents to DROP first. Emails to registry brokers then use the general wording above, because California isn't in the verified list.

The five people-search sites built into OptOut (BeenVerified, MyLife, Radaris, Spokeo, Whitepages) are handled through their web forms or by hand. Their files list the laws they fall under (`legal_basis`, for example CCPA and CPRA). That list is only used if one of them is ever sent an email.

### Verified state laws

| State | Law | Deletion right cited | Response deadline | Appeal answered within | Official text |
|---|---|---|---|---|---|
| Virginia | Virginia Consumer Data Protection Act | Va. Code § 59.1-577(A)(3) | 45 days, +45 once (§ 59.1-577(B)(1)) | 60 days | [law.lis.virginia.gov](https://law.lis.virginia.gov/vacode/title59.1/chapter53/section59.1-577/) |
| Texas | Texas Data Privacy and Security Act | Tex. Bus. & Com. Code § 541.051(b)(3) | 45 days, +45 once (§ 541.052) | — | [capitol.texas.gov](https://capitol.texas.gov/tlodocs/88R/billtext/html/HB00004F.htm) |
| Oregon | Oregon Consumer Privacy Act | ORS 646A.574(1)(c) | 45 days, +45 once (ORS 646A.574) | — | [oregonlegislature.gov](https://www.oregonlegislature.gov/bills_laws/ors/ors646A.html) |
| Montana | Montana Consumer Data Privacy Act | Mont. Code Ann. § 30-14-2808(1)(c) | 45 days, +45 once (§ 30-14-2808) | 60 days | [legmt.gov](https://archive.legmt.gov/bills/mca/title_0300/chapter_0140/part_0280/section_0080/0300-0140-0280-0080.html) |
| Delaware | Delaware Personal Data Privacy Act | 6 Del. C. § 12D-104(a)(3) | 45 days, +45 once (§ 12D-104) | 60 days | [delcode.delaware.gov](https://delcode.delaware.gov/title6/c012d/index.html) |
| New Hampshire | New Hampshire Privacy Act | RSA 507-H:4, I(c) | 45 days, +45 once (RSA 507-H:4) | 60 days | [gencourt.state.nh.us](https://www.gencourt.state.nh.us/rsa/html/LII/507-H/507-H-4.htm) |
| Minnesota | Minnesota Consumer Data Privacy Act | Minn. Stat. § 325M.14, subd. 1(d) | 45 days, +45 once (§ 325M.14) | — | [revisor.mn.gov](https://www.revisor.mn.gov/statutes/cite/325M.14) |
| Nebraska | Nebraska Data Privacy Act | Neb. Rev. Stat. § 87-1107(2)(c) | 45 days, +45 once ([§ 87-1108(2)](https://nebraskalegislature.gov/laws/statutes.php?statute=87-1108)) | — | [nebraskalegislature.gov](https://nebraskalegislature.gov/laws/statutes.php?statute=87-1107) |
| Rhode Island | Rhode Island Data Transparency and Privacy Protection Act | R.I. Gen. Laws § 6-48.1-5(e)(2) | Not cited | — | [rilegislature.gov](https://webserver.rilegislature.gov/Statutes/TITLE6/6-48.1/6-48.1-5.htm) |
| New Jersey | New Jersey Data Privacy Act | N.J.S.A. 56:8-166.10(a)(3) | 45 days, +45 once (§ 56:8-166.7(a)) | — | [pub.njleg.state.nj.us](https://pub.njleg.state.nj.us/Bills/2022/PL23/266_.HTM) |

All were last checked against their official sources on 2026-10-30. A dash means the app hasn't verified an appeal deadline for that law, not that the law has none. For Rhode Island, the app cites the deletion right but no response deadline, because that deadline hasn't been checked against the official text yet.

---

## What the email says

Emails to registry brokers are short by default. They give your name, address and contact email, plus the legal basis:

```
Basis: Va. Code § 59.1-577(A)(3) (Virginia Consumer Data Protection Act): "<the exact words of the deletion right>"
Where the law requires it, I ask that you respond within 45 days. If you need
more information to verify this request, reply to this email and I will
provide it. Please confirm in writing when deletion is complete.

I am making this request myself, not through an agent or a service.
```

Setting `email.identifiers: full` sends a longer version. It adds your date of birth, phones and previous addresses, and states the response deadline with its extension rule. The wording is in [`src/optout/templates/`](src/optout/templates/), and every email can be read in the dashboard's preview before anything is sent.

---

## How the citations stay accurate

Each entry in `statutes.yml` records the official URL, the exact words quoted, the deadline wording, and the date it was last verified. `optout statutes verify` fetches each official page and checks that every quote still appears word for word, ignoring only spacing and the difference between curly and straight quotes. If a legislature amends a section, the check fails before an outdated citation goes out. Entries that aren't marked verified are never cited.

To add a state: put the official legislature URL in `source`, copy the words exactly, run `optout statutes verify`, and set `verified` to that date.

---

## Deadlines, follow-ups and appeals

OptOut records a deadline for every request: the day it was sent plus the broker's response window, 45 days by default. When a deadline passes with no answer, the dashboard lists the broker under *Needs you*. `optout followups` emails every overdue broker again in one batch. `optout escalate` writes drafts for you to send yourself:
- **A broker that never answered:** a follow-up, then a complaint if it stays silent well past the deadline.
- **A broker that refused:** an appeal, then a complaint.

Most of these laws let a business extend its deadline once, by 45 days, if it tells you. Several also require it to offer an appeal when it refuses a request, and to answer the appeal within a set time (the "Appeal" column above).

---

## Enforcement

Under these state laws, enforcement is generally the state attorney general's job, not yours, so the next step after an ignored request or a refused appeal is a complaint to your attorney general. Virginia's law is a typical example:

- **Who enforces it:** the Attorney General has exclusive authority to enforce it.
- **Notice first:** before acting, the Attorney General must give the business 30 days' written notice to fix the problem.
- **No lawsuits of your own:** the law gives no private right of action, so you can't sue a broker under it yourself.

Source: [Va. Code § 59.1-584](https://law.lis.virginia.gov/vacode/title59.1/chapter53/section59.1-584/).

Other states' enforcement rules differ in the details. Check your own state's law before relying on them.

---

## What OptOut does not do

- **It doesn't guarantee removal.** It sends the request and tracks the answer; complying is the broker's legal duty.
- **It doesn't stop data coming back.** Brokers rebuild their records from public sources. OptOut emails each broker again after a set period (180 days by default), and `optout monitor` re-checks the people-search sites.
- **It doesn't send legal threats or file complaints for you.** It drafts follow-ups, appeals and complaints for you to review and send.
- **It doesn't cite laws that don't cover you.** Each state's law protects that state's residents, so a broker isn't legally bound to honour a request under another state's law, although many brokers accept requests from anyone.

---

## For contributors: legal basis on a hand-written broker

Brokers imported from the registry don't need anything here: they cite the user's state automatically. For one of the hand-written broker files in `src/optout/data/brokers/`, `legal_basis` lists the laws that apply to that broker, using keys from `src/optout/utils/statutes.py` (`CCPA`, `CPRA`, `GDPR`) or a verified state key from `statutes.yml`:

```yaml
legal_basis: [CCPA, CPRA]
statutory_response_days: 45   # the shortest deadline among those laws
```

`statutory_response_days` sets the deadline OptOut records for each request to that broker.
