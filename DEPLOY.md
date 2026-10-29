# Deploying Invigismart Pro

A runbook for taking this app from a laptop to a public host that can actually
collect **R1200 / year** from a school. Follow it in order; every step ends in a
check you can run, so a mistake surfaces immediately instead of at the first
customer.

**What you are deploying:** one Python process (standard library only) that
serves the landing page, the dashboard, the JSON API *and* the roster solver.
There is no database server — state lives in a single SQLite file. There is no
TLS inside the app, so a reverse proxy terminates HTTPS in front of it. That
means the shape of a production deployment is:

```
browser ──HTTPS──> Caddy/nginx ──HTTP loopback──> invigismart (python3 app.py) ──> SQLite file
    │                                                   │
    │                                                   └─ outbound HTTPS ─> api.payfast.co.za
    │                                                        (cancel / pause / fetch a subscription)
    └─ POSTs a signed form ──> www.payfast.co.za  (the payment page, card or Instant EFT)
                              │
                              └─ ITN ──HTTPS──> /api/billing/webhook  (verified, then applied)
```

Estimated time: ~45 minutes plus PayFast's own merchant-verification lead time.

---

## 0. Pre-flight checklist

Have these before you start, because the deployment is useless without them:

- [ ] A **domain** you control, e.g. `roster.your-school-domain.co.za`.
- [ ] A **server** with a public IP (1 vCPU / 1 GB RAM is plenty) running a
      Debian/Ubuntu-style distro, with `python3` 3.8+.
- [ ] A **PayFast account** for the legal entity that will receive the money,
      with **merchant verification completed** (CIPC registration, or your SA ID
      as a sole proprietor, plus a business bank account and proof of address)
      and a **passphrase set** under *Settings → Integration*. Until PayFast
      approves the account it only processes sandbox payments.
- [ ] A **decision on VAT**: is R1200 inclusive or exclusive of 15%? If you are
      VAT-registered, South African schools will expect a tax invoice.
- [ ] A **password-reset plan** — see "Known limits" at the end. The app has no
      self-service reset, so a paying school that forgets its password has no
      way back in unless you add one or handle it manually.
- [ ] A **support contact you actually monitor**, an email address and a phone or
      WhatsApp number. Set `SUPPORT_EMAIL` and `SUPPORT_PHONE`; they are printed
      on the landing page, the dashboard footer and the billing panel. Until a
      reset flow exists, this is the only route a school has for a failed
      payment, an invoice query or a forgotten password.

Throughout this document:

| Placeholder | Replace with |
| --- | --- |
| `roster.your-school-domain.co.za` | your real domain |
| `invigismart` | the Linux service user |
| `/opt/invigismart` | where the code lives |
| `/var/lib/invigismart` | where the database lives |
| `8080` | the port the app binds on loopback |

---

## 1. Service user and directories

The app never needs root. Give it its own user and three separate directories —
code that you replace on deploy, the database, and the uploaded PDFs. The last
two must survive every deploy.

```bash
sudo adduser --system --group --home /opt/invigismart invigismart
sudo mkdir -p /opt/invigismart /var/lib/invigismart /var/lib/invigismart/documents
sudo chown -R invigismart:invigismart /opt/invigismart /var/lib/invigismart
sudo chmod 750 /var/lib/invigismart /var/lib/invigismart/documents
sudo apt install -y sqlite3 tar  # used by the health checks and the backup job
```

**Check:** `sudo -u invigismart test -w /var/lib/invigismart && sudo -u invigismart test -w /var/lib/invigismart/documents && echo writable`

> The database and documents directories must exist and be writable *before*
> first boot — SQLite creates the file but not its parent directory, and
> uploads create one subdirectory per account.

## 2. Code and database path

```bash
sudo -u invigismart git clone <your-repo-url> /opt/invigismart
cd /opt/invigismart && sudo -u invigismart git log --oneline -1
```

Not using git? Copy the tree instead — this repository has a `.gitignore` but no
git history, so `rsync` from your working copy works just as well and you can
replace "pull" with the same command in step 9:

```bash
sudo rsync -a --delete --exclude '.git' --exclude '*.db' --exclude 'server/uploads' \
  /path/to/Invigismart-Pro--main/ /opt/invigismart/
sudo chown -R invigismart:invigismart /opt/invigismart
```

