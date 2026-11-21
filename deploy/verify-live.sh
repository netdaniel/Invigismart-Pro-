#!/usr/bin/env bash
#
# Is the site actually working? One command, no browser, no credentials.
#
# Everything else checks one half. `upload-static.sh --verify` proves the pages
# arrived; `upload-passenger.sh --verify` proves /api/health answers. Neither
# answers the question that matters - "can a school sign up, sign in and pay
# today?" - because the answer depends on the pages, the Passenger app, the .env
# file and the PayFast credentials all at once, and each failure looks like "the
# site is broken" from a browser.
#
# This asks all of them in one pass and prints one verdict per check:
#
#   ok        the check passed
#   FAIL      a real problem; the exit code is 1
#   warning   works, but costs money or trust (sandbox billing, for example)
#   note      information, not a verdict
#   skipped   does not apply to a development target
#
# Usage:
#   ./deploy/verify-live.sh                                # https://invigismart.online
#   ./deploy/verify-live.sh --base http://127.0.0.1:8100   # the local dev server
#   ./deploy/verify-live.sh --strict                       # a dev target must pass
#                                                          # the production rules too
#   ./deploy/verify-live.sh --delay 1                       # a second between requests
#                                                          # (if the host answers with
#                                                          # its bot-challenge page)
#
# Exit codes: 0 every check passed, 1 something failed, 2 nothing could be graded -
# the site did not answer at all (DNS, TLS or egress), or the host answered with
# its own "One moment, please..." holding page, which returns 200 for every URL
# and would otherwise make the whole report look green.
#
# What it does NOT do: sign up, log in or pay. Creating an account on the live
# database is not a health check, and PayFast's own sandbox purchase (DEPLOY.md
# section 8) is the only thing that proves the money path.

set -euo pipefail

cd "$(dirname "$0")/.."

BASE="${SITE_URL:-https://invigismart.online}"
STRICT=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --base)    BASE="${2:?--base needs a URL}"; shift 2 ;;
    --base=*)  BASE="${1#--base=}"; shift ;;
    --strict)  STRICT=1; shift ;;
    --delay)   DELAY="${2:?--delay needs a number of seconds}"; shift 2 ;;
    --delay=*) DELAY="${1#--delay=}"; shift ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

BASE="${BASE%/}"

# A target that is not https cannot be a production host: server/config.py ties
# every production rule to APP_BASE_URL starting with https. So the profile is
# decided by which URL was asked for, not by what the site claims about itself -
# a live host that *reports* publicHost=false is a failure, not a dev server.
DEV_MODE=0
case "$BASE" in
  https://*) ;;
  *) DEV_MODE=1 ;;
esac

CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-10}"
MAX_TIME="${MAX_TIME:-25}"
# Seconds to wait between requests. 0 by default: the checks are ~35 requests in
# half a minute, which is nothing for a normal host. Set DELAY=1 (or --delay 1)
# if the host starts answering with its bot-challenge page under that burst.
DELAY="${DELAY:-0}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass_n=0
fail_n=0
warn_n=0

row()  { printf '  %-42s %-24s %s\n' "$1" "$2" "$3"; }
pass() { pass_n=$((pass_n + 1)); row "$1" "${2:-}" "ok"; }
fail() { fail_n=$((fail_n + 1)); row "$1" "${2:-}" "FAIL - ${3:-see DEPLOY.md}"; }
warn() { warn_n=$((warn_n + 1)); row "$1" "${2:-}" "warning - ${3:-check why}"; }
note() { row "$1" "${2:-}" "${3:-}"; }
skip() { row "$1" "${2:-}" "skipped - ${3:-does not apply}"; }

