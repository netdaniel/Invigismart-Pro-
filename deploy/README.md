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

1. cPanel → **FTP Accounts** → the account → **Directory**: set `public_html`.
2. Save. cPanel creates the directory if it is missing.
3. Retry. `./deploy/upload-static.sh --apply` now preflights the login and prints
   this hint instead of a row of anonymous `FAILED` lines.

### The domain directory is not the docroot

`/home/<user>/domains/<domain>` reads like the website root and is easy to pick
in cPanel, but on a **main domain** it is only a log directory. On this host it
contains `logs/` — holding `Oct-2026.tar.gz` — and `stats/`, and nothing else:

```
$ curl -o /dev/null -w '%{http_code}' https://invigismart.online/logs/
404
```

A 404 there proves Apache does not serve those folders, so uploading into the
domain directory succeeds byte-for-byte and changes nothing on the site. That is
the worst failure mode, because every layer reports success.

The script now refuses a target that contains `logs/` and `stats/` but no
`index.html`:

```
Refusing to upload: ftp://dahost11.vpslocal.co.za/ is a cPanel log directory, not a web root.
```

Set that FTP account's **Directory** to `public_html` so its jail root *is* the
docroot, then leave `FTP_REMOTE_DIR=/`. `FTP_ALLOW_ANY_DIR=1` overrides the
check if a host ever genuinely serves such a directory.



## Option 3 — cPanel Git Version Control

cPanel → *Git™ Version Control* can clone this GitHub repo onto the host and
redeploy on demand. Useful because uploads then originate from the server, so
no password lives in the IDE. It still deploys static files only.

## Choosing between this and GitHub Pages

Both cannot own `invigismart.online` at once: the apex `A` records either point
at this Apache host (`156.155.252.98`) or at GitHub Pages (`185.199.10x.153`).
Pick one before changing DNS, and keep the cPanel account alive either way —
IMAP/SMTP and webmail for `daniel@invigismart.online` still run on it.