> `--exclude '*.db'` and `--exclude 'server/uploads'` are not optional: they stop
> local development data from being copied over the live data.

Then run the test suite on the server itself, so you know Python and the
toolchain work there before a proxy is involved:

```bash
cd /opt/invigismart && python3 server/tests/test_api.py
```

**Check:** the suite reports `OK` (it boots the real server against a throwaway
database and drives it over HTTP).

Deliberately **do not** create the database at the default location
(`server/invigismart.db`). It lives inside the code tree, so the next deploy —
or a fresh clone — would wipe every paying customer's subscription. Step 4 sets
`DB_PATH` to `/var/lib/invigismart/invigismart.db`.
## 3. Run it as a service (systemd)

The app has no daemon mode, so systemd supervises it. Create
`/etc/systemd/system/invigismart.service`:

```ini
[Unit]
Description=Invigismart Pro - exam invigilation roster service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=invigismart
Group=invigismart
WorkingDirectory=/opt/invigismart
EnvironmentFile=/etc/invigismart.env
# Keep stdout unbuffered so the startup banner and request log reach
# journalctl immediately instead of sitting in a block buffer.
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /opt/invigismart/server/app.py
Restart=always
RestartSec=3

# The process only needs its own code tree, its database directory and
# outbound HTTPS to api.payfast.co.za. Everything else is denied.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/invigismart
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable invigismart     # start on boot
```

**Check:** `systemctl cat invigismart | grep ExecStart` prints the command that
will run. Do not start it yet — there is no environment file and it would
(correctly) refuse to boot on a public domain.

## 4. Environment file

The server reads configuration from the environment, and the template is in the
repo. Copy it, lock it down, and fill in every `REQUIRED` value:

```bash
sudo install -m 600 .env.production.example /etc/invigismart.env
sudo nano /etc/invigismart.env
sudo chown root:root /etc/invigismart.env     # 600, root-only; systemd reads it as root
```

Generate the session secret rather than inventing one:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

The four values that matter most:

| Variable | Value | Why |
| --- | --- | --- |
| `JWT_SECRET` | 64 hex chars | Without it, every restart signs every customer out. The server refuses to boot on a public https host without it. |
| `DB_PATH` | `/var/lib/invigismart/invigismart.db` | Keeps subscriptions out of the code tree, so deploys cannot wipe them. |
| `DOCUMENTS_DIR` | `/var/lib/invigismart/documents` | Keeps uploaded school PDFs out of the code tree. Defaults to `server/uploads`, which the next deploy would replace. |
| `APP_BASE_URL` | `https://roster.your-school-domain.co.za` | PayFast sends the customer back here after payment, and the ITN is delivered here. A `localhost` value means a paying customer is redirected to a machine they do not own. |
| `BILLING_PROVIDER` | `payfast` | Selects the local gateway. Left blank it auto-detects from whichever credentials exist — convenient in development, a liability in production. |
| `PAYFAST_MERCHANT_ID`, `PAYFAST_MERCHANT_KEY` | from *Settings → Integration* | Without them no payment form can be built, so no school can pay at all. |
| `PAYFAST_PASSPHRASE` | the same value as in your PayFast account | PayFast validates the signature on the payment form with it: if it is missing or different, **every checkout is rejected**. |
| `PLAN_PRICE_AMOUNT` | `1200.00` | The amount actually charged. `PLAN_PRICE_DISPLAY` is cosmetic; this one moves money. |
| `SUPPORT_EMAIL`, `SUPPORT_PHONE` | `daniel@invigismart.online`, `+27 76 319 3749` | Printed on the site and served to the pages. No boot guard, but a school whose payment failed has nowhere else to go. |

Then boot it:

```bash
sudo systemctl start invigismart
sudo systemctl status invigismart --no-pager
sudo journalctl -u invigismart -n 30 --no-pager
```

**Check:** the log shows

```
Invigismart Pro backend listening on http://127.0.0.1:8080
  base url        : https://roster.your-school-domain.co.za  (public host: production mode)
  database        : /var/lib/invigismart/invigismart.db
  gateway         : payfast
  payfast         : live
  stripe          : NOT configured
  webhook secret  : NOT configured
  mock billing    : disabled
  itn verification: api
```

