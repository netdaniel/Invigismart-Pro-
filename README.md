# Invigismart Pro

Modern Web-Based SaaS application for school exam invigilation roster management
with automated constraint solving (1:30 student ratio, no own-subject
invigilation, no clashing teaching slots, balanced workload), annual subscription
licensing, CSV/JSON data management, and a server-stored PDF document library.

The single-page app in `index.html` is now backed by a real server that owns
**accounts, subscriptions and the roster solver**. The paywall is no longer a
client-side boolean: entitlement lives in a database, is verified on every
request, and roster generation returns `402 Payment Required` when the licence
has lapsed.

The site is split into two pages served by the same process:

* `landing.html` at **`/`** - the marketing page: what the tool does, the
  benefits, how it works, a step-by-step walkthrough of every dashboard tab,
  the plan and its price, an FAQ, and the three ways in
  (**register for the free trial**, **buy the annual licence**, or **log in** for
  existing paying customers).
* `index.html` at **`/app`** - the dashboard itself (teachers, schedules, exam
  timetable, roster generator, teacher duty sheets, the PDF document library and
  billing).

## Repository layout

```
landing.html            Marketing page: benefits, pricing, register / buy / log in
index.html              The dashboard (UI + API client)
terms.html              Terms & Conditions of Service (linked from signup + footers)
privacy.html            Privacy Policy, POPIA & GDPR compliant (linked from signup + footers)
assets/
  logo-mark.svg         Brand mark (shield + calendar + green check); favicon + header icon
  logo.svg              Full lockup (mark + "Invigismart Pro" wordmark)
  timetable-import.js   Pure staff-timetable grid parser (no DOM, testable)
  exam-types.js         Exam-type catalogue (browser mirror of server/examtypes.py)
server/
  app.py                HTTP server: routing, static files, API endpoints
  config.py             Environment-driven configuration
  db.py                 SQLite persistence (users, subscriptions, payments, documents, event log)
  documents.py          Multipart parsing, PDF validation and safe document names
  examtypes.py          Exam-type catalogue: grade scopes + timetable policy per sitting
  security.py           PBKDF2 password hashing + HS256 JWT sessions
  billing.py            Billing state machine: checkout, entitlement events, mock mode
  payfast.py            PayFast gateway: signed payment form, ITN validation, recurring API
  roster.py             The constraint solver (runs only for entitled accounts)
  uploads/              Uploaded PDFs (git-ignored; set DOCUMENTS_DIR in production)
  tests/test_api.py     End-to-end tests that drive the running server
.env.example            Every supported environment variable
.env.production.example Production template: which values are required and why
DEPLOY.md               Production runbook (domain, TLS, systemd, PayFast, go-live)
```

## Quick start

No dependencies, no build step - the standard library is enough.

```bash
python3 server/app.py          # serves the landing page, the app and the API
```

Open <http://127.0.0.1:8000>. The landing page explains what the tool does and
offers three ways in:

| Where | Route | What happens |
| --- | --- | --- |
| Landing page | `/` | Benefits, how it works, walkthrough, pricing, FAQ |
| Register / free trial | `/#pricing` → **Register free account** | `POST /api/auth/signup`, trial starts, straight to `/app` |
| Buy the licence | **Buy annual licence** | Register (or reuse the session), then `POST /api/billing/checkout` |
| Existing customer | `/login` (or `/#pricing` → **Log in**) | `POST /api/auth/login`, straight to `/app` |
| Dashboard | `/app` | Teachers, schedules, exams, roster generator, billing |
| Terms & Conditions | `/terms` | Terms and Conditions of Service |
| Privacy Policy | `/privacy` | POPIA & GDPR-compliant privacy policy |

The landing page and the dashboard share one `localStorage` session key
(`invigismart_token_v1`), so signing in on the landing page logs you into the
dashboard. New accounts get a trial (`TRIAL_DAYS`, default 14), which is enough
to register teachers, exams and schedules and generate unlimited rosters. The
trial is an *evaluation window*, so what it produces is an **evaluation copy**:
the duty sheet carries a watermark and the CSV is stamped. The clean,
unwatermarked artifact comes from `POST /api/roster/export`, which is paid-only.

The front end must be served by this process (not opened as a `file://` URL) so
that `/api/*` is same-origin. Point `window.INVIGISMART_API` at another origin if
you host the API elsewhere.

