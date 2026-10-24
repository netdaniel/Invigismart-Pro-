# Uploading the site to the live server

`invigismart.online` is served by **shared cPanel hosting** at `156.155.252.98`
(Apache/Pure-FTPd). That host runs static files only — it cannot run the Flask
backend in `server/`, which is a separate deployment (see `DEPLOY.md`).

## What the host actually allows

| Access | State | Consequence |
|---|---|---|
| SSH (22) | closed | no `git push` deploy target, no `rsync` over SSH |
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

## Option 2 — sync from the IDE

Copy `.vscode/sftp.json.example` to `.vscode/sftp.json` (gitignored) and fill in
the user/password, then use the **SFTP** extension (`natizyskunk.sftp`) to
browse `public_html` and upload individual files. The `ignore` list already
excludes `server/`, databases and credentials.

## Option 3 — cPanel Git Version Control

cPanel → *Git™ Version Control* can clone this GitHub repo onto the host and
redeploy on demand. Useful because uploads then originate from the server, so
no password lives in the IDE. It still deploys static files only.

## Choosing between this and GitHub Pages

Both cannot own `invigismart.online` at once: the apex `A` records either point
at this Apache host (`156.155.252.98`) or at GitHub Pages (`185.199.10x.153`).
Pick one before changing DNS, and keep the cPanel account alive either way —
IMAP/SMTP and webmail for `daniel@invigismart.online` still run on it.