If instead you see `Refusing to start: unsafe production billing configuration`,
the offending values are listed underneath — jump to "When it refuses to start".
(That message is on stderr, which is never buffered; the banner above is flushed
explicitly, and `PYTHONUNBUFFERED=1` in the unit keeps the request log immediate
too, so `journalctl` shows startup output the moment it happens.)

## 5. DNS and TLS

Point the domain at the server's IP, then let a proxy do HTTPS. **Caddy** is the
shortest path because it obtains and renews certificates on its own:

```bash
sudo apt install -y caddy
```

`/etc/caddy/Caddyfile`:

```caddy
roster.your-school-domain.co.za {
    encode gzip
    reverse_proxy 127.0.0.1:8080
}
```

```bash
sudo systemctl reload caddy
curl -sSI https://roster.your-school-domain.co.za/api/health | head -3
```

**Check:** HTTP `200` over HTTPS, and a valid certificate (Caddy logs the ACME
issuance into `journalctl -u caddy`).

<details>
<summary>nginx + certbot alternative</summary>

```nginx
server {
    listen 80;
    server_name roster.your-school-domain.co.za;
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name roster.your-school-domain.co.za;

    ssl_certificate     /etc/letsencrypt/live/roster.your-school-domain.co.za/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/roster.your-school-domain.co.za/privkey.pem;

    client_max_body_size 8m;   # roster payloads can be sizeable

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo certbot --nginx -d roster.your-school-domain.co.za
```

</details>

Notes that will bite you otherwise:

* The app binds `127.0.0.1`, so there is **no firewall rule to open** and the
  raw port is not reachable from the internet. Keep it that way — there is no
  rate limiting inside the app, so the proxy is also your only bandwidth shield.
* Every request will appear in the app log as coming from `127.0.0.1`
  (`self.address_string()` sees the proxy). Read `X-Forwarded-For` in the proxy
  log if you need real client IPs.
* `/api/billing/webhook` must be reachable **without** authentication and over
  HTTPS: that is where PayFast posts the ITN that actually issues a licence. A
  school whose notification cannot get through has paid and is still locked out.

## 6. PayFast: the money side

Do this in **sandbox mode first**, then repeat with live credentials.

1. **Credentials.** PayFast → *Settings → Integration*. Copy the **Merchant ID**
   and **Merchant Key** into `PAYFAST_MERCHANT_ID` and `PAYFAST_MERCHANT_KEY`.

2. **Passphrase.** On the same screen, set a passphrase and put the *same* value
   into `PAYFAST_PASSPHRASE`. PayFast validates the signature on the payment form
   with it, so a mismatch means every checkout is rejected before a school can
   pay. (This is why the server refuses to boot on a public host without it.)

   > Unlike Stripe, there is **no webhook to register in a dashboard**: the app
   > sends its own signed `notify_url`, `return_url` and `cancel_url` with each
   > payment, so the ITN comes straight back to `/api/billing/webhook`.

3. **`PLAN_PRICE_AMOUNT`.** Set it to the amount actually charged (`1200.00`).
   `PLAN_PRICE_DISPLAY` is only what the pages print; this is what PayFast
   collects, and it is also the figure a short payment is measured against.

4. **Sandbox first.** Set `PAYFAST_SANDBOX=1` with the sandbox merchant
   credentials from your PayFast account and run section 8 in full. The sandbox
   behaves like the live gateway but moves no money.

5. **How a payment becomes a licence.** The browser POSTs the signed form to
   PayFast; PayFast then POSTs an **ITN** (Instant Transaction Notification) to
   `https://<your-domain>/api/billing/webhook`. The server:

   * confirms the ITN with PayFast itself (`PAYFAST_ITN_VERIFICATION=api`, the
     default) before it is allowed to change anything;
   * ignores a notification it has already applied, so PayFast's retries cannot
     buy a second year;
   * grants a year only for `payment_status=COMPLETE`, extending the period the
     school already paid for rather than restarting it from today;
   * records the payment so the school can see its own invoice history;
   * records but does **not** credit a payment that is short of
     `PLAN_PRICE_AMOUNT`.

6. **Bank account and verification.** PayFast → *Settings → Banking* (or the
   account signup flow) → add the account that receives the money. Merchant
   verification — CIPC registration, or your SA ID as a sole proprietor, plus
   proof of address — must be complete before live payments are enabled.