## Sales and demo accounts

An account unlocks everything only while its **subscription row** is live, so a
demo login has to be created in the database. Editing browser storage or reusing
a trial still hits the `402` paywall on `/api/roster/export`.

`server/create_demo_account.py` creates (or refreshes) such an account. The
password is PBKDF2-hashed like any other, the subscription is written with
provider `demo` for `--days` (default 3650), and one invoice row is recorded so
the Billing tab has something to show. Re-running it is idempotent.

```bash
python3 server/create_demo_account.py \
  --email sales@your-domain.example \
  --password 'choose-a-strong-one'          # omit --password to generate one

# Rotate the password and extend the licence of an existing demo login
python3 server/create_demo_account.py \
  --email sales@your-domain.example \
  --password 'a-new-strong-one' --update-password --days 3650
```

It writes to `DB_PATH` (default `server/invigismart.db`); pass `--db` to target
another file, and `--no-invoice` to skip the invoice row. On a public `https`
deployment the tool refuses to run unless `--allow-production` is passed, because
a shared, long-lived password on the live server is a backdoor. Keep the
credential private and rotate it after public events.

## Brand & theming

The product identity is **Invigismart Pro** - the shield/calendar mark with the
green "verified" check. The mark lives in `assets/` and is used both as the
favicon (`/assets/logo-mark.svg`) and in the nav/header of both pages. The logo
palette is declared once per page in a small `tailwind.config` block in `<head>`:

| Token | Colour | Use |
| --- | --- | --- |
| `navy` (`900` = `#1a2b5c`) | wordmark navy | dark headers, hero panels, footers |
| `brand` (`600` = `#1d6fd8`) | shield blue | primary buttons, links, focus rings |
| `emerald` (`500` = `#1fae5b`) | logo green | success states, "Pro", check marks |

`indigo` is remapped to `brand` and `emerald` to the logo green, so the existing
utility classes pick up the new hues without being rewritten. Headings use
**Poppins** and body copy uses **Inter** (loaded from Google Fonts, with a
system-font fallback if the CDN is blocked).

## How entitlement works

1. `POST /api/auth/signup` or `/api/auth/login` returns a signed **JWT**
   (`HS256`, `JWT_SECRET`) plus the current entitlement snapshot.
2. The browser stores that token in `localStorage` and sends it as
   `Authorization: Bearer <token>`. The token only identifies the account; it
   carries no licence claims.
3. Every protected request re-derives entitlement from SQLite
   (`subscriptions.status` + `current_period_end`, else an unexpired trial).
4. `POST /api/roster/generate` runs the solver and refuses with `402` unless the
   account is entitled. Every generate response also carries `trialArtifact`,
   true unless the entitlement source is a paid subscription.
5. `POST /api/roster/export` renders the clean, unwatermarked CSV and refuses
   with `402 subscription_required` for anything that is not a live
   subscription &mdash; a trial included. That is what keeps the evaluation
   window a genuine evaluation rather than a free roster generator.

Because the licence lives in the database, clearing browser storage, editing
`localStorage`, or calling an internal function from the console no longer grants
access. The browser only caches a snapshot (`state.subscription`) for display.

## Pages & routing

| Route | Serves | Notes |
| --- | --- | --- |
| `/` | `landing.html` | Marketing page; the plan name, price and trial length are filled from `GET /api/health` |
| `/app`, `/app/` | `index.html` | The dashboard (server-verified entitlement) |
| `/login`, `/signup` | `landing.html` | Opens the log-in or registration modal on load |
| `/mock-checkout` | generated HTML | *(mock mode only)* local mock checkout stand-in |
| `/index.html`, `/landing.html` | the same files | Still reachable directly |

`?checkout=success` and `?checkout=cancel` (the targets the gateway sends the
customer back to) and `?billing=return` (coming back from Stripe's Billing
Portal) are read by `/app` and shown as a dismissible notice, then stripped from
the URL. With PayFast the notice means *payment received* — the licence itself is
issued by the **ITN**, a moment later.

## Staff timetable import

The **Teachers** and **Teacher Schedules** tabs both accept a CSV timetable grid
as well as the four-column schedule CSV. A grid is detected by a header row with
a teacher name and at least three weekday columns; each following row is a
period, and cells such as `ENGHL - 08H` become one teacher, one subject and one
busy slot (day, period, time and class). Subject codes are expanded to their
full names. Re-importing is safe: teachers are matched by name and slots already
on the schedule are skipped, so the import can be repeated without duplicates.

