#!/usr/bin/env python3
"""Build the production ``.env`` for the cPanel/Passenger app root.

The live backend needs a handful of settings that are *not* the ones in the
developer's ``.env``:

* ``APP_BASE_URL`` must be the public https origin, because that is what turns
  on the production checks and what PayFast records as the ``notify_url``;
* ``JWT_SECRET`` must be new, so a token minted on a laptop is not accepted on
  the live site;
* ``DB_PATH`` stays relative, so ``server/config.py`` resolves it beside the
  application root instead of inside whichever directory Passenger chose.

Writing that file by hand in File Manager is where deployments go wrong (a
pasted quote, a trailing space in the merchant key, a forgotten sandbox flag),
so this script derives it from the local ``.env`` and prints a masked summary.

Usage::

    python3 deploy/make-api-env.py                  # -> deploy/invigismart-api.env
    python3 deploy/make-api-env.py --out /tmp/x.env
    python3 deploy/make-api-env.py --force          # replace an existing file

The output is gitignored (``deploy/*.env``) because it holds the merchant key
and the JWT secret. Upload it with File Manager and **Rename** it to ``.env``
in the application root, then delete the local copy.

Refusing to overwrite matters: a new ``JWT_SECRET`` signs every school out, so
``--force`` is a deliberate act, not the default.
"""
import argparse
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
DEFAULT_SOURCE = os.path.join(REPO_ROOT, ".env")
DEFAULT_OUT = os.path.join(HERE, "invigismart-api.env")

API_ORIGIN = "https://api.invigismart.online"
SECRET_MARKERS = ("SECRET", "PASS", "KEY")


def read_env(path):
    """Parse a dotenv file into a dict; later keys win, as in the shell."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise SystemExit(
            "Cannot read %s (%s).\n"
            "Create it from .env.passenger.example and fill in the PayFast "
            "values first; this script copies them across." % (path, exc.strerror)
        )
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values



def build(source):
    """The text of the production .env, given the developer's settings."""

    def get(name, default=""):
        return source.get(name) or default

    # A PayFast account without a passphrase is legal, but only with the
    # explicit opt-in - otherwise server/config.py records a fatal billing
    # error and every checkout is refused. Carry the developer's choice over.
    lines = [
        "# " + "-" * 75,
        "# Invigismart Pro - production configuration for " + API_ORIGIN,
        "#",
        "# Built by deploy/make-api-env.py from the local .env. HOST/PORT are",
        "# omitted on purpose: Passenger never listens on a port and ignores them.",
        "#",
        "# CONTAINS LIVE SECRETS - gitignored, never commit it. Upload it with File",
        "# Manager, Rename it to \".env\" in the application root (Settings -> Show",
        "# Hidden Files to see it afterwards), then delete the local copy.",
        "# " + "-" * 75,
        "",
        "# Relative paths resolve against the application root, never the working",
        "# directory, so these land in <app root>/data/ whatever Passenger does.",
        "DB_PATH=data/invigismart.db",
        "DOCUMENTS_DIR=data/documents",
        "MAX_DOCUMENT_BYTES=10485760",
        "MAX_DOCUMENTS_PER_USER=50",
        "MAX_DOCUMENT_STORAGE_BYTES=104857600",
        "",
        "# --- sessions " + "-" * 63,
        "# Fresh, so a token minted against the dev server is not valid here.",
        "# Changing it later signs every school out; keep it.",
        "JWT_SECRET=" + secrets.token_hex(32),
        "ACCESS_TOKEN_TTL=" + get("ACCESS_TOKEN_TTL", "3600"),
        "TRIAL_DAYS=" + get("TRIAL_DAYS", "14"),
        "",
        "# --- public URL " + "-" * 61,
        "# Must be https: this is what enables the production checks, builds the",
        "# PayFast return/cancel URLs and is recorded as the notify_url.",
        "APP_BASE_URL=" + API_ORIGIN,
        "",
        "# --- billing " + "-" * 64,
        "BILLING_PROVIDER=" + get("BILLING_PROVIDER", "payfast"),
        "PAYFAST_MERCHANT_ID=" + get("PAYFAST_MERCHANT_ID"),
        "PAYFAST_MERCHANT_KEY=" + get("PAYFAST_MERCHANT_KEY"),
        "# Blank when the account has no passphrase - see the next line.",
        "PAYFAST_PASSPHRASE=" + get("PAYFAST_PASSPHRASE"),
        "PAYFAST_ALLOW_NO_PASSPHRASE=" + get("PAYFAST_ALLOW_NO_PASSPHRASE", "1"),
        "# 0 = take real money. Keep 1 until one sandbox purchase has worked.",
        "PAYFAST_SANDBOX=" + get("PAYFAST_SANDBOX", "1"),
        "PAYFAST_FREQUENCY=" + get("PAYFAST_FREQUENCY", "6"),
        "PAYFAST_CYCLES=" + get("PAYFAST_CYCLES", "0"),
        "PAYFAST_ITN_VERIFICATION=" + get("PAYFAST_ITN_VERIFICATION", "api"),
        "PAYFAST_IP_ALLOWLIST=",
        "",
        "# 1 would publish /mock-checkout, where any visitor can mint a free year.",
        "# It is force-disabled while APP_BASE_URL is https; keep the intent clear.",
        "ALLOW_MOCK_BILLING=0",
        "",
        "# --- plan copy and support " + "-" * 52,
        'PLAN_NAME="%s"' % get("PLAN_NAME", "Annual School Unlimited"),
        'PLAN_PRICE_DISPLAY="%s"' % get("PLAN_PRICE_DISPLAY", "R1200 / year"),
        "PLAN_PRICE_AMOUNT=" + get("PLAN_PRICE_AMOUNT", "1200.00"),
        "SUBSCRIPTION_DAYS=" + get("SUBSCRIPTION_DAYS", "365"),
        "SUPPORT_EMAIL=" + get("SUPPORT_EMAIL", "daniel@invigismart.online"),
        'SUPPORT_PHONE="%s"' % get("SUPPORT_PHONE", "+27 76 319 3749"),
    ]
    return "\n".join(lines) + "\n"


def summarise(text):
    """One line per setting, with every secret replaced by its length."""
    for line in text.splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        shown = value
        if any(marker in key for marker in SECRET_MARKERS):
            shown = "(set, %d chars)" % len(value.strip('"')) if value.strip('"') else "(blank)"
        print("  %-28s %s" % (key, shown))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help="dotenv to copy the PayFast/plan values from (default: .env)")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help="file to write (default: deploy/invigismart-api.env)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing file (the JWT secret changes, "
                             "which signs every school out)")
    args = parser.parse_args(argv)

    if os.path.exists(args.out) and not args.force:
        raise SystemExit(
            "%s already exists.\n"
            "It holds the JWT_SECRET the live site signs sessions with; "
            "regenerating it signs every school out. Re-run with --force if that "
            "is really what you want, or --out to write somewhere else."
            % args.out
        )

    text = build(read_env(args.source))

    directory = os.path.dirname(os.path.abspath(args.out))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    # 0600: the file holds the merchant key and the session secret, and it sits
    # in a home directory on a shared host until it is uploaded.
    descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)

    print("wrote %s" % args.out)
    summarise(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