7. **Tax.** If you are VAT-registered, decide whether R1200 is VAT-inclusive and
   issue a compliant tax invoice. The billing history in the app gives you the
   amount, date and reference for each school.

8. **Turn the sandbox off** (`PAYFAST_SANDBOX=0`) only when you are ready to take
   real money.

Restart to pick up the new values:

```bash
sudo systemctl restart invigismart
curl -s https://roster.your-school-domain.co.za/api/health \
  | python3 -m json.tool \
  | grep -E 'billingProvider|payfastConfigured|payfastSandbox|mockBilling|publicHost'
```

**Check:** `"billingProvider": "payfast"`, `"payfastConfigured": true`,
`"payfastSandbox": false`, `"mockBilling": false`.

## 7. Verify the guard rails before selling anything

Four quick checks that prove the two silent-failure modes are gone.

```bash
BASE=https://roster.your-school-domain.co.za

# 1. The mock endpoints must be unreachable, not merely hidden.
curl -s -o /dev/null -w 'mock-checkout  -> %{http_code}\n' $BASE/mock-checkout
curl -s -o /dev/null -w 'mock-activate  -> %{http_code}\n' -X POST $BASE/api/billing/mock-activate

# 2. The webhook must refuse an unverified body with 400.
curl -s -o /dev/null -w 'unsigned hook  -> %{http_code}\n' -X POST $BASE/api/billing/webhook \
  -H 'Content-Type: application/json' -d '{"type":"checkout.session.completed","id":"evt_forged"}'

# 3. The billing endpoints must require a session.
curl -s -o /dev/null -w 'summary anon   -> %{http_code}\n' $BASE/api/billing/summary
curl -s -o /dev/null -w 'cancel anon    -> %{http_code}\n' -X POST $BASE/api/billing/cancel

# 4. The live gateway is the one you think it is.
curl -s $BASE/api/health | python3 -c \
  "import json,sys; c=json.load(sys.stdin)['config']; print(c['billingProvider'], 'sandbox=%s' % c['payfastSandbox'], 'configured=%s' % c['billingConfigured'])"
```

Expected: `mock-checkout -> 404`, `mock-activate -> 401` anonymous (or `403`
with a valid session token), `unsigned hook -> 400`, `summary anon -> 401`,
`cancel anon -> 401`, and `payfast sandbox=False configured=True`. Anything else
means the proxy is misdirecting traffic or the deployment is still in mock mode —
stop and fix it before onboarding a school.

Then prove the **ITN** path with a real sandbox payment (section 8). PayFast has
no dashboard "send test event" button, and the notification carries the account
reference, so only a genuine payment exercises the whole path:

* `/api/billing/webhook` shows `200` in `journalctl -u invigismart`;
* `SELECT type FROM events ORDER BY id DESC LIMIT 3;` shows
  `subscription.activated`;
* the dashboard badge flips to **Active**.

A `400` here is the notification being refused: read the reason in the log, and
check the passphrase first (`signature mismatch`) before the field order.

## 8. Go live, then buy your own licence

The only way to be sure the money path works is to buy it yourself.

1. Run this whole section in the **sandbox** first (`PAYFAST_SANDBOX=1`), then
   repeat it with `PAYFAST_SANDBOX=0` and your live merchant credentials.
2. Register a real account at `https://<your-domain>` (that starts the 14-day
   trial) and click **Buy annual licence**.
3. You land on PayFast's payment page. Pay **R1200** — a card clears immediately,
   Instant EFT clears on the bank's schedule. You are returned to the dashboard
   at `/app?checkout=success`; the licence itself is issued by the **ITN**, which
   arrives a moment later.
4. Confirm all four:

```bash
BASE=https://roster.your-school-domain.co.za

# a) the licence is active, server-side
curl -s $BASE/api/health | python3 -c "import json,sys;print(json.load(sys.stdin)['config']['planPrice'])"

# b) the subscription row is really in the database (the token is PayFast's)
sqlite3 /var/lib/invigismart/invigismart.db \
  "SELECT status, current_period_end, provider, stripe_subscription_id FROM subscriptions;"

# c) the ITN was received and applied (newest first)
sqlite3 /var/lib/invigismart/invigismart.db \
  "SELECT type, created_at FROM events ORDER BY id DESC LIMIT 5;"

# d) the payment is in the school's own invoice history
sqlite3 /var/lib/invigismart/invigismart.db \
  "SELECT provider, amount, status, provider_payment_id FROM payments ORDER BY id DESC LIMIT 3;"
```