# fetch <path> [method] [json-body]
#
# Prints the HTTP status and leaves the body in $TMP/body. 000 means curl gave
# up (DNS, TLS, timeout); that is reported per check rather than aborting the
# run, so one unreachable path does not hide the other twenty results.
fetch() {
  local path=$1 method=${2:-GET} data=${3:-} code
  local args=(--silent --show-error --location
              --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME"
              --request "$method" --output "$TMP/body" --write-out '%{http_code}')
  if [ -n "$data" ]; then
    args+=(--header 'Content-Type: application/json' --data "$data")
  fi
  if [ "$DELAY" != "0" ]; then
    sleep "$DELAY"
  fi
  # curl still prints its code when the connection fails, so the fallback is
  # only for the case where it printed nothing at all - concatenating both is
  # how "000" became "000000" and every comparison silently stopped matching.
  code=$(curl "${args[@]}" "$BASE$path" 2>"$TMP/curl.err") || true
  printf '%s' "${code:-000}"
}

# expect <label> <path> <wanted status>...
expect() {
  local label=$1 path=$2 want code
  shift 2
  code=$(fetch "$path")
  for want in "$@"; do
    if [ "$code" = "$want" ]; then
      pass "$label" "$path $code"
      return
    fi
  done
  fail "$label" "$path $code" "wanted $*"
}

# expect_body <label> <path> <literal substring>
expect_body() {
  local label=$1 path=$2 needle=$3 code
  code=$(fetch "$path")
  if [ "$code" != "200" ]; then
    fail "$label" "$path $code" "wanted 200"
  elif grep -qiF -- "$needle" "$TMP/body"; then
    pass "$label" "$path"
  else
    fail "$label" "$path" "no '$needle' in the body (wrong file served?)"
  fi
}

# expect_request <label> <path> <method> <json body|-> <wanted status>...
expect_request() {
  local label=$1 path=$2 method=$3 data=$4 want code
  shift 4
  if [ "$data" = "-" ]; then
    data=""
  fi
  code=$(fetch "$path" "$method" "$data")
  for want in "$@"; do
    if [ "$code" = "$want" ]; then
      pass "$label" "$method $path $code"
      return
    fi
  done
  fail "$label" "$method $path $code" "wanted $*"
}

# health_value <key> -> the raw value of /api/health's config.<key>
#
# Read from the JSON rather than guessed, because two of the guard rails below
# have a different correct answer under mock billing: the fake checkout page is
# meant to exist there, and unsigned webhook events are its input.
health_value() {
  python3 - "$TMP/health.json" "$1" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        config = json.load(handle).get("config") or {}
except (OSError, ValueError):
    config = {}
print(config.get(sys.argv[2], ""))
PY
}

# must_not_expose <label> <path>
#
# 200 is the only real failure: it means the file is downloadable. Apache
# answers 403 for a dotfile, the app answers 404 for /server/* and *.py, and
# either is fine - this check cares about the difference between "denied" and
# "here is your database".
must_not_expose() {
  local label=$1 path=$2 code
  code=$(fetch "$path")
  case "$code" in
    200) fail "$label" "$path 200" "THE FILE IS PUBLIC - close this first" ;;
    401|403|404|410) pass "$label" "$path $code" ;;
    000) warn "$label" "$path 000" "no response (fine if the server drops it)" ;;
    *)   warn "$label" "$path $code" "not a flat denial - check what is served" ;;
  esac
}

# --- what is being checked -------------------------------------------------
echo "target   : $BASE"
if [ "$DEV_MODE" -eq 1 ]; then
  echo "profile  : development (not https, so production rules are reported as skipped)"
else
  echo "profile  : production (https, so every rule is enforced)"
fi

root_code=$(fetch /)
if [ "$root_code" = "000" ]; then
  cat "$TMP/curl.err" >&2
  echo >&2
  echo "cannot reach $BASE at all - that is DNS, TLS or egress, not the app." >&2
  echo "  ask a resolver what it returns (a stale answer stays cached for the TTL):" >&2
  echo "    curl -s -H 'accept: application/dns-json' 'https://cloudflare-dns.com/dns-query?name=invigismart.online&type=A'" >&2
  echo "  then talk to the host directly, bypassing the resolver:" >&2
  echo "    curl -sk --resolve invigismart.online:443:156.155.252.98 -o /dev/null -w '%{http_code}' https://invigismart.online/" >&2
  exit 2
fi
echo "site root: HTTP $root_code"