The parser lives in `assets/timetable-import.js`, outside the dashboard's main
script, so it is pure and testable without a browser. The schedules table has a
per-teacher filter because a full staff timetable can add roughly a thousand
rows.

## Exam types

A roster is always generated for **one sitting**, and the sitting decides the
rules. Nothing is generated until one is picked, and the catalogue is defined
once in `server/examtypes.py` and mirrored as data in `assets/exam-types.js`
(the test suite compares the two files, so they cannot drift):

| Exam type | Grades | Teaching timetable | Notes |
| --- | --- | --- | --- |
| `term2` — Term 2 exam (June) | 8–12 | optional, **off** by default | The mid-year sitting: lessons are suspended, so *all teachers are available for invigilation*. Switch the toggle on if some classes still run. |
| `gr12_prelim` — Grade 12 preliminary | 12 only | **required** | Only Grade 12 writes and the rest of the school is on timetable, so a teacher in front of a class cannot invigilate. |
| `gr12_final` — Grade 12 final (NSC) | 12 only | **required** | As above: Grade 8–11 lessons continue alongside the papers. |
| `term4` — Term 4 exam | 8–11 only | optional, **on** by default | Written after the Grade 12 candidates finish; teaching continues for the grades that are not writing. |

The rules that hold for every sitting are unchanged: 1:30 invigilator ratio, no
own-subject invigilation (subject aliases such as `ENGHL` and `English Home
Language` are folded together), no double-booking inside a slot, and load
balancing. A paper whose grade is outside the sitting is a **hard error** —
silently dropping it would let a school print a Grade 12 prelim roster that
quietly omits the Grade 10 papers. A paper with *no* grade at all is allowed and
reported as a warning, because the subject label may simply not say.

The same rules are enforced in three places, deliberately: `server/examtypes.py`
+ `server/roster.py` (authoritative), the browser engine in `index.html` (instant
redraw) and the API's 400 responses. `POST /api/roster/generate` and
`/api/roster/export` therefore require an `examType`, and a Gr 12 request that
asks for `considerTimetable: false` is refused rather than silently corrected.

## Paper list and master roster

The exams office usually knows the paper totals before the timetable is final,
so the **Exam Timetable** tab keeps a separate paper list of
`subject, grade, students`:

* upload a CSV (`subject,grade,students`, with or without a header) or add
  papers by hand;
* each paper shows the invigilators the 1:30 ratio will need, and
  **Needs a slot** until it is scheduled;
* scheduling a paper asks for date, day and period once and keeps them for the
  next unscheduled paper, which is how a slot-by-slot timetable is actually
  captured;
* a scheduled paper *is* an exam session, so it feeds the roster directly, and
  unscheduling or deleting it keeps the two tables consistent.

**Print Master Roster** (Generator tab) renders the printable sheet — school,
sitting, grade scope, timetable policy, generation timestamp, warnings, one line
per paper with its team and a prepared/checked sign-off — into a print-only
block, so the dashboard chrome never ends up on paper. The CSV export carries the
same provenance line and a `Grade` column as `/api/roster/export`.