5. In the dashboard: the badge reads **Active (365 days left)**, the notice says
   *Payment received*, and **Manage Billing & Invoices** now lists the payment.
   Before any payment that panel is deliberately empty, and the cancel button is
   hidden because there is no subscription to stop.
6. In PayFast: the payment appears in your transaction history and the payout
   reaches your bank on your payout schedule, less their transaction fee.
7. **Clean up, and test the revocation path.** Click *Cancel automatic renewal*,
   then refund the payment in PayFast. Do both: a refund does not stop the
   recurring charge, and cancelling does not return the money.

   Cancelling sets the subscription to `canceled` while **leaving the paid-for
   period intact** — the school keeps the year it bought and only the next charge
   stops. That is deliberate, so do not "fix" it. To watch access actually end,
   age the period instead of waiting a year:

```bash
# point the period end at yesterday, then reload the dashboard
sqlite3 /var/lib/invigismart/invigismart.db \
  "UPDATE subscriptions SET current_period_end = strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day');"
```

**Check:** after that update the badge reads **Expired**, the paywall appears, and
`POST /api/roster/generate` answers `402`.

## 9. Operating it

**Back up the database and the uploaded documents nightly.** They are the
irreplaceable assets: the database holds every account, subscription and
document record; the documents directory holds the PDFs themselves. SQLite's
`.backup` is safe on a live database (no need to stop the service):

```bash
sudo mkdir -p /var/backups/invigismart
sudo tee /etc/cron.daily/invigismart-backup >/dev/null <<'SH'
#!/bin/sh
set -e
sqlite3 /var/lib/invigismart/invigismart.db ".backup /var/backups/invigismart/$(date +%F).db"
tar -czf /var/backups/invigismart/documents-$(date +%F).tar.gz \
  -C /var/lib/invigismart documents
find /var/backups/invigismart \( -name '*.db' -o -name 'documents-*.tar.gz' \) \
  -mtime +30 -delete
SH
sudo chmod 755 /etc/cron.daily/invigismart-backup
```

Copy those files **off the server** (object storage, another host). A backup that
only exists on the machine that dies is not a backup.

**Restore drill** (do this once, before you need it):
`sudo systemctl stop invigismart &&`
`sudo cp /var/backups/invigismart/<date>.db /var/lib/invigismart/invigismart.db &&`
`sudo tar -xzf /var/backups/invigismart/documents-<date>.tar.gz -C /var/lib/invigismart &&`
`sudo chown -R invigismart:invigismart /var/lib/invigismart &&`
`sudo systemctl start invigismart`

**Deploying a change:**

```bash
cd /opt/invigismart && sudo -u invigismart git pull
python3 server/tests/test_api.py          # green before you restart
sudo systemctl restart invigismart
curl -s localhost:8080/api/health | python3 -m json.tool | head -5
```

(Deploying without git? Use the `rsync` command from step 2, then the same
test → restart → health-check sequence.)

No migration step is needed — the schema is applied with
`CREATE TABLE IF NOT EXISTS` at startup. Sessions survive a restart as long as
`JWT_SECRET` is stable; customers are not signed out.

**Logs.** `journalctl -u invigismart -f` for the app, `journalctl -u caddy -f`
for TLS. If `server.log` accumulates in the working directory, that is a stray
redirect from a manual run — the service logs to the journal. Keep an eye on
disk usage: SQLite grows with traffic, and `*.db-wal`/`*.db-journal` files may
appear next to the database. Uploaded PDFs count against
`MAX_DOCUMENT_STORAGE_BYTES` per account, but the disk itself is not capped:
watch `du -sh /var/lib/invigismart/documents`.

**Where schools reach you.** `SUPPORT_EMAIL` and `SUPPORT_PHONE` are printed in
the landing-page footer and FAQ, the dashboard footer and the billing panel, and
served through `/api/health` so all three agree. Monitor that inbox and that
number: with no self-service password reset, an unanswered message is a locked-out
school or a lost renewal.

## 10. When it refuses to start

`main()` prints the reason and exits with code `2`. Read it with:

```bash
sudo journalctl -u invigismart -n 40 --no-pager
```

