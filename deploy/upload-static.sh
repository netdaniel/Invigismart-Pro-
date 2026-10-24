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

# Everything the website is made of. Anything else in the repo stays local.
SITE_FILES=(
  index.html
  landing.html
  privacy.html
  terms.html
  assets/logo.svg
  assets/logo-mark.svg
  assets/timetable-import.js
)

for f in "${SITE_FILES[@]}"; do
  [ -f "$f" ] || { echo "missing site file: $f" >&2; exit 1; }
done

echo "host      : $FTP_HOST  (FTPS, explicit TLS)"
echo "remote dir: ~/$FTP_REMOTE_DIR"
echo "files     : ${#SITE_FILES[@]}"
for f in "${SITE_FILES[@]}"; do
  printf '            %-30s %8s bytes\n' "$f" "$(wc -c <"$f")"
done

if [ "$APPLY" -ne 1 ]; then
  echo
  echo "Dry run - nothing uploaded. Re-run with --apply to upload."
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

failed=0
for f in "${SITE_FILES[@]}"; do
  printf 'uploading %-30s ' "$f"
  if curl "${CURL_OPTS[@]}" --user "$FTP_USER:$FTP_PASS" \
       --upload-file "$f" "ftp://$FTP_HOST/$FTP_REMOTE_DIR/$f"; then
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
  echo
  echo "verifying over HTTPS:"
  for p in / /landing.html /privacy.html /terms.html /assets/timetable-import.js; do
    printf '  %-30s ' "$p"
    curl -s -o /dev/null -w '%{http_code}  %{size_download}b\n' "$SITE_URL$p"
  done
fi
