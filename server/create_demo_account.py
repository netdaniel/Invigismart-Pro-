#!/usr/bin/env python3
"""Create or refresh a fully entitled login for sales and presentations.

Entitlement is server-owned: a login unlocks every feature only while its
`subscriptions` row is active and its period has not ended. This tool writes
that row directly (provider `demo`), so a presenter can sign in on stage
without a payment gateway, and can still demonstrate the paid-only clean
roster export - something a trial account is deliberately denied.

The password is PBKDF2-hashed exactly like a normal signup. Re-running the tool
is safe: it keeps the account, extends the period and leaves a single demo
invoice in the Billing tab. A public https deployment is refused unless
--allow-production is passed, so a shared demo password cannot end up on the
live server by accident.
"""
import argparse
import datetime
import os
import re
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import billing
import security
from config import Config
from db import Store

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEMO_PLAN = "annual_school_unlimited"
DEMO_PROVIDER = "demo"


def positive_days(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a whole number of days")
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1 day")
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Create or refresh a fully entitled demo login for sales and "
            "presentations."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 server/create_demo_account.py \\\n"
            "      --email sales@example.com --password 'choose-a-strong-one'\n"
            "  python3 server/create_demo_account.py \\\n"
            "      --email sales@example.com --password 'new-secret' --update-password\n"
            "  python3 server/create_demo_account.py \\\n"
            "      --email sales@example.com --days 3650 --no-invoice\n"
        ),
    )
    parser.add_argument("--email", required=True, help="login email address")
    parser.add_argument(
        "--password",
        default=None,
        help="login password (min 8 chars); omit to generate a random one",
    )
    parser.add_argument(
        "--school",
        default="Invigismart Pro (Sales Demo)",
        help="school name shown in the dashboard (max 120 chars)",
    )
    parser.add_argument(
        "--days",
        type=positive_days,
        default=3650,
        help="licence length in days (default: 3650, about 10 years)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite file to write (default: DB_PATH, else server/invigismart.db)",
    )
    parser.add_argument(
        "--update-password",
        action="store_true",
        help="replace the password of an existing account",
    )
    parser.add_argument(
        "--no-invoice",
        action="store_true",
        help="do not record the demo invoice shown in the Billing tab",
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="permit the login on an https deployment (APP_BASE_URL=https://...)",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    email = (args.email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        print("A valid email address is required.", file=sys.stderr)
        return 2

    school = (args.school or "").strip()
    if not school or len(school) > 120:
        print("School name is required (max 120 characters).", file=sys.stderr)
        return 2

    if args.password is not None and len(args.password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 2

    cfg = Config()
    if cfg.public_host and not args.allow_production:
        print(
            "Refusing to create a demo login on a public https host.\n"
            "  A shared, long-lived demo password is a backdoor on the live server.\n"
            "  Re-run with --allow-production only if you accept that risk.",
            file=sys.stderr,
        )
        return 2

    db_path = os.path.abspath(args.db) if args.db else cfg.db_path
    if not os.path.exists(db_path):
        print(
            "Note: %s does not exist yet; it will be created." % db_path,
            file=sys.stderr,
        )
    store = Store(db_path)
    try:
        existing = store.get_user_by_email(email)
        password_changed = False
        generated = False

        if existing and args.password and not args.update_password:
            print(
                "Note: the account already exists, so --password was ignored.\n"
                "      Add --update-password to change it.",
                file=sys.stderr,
            )

        password = args.password
        if existing:
            if args.update_password:
                if password is None:
                    password = secrets.token_urlsafe(12)
                    generated = True
                store.set_password(existing["id"], security.hash_password(password))
                password_changed = True
            user = existing
        else:
            if password is None:
                password = secrets.token_urlsafe(12)
                generated = True
            period_end = billing.to_iso(
                billing.utcnow() + datetime.timedelta(days=args.days)
            )
            user = store.create_user(
                school, email, security.hash_password(password), period_end
            )

        period_end = billing.to_iso(
            billing.utcnow() + datetime.timedelta(days=args.days)
        )
        store.upsert_subscription(
            user["id"],
            plan=DEMO_PLAN,
            status="active",
            current_period_end=period_end,
            provider=DEMO_PROVIDER,
        )

        if not args.no_invoice:
            store.record_payment(
                user["id"],
                provider=DEMO_PROVIDER,
                # Deterministic id: re-running the tool does not stack invoices.
                provider_payment_id="demo-%s" % user["id"],
                amount=cfg.plan_price_amount,
                currency="ZAR",
                status="COMPLETE",
                item_name=cfg.plan_name,
                email=email,
                period_end=period_end,
            )

        fresh = store.get_user_by_id(user["id"])
        ent = billing.entitlement(store, fresh, cfg)
    finally:
        store.close()

    if not ent["entitled"] or ent["source"] != "subscription":
        print(
            "ERROR: the account was written but the entitlement is %r." % ent,
            file=sys.stderr,
        )
        return 1

    if existing:
        print("Demo account refreshed.")
    else:
        print("Demo account created.")
    print("  school    : %s" % fresh["school"])
    print("  email     : %s" % fresh["email"])
    if password_changed or not existing:
        marker = " (generated - save it now)" if generated else ""
        print("  password  : %s%s" % (password, marker))
    else:
        print("  password  : unchanged (use --update-password to rotate it)")
    print(
        "  licence   : %s via %s, %d days left (ends %s)"
        % (ent["plan"], ent["source"], ent["daysLeft"], ent["currentPeriodEnd"])
    )
    print("  database  : %s" % db_path)
    print("  sign in   : %s/login" % cfg.app_base_url.rstrip("/"))
    print("\nKeep this credential private and rotate it after public events.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

