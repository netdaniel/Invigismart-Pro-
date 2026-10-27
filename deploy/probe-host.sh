#!/bin/sh
# Read-only probe of a candidate host for the Invigismart Pro backend.
#
# Installs nothing, writes nothing outside stdout, reads no passwords. The
# backend is a standard-library ThreadingHTTPServer, so what matters is: is
# there a real init system, is Python 3 recent enough, is something already
# holding :80/:443, and can the box reach PayFast. Those four answers decide
# whether the app can live here at all.
#
# Usage, from the machine that holds the repo:
#   ssh root@HOST 'bash -s' < deploy/probe-host.sh > host.txt
# or, on the host itself:
#   sh deploy/probe-host.sh
set -u

say() { printf '%-22s %s\n' "$1" "$2"; }
have() { command -v "$1" >/dev/null 2>&1 && echo yes || echo no; }
yn() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }

# --- what the box is ---------------------------------------------------------
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  say os "${PRETTY_NAME:-unknown}"
else
  say os unknown
fi
say kernel "$(uname -r)"
say arch "$(uname -m)"
say hostname "$(hostname -f 2>/dev/null || hostname)"
say public_ipv4 "$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null \
  || curl -4 -fsS --max-time 8 https://ifconfig.co 2>/dev/null || echo unknown)"

# --- can it run the app ------------------------------------------------------
say python3 "$(python3 -V 2>&1 || echo MISSING)"
say sqlite3_lib "$(python3 -c 'import sqlite3;print(sqlite3.sqlite_version)' 2>/dev/null || echo MISSING)"
say init_systemd "$(if [ -d /run/systemd/system ]; then echo yes; else echo no; fi)"
say shared_cpanel "$(if [ -d /usr/local/cpanel ]; then echo yes; else echo no; fi)"
say memory_available "$(free -m 2>/dev/null | awk '/^Mem:/{print $7" MiB"}')"
say disk_slash_var "$(df -h /var 2>/dev/null | awk 'NR==2{print $4" free of "$2}')"
say clock_utc "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# --- who already owns the ports we need -------------------------------------
port_owner() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | awk -v p=":${1}$" '$4 ~ p {print $4" -> "$6"; "; f=1} END{if(!f) print "free"}'
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltnp 2>/dev/null | awk -v p=":${1}$" '$4 ~ p {print $4" -> "$7"; "; f=1} END{if(!f) print "free"}'
  else
    echo "unknown (no ss/netstat)"
  fi
}
for port in 22 80 443 8080; do
  say "port_$port" "$(port_owner "$port")"
done

say web_server_nginx "$(if command -v nginx >/dev/null 2>&1; then nginx -v 2>&1; else echo no; fi)"
say web_server_apache "$(if command -v apache2 >/dev/null 2>&1; then apache2 -v 2>&1 | head -1
  elif command -v httpd >/dev/null 2>&1; then httpd -v 2>&1 | head -1
  else echo no; fi)"
say web_server_caddy "$(if command -v caddy >/dev/null 2>&1; then caddy version 2>&1 | head -1; else echo no; fi)"
say vhost_dirs "$(ls -d /etc/nginx/sites-enabled /etc/apache2/sites-enabled 2>/dev/null | tr '\n' ' ')"
say www_dir "$(ls /var/www 2>/dev/null | tr '\n' ' ')"

# --- can it reach PayFast and the outside world -----------------------------
say outbound_payfast "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 12 \
  https://api.payfast.co.za/ 2>&1 || echo unreachable)"
say outbound_https "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 12 \
  https://api.github.com/ 2>&1 || echo unreachable)"

# --- privilege and firewall --------------------------------------------------
say sudo_passwordless "$(yn sudo -n true)"
say ufw "$(if command -v ufw >/dev/null 2>&1; then ufw status 2>/dev/null | head -1; else echo absent; fi)"
say iptables_filter "$(iptables -S 2>/dev/null | awk 'NR==1{print $3} END{if(NR==0) print "absent"}')"

# --- does the subdomain exist yet -------------------------------------------
resolves() {
  ip=$(getent hosts "$1" 2>/dev/null | awk 'NR==1{print $1}')
  echo "${ip:-none}"
}
say dns_app_record "$(resolves app.invigismart.online)"
say dns_roster_record "$(resolves roster.invigismart.online)"