| Message | Fix |
| --- | --- |
| `PAYFAST_PASSPHRASE is not set` | Set it to the same value as in your PayFast account (step 6.2). PayFast validates the payment form's signature with it, so without it **every checkout is rejected**. |
| `PayFast is not configured` | Set `PAYFAST_MERCHANT_ID` **and** `PAYFAST_MERCHANT_KEY` from *Settings → Integration*. |
| `PLAN_PRICE_AMOUNT must be a positive number` | Fix the typo (`1200.00`). This is the amount actually charged. |
| `JWT_SECRET is not set` | Generate one and restart. |
| `PAYFAST_SANDBOX=1 on a public host` (warning only) | You are pointed at the sandbox and **no real money moves**. Set `PAYFAST_SANDBOX=0` before selling. |
| `ALLOW_MOCK_BILLING ignored` (warning only) | Expected on any https host. Mock billing is force-disabled. |

Other failures:

| Symptom | Cause |
| --- | --- |
| `Address already in use` | Something else holds `PORT`. Change it and update the proxy. |
| `unable to open database file` | `/var/lib/invigismart` is missing or not writable by `invigismart` (step 1). |
| **A school paid and is still locked out** | The ITN was rejected or never arrived (look for a non-`200` on `/api/billing/webhook` in step 7). See the replay procedure below. |
| PayFast shows **"signature mismatch"** instead of the payment page | `PAYFAST_PASSPHRASE` differs from your PayFast account, or `CHECKOUT_FIELD_ORDER` in `server/payfast.py` no longer matches PayFast's documented field order. No money moved. |
| PayFast shows **"Merchant unable to receive payments due to invalid account details provided"** (HTTP 400) | The merchant id/key do not belong to the environment being used: live credentials against `sandbox.payfast.co.za` (or the reverse), or the account is not yet activated in PayFast. Check `payfastSandbox` in `/api/health` against the credentials' environment, and that PayFast has verified the account. No money moved. |
| Cancel reports the gateway is unreachable | The server could not reach `api.payfast.co.za`. Nothing was cancelled and access is unchanged — retry, and check outbound HTTPS from the host. |
| A year was granted with no payment | Check `PAYFAST_ITN_VERIFICATION`. `off` skips verification entirely; it exists for tests only and must never be set on a public host. |
| `Manage Billing & Invoices` shows an empty panel | Nothing has been paid on that account yet, so there is no history and nothing to cancel. |

**Replaying a notification for a school that paid.** The licence *is* the
`subscriptions` row, so replaying is safe and idempotent. Try PayFast first
(*Transaction history → the transaction → resend ITN*); if that is unavailable,
write the row an ITN would have written:

```bash
sqlite3 /var/lib/invigismart/invigismart.db <<'SQL'
INSERT INTO subscriptions (user_id, plan, status, current_period_end, provider, updated_at)
SELECT id, 'annual_school_unlimited', 'active',
       strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+365 days'), 'payfast',
       strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
  FROM users WHERE email = 'admin@the-school.edu'
ON CONFLICT(user_id) DO UPDATE SET
  status = 'active',
  provider = 'payfast',
  current_period_end = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+365 days'),
  updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now');
SQL
```

Then add the payment to the school's history so your books agree with the
dashboard:

```bash
sqlite3 /var/lib/invigismart/invigismart.db \
  "INSERT INTO payments (user_id, provider, provider_payment_id, amount, currency, status, item_name, created_at)
   SELECT id, 'payfast', 'manual-<pf_payment_id>', '1200.00', 'ZAR', 'COMPLETE', 'Annual School Unlimited',
          strftime('%Y-%m-%dT%H:%M:%SZ','now')
     FROM users WHERE email = 'admin@the-school.edu';"
```

Record what you did and why — a hand-written licence is the one row that no
notification will ever explain.

To bypass the checks deliberately (a disposable staging host, never production):

```bash
ALLOW_INSECURE_BILLING=1    # in /etc/invigismart.env, then restart
```

## 11. Known limits before you take money

These are decisions, not bugs — but each one costs money or trust if ignored.

* **No password reset and no email verification.** There is no reset endpoint and
  no admin CLI, so a paying school that forgets its password is locked out. Add a
  reset flow (you will need outbound email) or keep a documented manual
  procedure before onboarding real customers.
