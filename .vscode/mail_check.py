#!/usr/bin/env python3
"""Audit a mail domain and check whether one mailbox exists.

Read-only: it resolves DNS over HTTPS, tests the mail ports, and asks the MX
whether an address is deliverable (SMTP RCPT). It never logs in, never sends a
message, and never prints a credential.

    python3 .vscode/mail_check.py                       # invigismart.online / sales@
    python3 .vscode/mail_check.py --address demo@invigismart.online
    python3 .vscode/mail_check.py --dkim-selector x --no-smtp
    python3 .vscode/mail_check.py --domain prinslooconsulting.co.za --address info@prinslooconsulting.co.za

Exit codes: 0 mailbox exists, 1 mailbox missing, 2 inconclusive (catch-all,
greylisting, blocked probe), 3 DNS/port failure so no probe was possible.
"""

from __future__ import annotations

import argparse
import json
import smtplib
import socket
import ssl
import sys
import urllib.parse
import urllib.request

PORTS = (25, 465, 587, 993, 143, 995)
DEFAULT_SELECTORS = ("default", "mail", "dkim", "s1", "selector1", "k1", "google")


def doh(name: str, rtype: str, timeout: float = 15) -> list[str]:
    """Query Google's DNS-over-HTTPS endpoint; returns the raw answer strings."""
    url = "https://dns.google/resolve?" + urllib.parse.urlencode({"name": name, "type": rtype})
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.load(resp)
    want = {"A": 1, "AAAA": 28, "MX": 15, "TXT": 16}[rtype]
    return [a["data"] for a in data.get("Answer", []) if a.get("type") == want]


