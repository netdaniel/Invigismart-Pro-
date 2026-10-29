# Uploading the site to the live server

`invigismart.online` is served by **shared cPanel hosting** at `156.155.252.98`
(Apache/Pure-FTPd). That host runs static files only — it cannot run the Flask
backend in `server/`, which is a separate deployment (see `DEPLOY.md`).

## What the host actually allows

| Access | State | Consequence |
|---|---|---|
| SSH (22) | closed | no `git push` deploy target, no `rsync` over SSH, and **no SFTP** — use FTPS |
| FTPS (21) | open, explicit TLS | this is the upload channel |
| cPanel | `https://156.155.252.98:2222` | manage FTP accounts, DNS zone, Git Version Control |

The FTP service's certificate is issued for `dahost11.vpslocal.co.za` (which
resolves to the same box), so connect by **hostname**, not IP, and TLS
verification stays on.

## Option 1 — run the script (works from any agent, including Antigravity)

```bash
cp deploy/.deploy.env.example .deploy.env   # fill in cPanel/FTP user + password
./deploy/upload-static.sh                   # dry run: lists what would change
./deploy/upload-static.sh --apply --verify   # upload, then check over HTTPS
```

`.deploy.env` is gitignored. The script uploads only the files the website is
made of (`index.html`, `landing.html`, `privacy.html`, `terms.html`, `.htaccess`,
`api-base.js`, `assets/*`). `server/`, `.env`, `*.db`, `server/uploads/` and
timetable backups are never touched, and nothing is deleted on the server.

`api-base.js` is what points the pages at the backend (`window.INVIGISMART_API`).
Blank means same-origin, which is right when the Python server serves the pages
itself; set it to the API's origin when the pages live here and the backend runs
elsewhere. See `DEPLOY.md` §12.

`.htaccess` is not optional: the pages link to `/terms` and `/privacy`
(extensionless, the way the Flask app serves them), and on a static host those
paths only resolve because `.htaccess` rewrites them to the `.html` files. Skip
it and the legal links 404 — which is exactly the sort of thing a payment
provider rejects an account over. It also maps `/app` to `index.html`, matching
the backend's route for the dashboard.

## Option 2 — upload on save in VS Code (the Dreamweaver replacement)

Install the maintained **SFTP** extension (`natizyskunk.sftp`) — not
`liximomo.sftp`, which its own author deprecated in favour of this fork — then
open `.vscode/sftp.json` (already present, gitignored) and fill in the real
cPanel user and FTP password. That is the whole setup.

Do **not** then run *SFTP: Config* from the Command Palette: that command exists
to create the file, and with one already there it either refuses or drops a
second, empty configuration next to yours — which is how a working setup stops
uploading for no visible reason.

```json
{
  "name": "invigismart.online (cPanel FTPS)",
  "host": "dahost11.vpslocal.co.za",
  "protocol": "ftp",
  "port": 21,
  "secure": true,
  "username": "cpaneluser",
  "password": "ftp-password",
  "remotePath": "/public_html",
  "uploadOnSave": true,
  "useTempFile": true,
  "ignore": [".git/**", ".vscode/**", "server/**", "deploy/**", "*.db"]
}
```

Five details matter here, and the copy-paste guides circulating online get most of
them wrong:

- **`"protocol": "ftp"`, not `"sftp"`.** SFTP is FTP tunnelled over SSH; port 22
  is closed on this host, so an `sftp`/`22` config fails at connect with
  `ECONNREFUSED`. The extension speaks plain/secure FTP fine — use it.
- **`"port": 21` is not optional.** `22` is the extension's default, and it is
  the value every generic example carries, so omitting the key sends you to a
  closed port.
- **`"remotePath": "/public_html"`, not `/`.** `/` is the FTP jail root, which
  holds `logs/` and `stats/` — Apache never serves it, so uploads "succeed" and
  the site never changes.
- **`"username"`, not `"user"`.** The extension reads `username`; a `user` key is
  silently ignored and the login fails with no useful error. The value is the
  whole cPanel FTP account name, `user@domain`, not the bare mailbox name.
- **`"secure": true`** turns on explicit TLS for both the control and data
  connections, which is what Pure-FTPd here expects. If the handshake fails, try
  `"secure": "control"` (control channel only); if it fails on the certificate
  name instead, add `"secureOptions": { "rejectUnauthorized": false }` — but
  prefer fixing the hostname, since connecting as `dahost11.vpslocal.co.za`
  already matches the certificate.

If your host ever enables real SSH, switch to `"protocol": "sftp"`, `"port": 22`
and `"remotePath": "/home/cpaneluser/public_html"`.

Two keys that look plausible and are not:

- **`"openSftp"` does nothing.** It does not exist in the extension's schema.
  The real key is **`"openSsh"`**, which enables atomic uploads and only works
  against an OpenSSH server — so it stays `false` here, and its documented
  prerequisite (`useTempFile` must also be `true`) is already met.
- **`"useTempFile": false` is the wrong default for a live site.** Set to `true`
  the extension writes to a temporary name and renames it, so a visitor
  downloading a page mid-upload cannot receive half a file. On a page that must
  stay byte-identical for review, that is the difference between "slow" and
  "broken".

