#!/usr/bin/env bash
#
# Upload the Invigismart Pro *static site* to the cPanel/Apache host over FTPS.
#
# The host (156.155.252.98) has no SSH - port 22 is closed - so rsync and
# `git push` deploys are not available. FTPS (port 21, Pure-FTPd + explicit
# TLS) is the upload channel, or the --zip archive below for File Manager when
# port 21 is unreachable. That host is shared hosting: it cannot run the Flask
# backend, so this uploads the static site only.
#
#   server/ is deliberately NOT uploaded. Shared hosting cannot execute it, and
#   it would publish the API source at https://invigismart.online/server/app.py
#
# Usage:
#   cp deploy/.deploy.env.example .deploy.env     # then fill in user/password
#   ./deploy/upload-static.sh                     # dry run, shows the plan
#   ./deploy/upload-static.sh --verify            # check the live site, upload nothing
#   ./deploy/upload-static.sh --apply             # upload
#   ./deploy/upload-static.sh --apply --verify    # upload, then check over HTTPS
#   ./deploy/upload-static.sh --zip               # no FTP: build a File Manager zip
#
# Credentials come from .deploy.env (gitignored) or the environment. FTP is not
# the only way up: a plan with no usable FTP account, or a network whose packets
# port 21 drops, can ship the same file list as one archive for File Manager's
# Upload + Extract.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLY=0
VERIFY=0
ZIP=0
for arg in "$@"; do
  case "$arg" in
    --apply)   APPLY=1 ;;
    --verify)  VERIFY=1 ;;
    --zip)     ZIP=1 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ -f .deploy.env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.deploy.env; set +a
fi

# The server's Let's Encrypt certificate is only valid for this hostname, so
# connecting by name (not by IP) keeps TLS verification switched on.
FTP_HOST="${FTP_HOST:-dahost11.vpslocal.co.za}"
FTP_USER="${FTP_USER:-}"
FTP_PASS="${FTP_PASS:-}"
FTP_REMOTE_DIR="${FTP_REMOTE_DIR:-public_html}"
FTP_INSECURE="${FTP_INSECURE:-0}"
SITE_URL="${SITE_URL:-https://invigismart.online}"

# A value like "Login: /home/xxx/public_ftp" is panel text pasted by mistake.
# Legitimate values are a directory relative to the FTP login root: "/" or
# "public_html". Spaces and colons never appear in a usable FTP path here.
case "$FTP_REMOTE_DIR" in
  *" "*|*:*)
    echo "FTP_REMOTE_DIR is not a path: '$FTP_REMOTE_DIR'" >&2
    echo "It looks like text copied from the cPanel FTP Accounts screen." >&2
    echo "Set it to the remote path below the login root, e.g. / or public_html" >&2
    exit 1
    ;;
esac

# Normalise so the upload URL never contains "//": a leading or trailing slash
# on FTP_REMOTE_DIR is a matter of taste, the FTP path it produces is not.
REMOTE_PATH="${FTP_REMOTE_DIR#/}"
REMOTE_PATH="${REMOTE_PATH%/}"
REMOTE_URL="ftp://$FTP_HOST/${REMOTE_PATH:+$REMOTE_PATH/}"

# Everything the website is made of. Anything else in the repo stays local.
# .htaccess belongs here too: it is what makes the extensionless "/terms" and
# "/privacy" links on the landing page resolve, and the site is broken without it.
SITE_FILES=(
  index.html
  landing.html
  privacy.html
  terms.html
  .htaccess
  # Where the pages find the API. ships even when blank: without the file the
  # pages fall back to same-origin, which on this host has no backend.
  api-base.js
  # Search-engine furniture. DEPLOY.md section 11 used to list these as missing:
  # without them the landing page is only findable by word of mouth, and PayFast's
  # account review looks for a public site that search engines can actually see.
  robots.txt
  sitemap.xml
  assets/logo.svg
  assets/logo-mark.svg
  assets/timetable-import.js
  assets/exam-types.js
  # Pinned front-end libraries, self-hosted so a page that keeps its session
  # token in localStorage never executes a script from another origin. The
  # version is part of the filename, so a new release is a new URL and can be
  # cached for a year (see .htaccess). The SRI hashes in the pages pin them.
  assets/tailwind-3.4.16.js
  assets/lucide-0.451.0.min.js
  # Icons: /favicon.ico is what a browser asks for when a page names none, and
  # iOS uses the 180px tile when a school adds the site to a home screen.
  favicon.ico
  assets/apple-touch-icon.png
)