def dns_report(domain: str, selectors: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Print MX/SPF/DMARC/DKIM, return (mx_hosts, mx_ipv4)."""
    print(f"=== DNS for {domain} ===")
    mx_hosts: list[str] = []
    try:
        for rec in sorted(doh(domain, "MX"), key=lambda r: int(r.split()[0])):
            prio, host = rec.split()[0], rec.split()[1].rstrip(".")
            mx_hosts.append(host)
            print(f"  MX {prio:>3}  {host}")
    except Exception as exc:  # noqa: BLE001 - report and continue
        print(f"  MX lookup failed: {type(exc).__name__}: {exc}")

    for name, label, keys in ((domain, "SPF", ("v=spf1",)), ("_dmarc." + domain, "DMARC", ("v=dmarc1",))):
        try:
            recs = [r.strip('"') for r in doh(name, "TXT") if any(k in r.lower() for k in keys)]
            print(f"  {label}: {recs[0] if recs else 'NOT SET'}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label} lookup failed: {type(exc).__name__}: {exc}")

    found = []
    for sel in selectors:
        try:
            if doh(f"{sel}._domainkey.{domain}", "TXT"):
                found.append(sel)
                print(f"  DKIM: selector '{sel}' present")
        except Exception:  # noqa: BLE001 - a missing selector is normal
            pass
    if not found:
        print(f"  DKIM: none of the common selectors answered {list(selectors)}")

    mx_ipv4: list[str] = []
    for host in mx_hosts:
        try:
            ips = doh(host, "A")
        except Exception:  # noqa: BLE001
            ips = []
        mx_ipv4.extend(ips)
        print(f"  {host:<24} A -> {ips or 'none'}")
    return mx_hosts, mx_ipv4


def cert_note(ip: str, port: int, host: str, timeout: float) -> str:
    """Best-effort peek at the certificate so the client hostname can be chosen."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_OPTIONAL  # parse the cert without demanding trust
    try:
        with socket.create_connection((ip, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                names = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
                cn = next((v for rdn in cert.get("subject", ()) for k, v in rdn if k == "commonName"), "?")
                return f"cert CN={cn} SAN={names[:3]}"
    except Exception as exc:  # noqa: BLE001
        return f"TLS peek failed ({type(exc).__name__})"


def port_report(hosts: list[str], timeout: float) -> None:
    """IPv4-only connect test; the MX cluster also publishes AAAA records that
    stall on networks without working IPv6, so A records are used explicitly."""
    print("\n=== mail ports (IPv4) ===")
    for host in hosts:
        try:
            ips = doh(host, "A")
        except Exception as exc:  # noqa: BLE001
            print(f"  {host}: cannot resolve ({type(exc).__name__})")
            continue
        for ip in ips[:2]:
            for port in PORTS:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                        sock.settimeout(timeout)
                        sock.connect((ip, port))
                        note = ""
                        if port in (993, 465):
                            note = " " + cert_note(ip, port, host, timeout)
                        print(f"  {host:<22} {ip:<15} :{port:<4} open{note}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  {host:<22} {ip:<15} :{port:<4} {type(exc).__name__}")


def probe(address: str, ip: str, helo: str, sender: str, timeout: float) -> tuple[str, str]:
    """Return (verdict, detail) for one recipient: exists / missing / inconclusive.

    Connects to the MX's IPv4 address directly: the cluster publishes AAAA
    records, and an IPv6 stall would turn every probe into a timeout.
    """
    try:
        with smtplib.SMTP(ip, 25, timeout=timeout, local_hostname=helo) as smtp:
            smtp.ehlo(helo)
            if smtp.has_extn("starttls"):
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo(helo)
            code, _ = smtp.mail(sender)
            if code >= 400:
                return "inconclusive", f"MAIL FROM refused ({code}) - server wants authentication"
            rcode, rmsg = smtp.rcpt(address)
            text = rmsg.decode("utf-8", "replace").strip()
            return ("exists" if rcode < 300 else "missing"), f"RCPT {rcode} {text}"
    except smtplib.SMTPRecipientsRefused as exc:
        code, msg = next(iter(exc.recipients.values()))
        return "missing", f"RCPT {code} {msg.decode('utf-8', 'replace').strip()}"
    except Exception as exc:  # noqa: BLE001
        return "inconclusive", f"{type(exc).__name__}: {exc}"


def smtp_report(target: str, mx_hosts: list[str], sender: str, timeout: float) -> int:
    if not mx_hosts:
        print("\n=== SMTP probe ===\n  skipped: no MX host")
        return 3
    host = mx_hosts[0]
    domain = target.split("@", 1)[1]
    helo = sender.split("@", 1)[1]
    try:
        ip = doh(host, "A")[0]
    except Exception as exc:  # noqa: BLE001
        print(f"\n=== SMTP probe on {host} ===\n  skipped: no IPv4 address ({type(exc).__name__})")
        return 3
    control_random = f"no-such-mailbox-9f3a2b@{domain}"
    tests = (
        (control_random, "random control"),
        (f"postmaster@{domain}", "postmaster control"),
        (target, "target"),
    )
    print(f"\n=== SMTP probe on {host} ({ip}) ===")
    verdicts = {}
    for addr, label in tests:
        verdict, detail = probe(addr, ip, helo, sender, timeout)
        verdicts[label] = verdict
        print(f"  {label:<18} {addr:<42} {verdict:<12} {detail}")

    if verdicts["target"] == "exists" and verdicts["random control"] == "exists":
        print("  => INCONCLUSIVE: server accepts every address (catch-all), so RCPT proves nothing")
        return 2
    if verdicts["target"] == "exists":
        print(f"  => {target} EXISTS and is deliverable")
        return 0
    if verdicts["target"] == "missing":
        print(f"  => {target} DOES NOT EXIST on this server")
        return 1
    print("  => INCONCLUSIVE: probe refused or timed out; retry or test from another network")
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit a mail domain and test whether a mailbox exists.")
    ap.add_argument("--domain", default="invigismart.online")
    ap.add_argument("--address", default=None, help="mailbox to test (default sales@<domain>)")
    ap.add_argument("--sender", default="postmaster@invigismart.online", help="envelope sender for the probe")
    ap.add_argument("--dkim-selector", action="append", default=None, help="extra DKIM selector to try")
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--no-smtp", action="store_true", help="skip the recipient probe")
    args = ap.parse_args()

    target = args.address or f"sales@{args.domain}"
    if "@" not in target:
        sys.exit(f"error: {target!r} is not a full address")
    selectors = tuple(args.dkim_selector or ()) + DEFAULT_SELECTORS

    mx_hosts, _ = dns_report(args.domain, selectors)
    port_report(list(dict.fromkeys(mx_hosts + ["mail." + args.domain])), args.timeout)
    if args.no_smtp:
        print("\n=== SMTP probe ===\n  skipped (--no-smtp)")
        return 0
    return smtp_report(target, mx_hosts, args.sender, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
