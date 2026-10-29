#!/usr/bin/env bash
#
# Upload the Invigismart Pro *static site* to the cPanel/Apache host over FTPS.
#
# The host (156.155.252.98) has no SSH - port 22 is closed - so rsync and
# `git push` deploys are not available. FTPS (port 21, Pure-FTPd + explicit
# TLS) is the only way in. That host is shared hosting: it cannot run the Flask
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
#
# Credentials come from .deploy.env (gitignored) or the environment.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLY=0
VERIFY=0
for arg in "$@"; do
  case "$arg" in
    --apply)   APPLY=1 ;;
    --verify)  VERIFY=1 ;;
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
  assets/logo.svg
  assets/logo-mark.svg
  assets/timetable-import.js
)

for f in "${SITE_FILES[@]}"; do
  [ -f "$f" ] || { echo "missing site file: $f" >&2; exit 1; }
done

echo "host      : $FTP_HOST  (FTPS, explicit TLS)"
echo "remote dir: $REMOTE_URL"
echo "files     : ${#SITE_FILES[@]}"
for f in "${SITE_FILES[@]}"; do
  printf '            %-30s %8s bytes\n' "$f" "$(wc -c <"$f")"
done

# The site is fetched through public DNS, and the zone has been mid-move: while
# two A records were live, a resolver that cached both hands out either one, so
# some requests land on the previous host - which has no .htaccess and no
# privacy.html - and report 404 for pages that are demonstrably present. Naming
# the address that answered turns that from a mystery into a diagnosis.
SITE_ORIGIN_IP="${SITE_ORIGIN_IP:-156.155.252.98}"

verify_site() {
  echo
  echo "verifying over HTTPS:"
  local p code size ip stale=0
  for p in / /landing.html /privacy /terms /privacy.html /terms.html \
           /assets/timetable-import.js; do
    printf '  %-30s ' "$p"
    read -r code size ip < <(curl -s -o /dev/null \
      -w '%{http_code} %{size_download} %{remote_ip}\n' "$SITE_URL$p")
    printf '%s  %sb  via %s\n' "$code" "$size" "$ip"
    if [ "$code" != "200" ] || [ "$ip" != "$SITE_ORIGIN_IP" ]; then
      stale=1
    fi
  done

  if [ "$stale" -ne 0 ]; then
    echo
    echo "warning: not every page answered 200 from $SITE_ORIGIN_IP (this host)." >&2
    echo "  A resolver can still hold the previous host's address, and that server" >&2
    echo "  has no .htaccess, so /terms and /privacy are 404 there no matter what" >&2
    echo "  was just uploaded. Ask the authoritative name server instead:" >&2
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
if ! probe_list=$(curl "${PROBE_OPTS[@]}" --user "$FTP_USER:$FTP_PASS" \
                     --list-only "$REMOTE_URL" 2>/dev/null); then
  # Re-run verbosely: curl collapses a 421 into "(28) Timeout" and never prints
  # the server's reply, which hides the real cause. The trace contains the
  # password, so it is captured into a variable and never printed.
  probe_trace=$(curl -v "${PROBE_OPTS[@]}" --user "$FTP_USER:$FTP_PASS" \
                    --list-only "$REMOTE_URL" 2>&1 >/dev/null || true)
  reply_code=$(printf '%s\n' "$probe_trace" | sed -n 's/^< \([0-9][0-9][0-9]\) .*/\1/p' | tail -n 1)
  reply_text=$(printf '%s\n' "$probe_trace" | sed -n 's/^< [0-9][0-9][0-9] //p' | tail -n 1)
  curl_err=$(printf '%s\n' "$probe_trace" | grep '^curl:' | tail -n 1 || true)
  echo "FTP preflight failed: ${reply_code:+$reply_code }${reply_text:-${curl_err:-no response from server}}" >&2
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
