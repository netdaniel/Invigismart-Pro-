"""WSGI bridge, so the same server runs under Apache + Passenger (cPanel).

`app.py` is a standalone `ThreadingHTTPServer`: it owns a socket, parses the
request line itself and writes headers straight to `wfile`. Passenger - what
cPanel's "Setup Python App" installs - has no port to proxy to. It imports a
module and calls `application(environ, start_response)`.

This module is the seam between the two, and it deliberately contains no routing
logic of its own. It rebuilds one HTTP/1.1 request from the WSGI environ, writes
it into an in-memory socket, hands that to the real `app.Handler` - the same
class the end-to-end tests drive - and turns the captured response back into a
WSGI response. Every route, every status code and every header therefore behaves
identically under Passenger and under `python3 server/app.py`, and a fix to one
is a fix to both.

Run it locally the way Passenger does, to check a deployment before uploading:

    python3 -c "import sys; sys.path.insert(0, 'server'); \
from wsgiref.simple_server import make_server; import wsgi_app; \
make_server('127.0.0.1', 8200, wsgi_app.application).serve_forever()"

Limits, all of them properties of shared hosting rather than of this bridge:
Passenger handles one request per process at a time, there is no keep-alive, and
the request body is read into memory (the server caps it at MAX_DOCUMENT_BYTES +
1 MB in any case, and answers 413 past that).
"""
import io
import os
import sys
import traceback
from http import HTTPStatus
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:                      # `import app` below needs it
    sys.path.insert(0, HERE)

import app as backend  # noqa: E402  (the path fix above must run first)


class _Output(io.BytesIO):
    """Response buffer that survives the handler closing its own streams.

    `socketserver.StreamRequestHandler.finish()` closes `wfile` and `rfile` on
    the way out. With a real socket that is correct; with a capture buffer it
    would throw away the response, so `close()` is redefined to do nothing and
    `closed` is pinned to False.
    """

    closed = False                            # shadows BytesIO.closed

    def close(self):
        self.flush()


class _RequestSocket:
    """The slice of a socket that `BaseHTTPRequestHandler` actually touches."""

    def __init__(self, rfile, wfile, client_address):
        self._rfile = rfile
        self._wfile = wfile
        self._client_address = client_address

    def makefile(self, mode="rb", *args, **kwargs):
        return self._rfile if "r" in str(mode) else self._wfile

    # http.server uses socketserver._SocketWriter when wbufsize is 0, and that
    # writes straight to the socket instead of going through makefile().
    def sendall(self, data):
        self._wfile.write(data)

    def send(self, data):
        self._wfile.write(data)
        return len(data)

    def fileno(self):
        return -1

    def getpeername(self):
        return self._client_address

    def getsockname(self):
        return ("127.0.0.1", 80)

    def settimeout(self, value):
        pass

    def setsockopt(self, *args, **kwargs):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        pass


def _body(environ):
    """The request body, capped at just over what the server will accept.

    Reading a byte past the limit is deliberate: the existing handler then sees
    Content-Length > MAX_REQUEST_BYTES and answers 413 itself, instead of this
    bridge inventing a status code of its own.
    """
    stream = environ.get("wsgi.input")
    if stream is None:
        return b""
    limit = backend.CONFIG.max_request_bytes + 1
    declared = (environ.get("CONTENT_LENGTH") or "").strip()
    if declared.isdigit():
        wanted = int(declared)
        return stream.read(min(wanted, limit)) if wanted > 0 else b""
    # Only a chunked body has no CONTENT_LENGTH, and only then may we read until
    # the end. Reading unconditionally would block for ever on every GET: the
    # WSGI input stream does not signal "no body" as EOF, it simply waits.
    transfer = (environ.get("HTTP_TRANSFER_ENCODING") or "").lower()
    if "chunked" in transfer:
        return stream.read(limit)
    return b""


def _raw_uri(environ):
    """The request target, percent-encoded again for the request line."""
    uri = environ.get("REQUEST_URI")            # set by Passenger and mod_wsgi
    if uri:
        return uri
    path = (environ.get("SCRIPT_NAME") or "") + (environ.get("PATH_INFO") or "/")
    try:
        path = path.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    uri = quote(path, safe="/:@!$&'()*+,;=~-._")
    query = environ.get("QUERY_STRING")
    if query:
        uri += "?" + query
    return uri


def _request_bytes(environ, body):
    """A raw HTTP/1.1 request, rebuilt from the WSGI environ."""
    lines = [
        "%s %s HTTP/1.1"
        % (environ.get("REQUEST_METHOD", "GET").upper(), _raw_uri(environ))
    ]
    for key, value in environ.items():
        if not key.startswith("HTTP_") or not value:
            continue
        if key in ("HTTP_CONTENT_LENGTH", "HTTP_CONTENT_TYPE"):
            continue                            # those two travel separately
        lines.append("%s: %s" % (key[5:].replace("_", "-").title(), value))
    if environ.get("CONTENT_TYPE"):
        lines.append("Content-Type: %s" % environ["CONTENT_TYPE"])
    # Always our own length: the handler decides 413 from it, so it must describe
    # the bytes actually being handed over.
    lines.append("Content-Length: %d" % len(body))
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")
    return head + body


def _split_response(raw):
    """Raw HTTP response bytes -> (status, headers, body) for WSGI."""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    code, reason = "500", "Internal Server Error"
    if lines and lines[0]:
        parts = lines[0].decode("latin-1").split(" ", 2)
        if len(parts) > 1 and parts[1].strip().isdigit():
            code = parts[1].strip()
            try:
                reason = (parts[2].strip() if len(parts) > 2 and parts[2].strip()
                          else HTTPStatus(int(code)).phrase)
            except ValueError:
                reason = "Unknown"
    headers = []
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        name = name.decode("latin-1").strip()
        if name:
            headers.append((name, value.decode("latin-1").strip()))
    return "%s %s" % (code, reason), headers, body


def dispatch(environ):
    """Run one request through the real handler; return the raw response bytes."""
    body = _body(environ)
    client = (environ.get("REMOTE_ADDR") or "127.0.0.1", 0)
    output = _Output()
    connection = _RequestSocket(io.BytesIO(_request_bytes(environ, body)), output, client)
    # server=None: BaseHTTPRequestHandler only stores it, and this instance has no
    # listening server to report to.
    backend.Handler(connection, client, None)
    return output.getvalue()


def application(environ, start_response):
    try:
        status, headers, body = _split_response(dispatch(environ))
    except Exception:
        traceback.print_exc()
        payload = b'{"error": "Internal server error."}'
        start_response(
            "500 Internal Server Error",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(payload))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [payload]
    start_response(status, headers)
    if (environ.get("REQUEST_METHOD") or "").upper() == "HEAD":
        return [b""]                            # headers only, as the spec says
    return [body]