# --- is this the site, or the host's holding page? -------------------------
# Hosts put an openresty page in front of the account for two different reasons,
# and both look identical: "One moment, please...", a spinner, and
# window.location.reload() every five seconds.
#
#   1. The account itself is being held - a migration, a cache purge, a resource
#      review. Every visitor sees it.
#   2. This client has been challenged as a bot, because it just made ~35 rapid
#      requests. Only this client sees it; the site is fine for everyone else.
#
# Either way the page answers 200 for every URL - /api/* included - so every
# status check below would pass while the site is unusable, and the report would
# be eleven green lines about a spinner. It happened on 2026-11-07: pages "ok",
# /api/health not JSON, guard rails all 200, while r.jina.ai (a different client)
# fetched the real landing page and the sandbox IP was being throttled.
# So: detect it before grading anything, and ask twice before believing it.
is_holding_page() {
  [ "$1" = "200" ] \
    && { grep -qF 'One moment, please' "$TMP/body" 2>/dev/null \
         || grep -qF 'window.location.reload' "$TMP/body" 2>/dev/null; }
}

if is_holding_page "$root_code"; then
  echo "  (the host's 'One moment, please...' page - waiting 20s and asking again)"
  sleep 20
  root_code=$(fetch /)
  if is_holding_page "$root_code"; then
    cat >&2 <<EOF

  The host is answering with its "One moment, please..." page, not this site.

    GET $BASE/  ->  200, but the body is a spinner that reloads every 5 seconds,
    it is returned for every URL (/api/* included), and asking again 20 seconds
    later got the same page.

  That page has two causes and they look identical from here:

    a) the account is being held by the host - a migration, a cache purge or a
       resource review - so every visitor sees it; or
    b) this client was challenged as a bot because this script just made ~35
       requests in half a minute. The site is fine for everyone else.

  For (b) the fix is your side: wait a few minutes, then re-run with a pause
  between requests - ./deploy/verify-live.sh --delay 2 - and it will not look
  like a burst. For (a) there is nothing to do but wait, or open a ticket with
  the host and quote this page rather than the application.

  To tell them apart, ask a different client what it sees:

    curl -s $BASE/ | head -c 200 ; echo                       # spinner HTML?
    curl -s https://r.jina.ai/$BASE/ | head -c 200 ; echo      # real site = (b)

  Every check is skipped on purpose: against a placeholder that answers 200 to
  everything, a green report would mean nothing.
EOF
    exit 2
  fi
  echo "  the second request got the real site - once past the challenge, continue"
  DELAY="${DELAY:-0}"
fi

echo

# --- the pages ------------------------------------------------------------
# Each page is matched on a string that only that page contains, not just on its
# status: on 2026-11-07 the host answered 200 with a holding page for *every*
# URL, and status-only checks reported a site that nobody could use as healthy.
# The marker is the <title>, which is also what a search engine and a shared
# link show, so a page that fails here is wrong in a way that matters.
echo "pages"
expect_body "marketing page at /"       /       "Rostering for Schools"
expect_body "dashboard at /app"         /app    "Exam Invigilation Roster</title>"
expect_body "terms"                     /terms  "Terms &amp; Conditions"
expect_body "privacy"                   /privacy "Privacy Policy &mdash;"
expect_body "login (landing + modal)"   /login  "Rostering for Schools"
expect_body "signup (landing + modal)"  /signup "Rostering for Schools"
expect      "assets/timetable-import.js" /assets/timetable-import.js 200
expect      "assets/exam-types.js"      /assets/exam-types.js 200
expect      "assets/logo.svg"           /assets/logo.svg 200
# Self-hosted on purpose: the pages keep the session token in localStorage, so a
# script from another origin would run with access to it. A 404 here means the
# .htaccess rewrite, the upload list or the vendored file went missing.
expect      "tailwind (self-hosted)"    /assets/tailwind-3.4.16.js 200
expect      "lucide (self-hosted)"      /assets/lucide-0.451.0.min.js 200
expect      "favicon.ico"               /favicon.ico 200
expect      "apple-touch-icon"          /assets/apple-touch-icon.png 200
expect_body "robots.txt"                /robots.txt "Sitemap:"
expect_body "sitemap.xml"               /sitemap.xml "<urlset"

# --- browser headers -------------------------------------------------------
# Apache serves the pages from disk, so these come from .htaccess or not at all.
# Missing on the live site is a finding; locally there is no Apache, so a
# development run only notes it.
header_of() {
  curl -sS --silent -o /dev/null -D - --connect-timeout "$CONNECT_TIMEOUT" \
       --max-time "$MAX_TIME" "$BASE$1" 2>/dev/null \
    | tr -d '\r' \
    | grep -i "^$2:" \
    | head -n 1 \
    | cut -d: -f2- \
    | sed 's/^ *//'
}