### What `uploadOnSave` does and does not do

`"uploadOnSave": true` pushes a file the moment you press Ctrl+S — that is the
Dreamweaver behaviour you want. Two consequences worth knowing:

- Saving **is** publishing. There is no staging step, so Ctrl+S on
  `index.html` changes the live public site immediately.
- It honours `ignore`, which is why `server/`, `deploy/`, `.env`, `*.db` and
  timetable backups are listed. Without that list, saving `server/app.py` would
  upload your source and any `*.db` would expose pupil data over HTTP.

It uploads and overwrites; it never deletes remote files. Deleting a local file
leaves the remote copy in place — remove those by hand or with Option 1.

`useTempFile: true` writes to a temporary name and renames, so a visitor loading
the page mid-upload never receives a half-written `index.html`.

### Setting the remote path right

`remotePath` (and `FTP_REMOTE_DIR`) are **relative to the FTP account's login
root**, not to the cPanel home directory. A dedicated FTP account created in
cPanel is jailed to the directory chosen for it, so the session already starts
inside `public_html` and the correct value is `/`. Writing `/public_html` there
would target `public_html/public_html`.

Only the **main cPanel account** (user `shfckyzk`) logs in at the home
directory, and only that account needs `public_html`.

Do not paste text from the cPanel FTP Accounts screen into these fields. It
renders entries as `Login: user@domain` with a directory line, and copying that
verbatim produces a value like `Login: /home/xxx/domains/…/public_ftp`, which is
not a path at all. `public_ftp` is the anonymous-FTP directory and is **not**
served as your website, so files uploaded there never appear on the domain.

### `421 Home directory not available`

This is the error to expect when the account's configured directory does not
exist on the server. The FTP login is accepted, then Pure-FTPd aborts before any
file transfer, so curl reports a timeout. It is a cPanel-side setting, not a
network or firewall problem, and no password or TLS change fixes it:

1. cPanel → **FTP Accounts** → the account → **Directory**: set it to the folder
   that actually holds the site, relative to the account home.
2. Save. Do not assume cPanel created it — see the next section for exactly why.
3. Retry. `./deploy/upload-static.sh --apply` now preflights the login and prints
   this hint instead of a row of anonymous `FAILED` lines.

### Finding the docroot, and the `public_html` that went missing

On this account the FTP jail is `/home/shfckyzk/domains/invigismart.online`, and
it holds `stats/`, `logs/` and `public_html/`. That `public_html` **is** the
Apache docroot — and it had gone missing. With no docroot the vhost answers
`404` for every URL, including `/`, and the fallback error page 404s as well:

```
HTTP/2 404
server: Apache/2

<p>Additionally, a 404 Not Found
error was encountered while trying to use an ErrorDocument to handle the request.</p>
```

The server itself was healthy, though: a request that did not carry the domain in
its `Host:` header got a plain `<html>webserver is functioning normally</html>`.
That contrast — default page fine, the domain 404 — is the signature of a vhost
whose document root does not exist. It also explains an FTP jail that looks like
a logs-only directory.

The reliable way to prove where the docroot is: write a marker over FTP, then ask
Apache for it **by IP**, so neither DNS nor a cache can lie to you.

```
$ printf 'PROBE-%s\n' "$(date +%s)" > /tmp/probe.html
$ curl -sS --ssl-reqd --user "$FTP_USER:$FTP_PASS" \
       -T /tmp/probe.html "ftp://$FTP_HOST/public_html/index.html"
$ curl -sk --resolve invigismart.online:443:156.155.252.98 \
       https://invigismart.online/
PROBE-1793006042
```

The marker coming back proves two things at once: that directory is the docroot,
and this FTP account can write to it. Here it settled the question at
`FTP_REMOTE_DIR=/public_html`.

Two traps worth knowing:

- **The jail root is not the docroot.** Uploading there drops files next to
  `logs/` and `stats/`, which Apache never serves: every transfer reports `ok`
  and the site does not change. `--apply` therefore refuses a target holding
  `logs/` and `stats/` but no `index.html`, which is what a log directory looks
  like. `FTP_ALLOW_ANY_DIR=1` overrides that if you are certain.
- **`public_ftp` is not a web directory** either — it is the anonymous-FTP drop
  box.



## Option 3 — cPanel Git Version Control

cPanel → *Git™ Version Control* can clone this GitHub repo onto the host and
redeploy on demand. Useful because uploads then originate from the server, so
no password lives in the IDE. It still deploys static files only.

## Choosing between this and GitHub Pages

Both cannot own `invigismart.online` at once: the apex `A` records either point
at this Apache host (`156.155.252.98`) or at GitHub Pages (`185.199.10x.153`).
Pick one before changing DNS, and keep the cPanel account alive either way —
IMAP/SMTP and webmail for `daniel@invigismart.online` still run on it.

### DNS records that make this host serve the domain

The zone lives on the registrar's nameservers (`ns1.clusterdns.co.za`,
`ns4.clusterdns.org`, …), edited from cPanel → **Zone Editor**. Only two records
decide who serves the website:

| Type | Name | Value | TTL |
| --- | --- | --- | --- |
| `A` | `invigismart.online` (`@`) | `156.155.252.98` | 14400 |
| `CNAME` | `www` | `invigismart.online` | 14400 |

Both records carry a 4-hour TTL, which makes a provider move slow. Lower them to
`300` *before* the next switch. There is also a wart to expect during one: while
the old and the new `A` record both exist, a resolver that queries in that window
caches **both** and then hands out either address, so some visitors reach the old
host until the entry expires. Editing the record in place — rather than adding
the new address and deleting the old one as separate saves — avoids the window.

### Leftovers still in the zone

The apex `A` already points here, so the site resolves correctly. What survived
the move back are three Pages addresses on *other* names — and one of them is an
invalid record, because a `CNAME` and an `A` cannot share a name:

| Name | Type | Value | Verdict |
| --- | --- | --- | --- |
| `www` | `CNAME` | `invigismart.online` | **keep** — this is what serves `www` |
| `www` | `A` | `185.199.108.153` | **delete** — shadowed by the `CNAME` today, so it takes `www` straight to GitHub the moment anyone removes the `CNAME` |
| `mail` | `A` | `185.199.109.153` | **delete** — GitHub Pages, which answers nothing there (`HTTP 000`, certificate mismatch) |
| `ftp` | `A` | `185.199.110.153` | **delete** — same, and repointing it here would not help: the FTP service's certificate is issued for `dahost11.vpslocal.co.za`, not for `ftp.invigismart.online` |

Nothing needs adding. `www` is served through its `CNAME`, and this host does not
serve mail at all: `MX` hands incoming mail to `tld-mx.com` / `mx2.tld-mx.net`, so
a `mail.` record has no job. Do not touch `MX` or the `TXT` SPF policy
`v=spf1 +a +mx +ip4:156.155.252.98 include:relay.mailchannels.net ~all` — its
`+a` term follows the apex `A`, which is already correct.

Deleting a record does not expire the copies already cached: each survives its
remaining `14400`-second TTL, so a resolver can keep handing out
`185.199.10x.153` for up to four hours. That is what makes `--verify` mix `200`s
from this host with `404`s from GitHub on the same run — which is why it prints
the address that answered each page.

TLS needs no work: the host already presents a Let's Encrypt certificate with
`invigismart.online` and `www.invigismart.online` in its SAN list, so HTTPS
answers correctly at the apex and at `www`.

Check the change at the authoritative server, which ignores resolver caches:

```
$ dig @ns1.clusterdns.co.za invigismart.online A +short
156.155.252.98
$ dig @ns1.clusterdns.co.za www.invigismart.online CNAME +short
invigismart.online.
$ dig @ns1.clusterdns.co.za mail.invigismart.online A +short
$ dig @ns1.clusterdns.co.za ftp.invigismart.online A +short
```

The last two print nothing once the leftovers are gone.

To check the cPanel copy directly, bypassing whatever a resolver currently
returns — this is also how `--apply` was confirmed byte-for-byte:

```
$ curl -s --resolve invigismart.online:443:156.155.252.98 \
       https://invigismart.online/landing.html | wc -c
```

The Pages site itself is already gone: `185.199.111.153` answers
`404 · Site not found · GitHub Pages` for every path, there is no `CNAME` file in
the repo, and nothing needs unpublishing in *Settings → Pages*. GitHub's edge
just keeps answering because DNS still sends `www`, `mail` and `ftp` there — so
deleting those three records is the whole fix.

## Before you deploy `server/` — probe the host first

`server/` is not a Flask app and needs no `pip install`: `app.py` is a
standard-library `ThreadingHTTPServer` that binds `127.0.0.1:8080` and expects a
reverse proxy in front of it. That shape rules hosts out quickly, so probe before
you provision anything:

```bash
ssh root@HOST 'bash -s' < deploy/probe-host.sh > host.txt
# or, already on the host:  sh deploy/probe-host.sh
```

It installs nothing, writes nothing and reads no passwords. Four lines decide
whether the backend can live there:

| Line | Must say | If it does not |
| --- | --- | --- |
| `init_systemd` | `yes` | The unit in `DEPLOY.md` §3 needs systemd; a bare container needs a supervisor instead |
| `python3` | `3.8` or newer | The code uses f-strings and `datetime.timezone`; install a newer interpreter |
| `shared_cpanel` | `no` | Shared cPanel is Apache + PHP: no long-running process, no bound port. It cannot host this |
| `port_80` / `port_443` | `free`, or already held by a proxy | Whatever holds them needs a `reverse_proxy 127.0.0.1:8080` block, not removal |

Two more lines are worth reading before you commit: `outbound_payfast` answering
`404`/`200` (any HTTP code) proves TLS and DNS to `api.payfast.co.za` work —
anything else means the ITN can never be verified; and `disk_slash_var` should
hold the database, the uploaded PDFs and room to grow.

If the box already serves other sites, do not point Caddy at `:80`/`:443`
wholesale — add one vhost next to the existing ones, or the other sites fall over.