for f in "${SITE_FILES[@]}"; do
  [ -f "$f" ] || { echo "missing site file: $f" >&2; exit 1; }
done

print_files() {
  echo "files     : ${#SITE_FILES[@]}"
  for f in "${SITE_FILES[@]}"; do
    printf '            %-30s %8s bytes\n' "$f" "$(wc -c <"$f" | tr -d ' ')"
  done
}

# File Manager route. A login that is blocked, missing, or jailed somewhere the
# docroot is not is common enough that the website has to go up without port 21
# at all: this builds the same list as one archive for DirectAdmin/cPanel's
# Upload + Extract. Nothing secret is in it - every file here is already served
# by the public website.
if [ "$ZIP" -eq 1 ]; then
  # ZIP_PATH overrides the output (the test suite builds into a temp dir).
  ZIP_PATH="${ZIP_PATH:-deploy/invigismart-site.zip}"
  echo "archive   : $ZIP_PATH  (uploaded by hand, no FTP)"
  print_files
  rm -f "$ZIP_PATH"
  python3 - "$ZIP_PATH" "${SITE_FILES[@]}" <<'PY'
import sys, zipfile

archive, files = sys.argv[1], sys.argv[2:]
# Write the folders as their own entries: DirectAdmin's extractor, unzip and
# Windows Explorer then all recreate them instead of relying on implied parents.
dirs = sorted({f.rsplit("/", 1)[0] for f in files if "/" in f})
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
    for d in dirs:
        z.writestr(d + "/", "")
    for f in files:
        z.write(f, f)
PY
  echo
  echo "built $ZIP_PATH - $(wc -c <"$ZIP_PATH" | tr -d ' ') bytes"
  cat <<TXT

Upload it without FTP:
  1. DirectAdmin -> File Manager -> the docroot, the directory that already
     holds index.html (on this host:
     /home/shfckyzk/domains/invigismart.online/public_html).
  2. Upload -> choose $ZIP_PATH -> Reload.
  3. Tick the archive -> Extract -> Extract Files. Keep the prefilled path, so
     index.html, .htaccess and assets/ land beside the copies already there.
     Delete the archive afterwards.
  4. File Manager -> Settings -> Show Hidden Files, then confirm .htaccess is
     there: it is what makes /terms and /privacy resolve, and without it those
     links 404 however correct the pages are.
  5. ./deploy/verify-live.sh, from a network that can reach the site.
TXT
  exit 0
fi

echo "host      : $FTP_HOST  (FTPS, explicit TLS)"
echo "remote dir: $REMOTE_URL"
print_files

# The site is fetched through public DNS, and the zone has been mid-move: while
# two A records were live, a resolver that cached both hands out either one, so
# some requests land on the previous host - which has no .htaccess and no
# privacy.html - and report 404 for pages that are demonstrably present. Naming
# the address that answered turns that from a mystery into a diagnosis.
SITE_ORIGIN_IP="${SITE_ORIGIN_IP:-156.155.252.98}"

# Every file the pages load from this origin, taken from the pages themselves
# rather than from a hand-kept list. The hand-kept list went stale exactly once:
# .htaccess arrived with the CDN dropped from script-src while landing.html still
# pointed at it and the self-hosted copies were missing, so every page rendered
# unstyled while this function reported nothing but 200s. A URL read out of the
# HTML cannot drift from the HTML: if a page references a file, it is checked.
page_assets() {
  grep -ho '\(src\|href\)="/[^"]*"' index.html landing.html \
    | sed 's/^[a-z]*="//; s/"$//' \
    | grep -E '\.(js|css|png|jpe?g|gif|svg|ico|webp|woff2?|json)$' \
    | sort -u
}