expect_header() {
  local label=$1 path=$2 name=$3 needle=$4 value
  value=$(header_of "$path" "$name")
  case "$value" in
    "") if [ "$DEV_MODE" -eq 1 ]; then
          note "$label" "(not sent)" "Apache sets it from .htaccess; nothing to test here"
        else
          fail "$label" "missing $name" "the .htaccess is not on the host, or mod_headers is off"
        fi
        return ;;
  esac
  case "$value" in
    *"$needle"*) pass "$label" "$value" ;;
    *)           warn "$label" "$value" "expected it to contain $needle" ;;
  esac
}

echo
echo "browser headers (.htaccess)"
expect_header "HSTS"             /  Strict-Transport-Security "max-age=31536000"
expect_header "CSP"              /  Content-Security-Policy   "default-src 'self'"
expect_header "frame guard"      /  X-Frame-Options           "SAMEORIGIN"
expect_header "no type sniffing" /  X-Content-Type-Options    "nosniff"
expect_header "referrer policy"  /  Referrer-Policy           "strict-origin-when-cross-origin"
expect_header "permissions"      /  Permissions-Policy        "camera=()"
expect_header "asset caching"    /assets/tailwind-3.4.16.js Cache-Control "immutable"

# --- what must never be downloadable ---------------------------------------
# A 200 here is worse than every other failure on this page combined: it means
# the database, the source or the PayFast key is on the open web.
echo
echo "nothing private is reachable"
must_not_expose "source (/server/app.py)"        /server/app.py
must_not_expose ".env in the docroot"            /.env
must_not_expose "the database (/data/*.db)"      /data/invigismart.db
must_not_expose "deploy/invigismart-api.env"     /deploy/invigismart-api.env

# The app is mounted at /api (public_html/api/.htaccess -> PassengerAppRoot), and
# Passenger strips that prefix, so /api/data/invigismart.db reaches the app's own
# static handler as /data/invigismart.db. That handler is why server/app.py
# refuses *.env, deploy/ and data/: without it this pair answers 200 with the
# database and the JWT secret. These two are the only checks here that test the
# Python code rather than Apache's docroot, so a 200 is the app leaking.
must_not_expose "database via the /api mount"    /api/data/invigismart.db
must_not_expose ".env via the /api mount"        /api/deploy/invigismart-api.env

# --- where the pages think the API is --------------------------------------
echo
echo "front end -> API wiring"
api_code=$(fetch /api-base.js)
# Parsed rather than grepped: the file's own comment block contains a sample
# assignment (the api.invigismart.online example), so the last match - the real
# one at the bottom - is the only reliable answer.
api_target=$(python3 - "$TMP/body" <<'PY'
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
        text = handle.read()
except OSError:
    text = ""
found = re.findall(r'''INVIGISMART_API\s*=\s*["']([^"']*)["']''', text)
print(found[-1].strip() if found else "")
PY
)
if [ "$api_code" != "200" ]; then
  fail "api-base.js" "$api_code" "the pages cannot read it, so they fall back to same-origin"
elif [ -z "$api_target" ]; then
  note "api-base.js" "blank" "pages call $BASE/api/... - correct on this host"
elif [ "$api_target" = "$BASE" ]; then
  note "api-base.js" "$api_target" "same origin, spelled out"
