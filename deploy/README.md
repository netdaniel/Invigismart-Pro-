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
made of (`index.html`, `landing.html`, `privacy.html`, `terms.html`,
`assets/*`). `server/`, `.env`, `*.db`, `server/uploads/` and timetable backups
are never touched, and nothing is deleted on the server.

## Option 2 — upload on save in VS Code (the Dreamweaver replacement)

Install the maintained **SFTP** extension (`natizyskunk.sftp`), then open
`.vscode/sftp.json` (already present, gitignored) and fill in the real cPanel
user and FTP password. That is the whole setup.

```json
{
  "name": "invigismart.online (cPanel FTPS)",
  "host": "dahost11.vpslocal.co.za",
  "protocol": "ftp",
  "port": 21,
  "secure": true,
  "username": "cpaneluser",
  "password": "ftp-password",
  "remotePath": "/",
  "uploadOnSave": true,
  "useTempFile": true,
  "ignore": [".git/**", ".vscode/**", "server/**", "deploy/**", "*.db"]
}
```

Three details matter here, and the common online guides get two of them wrong:

- **`"protocol": "ftp"`, not `"sftp"`.** SFTP is FTP tunnelled over SSH; port 22
  is closed on this host, so an `sftp`/`22` config fails at connect with
  `ECONNREFUSED`. The extension speaks plain/secure FTP fine — use it.
- **`"username"`, not `"user"`.** The extension reads `username`; a `user` key is
  silently ignored and the login fails with no useful error.
- **`"secure": true`** turns on explicit TLS for both the control and data
  connections, which is what Pure-FTPd here expects. If the handshake fails, try
  `"secure": "control"` (control channel only); if it fails on the certificate
  name instead, add `"secureOptions": { "rejectUnauthorized": false }` — but
  prefer fixing the hostname, since connecting as `dahost11.vpslocal.co.za`
  already matches the certificate.

If your host ever enables real SSH, switch to `"protocol": "sftp"`, `"port": 22`
and `"remotePath": "/home/cpaneluser/public_html"`.

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
| `A` | `invigismart.online` (`@`) | `156.155.252.98` | 7200 |
| `CNAME` | `www` | `invigismart.online` | 3600 |

To leave GitHub Pages, delete the GitHub `A` record(s)
(`185.199.108.153`, `.109.153`, `.110.153`, `.111.153`) and any `AAAA` records
(`2606:50c0:800x::153`), then set the two rows above. Do not touch the mail
records: `MX` stays `10 mx1.tld-mx.com` / `20 mx2.tld-mx.net`, and the `TXT`
policy `v=spf1 +a +mx +ip4:156.155.252.98 include:relay.mailchannels.net ~all`
stays as is. Its `+a` term follows the apex `A` record, so it is correct again
once the apex points back here.

TLS needs no work: the host already presents a Let's Encrypt certificate with
`invigismart.online` and `www.invigismart.online` in its SAN list, so HTTPS
answers correctly the moment DNS resolves back.

Check the change at the authoritative server, which ignores resolver caches:

```
$ dig @ns1.clusterdns.co.za invigismart.online A +short
156.155.252.98
$ dig @ns1.clusterdns.co.za www.invigismart.online CNAME +short
invigismart.online.
```

While DNS still points at GitHub, verify the cPanel copy directly by IP — this
is also how `--apply` was confirmed byte-for-byte:

```
$ curl -s --resolve invigismart.online:443:156.155.252.98 \
       https://invigismart.online/landing.html | wc -c
```

Leaving the `CNAME` file in the repo keeps Pages claiming the custom domain, so
GitHub will report a failed DNS check and keep trying to issue certificates.
That is harmless noise; remove the custom domain in *Settings → Pages* (or delete
`CNAME`) if you want it to stop.
