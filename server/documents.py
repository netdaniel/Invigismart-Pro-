"""PDF document storage helpers - parsing, validation and safe filenames.

Schools upload timetables and other reference documents as PDFs. Browsers send
those as `multipart/form-data`, and the standard library has no parser for that
(``cgi`` was removed in Python 3.13), so this module parses the handful of fields
the API accepts itself. Nothing here touches the database or the network.

Only PDFs are accepted. The magic bytes are checked, not just the file name or
the browser-supplied Content-Type, because both are trivial to forge.
"""
import os
import re
from email.message import Message
from email.parser import Parser
from email.policy import compat32
from urllib.parse import quote

# The API's categories. The browser labels these; the server only stores the key.
CATEGORIES = ("timetable", "exam_timetable", "policy", "other")

PDF_MAGIC = b"%PDF-"
DOC_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ ()-]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


class MultipartError(ValueError):
    """The request body was not a usable multipart/form-data payload."""


def parse_multipart(content_type, body):
    """Return ``{fields: {...}, files: [...]}`` for a multipart/form-data body.

    Each file part is ``{field, filename, content_type, data}`` where ``data`` is
    the raw bytes exactly as the browser sent them. A missing or broken boundary
    raises :class:`MultipartError`; the caller turns that into HTTP 400.
    """
    boundary = _boundary(content_type)
    delimiter = b"--" + boundary
    if delimiter not in body:
        raise MultipartError("The upload body is not valid multipart/form-data.")

    fields = {}
    files = []
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):                 # closing delimiter
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]

        header_blob, separator, content = chunk.partition(b"\r\n\r\n")
        if not separator:
            header_blob, separator, content = chunk.partition(b"\n\n")
            if not separator:
                continue

        # The CRLF before the next boundary belongs to the boundary, not the
        # value. Strip exactly one so binary payloads lose no data of their own.
        if content.endswith(b"\r\n"):
            content = content[:-2]
        elif content.endswith(b"\n"):
            content = content[:-1]

        # Decode headers as latin-1 so each byte keeps its value. Browsers put
        # raw UTF-8 in filename="..."; _utf8_filename() then recovers it.
        part = Parser(policy=compat32).parsestr(
            header_blob.decode("latin-1") + "\r\n\r\n"
        )
        disposition = str(part.get("Content-Disposition") or "").strip()
        if not disposition.lower().startswith("form-data"):
            continue
        name = _param(part, "name")
        filename = part.get_filename()
        if filename is not None:
            files.append({
                "field": name,
                "filename": _utf8_filename(filename),
                "content_type": str(part.get("Content-Type") or "").split(";")[0].strip().lower(),
                "data": content,
            })
        elif name:
            fields[name] = content.decode("utf-8", "replace").strip()
    return {"fields": fields, "files": files}


def _boundary(content_type):
    message = Message()
    message["content-type"] = content_type or ""
    if message.get_content_type() != "multipart/form-data":
        raise MultipartError("Expected a multipart/form-data upload.")
    boundary = message.get_param("boundary")
    if isinstance(boundary, tuple):                 # RFC 2231 parameter
        boundary = boundary[2]
    if not boundary or len(boundary) > 200:
        raise MultipartError("The upload is missing its multipart boundary.")
    try:
        return boundary.encode("latin-1")
    except UnicodeEncodeError:
        raise MultipartError("The multipart boundary is not valid.")


def _param(part, name):
    value = part.get_param(name, header="content-disposition")
    if isinstance(value, tuple):                   # RFC 2231 parameter
        value = value[2]
    return (value or "").strip()


def _utf8_filename(filename):
    """Browsers send UTF-8 bytes in filename="..."; recover them cleanly.

    The header parser reads those bytes as latin-1, so a name such as
    "timetable-ñ.pdf" would otherwise arrive mojibake'd.
    """
    try:
        return filename.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return filename


def pdf_problem(data):
    """Return a human-readable reason the payload is not a PDF, or None.

    The PDF specification places ``%PDF-`` within the first 1024 bytes, which is
    where readers look. Checking it stops an HTML file (or a script) being stored
    and later served as a document.
    """
    if not data:
        return "The PDF file is empty."
    if PDF_MAGIC not in data[:1024]:
        return "Only PDF files are accepted. The uploaded file does not look like a PDF."
    return None


def clean_filename(raw):
    """Strip directories/control characters and keep a readable download name."""
    name = _CONTROL_RE.sub(" ", str(raw or "")).replace("\\", "/").split("/")[-1].strip()
    name = _SAFE_FILENAME_RE.sub("_", name).strip(" .")
    if not name:
        name = "document.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:160]


def clean_title(raw, fallback="Untitled document"):
    """A title safe to render and store, with a useful fallback."""
    title = _CONTROL_RE.sub(" ", str(raw or "")).strip()
    return (title or fallback)[:120]


def content_disposition(filename, attachment=False):
    """Build a UTF-8-safe Content-Disposition header for one document."""
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "document.pdf"
    encoded = quote(filename, safe="")
    return "%s; filename=\"%s\"; filename*=UTF-8''%s" % (
        "attachment" if attachment else "inline",
        ascii_name,
        encoded,
    )


def storage_path(root, user_id, document_id):
    """Resolve one document's path, refusing anything that could escape root."""
    if not DOC_ID_RE.match(document_id or ""):
        raise ValueError("invalid document id")
    if not re.match(r"^[0-9a-f]{32}$", user_id or ""):
        raise ValueError("invalid user id")
    return os.path.join(root, user_id, document_id + ".pdf")