else
  # A cross-origin deployment works only if that origin answers: this is how a
  # NXDOMAIN (api.invigismart.online before its A record exists) is caught,
  # which no other check here would see - every page would still load.
  cross_code=$(curl --silent --show-error --location --connect-timeout "$CONNECT_TIMEOUT" \
                    --max-time "$MAX_TIME" --output /dev/null --write-out '%{http_code}' \
                    "$api_target/api/health" 2>/dev/null || printf '000')
  if [ "$cross_code" = "200" ]; then
    note "api-base.js" "$api_target" "cross-origin, reachable"
  else
    fail "api-base.js" "$api_target" "the pages call this host and it answered $cross_code"
  fi
fi

# --- the backend -----------------------------------------------------------
# server/config.py's describe() is the summary the app makes about itself, and
# every rule below is one that only breaks in production: a dev server
# legitimately has publicHost=false and a mock gateway, so on a development
# target those are reported as skipped, not as failures.
echo
echo "backend (the Passenger app)"
health_code=$(fetch /api/health)
cp "$TMP/body" "$TMP/health.json" 2>/dev/null || true
if [ "$health_code" != "200" ]; then
  fail "/api/health" "$health_code" "the Python app is not answering - DEPLOY.md 13.3"
  note "  what to check" "" "passenger_wsgi.py is 3068 bytes, .env is in the app root, then restart"
else
  while IFS='|' read -r kind name value why; do
    case "$kind" in
      PASS) pass "$name" "$value" ;;
      FAIL) fail "$name" "$value" "$why" ;;
      WARN) warn "$name" "$value" "$why" ;;
      NOTE) note "$name" "$value" "$why" ;;
      DEV)  if [ "$STRICT" -eq 1 ]; then fail "$name" "$value" "$why"; else skip "$name" "$value" "$why"; fi ;;
      *)    note "$name" "$value" "$why" ;;
    esac
  done < <(python3 - "$TMP/health.json" "$DEV_MODE" <<'PY'
import json
import sys

PRODUCTION = sys.argv[2] != "1"


def emit(kind, name, value="", why=""):
    print("%s|%s|%s|%s" % (kind, str(name).replace("|", "/"),
                           str(value).replace("|", "/"),
                           str(why).replace("|", "/")))


try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, ValueError) as exc:
    emit("FAIL", "health body", "not JSON", exc)
    raise SystemExit(0)

if payload.get("ok") is True:
    emit("PASS", "health ok", "true")
else:
    emit("FAIL", "health ok", payload.get("ok"), "the app answered without ok")

config = payload.get("config") or {}


def required(name, held, why, expected):
    """A rule that must hold on a public host."""
    if held:
        emit("PASS", name, "true")
    elif PRODUCTION:
        emit("FAIL", name, "false", why)
    else:
        emit("DEV", name, "false", "live site expects " + expected)


required("config.publicHost", bool(config.get("publicHost")),
         "APP_BASE_URL is not https, so config.py has switched every production "
         "check off - the .env in the app root is missing or wrong",
         "publicHost=true")
required("config.jwtSecret real", not config.get("ephemeralJwtSecret"),
         "JWT_SECRET is unset: every restart signs every school out",
         "ephemeralJwtSecret=false")
required("config.billingConfigured", bool(config.get("billingConfigured")),
         "no usable gateway credentials: a checkout cannot be built at all",
         "billingConfigured=true")
required("config.mockBilling off", not config.get("mockBilling"),
         "free licences are on offer to anyone with a session",
         "mockBilling=false")

if config.get("payfastSandbox"):
    emit("WARN", "config.payfastSandbox", "true",
         "checkouts go to PayFast's sandbox - no real money until PAYFAST_SANDBOX=0")
else:
    emit("PASS", "config.payfastSandbox", "false")

db_path = str(config.get("dbPath") or "")
if db_path.endswith("invigismart-api/data/invigismart.db"):
    emit("PASS", "config.dbPath", db_path)
