#!/usr/bin/env bash
#
# Upload the Invigismart Pro backend to the cPanel host, as a Passenger app.
#
# cPanel -> "Setup Python App" runs the server under Phusion Passenger, which
# imports passenger_wsgi.py instead of proxying to a port. That is why this host
# can serve the API even though it gives you no shell and no ports: see
# DEPLOY.md section 13 for the one-time cPanel setup this script assumes.
#
# Unlike upload-static.sh this one *does* ship server/ - that is the point - but
# it ships only what runs: no tests, no database, no uploads, and never the .env
# holding JWT_SECRET and the PayFast credentials. Those stay on the server.
#
# Usage:
#   cp deploy/.deploy.env.example .deploy.env     # once, fill in user/password
#   ./deploy/upload-passenger.sh                  # dry run, shows the plan
#   ./deploy/upload-passenger.sh --apply          # upload
#   ./deploy/upload-passenger.sh --apply --verify # upload, then check /api/health
#   ./deploy/upload-passenger.sh --zip            # no FTP: build a File Manager zip
#
# Credentials come from .deploy.env (gitignored) or the environment. FTP is not
# the only way up: many cPanel plans allow a single FTP account, so --zip builds
# the same file list into an archive for File Manager's Upload + Extract.

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

FTP_HOST="${FTP_HOST:-dahost11.vpslocal.co.za}"
FTP_USER="${FTP_USER:-}"
FTP_PASS="${FTP_PASS:-}"
# The application root created in "Setup Python App", below the FTP login root.
FTP_REMOTE_DIR="${BACKEND_REMOTE_DIR:-invigismart-api}"
FTP_INSECURE="${FTP_INSECURE:-0}"
API_HOST="${API_HOST:-api.invigismart.online}"

case "$FTP_REMOTE_DIR" in
  *" "*|*:*)
    echo "BACKEND_REMOTE_DIR is not a path: '$FTP_REMOTE_DIR'" >&2
    echo "Use the application root cPanel created, e.g. invigismart-api" >&2
    exit 1
    ;;
esac

REMOTE_PATH="${FTP_REMOTE_DIR#/}"
REMOTE_PATH="${REMOTE_PATH%/}"
REMOTE_URL="ftp://$FTP_HOST/$REMOTE_PATH/"

# What runs. server/tests, server/uploads, the SQLite file and .env are not here
# on purpose: the first two are development-only, the last two are local state
# and secrets - the server has its own. server/create_demo_account.py is a
# developer CLI that mints a password, so it stays off the live host too.
BACKEND_FILES=(
  passenger_wsgi.py
  index.html
  landing.html
  privacy.html
  terms.html
  api-base.js
  assets/logo.svg
  assets/logo-mark.svg
  assets/timetable-import.js
  server/app.py
  server/config.py
  server/db.py
  server/billing.py
  server/documents.py
  server/payfast.py
  server/roster.py
  server/security.py
  server/wsgi_app.py
)

for f in "${BACKEND_FILES[@]}"; do
  [ -f "$f" ] || { echo "missing file: $f" >&2; exit 1; }
done

print_files() {
  echo "files     : ${#BACKEND_FILES[@]}"
  for f in "${BACKEND_FILES[@]}"; do
    printf '            %-30s %8s bytes\n' "$f" "$(wc -c <"$f" | tr -d ' ')"
  done
}

# File Manager route: the same list, in one archive, for hosts that only give
# you one FTP account (or none you can point at the application root).
if [ "$ZIP" -eq 1 ]; then
  # ZIP_PATH overrides the output (the test suite builds into a temp dir).
  ZIP_PATH="${ZIP_PATH:-deploy/invigismart-api.zip}"
  echo "archive   : $ZIP_PATH  (uploaded by hand, no FTP)"
  print_files
  rm -f "$ZIP_PATH"
  python3 - "$ZIP_PATH" "${BACKEND_FILES[@]}" <<'PY'
import sys, zipfile

archive, files = sys.argv[1], sys.argv[2:]
# Write the folders as their own entries: cPanel's extractor, unzip and Windows
# Explorer then all recreate them, instead of relying on implied parents.
dirs = sorted({f.rsplit("/", 1)[0] for f in files if "/" in f})
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
    for d in dirs:
        z.writestr(d + "/", "")
    for f in files:
        z.write(f, f)