## API reference

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/health` | - | Liveness + effective configuration |
| `POST` | `/api/auth/signup` | - | Create account, start trial, return token |
| `POST` | `/api/auth/login` | - | Verify password, return token |
| `GET` | `/api/me` | Bearer | Account + authoritative entitlement |
| `POST` | `/api/billing/checkout` | Bearer | Start a payment: `{ url, fields }` for PayFast (POST the fields), or `{ url }` for Stripe |
| `POST` | `/api/billing/portal` | Bearer | Stripe only: open the hosted Billing Portal |
| `GET` | `/api/billing/summary` | Bearer | Licence, payment history and cancellation options for the Billing tab |
| `POST` | `/api/billing/cancel` | Bearer | Stop the next PayFast charge, keeping the period already paid for |
| `POST` | `/api/billing/webhook` | gateway | PayFast ITN (form-encoded, verified) or a Stripe event (signature-verified) |
| `POST` | `/api/billing/mock-activate` | Bearer | *(mock mode)* simulate a payment |
| `POST` | `/api/billing/dev-expire` | Bearer | *(mock mode)* force expiry |
| `POST` | `/api/roster/generate` | Bearer + entitled | Run the solver for one exam type |
| `POST` | `/api/roster/export` | Bearer + **paid** | Render the clean, unwatermarked roster CSV |
| `GET` | `/api/exam-types` | - | The exam-type catalogue and the 1:30 ratio the solver uses |
| `GET` | `/api/events` | Bearer | Recent webhook events (audit trail) |
| `GET` | `/api/documents` | Bearer | List this account's PDFs plus storage limits |
| `POST` | `/api/documents` | Bearer | Upload one PDF (`multipart/form-data`: `file`, `title`, `category`) |
| `GET` | `/api/documents/<id>` | Bearer | Stream one PDF inline (`?download=1` forces a download) |
| `DELETE` | `/api/documents/<id>` | Bearer | Delete one PDF |
| `GET` | `/mock-checkout` | - | *(mock mode)* local mock checkout stand-in |

`POST /api/roster/generate` accepts
`{ teachers, schedules, exams, examType, considerTimetable }` and returns
`{ assignments: [{ examId, teacherIds }], warnings, generatedAt, summary,
examType, examTypeLabel, gradeScope, considerTimetable, entitlement,
trialArtifact }`.

`examType` is **required** (`term2`, `gr12_prelim`, `gr12_final` or `term4`) and
is checked before any work is done: an unknown or missing type, a paper outside
the sitting's grade scope, or `considerTimetable: false` on a Grade 12 sitting is
a `400` with `code: "exam_type_invalid"` and a message written for the exams
office. `considerTimetable` is optional: Term 2 defaults to `false` ("all
teachers available"), Term 4 to `true`, and the Grade 12 types ignore it because
they always consult the timetable. Every paper may carry `grade` (`12`, `"Gr
12"`), otherwise the grade is read from the subject label.

`POST /api/roster/export` accepts
`{ teachers, exams, assignments, examType, considerTimetable }` and returns
`{ filename, csv }`. It re-renders the rows it is given (no solver, so the file
cannot drift from what is on screen), opens with a `#` provenance line naming
the sitting, its grade scope and whether teaching was respected, and includes a
`Grade` column. It is the **only** path to a clean, unwatermarked artifact:
anything other than a live subscription gets `402`.

### Document library

The **Documents** tab keeps a school's source material — exam timetables,
teacher timetables, policies and anything else — beside the roster data. It is
available to any signed-in account (a trial included), because it stores
reference documents rather than producing a licensed artifact.

* **PDF only.** The extension, the browser-supplied content type and the
  `%PDF-` magic bytes are all checked, so a renamed HTML file is refused.
* **Account-scoped.** Listing, downloading and deleting always filter by the
  signed-in user id; another school's document id answers `404`.
* **Server-side.** Metadata lives in the `documents` table and the bytes in
  `DOCUMENTS_DIR` (default `server/uploads`, git-ignored). Nothing is kept in
  `localStorage`, so the library follows the account across devices.
* **Bounded.** Defaults are 10 MB per PDF, 50 PDFs and 100 MB per account,
  configurable through `MAX_DOCUMENT_BYTES`, `MAX_DOCUMENTS_PER_USER` and
  `MAX_DOCUMENT_STORAGE_BYTES`.
* **Not public.** Files are never served by the static handler; every download
  requires a Bearer token, and filenames are sanitised before storage and in
  `Content-Disposition`.
* **Reference copies, not an importer.** The PDF is stored as-is; the library
  does not extract exam sessions from it. CSV import on the Exam Timetable tab
  remains the structured path into the solver.
* **Backups.** The JSON backup covers roster data, not PDF bytes. Back up
  `DOCUMENTS_DIR` alongside the database (see `DEPLOY.md`).

Categories are `exam_timetable`, `timetable`, `policy` and `other`. The Exam
Timetable and Teacher Schedules tabs each carry an **Upload PDF** shortcut that
opens the library with the matching category selected.

## Billing

### Choosing a provider

`BILLING_PROVIDER` is `payfast`, `stripe` or `mock`. Left blank it is
auto-detected from whichever credentials are present, so a development machine
with nothing set runs the mock checkout described at the end of this section.

### PayFast (South Africa)