* **Single process, single instance.** SQLite plus `ThreadingHTTPServer` means one
  machine, no horizontal scaling, and no shared session store. Scale by adding a
  WSGI/ASGI server and a real RDBMS, not by adding replicas.
* **No rate limiting or lockout** on `/api/auth/*`. The reverse proxy is your only
  shield; consider `limit_req` (nginx) or rate limiting middleware.
* **Cancellation is in-app for PayFast, and through the portal for Stripe.** On
  PayFast, *Manage Billing & Invoices → Cancel automatic renewal* calls PayFast's
  recurring API and keeps the period already paid for. If the gateway cannot be
  reached, the cancellation is refused rather than silently claimed — a school is
  never told something untrue about its licence.
* **There is no hosted invoice portal on a local gateway.** The Billing panel
  lists every payment (date, amount, status, gateway reference) from your own
  database, but the PDF a South African school may need for its books, and any
  VAT invoice, you issue yourself. Keep the amounts and VAT decision consistent
  with `PLAN_PRICE_AMOUNT`.
* **Entitlement is duration-based, not metered.** A `COMPLETE` payment extends the
  period; a failed or refunded payment does **not** revoke access, and nothing
  shortens a period that has been granted. Remove a licence deliberately (the
  replay procedure in section 10, inverted) rather than expecting a refund to do it.
* **PayFast's signature is pinned by tests; the field order has not yet met a live
  sandbox payment.** The algorithm is verified against PayFast's own published
  test vectors, and a wrong order fails closed — PayFast shows *signature mismatch*
  and takes no money — but it is still a checkout a school cannot complete. Run
  section 8's sandbox purchase before selling.
* **Card recurrence is what makes a subscription; EFT is a one-off payment.**
  Schools that pay by EFT simply pay once and you grant the year. That is normal
  in South African procurement (quotation → PO → 30-day EFT), so budget for a
  manual lane alongside the gateway.
* **SEO basics are still missing** (`robots.txt`, `sitemap.xml`, canonical and
  social tags); buyers will reach you by word of mouth rather than search until
  those exist.
* **The Tailwind Play CDN compiles CSS in the browser** on both pages, which
  slows first paint and breaks if the CDN is unreachable. Vendor a built
  stylesheet if the landing page becomes your main sales channel — school
  networks in particular block third-party CDNs.

## 12. Hosting the backend somewhere the static site can reach it

`invigismart.online` is shared cPanel hosting: Apache and PHP, no long-running
process, no port to bind, no systemd. The backend therefore lives on a second
host, and the two halves talk over HTTPS:

```
https://invigismart.online      static pages      cPanel (upload-static.sh)
        │  fetch('https://api.invigismart.online/api/...')
        ▼
https://api.invigismart.online  server/app.py     a container or a VPS
        ▲  PayFast ITN POSTs here (notify_url)
```

Three ways to get the second half. Pick one; all of them end at the same
verification step.

| Path | Cost | Effort | Right when |
|---|---|---|---|
| **A. Render Blueprint** (`render.yaml`) | ~$7/mo + $0.25/GB disk | 15 min, no server admin | you want TLS, restarts and logs handled for you |
| **B. Docker on any VPS / your own box** | VPS ~$5/mo | 30–45 min; §1–§5 also apply | you already pay for a VPS, or want it in your own country |
| **C. `cloudflared` tunnel from this laptop** | free | 5 min, laptop must stay on | a demo for one school this week, not a product |

### Path A — Render Blueprint

1. Push this repo to GitHub (already done: `main` on
   `netdaniel/Invigismart-Pro-`).
2. Render dashboard → **New + → Blueprint** → pick the repo → **Apply**. It reads
   `render.yaml`, builds `./Dockerfile`, mounts a 1 GB disk at `/data` and asks
   for the values marked `sync: false`:

   | Variable | Value |
   |---|---|
   | `APP_BASE_URL` | the URL Render assigns, e.g. `https://invigismart-api.onrender.com` |
   | `PAYFAST_MERCHANT_ID` | from PayFast → Settings → Integration |
   | `PAYFAST_MERCHANT_KEY` | ditto |
   | `PAYFAST_PASSPHRASE` | ditto — the same passphrase, or every checkout is rejected |

   `JWT_SECRET` is generated by Render; `PAYFAST_SANDBOX` stays `1` until a
   sandbox purchase works.
