# Sales mailbox + demo login — `invigismart.online`

**Status (measured 20 Nov 2026):** the mail infrastructure is audited and the
`create_demo_account.py` tool exists in this repo, but neither account has been
created yet. Both steps need access this workspace does not have: the DirectAdmin
panel for the mailbox, and a shell on `156.155.252.98` for the database write.

## 1. Two different "sales accounts" — do not conflate them

| | Sales **mailbox** | Demo **dashboard login** |
| --- | --- | --- |
| Purpose | the address on slides, brochures and the footer | the account you sign in with on stage |
| Stored in | DirectAdmin (or the `tld-mx` mail cluster) | `subscriptions` + `users` rows in the app's SQLite file |
| Created by | panel → *E-mail Manager → E-mail Accounts* | `python3 server/create_demo_account.py` |
| Password hashed as | the mail server's own scheme | PBKDF2 (`server/security.py`) |
| Unlocks the product | no | yes — provider `demo`, `annual_school_unlimited` |

They can share the address `sales@invigismart.online`, but keep the **passwords
different**: the mailbox password ends up in the app's `.env` (so the app can send
password-reset mail), while the demo login password is a shared credential you
rotate after every public event. A separate `demo@invigismart.online` login is
tidier still, because the app DB never learns the mailbox secret.

Why the DB write is unavoidable: entitlement is server-owned. A trial account, or
a hand-edited `localStorage` token, still gets `402 Payment Required` on
`/api/roster/export` — so a demo cannot fake the paid-only clean export.

## 2. Measured mail infrastructure

| Record | Value |
| --- | --- |
| `invigismart.online` A | `156.155.252.98` (DirectAdmin + CloudLinux + Apache + Passenger) |
| `mail.invigismart.online` A | `156.155.252.98` (same host) |
| `api.invigismart.online` | same host; the TLS certificate is issued for it |
| MX | `10 mx1.tld-mx.com`, `20 mx2.tld-mx.net` |
| SPF | `v=spf1 +a +mx +ip4:156.155.252.98 include:relay.mailchannels.net ~all` |
| DMARC | **not published** |
| DKIM | **no record** for common selectors |
| Open ports | 993, 143, 587, 465 (25 and 995 filtered) |
| TLS | `mail.invigismart.online:993` → CN `api.invigismart.online`, SAN covers `api.`/`mail.`/apex; `mx1.tld-mx.com:465` → `*.tld-mx.com` |

No "accept untrusted certificate" workaround is needed for either host.

## 3. Which panel actually owns the mailbox

MX is delegated to `tld-mx.com`, which is **not** this server — it is the mail
filter/cluster the hosting provider runs. So the mailbox may be provisioned in
either place:

1. **DirectAdmin on `156.155.252.98`** — *E-mail Manager → E-mail Accounts*. Try
   this first; it is the account whose home directory `shfckyzk` also owns the app.
2. **The `tld-mx` control panel** — if DirectAdmin refuses the domain, shows no
   account list, or mail sent to a new address never arrives.

Tell them apart after creating the account: `mail_verify_login.py` already tries
`mail.invigismart.online` and then `mx1.tld-mx.com` (the `--imap-host` default is
a comma-separated list). The host that answers `[AUTHENTICATIONFAILED]` for a
deliberately wrong password is the one holding the mailbox; a host that times out
or rejects the hostname holds nothing.

## 4. Create the mailbox

In **DirectAdmin → E-mail Manager → E-mail Accounts** (older skins: *Email
Accounts*) on the `shfckyzk` account:

1. **Create E-mail Account** → username `sales`, domain `invigismart.online`.
2. **Password:Inkd@123 generate it locally and paste from a password manager; never type
   one into a chat, ticket or commit.
   `python3 -c "import secrets;print(secrets.token_urlsafe(18))"`
3. **Quota:** 250–500 MB is plenty for a demo mailbox.
4. Leave *send welcome email* off unless you want it.
5. Confirm `sales@invigismart.online` appears in the account list.

Then, while still in the panel:

- **E-mail Manager → MX Records:** the domain must *not* be set to "use this
  server" for delivery while MX points at `mx1.tld-mx.com`. A local-delivery
  setting sends mail to the server's own Dovecot and it silently disappears.
- **E-mail Manager → DKIM Keys:** create a key and publish it. DirectAdmin's
  default selector is `x`, so the record lands at `x._domainkey.invigismart.online`;
  confirm with `python3 .vscode/mail_check.py --dkim-selector x --no-smtp`.

The login **is** the full address: `sales@invigismart.online`, never bare `sales`.

## 5. Client settings (verified reachable)

| Setting | Value |
| --- | --- |
| Username | `sales@invigismart.online` (full address) |
| IMAP | `mail.invigismart.online` port **993** SSL/TLS, fallback `mx1.tld-mx.com` |
| SMTP | `mail.invigismart.online` port **465** SSL/TLS or **587** STARTTLS, same fallback |
| Display name | `Invigismart Pro — Sales` |
| Signature | name, `sales@invigismart.online`, `https://invigismart.online`, phone |

Skip plain 143/25: 25 is filtered on this network and 143 is unnecessary.
## 6. Create or refresh the demo dashboard login

Plan for this to happen on the live server, next to the live database — the path
`/api/health` reports is `/home/shfckyzk/invigismart-api/data/invigismart.db`.