elif not db_path:
    # /api/health is public and no longer names the database path, so a missing
    # value is the expected answer here. The operator reads it with the owner
    # token: curl -H "X-Admin-Token: ..." $BASE/api/health
    emit("NOTE", "config.dbPath", "(hidden)",
         "not reported anonymously; send X-Admin-Token to see it")
elif PRODUCTION:
    emit("WARN", "config.dbPath", db_path,
         "not <app root>/data/invigismart.db - confirm it is outside the docroot")
else:
    emit("NOTE", "config.dbPath", db_path, "development path")

emit("NOTE", "config.billingProvider", config.get("billingProvider") or "?")
emit("NOTE", "config.adminOverview", "on" if config.get("adminOverview") else "off",
     "" if config.get("adminOverview") else "set ADMIN_TOKEN to switch the owner report on")
emit("NOTE", "config.emailConfigured", "on" if config.get("emailConfigured") else "off",
     "" if config.get("emailConfigured") else "set SMTP_HOST so password-reset links can be emailed")

for message in config.get("billingWarnings") or []:
    emit("WARN", "config.billingWarnings", "", message)
for message in config.get("emailWarnings") or []:
    emit("WARN", "config.emailWarnings", "", message)
PY
)
fi

# --- the rails -------------------------------------------------------------
# DEPLOY.md section 7: what an anonymous visitor must not be able to do. These
# hold on a development server too, so they are never skipped - a host that
# answers 200 to any of them hands out a licence or a record.
echo
echo "guard rails (DEPLOY.md 7)"
if [ "$health_code" != "200" ]; then
  for label in "mock checkout page" "anonymous billing summary" \
               "anonymous mock activation" "forged webhook" \
               "anonymous cancellation" "anonymous roster generation" \
               "anonymous document upload" "owner report without a token" \
               "forged reset token"; do
    skip "$label" "" "the backend is not answering"
  done
else
  mock_mode=0
  case "$(health_value billingProvider)$(health_value mockBilling)" in
    mock*|*True) mock_mode=1 ;;
  esac

  if [ "$mock_mode" -eq 1 ]; then
    skip "mock checkout page" 200 "mock billing is on: the page is meant to exist"
    skip "forged webhook" 200 "mock billing: an unsigned event is its input"
  else
    expect         "mock checkout page"           /mock-checkout 404
    expect_request "forged webhook"               /api/billing/webhook POST '{"pf_payment_id":"x"}' 400 403
  fi
  expect         "anonymous billing summary"    /api/billing/summary 401
  expect         "owner report without a token" /api/admin/overview 401 403 404
  expect_request "anonymous mock activation"    /api/billing/mock-activate POST '{}' 401
  expect_request "anonymous cancellation"       /api/billing/cancel POST '{}' 401
  expect_request "anonymous roster generation"  /api/roster/generate POST '{}' 401
  expect_request "anonymous document upload"    /api/documents POST '{}' 401
  expect_request "forged reset token"           /api/auth/reset-password POST '{"token":"not-a-token","password":"newsecret123"}' 400
fi

# --- verdict ---------------------------------------------------------------
echo
echo "----------------------------------------------------------------------"
if [ "$fail_n" -ne 0 ]; then
  echo "$fail_n of $((pass_n + fail_n + warn_n)) checks FAILED ($pass_n ok, $warn_n warning)."
  echo "Each FAIL above names the DEPLOY.md section that fixes it. Re-run this after."
  exit 1
fi
echo "$pass_n of $((pass_n + warn_n)) checks passed, $warn_n warning(s)."
if [ "$warn_n" -ne 0 ]; then
  echo "A warning is working behaviour that costs money or trust, not a crash."
fi
if [ "$DEV_MODE" -eq 1 ]; then
  echo
  echo "This was a development target, so the production-only rules were skipped."
  echo "The deploy is not proven until this passes against the real site:"
  echo "  ./deploy/verify-live.sh"
fi
echo
echo "Money is the one thing this cannot check: DEPLOY.md section 8 - a sandbox"
echo "purchase that ends with an Active badge - is the only proof the licence"
echo "flow works end to end."
exit 0