verify_site() {
  echo
  echo "verifying over HTTPS:"
  local p code size ip failed=0

  check_url() {
    local code size ip
    printf '  %-34s ' "$1"
    read -r code size ip < <(curl -s -o /dev/null \
      -w '%{http_code} %{size_download} %{remote_ip}\n' "$SITE_URL$1")
    printf '%s  %sb  via %s\n' "$code" "$size" "$ip"
    { [ "$code" = "200" ] && [ "$ip" = "$SITE_ORIGIN_IP" ]; } || return 1
  }

  for p in / /landing.html /privacy /terms /privacy.html /terms.html \
           /app /login /signup /reset-password /robots.txt /sitemap.xml; do
    check_url "$p" || failed=$((failed + 1))
  done
  while read -r p; do
    check_url "$p" || failed=$((failed + 1))
  done < <(page_assets)

  if [ "$failed" -ne 0 ]; then
    echo
    echo "warning: $failed path(s) did not answer 200 from $SITE_ORIGIN_IP (this host)." >&2
    echo "  A 404 on an /assets/ path means the page is live but a file it loads is" >&2
    echo "  not, which looks like a broken design rather than an error: no styling," >&2
    echo "  no icons, no error in any log. Re-run with --apply and confirm the file" >&2
    echo "  reached the docroot's assets/ directory." >&2
    echo "  An answer from another address is the other cause. A resolver can still" >&2
    echo "  hold the previous host's address, and that server has no .htaccess, so" >&2
    echo "  /terms and /privacy are 404 there no matter what was just uploaded. Ask" >&2
    echo "  the authoritative name server instead:" >&2
    echo "    dig @ns1.clusterdns.co.za invigismart.online A +short" >&2
    echo "  then clear the local cache (resolvectl flush-caches) and retry." >&2
  fi
}

if [ "$APPLY" -ne 1 ]; then
  if [ "$VERIFY" -eq 1 ]; then
    verify_site
    echo
    echo "Dry run - nothing uploaded (only the live site was checked)."
  else
    echo
    echo "Dry run - nothing uploaded. Re-run with --apply to upload."
  fi
  exit 0
fi

if [ -z "$FTP_USER" ] || [ -z "$FTP_PASS" ]; then
  echo "FTP_USER / FTP_PASS are not set." >&2
  echo "Copy deploy/.deploy.env.example to .deploy.env and fill it in." >&2
  exit 1
fi

CURL_OPTS=(--ssl-reqd --fail --silent --show-error
           --connect-timeout 20 --max-time 180 --ftp-create-dirs)
if [ "$FTP_INSECURE" = "1" ]; then
  CURL_OPTS+=(-k)
  echo "warning: TLS verification disabled (FTP_INSECURE=1)" >&2
fi

# Log in and list the target directory once, so a broken FTP account fails
# here with a readable reason instead of five times as "uploading ... FAILED".
PROBE_OPTS=(--ssl-reqd --silent --show-error --connect-timeout 20 --max-time 60)
if [ "$FTP_INSECURE" = "1" ]; then
  PROBE_OPTS+=(-k)
fi
echo "checking login and remote directory..."
probe_rc=0
probe_list=$(curl "${PROBE_OPTS[@]}" --user "$FTP_USER:$FTP_PASS" \
                  --list-only "$REMOTE_URL" 2>/dev/null) || probe_rc=$?