```bash
# on 156.155.252.98, as the account user (not root, so file ownership stays correct)
cd /home/shfckyzk/invigismart-api
python3 server/create_demo_account.py \
  --email demo@invigismart.online \
  --school "Invigismart Pro (Sales Demo)" \
  --days 3650 \
  --allow-production            # required: APP_BASE_URL is https
```

- Omit `--password` and the tool prints a generated one **once**. Save it before
  the terminal scrolls.
- Rotate after a public event: same command plus
  `--password '<new>' --update-password`.
- `--no-invoice` skips the demo invoice row in the Billing tab; leave it in so the
  billing screen has something to show.
- Pass `--db /path/to/invigismart.db` if `DB_PATH` in the environment is not set to
  the live file, otherwise the tool writes to `server/invigismart.db` and the live
  site never sees the account.
- It refuses to run on an https deployment without `--allow-production`, by
  design: a shared long-lived password on a public host is a backdoor.

Ways to get a shell on that host, in order of preference:

| Route | Note |
| --- | --- |
| DirectAdmin → *System Info & Files → Terminal* (or SSH) | the clean way; run as the account user |
| DirectAdmin → *Advanced Features → Cron Jobs*, one-shot, then delete it | works without SSH; check the mail/file output afterwards |
| Download the `.db`, run the tool locally, upload it back | **avoid** — the app holds it open, and you would overwrite live writes |

Use invented teacher and learner names on stage. A demo account that contains a
real school's roster is a POPIA problem, not a demo.

## 7. Close the password-reset gap (the app currently cannot send mail)

`/api/health` reports `"emailConfigured": false`, and the config warning says a
school that forgets its password stays locked out until you reset it by hand.
`server/mailer.py` (standard-library SMTP) is in the working tree but **still
untracked in git** — confirm it is in the deployed bundle before relying on it.

Add to the API's `.env` (the one `deploy/make-api-env.py` generates — never commit it):

```ini
SMTP_HOST=mail.invigismart.online
SMTP_PORT=587
SMTP_SSL=0
SMTP_STARTTLS=1
SMTP_USER=sales@invigismart.online
SMTP_PASSWORD=Inkd@123
SMTP_FROM=Invigismart Pro <sales@invigismart.online>
SUPPORT_EMAIL=sales@invigismart.online
PASSWORD_RESET_BASE_URL=https://invigismart.online
```

Then re-read `https://invigismart.online/api/health`: `emailConfigured` must flip
to `true` and `emailWarnings` must empty. Finally prove it end to end by
requesting a reset from `/reset-password` on an account you control. Use
`--send-to` on the verifier only if you want a plain test message first.

Because the app now holds the mailbox password, the mailbox and the demo *login*
should not share a password (§1).

## 8. Verify the mailbox

```bash
python3 .vscode/mail_verify_login.py                        # IMAP + SMTP auth, password prompted
python3 .vscode/mail_verify_login.py --send-to you@example.com   # plus an end-to-end send
python3 .vscode/mail_check.py                               # DNS, ports, TLS, DKIM, RCPT probe
```

`mail_verify_login.py` exits 0 (both accepted), 1 (IMAP failed), 2 (SMTP failed).
`mail_check.py` exits 0 healthy, 1 warnings, 2 failures, 3 usage error. Both read
the password with `getpass`, so it never enters shell history or a log.

## 9. Deliverability gaps

| Item | State | Recommendation |
| --- | --- | --- |
| SPF | `v=spf1 +a +mx +ip4:156.155.252.98 include:relay.mailchannels.net ~all` | fine as is |
| DKIM | no record for common selectors | create a key in DirectAdmin (`x` selector), publish it |
| DMARC | **not published** | start at `_dmarc` TXT `v=DMARC1; p=none; rua=mailto:sales@invigismart.online`, then move to `p=quarantine` |

Without DKIM, DMARC can never reach `p=reject`, and school IT departments do check.

## 10. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `[AUTHENTICATIONFAILED]` on 993 | wrong password, or mailbox not created | confirm the address is listed under E-mail Accounts |
| `535 Incorrect authentication data` on 587 | login refused | full address as username; 465 = SSL/TLS, 587 = STARTTLS |
| login works on `mail.` but not `mx1.` | mailbox lives on the other node | the verifier tries both; use whichever answered |
| mail to the new address vanishes | MX delegated to `tld-mx`, mailbox created locally | create it where the MX points, or fix E-mail Manager → MX Records |
| reset email never arrives, `emailConfigured` still false | `SMTP_*` not set, or the API was not restarted | set the variables, restart Passenger, re-read `/api/health` |
| demo login signs in but export still returns 402 | the row went into the wrong database | re-run with `--db` pointing at the path `/api/health` reports |

## 11. Change log

| Date | Change |
| --- | --- |
| 2026-11-20 | Infrastructure audited (DNS, MX, SPF, DKIM, DMARC, ports, TLS) from this machine. |
| 2026-11-20 | Mailbox creation documented for DirectAdmin, not cPanel — this host runs DirectAdmin Evolution (DEPLOY.md §13). |
| 2026-11-20 | Doc moved into the Invigismart Pro repo; linked the mailbox to `create_demo_account.py` and the `SMTP_*` reset path. |


