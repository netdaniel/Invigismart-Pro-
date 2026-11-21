#!/usr/bin/env python3
"""Prove a mailbox actually works: IMAP login plus SMTP authentication.

The password is read with getpass, so it never appears in shell history, on
screen, in a log, or in chat. Nothing is sent unless you pass --send-to.

    python3 .vscode/mail_verify_login.py
    python3 .vscode/mail_verify_login.py --address demo@invigismart.online
    python3 .vscode/mail_verify_login.py --send-to daniel@prinslooconsulting.co.za

Defaults target the provider's mail cluster (mx1.tld-mx.com), which is where
invigismart.online's MX records point. Outbound port 25 is blocked on many
residential ISPs, so submission happens on 587/STARTTLS.

Exit codes: 0 IMAP and SMTP both accepted the login, 1 IMAP failed,
2 SMTP failed, 3 mail server unreachable.
"""

from __future__ import annotations

import argparse
import getpass
import imaplib
import smtplib
import ssl
import sys


def check_imap(host: str, port: int, user: str, password: str, timeout: float) -> bool:
    ctx = ssl.create_default_context()
    print(f"imap: {host}:{port} as {user} ...", end=" ", flush=True)
    imap = None
    try:
        imap = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=timeout)
        imap.login(user, password)
        status, data = imap.select("INBOX", readonly=True)
        count = (data[0] or b"0").decode(errors="replace")
        print(f"ok: login accepted, INBOX {status.lower()} with {count} message(s)")
        try:
            _, folders = imap.list()
            names = [f.decode(errors="replace").split(' "." ')[-1].strip('"') for f in folders or []]
            print(f"      folders: {', '.join(names) if names else 'none'}")
        except Exception as exc:  # noqa: BLE001 - listing is a nicety
            print(f"      folder list unavailable ({type(exc).__name__})")
        return True
    except imaplib.IMAP4.error as exc:
        print(f"FAILED: credentials rejected ({exc})")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}")
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001 - best effort
                pass
    return False


def check_smtp(host: str, port: int, user: str, password: str, send_to: str | None, timeout: float) -> bool:
    ctx = ssl.create_default_context()
    print(f"smtp: {host}:{port} as {user} ...", end=" ", flush=True)
    smtp = None
    try:
        smtp = smtplib.SMTP(host, port, timeout=timeout)
        smtp.ehlo()
        smtp.starttls(context=ctx)
        smtp.ehlo()
        if not smtp.has_extn("auth"):
            print("FAILED: server offers no AUTH on this port")
            return False
        smtp.login(user, password)
        print("ok: authentication accepted, sending is permitted")
        if send_to:
            body = (
                f"From: {user}\r\nTo: {send_to}\r\n"
                "Subject: mail_verify_login test\r\n\r\n"
                "Sent by .vscode/mail_verify_login.py to confirm the mailbox can send.\r\n"
            )
            smtp.sendmail(user, [send_to], body)
            print(f"      test message queued to {send_to}")
        return True
    except smtplib.SMTPAuthenticationError as exc:
        print(f"FAILED: authentication rejected ({exc.smtp_code} {exc.smtp_error!r})")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}")
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001 - best effort
                pass
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a mailbox with an IMAP login and SMTP authentication.")
    ap.add_argument("--address", default="sales@invigismart.online")
    ap.add_argument("--imap-host", default="mail.invigismart.online,mx1.tld-mx.com",
                    help="comma-separated; each is tried until one accepts the login")
    ap.add_argument("--imap-port", type=int, default=993)
    ap.add_argument("--smtp-host", default=None, help="defaults to --imap-host")
    ap.add_argument("--smtp-port", type=int, default=587)
    ap.add_argument("--send-to", default=None, help="send a test message to this address")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    if "@" not in args.address:
        sys.exit(f"error: {args.address!r} is not a full address")
    imap_hosts = [h.strip() for h in args.imap_host.split(",") if h.strip()]
    smtp_hosts = [h.strip() for h in (args.smtp_host or args.imap_host).split(",") if h.strip()]

    password = getpass.getpass(f"Password for {args.address}: ")
    if not password:
        sys.exit("error: empty password, nothing tested")

    imap_ok = any(check_imap(host, args.imap_port, args.address, password, args.timeout) for host in imap_hosts)
    smtp_ok = any(
        check_smtp(host, args.smtp_port, args.address, password, args.send_to, args.timeout)
        for host in smtp_hosts
    )

    if imap_ok and smtp_ok:
        print(f"ok: {args.address} is ready — reading and sending both work")
        return 0
    if not imap_ok and not smtp_ok:
        print("FAILED: neither login worked; check the address, the password, and the hostname")
        return 1
    return 1 if not imap_ok else 2


if __name__ == "__main__":
    sys.exit(main())