```bash
export BILLING_PROVIDER=payfast
export PAYFAST_MERCHANT_ID=10000100
export PAYFAST_MERCHANT_KEY=46f0cd694581a     # Settings -> Integration
export PAYFAST_PASSPHRASE=the-same-value-you-set-in-payfast
export PAYFAST_SANDBOX=1                      # 0 before you take real money
export PLAN_PRICE_AMOUNT=1200.00              # the amount actually charged
export APP_BASE_URL=https://roster.example.edu
```

Why PayFast: Stripe reaches South African businesses only through its "extended
network", while PayFast supports **annual** ZAR recurring billing and exposes an
API to cancel a subscription - which is what a 12-month school licence needs.

`POST /api/billing/checkout` answers with the **signed form fields**, the browser
POSTs them to PayFast, and the licence is then issued by the **ITN** PayFast sends
to `/api/billing/webhook`. That notification is confirmed with PayFast before it
is allowed to change anything, de-duplicated by payment id, and only a `COMPLETE`
payment extends the licence - a short payment is recorded but not credited.
`DEPLOY.md` section 6 has the full walkthrough, including the sandbox purchase
that proves the signature.

### Live Stripe

```bash
export STRIPE_SECRET_KEY=sk_live_...
export STRIPE_PRICE_ID=price_...          # yearly recurring price, R1200 (ZAR)
export STRIPE_WEBHOOK_SECRET=whsec_...    # from the endpoint below
export APP_BASE_URL=https://roster.example.edu
python3 server/app.py
```

1. Create a product plus a yearly recurring price in the Stripe dashboard,
   priced at **R1200** (currency **ZAR**).
2. Add a webhook endpoint pointing at `https://<host>/api/billing/webhook`,
   subscribed to `checkout.session.completed`, `customer.subscription.*` and
   `invoice.paid`, then copy its signing secret into `STRIPE_WEBHOOK_SECRET`.
3. Locally, forward events with
   `stripe listen --forward-to http://127.0.0.1:8000/api/billing/webhook`.

### Local mock mode (no gateway configured)

With no gateway credentials set, `ALLOW_MOCK_BILLING` defaults to on. `checkout`
returns a link to `/mock-checkout`, a local page whose button emits the same
`customer.subscription.updated` event a live webhook would - so the real
entitlement path is exercised without touching a payment provider. **Buy annual
licence** on the landing page and **Subscribe or Renew 12 Months** in the
dashboard both go through this flow and land back on `/app?checkout=success`.
The dashboard cannot grant a licence itself - it only renders the
`currentPeriodEnd` that `/api/me` reports - so cancelling the subscription in
the billing panel is enough to watch the paywall return, and buying again
restores access.

### Invoices and cancellation

**Manage Billing & Invoices** in the dashboard calls `GET /api/billing/summary`
and shows what the account has actually paid, read straight from the `payments`
table.

On **PayFast** — which has no hosted customer portal — that panel is the whole
story: the payment history plus a *Cancel automatic renewal* button that calls
`POST /api/billing/cancel`. Cancelling stops the next charge **and keeps the
period the school already paid for**; if PayFast cannot be reached the
cancellation is refused and access is left untouched, rather than telling the
school something untrue. The panel also prints the support email and WhatsApp
number, so a query about an invoice has somewhere to go without leaving the app.

On **Stripe** the same button opens the hosted **Billing Portal** instead
(`POST /api/billing/portal`), where the school downloads invoices, updates the
card and cancels. It must be enabled once in the Stripe dashboard (*Settings →
Billing → Customer portal*) or Stripe rejects the session request. Either way no
card details touch this app, and the button is disabled while billing is in mock
mode.

### Production guard

The server treats any `APP_BASE_URL` starting with `https://` as production and
**refuses to start** (exit code `2`, reasons printed to stderr) unless:

* `JWT_SECRET` is set — otherwise every restart signs every customer out;
* the selected gateway is configured — `PAYFAST_MERCHANT_ID` and
  `PAYFAST_MERCHANT_KEY` (or `STRIPE_SECRET_KEY` and `STRIPE_PRICE_ID`) —
  otherwise nobody can pay;
* `PAYFAST_PASSPHRASE` is set and `PLAN_PRICE_AMOUNT` parses — PayFast signs the
  payment form with the passphrase, so a missing or different value rejects every
  checkout before a school can pay, and an unreadable amount cannot build a
  payment form at all;