if [ "$probe_rc" -ne 0 ]; then
  # Re-run verbosely: curl collapses a 421 into "(28) Timeout" and never prints
  # the server's reply, which hides the real cause. The trace contains the
  # password, so it is captured into a variable and never printed.
  trace_rc=0
  probe_trace=$(curl -v "${PROBE_OPTS[@]}" --user "$FTP_USER:$FTP_PASS" \
                    --list-only "$REMOTE_URL" 2>&1 >/dev/null) || trace_rc=$?
  reply_code=$(printf '%s\n' "$probe_trace" | sed -n 's/^< \([0-9][0-9][0-9]\) .*/\1/p' | tail -n 1)
  reply_text=$(printf '%s\n' "$probe_trace" | sed -n 's/^< [0-9][0-9][0-9] //p' | tail -n 1)
  curl_err=$(printf '%s\n' "$probe_trace" | grep '^curl:' | tail -n 1 || true)
  echo "FTP preflight failed: ${reply_code:+$reply_code }${reply_text:-${curl_err:-no response from server}}" >&2
  # No reply at all means the TCP connection never opened. That is the one
  # failure that says nothing about the password or the directory: a wrong
  # password answers 530 and a wrong jail answers 550, both server replies.
  if [ -z "$reply_code" ]; then
    case "$trace_rc" in
      6)  echo "  $FTP_HOST does not resolve from this machine." >&2 ;;
      7)  echo "  $FTP_HOST:21 refused the connection." >&2 ;;
      28) echo "  Nothing answered on $FTP_HOST:21 within 20 seconds - the packets" >&2
          echo "  are being dropped, not refused." >&2 ;;
      *)  echo "  curl exited $trace_rc before the server sent an FTP reply." >&2 ;;
    esac
    echo "  The login was never offered, so the password and the jail are not in" >&2
    echo "  question. If every port on this host times out from this network while" >&2
    echo "  the same ports answer from elsewhere (check-host.net/check-tcp), the" >&2
    echo "  server's firewall is holding this IP - CSF/LFD on DirectAdmin, usually" >&2
    echo "  after repeated failed logins. Either unblock it (csf -dr <ip>), upload" >&2
    echo "  from another network (a phone hotspot), or skip port 21 entirely:" >&2
    echo "      ./deploy/upload-static.sh --zip     # File Manager, same files" >&2
    echo "  DEPLOY.md 13.6 has the unblock commands." >&2
    exit 1
  fi
  case "$reply_code" in
    421)
      echo "  The FTP account's home directory is missing on the server." >&2
      echo "  cPanel -> FTP Accounts -> set Directory to public_html, save, retry." >&2
      ;;
    530) echo "  Wrong username or password." >&2 ;;
    550) echo "  The remote directory '$FTP_REMOTE_DIR' does not exist." >&2 ;;
    *)   echo "  Check FTP_HOST, the network and FTP_INSECURE." >&2 ;;
  esac
  exit 1
fi

# cPanel will happily point an FTP account at domains/<domain>, which exists and
# accepts uploads but is only log archives - Apache never serves it. Uploading
# there reports success and changes nothing on the website, so refuse it.
if [ "${FTP_ALLOW_ANY_DIR:-0}" != "1" ] \
   && printf '%s\n' "$probe_list" | grep -qx 'logs' \
   && printf '%s\n' "$probe_list" | grep -qx 'stats' \
   && ! printf '%s\n' "$probe_list" | grep -qx 'index.html'; then
  echo "Refusing to upload: $REMOTE_URL is a cPanel log directory, not a web root." >&2
  echo "It holds logs/ and stats/ but no index.html, so Apache never serves it." >&2
  echo "Point FTP_REMOTE_DIR at the real docroot (on this host: /public_html)." >&2
  echo "Only set FTP_ALLOW_ANY_DIR=1 if you are certain this is the docroot." >&2
  exit 1
fi
echo "login ok"

failed=0
for f in "${SITE_FILES[@]}"; do
  printf 'uploading %-30s ' "$f"
  if curl "${CURL_OPTS[@]}" --user "$FTP_USER:$FTP_PASS" \
       --upload-file "$f" "$REMOTE_URL$f"; then
    echo ok
  else
    echo FAILED
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "one or more uploads failed" >&2
  exit 1
fi

echo "all ${#SITE_FILES[@]} files uploaded"

if [ "$VERIFY" -eq 1 ]; then
  verify_site
fi