3. Wait for **Live**, then read the boot log for the startup banner: it must say
   `payfast : sandbox` and must **not** print a `WARNING` about `JWT_SECRET` or
   `PAYFAST_PASSPHRASE`. A `billing fatal` message means the server exited on
   purpose (section 10) — fix the variable, not the code.
4. Verify from outside, through TLS, the way the browser will:

   ```bash
   curl -s https://invigismart-api.onrender.com/api/health | python3 -m json.tool
   ```

   Expect `"publicHost": true`, `"ephemeralJwtSecret": false`,
   `"billingConfigured": true`, `"mockBilling": false`, and one
   `billingWarnings` entry about `PAYFAST_SANDBOX=1` on a public host.
5. **Custom domain (recommended, and required before you sell).** The
   `onrender.com` name works, but it is not what PayFast, schools or your invoices
   should carry, and moving off it later breaks every stored `notify_url`.
   Render → Settings → Custom Domains → `api.invigismart.online`, then add the
   record Render asks for at your DNS provider (a `CNAME` to
   `<service>.onrender.com`). Change `APP_BASE_URL` to
   `https://api.invigismart.online` and redeploy: that value builds the
   return/cancel URLs and the `notify_url` sent with every checkout.

### Path B — Docker on a VPS, or your own machine

The image is self-contained (standard library, no `pip install`):

```bash
docker build -t invigismart .
docker run -d --name invigismart --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v invigismart-data:/data \
  --env-file /etc/invigismart.env \
  invigismart
```

The image already defaults `HOST`/`PORT`/`DB_PATH`/`DOCUMENTS_DIR`, so a plain
`.env.production` (section 4) is the whole configuration. Put Caddy or nginx in
front for TLS (section 5) and keep the port bound to `127.0.0.1`: the container
speaks plain HTTP, and the only thing that should face the internet is the
proxy.

Without Docker, §1–§5 are unchanged: service user, `/var/lib/invigismart`, the
systemd unit, and the reverse proxy.

### Path C — Cloudflare tunnel (demo only)

```bash
cloudflared tunnel --url http://localhost:8100
```

It prints a `https://<random>.trycloudflare.com` URL. Set `APP_BASE_URL` to it,
restart the backend, and point `api-base.js` at it. This dies the moment the
laptop sleeps, and a quick tunnel gets a new name on every start — so every
stored `notify_url` dies with it. Fine for showing a school the dashboard; never
for taking a payment you intend to keep.

### Then connect the two halves

Both pages load `/api-base.js` before building any request URL, so pointing the
site at the API is one line, then a re-upload:

```js
// api-base.js
window.INVIGISMART_API = 'https://api.invigismart.online';   // no trailing slash
```

```bash
./deploy/upload-static.sh --apply --verify
```

No CORS work is needed: `server/app.py` answers `Access-Control-Allow-Origin: *`
and allows the `Authorization` header. That is safe here because sessions are
bearer tokens rather than cookies, so no ambient credentials ride along to a
third-party origin.

Check the preflight the browser sends before it ever POSTs:

```bash
curl -si -X OPTIONS https://api.invigismart.online/api/billing/checkout \
  -H 'Origin: https://invigismart.online' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: authorization,content-type' \
  | grep -i '^access-control'
```

Then click **Buy annual licence** on the live site. With `PAYFAST_SANDBOX=1` you
land on PayFast's sandbox payment page; complete it with a sandbox card and
confirm the licence flips to active with no browser involvement — that is the
ITN arriving (section 8 has the four checks).

### Two things that bite on every PaaS

* **The disk is the database.** `/data` must be a persistent volume. With no
  disk (every free plan), SQLite lives in the container filesystem and each
  deploy or idle-restart silently deletes every licence and every uploaded
  document.
* **One instance, never more.** SQLite plus `ThreadingHTTPServer` is a single
  writer on a single machine (section 11). Two instances would each keep a
  different database, and whichever answered an ITN would grant a licence the
  other one cannot see.

Next practical steps, in order: add the password-reset flow, then `robots.txt` +
`sitemap.xml` for the domain you just deployed, then a smoke test in your
monitoring tool that hits `/api/health` and alerts if `billingConfigured` flips
to `false`, `payfastSandbox` flips to `true`, or the service exits.