* (Stripe only) `STRIPE_WEBHOOK_SECRET` is set — otherwise a school pays R1200 and
  the licence never activates.

Mock billing is additionally **force-disabled** on any https host, whatever
`ALLOW_MOCK_BILLING` says, because `/api/billing/mock-activate` would otherwise
let any signed-in visitor grant themself a free year. `ALLOW_INSECURE_BILLING=1`
waives the checks for a throwaway staging host and is never appropriate in
production.

Inbound notifications are verified before they can change anything: a PayFast ITN
is confirmed with PayFast itself, a Stripe delivery is signature-checked
(HMAC-SHA256, 5-minute tolerance), and both are de-duplicated by event id so a
gateway retry cannot buy a second year. Unsigned bodies are accepted only in local
mock mode. Configuring a gateway also disables mock billing unless
`ALLOW_MOCK_BILLING=1` is explicit, and when `BILLING_PROVIDER` is left blank the
provider is auto-detected from whichever credentials exist (PayFast wins if both
are set).

Deploying for real? Follow **`DEPLOY.md`** (domain → TLS → systemd → persistent
database → PayFast credentials and passphrase → the go-live purchase test).

## Tests

```bash
python3 server/tests/test_api.py
```

The suite boots the real server on a throwaway database and covers signup,
validation, duplicate emails, login failures, tampered tokens, unauthenticated
access, trial entitlement, the trial watermark (`trialArtifact` is set for a
trial and clear for a licence, and the clean export refuses trials and lapsed
licences with `402`), server-side roster solving (own-subject and
clashing-slot rules), `402` after expiry, mock checkout reactivation, valid /
invalid / stale / duplicate webhook deliveries, revocation on cancellation,
the Billing Portal endpoint (auth required, and honest `503`/`409` answers when
there is no configured billing or no linked Stripe customer), which page each
route serves (`/`, `/app`, `/login`, `/signup`), that the brand logo assets are
served and both pages wire up the palette, and that server sources and the
database are not publicly served.

The exam-type suites pin the four sittings, their grade scopes and their
timetable policy; prove a missing or unknown `examType` and a paper outside the
sitting's grades are refused; prove a Grade 12 roster cannot switch the
timetable off while Term 2 uses every teacher; compare the browser catalogue in
`assets/exam-types.js` against `server/examtypes.py` so the two engines cannot
drift; and check the export carries the sitting stamp and the `Grade` column.

The document-library tests upload real multipart bodies (binary bytes and
UTF-8 filenames included), prove that PDF magic bytes are enforced, check that
one account cannot list, download or delete another's file, and exercise the
per-file, file-count and total-storage quotas.

The PayFast suites are the money tests: the signature algorithm is pinned to
**PayFast's own published vectors** (with and without a passphrase, including the
PHP `urlencode` quirks), the signed annual payment form is checked field by field,
and a real server is driven end to end with ITNs — a `COMPLETE` payment activates
the licence and lifts the `402` on the solver, a retried notification buys nothing
extra, a renewal adds a year to the period already paid for, a short or failed
payment is recorded but credited nothing, a tampered signature changes nothing,
and an unreachable gateway fails a cancellation loudly instead of claiming success.

`ConfigGuardTests` and `PayFastGuardTests` additionally boot servers with
deliberately broken environments and assert the production guards: a public https
host without `STRIPE_WEBHOOK_SECRET` (or without `PAYFAST_PASSPHRASE`) exits `2`
with the reason on stderr, a sandbox host warns while still booting, and a host
that waives the checks with `ALLOW_INSECURE_BILLING=1` still serves `404` for
`/mock-checkout`, `403` for `/api/billing/mock-activate` and `400` for an
unsigned webhook.

## Configuration

See `.env.example` for the full list. The essentials:

| Variable | Default | Notes |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address |
| `JWT_SECRET` | random per start | **Set this in production** |
| `ACCESS_TOKEN_TTL` | `3600` | Session lifetime (seconds) |
| `TRIAL_DAYS` | `14` | Trial length for new accounts |
| `DB_PATH` | `server/invigismart.db` | SQLite file |
| `DOCUMENTS_DIR` | `server/uploads` | Where uploaded PDFs are stored; production should point this at persistent storage |
| `MAX_DOCUMENT_BYTES` | `10485760` | Largest single PDF (10 MB) |
| `MAX_DOCUMENTS_PER_USER` | `50` | Files allowed per school account |
| `MAX_DOCUMENT_STORAGE_BYTES` | `104857600` | Total PDF storage per account (100 MB) |
| `BILLING_PROVIDER` | auto | `payfast`, `stripe` or `mock`; blank auto-detects from the credentials present |
| `PAYFAST_MERCHANT_ID`, `PAYFAST_MERCHANT_KEY`, `PAYFAST_PASSPHRASE` | - | PayFast credentials (*Settings → Integration*); the passphrase must match the account |
| `PAYFAST_SANDBOX`, `PAYFAST_FREQUENCY`, `PAYFAST_CYCLES` | `0` / `6` / `0` | Sandbox switch, annual billing, renew until cancelled |
| `PAYFAST_ITN_VERIFICATION` | `api` | `api`, `api+signature`, `signature` or `off` (tests only) |
| `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`, `STRIPE_WEBHOOK_SECRET` | - | Stripe alternative, where the entity is Stripe-supported |
| `APP_BASE_URL` | `http://localhost:<PORT>` | Payment return base; **https here means production** |
| `ALLOW_MOCK_BILLING` | on while no gateway is configured | Exposes demo endpoints; force-disabled on any https host |
| `ALLOW_INSECURE_BILLING` | off | `1` waives the production billing checks (staging only) |
| `PLAN_NAME`, `PLAN_PRICE_DISPLAY`, `PLAN_PRICE_AMOUNT` | `Annual School Unlimited`, `R1200 / year`, `1200.00` | UI copy, plus the amount actually charged |
| `SUBSCRIPTION_DAYS` | `365` | Length of one paid period |
| `SUPPORT_EMAIL`, `SUPPORT_PHONE` | the live address and number | Contact details printed on the landing page, the dashboard footer and the billing panel; served via `/api/health` so every page agrees |

## Contact

Questions, quotes, or a query about a payment:

| | |
| --- | --- |
| Email | `daniel@invigismart.online` |
| Phone / WhatsApp | `+27 76 319 3749` (`076 319 3749` locally) |

The pages are served these values from `SUPPORT_EMAIL` and `SUPPORT_PHONE` (see
*Configuration* above). They appear in the landing-page footer and FAQ, the
dashboard footer and the billing panel — change the environment variables rather
than the HTML, which is served, not built.

Worth knowing while there is no self-service password reset: if a school loses
its password, this address is its only way back in.

## Security notes

* Passwords are stored as PBKDF2-HMAC-SHA256 (200k iterations, per-user salt) and
  never returned by any endpoint.
* Session tokens are HMAC-signed; tampering or expiry yields `401`.
* Inbound payment notifications are verified before touching the database: a
  PayFast ITN is confirmed with PayFast itself (and can additionally be
  signature-checked and IP-pinned), a Stripe delivery is signature-checked, and
  both are de-duplicated by id. An unsigned body is accepted only in local mock
  mode, where no money exists.
* A public https host refuses to boot without `JWT_SECRET` and working gateway
  credentials — including the PayFast passphrase, because a mismatched signature
  means a school simply cannot pay. Mock billing is force-disabled there, so a
  deployment cannot silently give away licences or take money it does not unlock.
* Static serving never exposes `server/**`, `*.py`, `*.db` or dotfiles (so
  `.env`, `.env.production.example`, `DEPLOY.md` and uploaded PDFs are not
  reachable without a token).
* Uploaded files are validated by PDF magic bytes, size-checked before they are
  written, stored with `0600` permissions under `DOCUMENTS_DIR`, and every
  list/download/delete is scoped to the signed-in account.
* Restoring a JSON backup can no longer import `auth`/`licenseExpiry` fields,
  because entitlement is server-owned.

### Known limitations

This is a deliberately dependency-free server (`ThreadingHTTPServer` + SQLite)
aimed at a single school or a small deployment. Before running it at scale,
consider a production WSGI/ASGI server with a real RDBMS, rate limiting and
lockout on `/api/auth/*`, email verification and password reset, and TLS
termination at a reverse proxy. Roster, teacher and exam data stay in the
browser; account, subscription and uploaded-document metadata live on the
server, with the PDF bytes in `DOCUMENTS_DIR`.