PY
  echo
  echo "built $ZIP_PATH - $(wc -c <"$ZIP_PATH" | tr -d ' ') bytes"
  cat <<'TXT'

Upload it without FTP:
  1. cPanel -> File Manager -> the application root (invigismart-api).
  2. Upload -> choose deploy/invigismart-api.zip -> Reload.
  3. Tick the zip -> Extract -> Extract Files. The app root path is prefilled;
     keep it, so server/ and assets/ land in the right place. Delete the zip.
  4. Create .env there: + File -> name it .env -> Edit -> paste
     .env.passenger.example and fill in the real values (DEPLOY.md 13.2).
  5. Setup Python App -> Restart.

Nothing secret is in the archive: no .env, no database, no uploads, no tests.
TXT
  exit 0
fi

echo "host      : $FTP_HOST  (FTPS, explicit TLS)"
echo "app root  : $REMOTE_URL"
print_files
echo

verify_api() {
  echo "verifying over HTTPS:"
  local base="https://$API_HOST"
  local body
  body=$(curl -sS -m 25 "$base/api/health" || true)
  if [ -z "$body" ]; then
    echo "  /api/health                     NO ANSWER from $base" >&2
    return 1
  fi
  printf '  %-32s %s\n' "/api/health" "$body" | head -c 400
  echo
  # The three checks that mean "this deployment can take money".
  python3 - "$body" <<'PY'
import json, sys
try:
    cfg = json.loads(sys.argv[1])["config"]
except Exception:
    print("  health payload is not JSON - Passenger probably raised on import")
    sys.exit(1)
for label, key, want in (
    ("public host (production mode)", "publicHost", True),
    ("JWT_SECRET is stable", "ephemeralJwtSecret", False),
    ("billing configured", "billingConfigured", True),
    ("mock checkout disabled", "mockBilling", False),
):
    got = cfg.get(key)
    print("  %-32s %s%s" % (label, got, "" if got == want else "   <-- expected %s" % want))
print("  %-32s %s" % ("payfast sandbox", cfg.get("payfastSandbox")))
print("  %-32s %s" % ("database", cfg.get("dbPath")))
PY
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$base/api-base.js" || true)
  printf '  %-32s %s\n' "/api-base.js" "$code"
}

if [ "$APPLY" -eq 0 ]; then
  if [ "$VERIFY" -eq 1 ]; then
    verify_api || exit 1
  else
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
PROBE_OPTS=(--ssl-reqd --silent --show-error --connect-timeout 20 --max-time 60)
if [ "$FTP_INSECURE" = "1" ]; then
  CURL_OPTS+=(-k); PROBE_OPTS+=(-k)
  echo "warning: TLS verification disabled (FTP_INSECURE=1)" >&2
fi

echo "checking login and remote directory..."
if ! curl "${PROBE_OPTS[@]}" --user "$FTP_USER:$FTP_PASS" --list-only "$REMOTE_URL" >/dev/null 2>&1; then
  echo "FTP preflight failed for $REMOTE_URL" >&2
  echo "Either the login is wrong, or this account cannot see $REMOTE_PATH." >&2
  echo "  - the account may be jailed to public_html; the app root sits beside it" >&2
  echo "  - the app root may not exist yet: create the application first" >&2
  echo "  - the primary cPanel login can always write the whole home directory" >&2
  echo "No second FTP account? ./deploy/upload-passenger.sh --zip, then File" >&2
  echo "Manager -> Upload -> Extract (DEPLOY.md 13.2)." >&2
  exit 1
fi
echo "login ok"

failed=0
for f in "${BACKEND_FILES[@]}"; do
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

echo "all ${#BACKEND_FILES[@]} files uploaded"
echo
echo "Next, in cPanel:"
echo "  1. Setup Python App -> your application -> Restart (Passenger caches the code)."
echo "  2. Make sure $REMOTE_PATH/.env exists on the server - see DEPLOY.md 13."

if [ "$VERIFY" -eq 1 ]; then
  verify_api
fi
