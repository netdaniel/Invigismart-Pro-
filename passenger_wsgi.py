"""Passenger entry point - the file cPanel's "Setup Python App" runs.

cPanel's Python applications are Phusion Passenger applications: Apache imports
this module and calls ``application(environ, start_response)``. Nothing else is
installed - the server, the bridge and every helper are standard library only -
so the virtualenv Passenger creates needs no packages at all.

In cPanel, create the application with:

    Application root      invigismart-api        (a folder, not the web root)
    Application startup   passenger_wsgi.py
    Application entry     application

then upload this repository's files into that folder. See DEPLOY.md section 13.

Configuration comes from an env file rather than from the shell, because
Passenger starts the process without one. By default that file is ``.env`` in
this folder - a dotfile, so both Apache and the server's own static handler
refuse to serve it. Point ``INVIGISMART_ENV`` at another path to keep it outside
the account's web root entirely. The format is the same KEY=VALUE used by
``.env.production``, with optional surrounding quotes; a variable that is
already in the real environment always wins, so cPanel's "Environment
variables" box keeps working too.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV = os.path.join(HERE, ".env")


def load_env(path):
    """Populate os.environ from a KEY=VALUE file. Never overrides a real value."""
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError as exc:
        sys.stderr.write("[invigismart] cannot read %s: %s\n" % (path, exc))
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name, value)
    return True


ENV_FILE = os.environ.get("INVIGISMART_ENV") or DEFAULT_ENV
if not load_env(ENV_FILE):
    # Loud, because the fallback is a development configuration: an ephemeral
    # signing key (every restart signs everyone out) and a database beside the
    # code. /api/health reports both as ephemeralJwtSecret and dbPath.
    sys.stderr.write(
        "[invigismart] WARNING: no env file at %s, so the app is running on the "
        "process environment alone. DEPLOY.md section 13 lists what must be in "
        "it.\n" % ENV_FILE
    )

# server/ holds app.py and its helpers; app.py adds its own directory to
# sys.path as well, so importing it from here is enough.
sys.path.insert(0, os.path.join(HERE, "server"))

from wsgi_app import application  # noqa: E402,F401  (Passenger looks for this name)
