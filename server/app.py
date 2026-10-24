#!/usr/bin/env python3
"""Invigismart Pro backend (Option B).

Server-enforced accounts, subscriptions and the roster solver for the
single-page front end:

  * accounts          - email + password (PBKDF2), signed JWT sessions
  * billing           - Stripe Checkout + signed webhook, or a local mock mode
  * entitlement       - always derived from the database, never the browser
  * roster generation - the solver runs here and returns HTTP 402 unless the
                        account has a live subscription or trial

Standard library only. Run with:

    python3 server/app.py          # app + API on http://127.0.0.1:8000
"""
import datetime
import json
import os
import re
import sys
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import billing
import documents
import payfast
import roster
import security
from config import Config
from db import Store

CONFIG = Config()
STORE = Store(CONFIG.db_path)
# Uploads check a quota and then write a file. Serialising that section stops two
# simultaneous uploads from both passing the last free slot.
DOCUMENT_LOCK = threading.Lock()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


class ApiError(Exception):
    def __init__(self, status, message, code=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def is_valid_email(email):
    return bool(email) and len(email) <= 254 and bool(EMAIL_RE.match(email))


def is_valid_password(password):
    return isinstance(password, str) and len(password) >= 8


def _megabytes(value):
    """Human-readable size for quota errors (10 MB, 1.5 MB, ...)."""
    return "%g MB" % round(float(value) / (1024.0 * 1024.0), 1)


class Handler(BaseHTTPRequestHandler):
    server_version = "InvigismartPro/1.0"

    # ---- logging -----------------------------------------------------------
    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s %s\n" % (self.address_string(), fmt % args))

    # ---- response helpers --------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _send(self, status, body, content_type, extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload), "application/json; charset=utf-8")

    def _content_length(self):
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except (TypeError, ValueError):
            return 0

    def _parse_json(self, raw):
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Request body must be valid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        return data

    # ---- auth --------------------------------------------------------------
    def _current_user(self):
        header = self.headers.get("Authorization") or ""
        if not header.lower().startswith("bearer "):
            return None
        claims = security.decode_jwt(header[7:].strip(), CONFIG.jwt_secret)
        if not claims:
            return None
        return STORE.get_user_by_id(claims.get("sub"))

    def _require_user(self):
        user = self._current_user()
        if not user:
            raise ApiError(401, "Authentication required.", code="unauthorized")
        return user

    # ---- session payloads --------------------------------------------------
    def _session_payload(self, user):
        return {
            "token": security.issue_access_token(user, CONFIG.jwt_secret, CONFIG.access_token_ttl),
            "user": billing.public_user(user),
            "subscription": billing.entitlement(STORE, user, CONFIG),
        }

    def _me_payload(self, user):
        return {
            "user": billing.public_user(user),
            "subscription": billing.entitlement(STORE, user, CONFIG),
        }

    # ---- API handlers ------------------------------------------------------
    def _api_signup(self, body):
        school = str(body.get("school") or "").strip()
        email = str(body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        if not school or len(school) > 120:
            raise ApiError(400, "School name is required (max 120 characters).")
        if not is_valid_email(email):
            raise ApiError(400, "A valid email address is required.")
        if not is_valid_password(password):
            raise ApiError(400, "Password must be at least 8 characters.")
        if STORE.get_user_by_email(email):
            raise ApiError(409, "An account with that email already exists.")
        trial_end = billing.to_iso(
            billing.utcnow() + datetime.timedelta(days=CONFIG.trial_days)
        )
        user = STORE.create_user(school, email, security.hash_password(password), trial_end)
        self._json(201, self._session_payload(user))

    def _api_login(self, body):
        email = str(body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        user = STORE.get_user_by_email(email)
        if not user or not security.verify_password(password, user.get("pw_hash")):
            raise ApiError(401, "Invalid email or password.")
        self._json(200, self._session_payload(user))

    def _api_checkout(self, user, body):
        try:
            session = billing.create_checkout_session(STORE, user, CONFIG, body.get("plan"))
        except billing.BillingError as exc:
            raise ApiError(502, str(exc))
        self._json(200, session)

    def _api_portal(self, user):
        """Open Stripe's hosted Billing Portal: invoices, card, cancellation."""
        try:
            session = billing.create_portal_session(STORE, user, CONFIG)
        except billing.BillingError as exc:
            status = {
                "not_configured": 503,
                "no_customer": 409,
                "unsupported": 409,
            }.get(exc.code, 502)
            raise ApiError(status, str(exc), code=exc.code)
        self._json(200, session)

    def _api_webhook(self, raw):
        if CONFIG.billing_provider == "payfast":
            self._api_payfast_itn(raw)
            return
        secret = CONFIG.stripe_webhook_secret
        signature = self.headers.get("Stripe-Signature")
        if secret:
            if not billing.verify_stripe_signature(raw, signature, secret):
                raise ApiError(400, "Invalid Stripe signature.")
        elif not (CONFIG.allow_mock_billing and not CONFIG.billing_configured):
            # Unsigned deliveries are acceptable only in local mock mode. On a
            # live server an unverifiable body is a 400, never a state change.
            raise ApiError(400, "Webhook secret is not configured.")
        try:
            event = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Webhook body must be valid JSON.")
        if not isinstance(event, dict):
            raise ApiError(400, "Webhook body must be a JSON object.")

        event_id = event.get("id")
        if event_id and not STORE.record_event(event_id, event.get("type") or "unknown", event):
            self._json(200, {"received": True, "duplicate": True, "action": "duplicate"})
            return

        user_id, action = billing.apply_subscription_event(STORE, event, CONFIG)
        user = STORE.get_user_by_id(user_id) if user_id else None
        self._json(
            200,
            {
                "received": True,
                "action": action,
                "userId": user_id,
                "subscription": billing.entitlement(STORE, user, CONFIG) if user else None,
            },
        )

    # ---- PayFast ITN -------------------------------------------------------
    def _resolve_itn_user(self, candidate):
        """Map an ITN field back to an account: id first, then email."""
        if not candidate:
            return None
        user = STORE.get_user_by_id(str(candidate).strip())
        if user:
            return user["id"]
        if "@" in str(candidate):
            user = STORE.get_user_by_email(str(candidate).strip())
            if user:
                return user["id"]
        return None

    def _api_payfast_itn(self, raw):
        """Handle a PayFast ITN: form-encoded, and verified before it counts.

        PayFast retries until it gets a 2xx, so anything we reject with a 400 is
        handed back to us later - which is why an unverifiable notification is
        never applied instead.
        """
        fields = payfast.form_to_dict(raw)
        if not fields:
            raise ApiError(400, "ITN body was empty.")
        try:
            payfast.verify_itn(
                fields,
                raw,
                CONFIG,
                remote_ip=self.client_address[0] if self.client_address else None,
                headers=self.headers,
            )
        except payfast.ItnError as exc:
            raise ApiError(400, str(exc))

        event = payfast.canonical_event(fields, resolve_user=self._resolve_itn_user)
        if not STORE.record_event(event["id"], event["type"], event):
            self._json(200, {"received": True, "duplicate": True, "action": "duplicate"})
            return

        user_id, action = billing.apply_subscription_event(STORE, event, CONFIG)
        user = STORE.get_user_by_id(user_id) if user_id else None
        self._json(
            200,
            {
                "received": True,
                "action": action,
                "userId": user_id,
                "subscription": billing.entitlement(STORE, user, CONFIG) if user else None,
            },
        )

    def _api_billing_summary(self, user):
        """In-app billing: licence state, invoice history and cancellation."""
        self._json(200, billing.create_billing_summary(STORE, user, CONFIG))

    def _api_cancel(self, user):
        try:
            result = billing.cancel_licence(STORE, user, CONFIG)
        except billing.BillingError as exc:
            status = {
                "not_configured": 503,
                "no_subscription": 409,
                "unsupported": 409,
            }.get(exc.code, 502)
            raise ApiError(status, str(exc), code=exc.code)
        self._json(200, result)

    def _api_mock_activate(self, user, body):
        if not CONFIG.allow_mock_billing:
            raise ApiError(403, "Mock billing is disabled on this server.")
        days = 365
        try:
            if body.get("days"):
                days = max(1, min(3650, int(body["days"])))
        except (TypeError, ValueError):
            days = 365
        event = billing.mock_checkout_event(user, CONFIG, days)
        STORE.record_event(event["id"], event["type"], event)
        billing.apply_subscription_event(STORE, event, CONFIG)
        self._json(200, self._me_payload(STORE.get_user_by_id(user["id"])))

    def _api_dev_expire(self, user):
        if not CONFIG.allow_mock_billing:
            raise ApiError(403, "Development endpoints are disabled on this server.")
        STORE.upsert_subscription(user["id"], status="canceled", clear_period_end=True)
        STORE.set_trial_end(
            user["id"], billing.to_iso(billing.utcnow() - datetime.timedelta(days=1))
        )
        self._json(200, self._me_payload(STORE.get_user_by_id(user["id"])))

    def _roster_payload(self, body):
        """Validate the roster-shaped part of a generate/export request body."""
        teachers = body.get("teachers")
        exams = body.get("exams")
        schedules = body.get("schedules")
        if schedules is None:
            schedules = []
        if not all(isinstance(x, list) for x in (teachers, exams, schedules)):
            raise ApiError(400, "teachers, schedules and exams must be arrays.")
        if not teachers or not exams:
            raise ApiError(400, "Need at least one teacher and one exam session.")
        if len(teachers) > 2000 or len(exams) > 2000:
            raise ApiError(400, "Too many teachers or exams in a single request.")
        return teachers, exams, schedules

    def _api_generate_roster(self, user, body):
        ent = billing.entitlement(STORE, user, CONFIG)
        if not ent["entitled"]:
            raise ApiError(
                402,
                "An active subscription is required to generate rosters.",
                code="subscription_required",
            )
        teachers, exams, schedules = self._roster_payload(body)
        result = roster.generate_roster(teachers, schedules, exams)
        result["entitlement"] = ent
        # The evaluation window runs the real solver, so what it produces is
        # stamped as an evaluation copy: only a paid licence yields a clean export.
        result["trialArtifact"] = ent["source"] != "subscription"
        self._json(200, result)

    def _api_export_roster(self, user, body):
        """The clean, unwatermarked CSV - refused for trials. That is the point.

        Entitlement is read from the database, so a trial account (or a lapsed
        one) gets HTTP 402 here no matter what the browser asks for.
        """
        ent = billing.entitlement(STORE, user, CONFIG)
        if ent["source"] != "subscription":
            raise ApiError(
                402,
                "A clean, unwatermarked export requires an annual subscription.",
                code="subscription_required",
            )
        teachers, exams, _schedules = self._roster_payload(body)
        assignments = body.get("assignments")
        if not isinstance(assignments, list):
            raise ApiError(400, "assignments must be an array.")
        if len(assignments) > 2000:
            raise ApiError(400, "Too many assignments in a single request.")
        self._json(
            200,
            {
                "filename": "invigilation_roster.csv",
                "csv": roster.export_roster_csv(teachers, exams, assignments),
                "entitlement": ent,
            },
        )

    # ---- document library --------------------------------------------------
    @staticmethod
    def _document_payload(row):
        return {
            "id": row["id"],
            "filename": row["filename"],
            "title": row["title"],
            "category": row["category"],
            "size": row["size"],
            "uploadedAt": row["created_at"],
        }

    def _document_limits(self, user_id):
        count, used = STORE.document_usage(user_id)
        return {
            "maxBytes": CONFIG.max_document_bytes,
            "maxDocuments": CONFIG.max_documents_per_user,
            "maxStorageBytes": CONFIG.max_document_storage_bytes,
            "count": count,
            "usedBytes": used,
        }

    def _api_list_documents(self, user):
        rows = STORE.list_documents(user["id"])
        self._json(
            200,
            {
                "documents": [self._document_payload(row) for row in rows],
                "limits": self._document_limits(user["id"]),
            },
        )

    def _api_upload_document(self, user, raw):
        """Accept one PDF via multipart/form-data: file, title, category."""
        try:
            parsed = documents.parse_multipart(self.headers.get("Content-Type"), raw)
        except documents.MultipartError as exc:
            raise ApiError(400, str(exc), code="invalid_upload")

        files = parsed["files"]
        if not files:
            raise ApiError(400, "Choose a PDF file to upload.", code="file_required")
        if len(files) > 1:
            raise ApiError(400, "Upload one PDF at a time.", code="too_many_files")
        part = files[0]
        if part.get("field") != "file":
            raise ApiError(400, "The PDF must be sent in the 'file' field.", code="file_required")

        # Check the *original* name before cleaning: clean_filename() appends
        # ".pdf" to extensionless names, which would hide a renamed .txt upload.
        original = str(part.get("filename") or "")
        if not original.lower().endswith(".pdf"):
            raise ApiError(415, "Only PDF files are accepted.", code="pdf_required")
        content_type = (part.get("content_type") or "").lower()
        if content_type not in ("", "application/pdf", "application/x-pdf",
                                "application/octet-stream"):
            raise ApiError(415, "Only PDF files are accepted.", code="pdf_required")

        data = part.get("data") or b""
        if len(data) > CONFIG.max_document_bytes:
            raise ApiError(
                413,
                "That PDF is larger than %s." % _megabytes(CONFIG.max_document_bytes),
                code="document_too_large",
            )
        problem = documents.pdf_problem(data)
        if problem:
            raise ApiError(415, problem, code="pdf_required")

        fields = parsed["fields"]
        category = (fields.get("category") or "other").strip().lower()
        if category not in documents.CATEGORIES:
            raise ApiError(400, "Unknown document category.", code="invalid_category")
        filename = documents.clean_filename(original)
        fallback = filename[:-4].replace("_", " ").strip() or "Untitled document"
        title = documents.clean_title(fields.get("title"), fallback=fallback)

        with DOCUMENT_LOCK:
            count, used = STORE.document_usage(user["id"])
            if count >= CONFIG.max_documents_per_user:
                raise ApiError(
                    409,
                    "This account already has the maximum of %d documents. "
                    "Delete one before uploading another."
                    % CONFIG.max_documents_per_user,
                    code="document_limit_reached",
                )
            if used + len(data) > CONFIG.max_document_storage_bytes:
                raise ApiError(
                    409,
                    "This upload would exceed the %s document storage allowance. "
                    "Delete a document and try again."
                    % _megabytes(CONFIG.max_document_storage_bytes),
                    code="storage_limit_reached",
                )

            document_id = uuid.uuid4().hex
            path = documents.storage_path(CONFIG.documents_dir, user["id"], document_id)
            try:
                os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                handle = os.fdopen(
                    os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
                )
                with handle:
                    handle.write(data)
            except OSError:
                traceback.print_exc()
                raise ApiError(
                    500,
                    "Could not store the document. Check that DOCUMENTS_DIR is writable.",
                )
            try:
                row = STORE.create_document(
                    document_id, user["id"], filename, title, category, len(data)
                )
            except Exception:
                # Never leave bytes behind when the metadata write fails.
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise

        self._json(
            201,
            {
                "document": self._document_payload(row),
                "limits": self._document_limits(user["id"]),
            },
        )

    def _api_download_document(self, user, document_id, attachment=False):
        if not documents.DOC_ID_RE.match(document_id or ""):
            raise ApiError(404, "Document not found.")
        row = STORE.get_document(document_id, user["id"])
        if not row:
            # Same answer for "does not exist" and "belongs to another school".
            raise ApiError(404, "Document not found.")
        try:
            path = documents.storage_path(CONFIG.documents_dir, user["id"], document_id)
            with open(path, "rb") as handle:
                body = handle.read()
        except (OSError, ValueError):
            raise ApiError(404, "The document file is missing.")
        self._send(
            200,
            body,
            "application/pdf",
            {
                "Content-Disposition": documents.content_disposition(
                    row["filename"], attachment=attachment
                )
            },
        )

    def _api_delete_document(self, user, document_id):
        if not documents.DOC_ID_RE.match(document_id or ""):
            raise ApiError(404, "Document not found.")
        row = STORE.get_document(document_id, user["id"])
        if not row:
            raise ApiError(404, "Document not found.")
        STORE.delete_document(document_id, user["id"])
        try:
            os.unlink(documents.storage_path(CONFIG.documents_dir, user["id"], document_id))
        except OSError:
            # Metadata is gone, so the document is gone from the school's view;
            # a leftover file is harmless and can be cleaned up by the operator.
            traceback.print_exc()
        self._json(
            200,
            {
                "deleted": True,
                "id": document_id,
                "limits": self._document_limits(user["id"]),
            },
        )

    # ---- routing -----------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                self._json(
                    200,
                    {
                        "ok": True,
                        "time": billing.to_iso(billing.utcnow()),
                        "config": CONFIG.describe(),
                    },
                )
            elif path == "/api/me":
                self._json(200, self._me_payload(self._require_user()))
            elif path == "/api/events":
                self._require_user()
                self._json(200, {"events": STORE.list_events(50)})
            elif path == "/api/billing/summary":
                self._api_billing_summary(self._require_user())
            elif path == "/api/documents":
                self._api_list_documents(self._require_user())
            elif path.startswith("/api/documents/"):
                document_id = path[len("/api/documents/"):].strip("/")
                if "/" in document_id:
                    raise ApiError(404, "Document not found.")
                attachment = parse_qs(parsed.query).get("download", ["0"])[0].lower() in (
                    "1", "true", "yes",
                )
                self._api_download_document(
                    self._require_user(), document_id, attachment
                )
            elif path == "/mock-checkout":
                self._serve_mock_checkout()
            elif path.startswith("/api/"):
                raise ApiError(404, "Unknown endpoint.")
            else:
                self._serve_static(path)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message, "code": exc.code})
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Internal server error."})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            length = self._content_length()
            if length > CONFIG.max_request_bytes:
                # Refuse before allocating the body. Closing the connection is
                # the only honest answer when the client may still be writing.
                self.close_connection = True
                raise ApiError(
                    413,
                    "The request body is too large.",
                    code="request_too_large",
                )
            raw = self.rfile.read(length) if length else b""
            if path == "/api/auth/signup":
                self._api_signup(self._parse_json(raw))
            elif path == "/api/auth/login":
                self._api_login(self._parse_json(raw))
            elif path == "/api/billing/checkout":
                self._api_checkout(self._require_user(), self._parse_json(raw))
            elif path == "/api/billing/portal":
                self._api_portal(self._require_user())
            elif path == "/api/billing/cancel":
                self._api_cancel(self._require_user())
            elif path == "/api/billing/webhook":
                self._api_webhook(raw)
            elif path in ("/api/billing/mock-activate", "/api/billing/dev-activate"):
                self._api_mock_activate(self._require_user(), self._parse_json(raw))
            elif path in ("/api/billing/dev-expire", "/api/billing/mock-expire"):
                self._api_dev_expire(self._require_user())
            elif path == "/api/roster/generate":
                self._api_generate_roster(self._require_user(), self._parse_json(raw))
            elif path == "/api/roster/export":
                self._api_export_roster(self._require_user(), self._parse_json(raw))
            elif path == "/api/documents":
                self._api_upload_document(self._require_user(), raw)
            else:
                raise ApiError(404, "Unknown endpoint.")
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message, "code": exc.code})
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Internal server error."})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/documents/"):
                document_id = path[len("/api/documents/"):].strip("/")
                if "/" in document_id:
                    raise ApiError(404, "Document not found.")
                self._api_delete_document(self._require_user(), document_id)
            elif path.startswith("/api/"):
                raise ApiError(404, "Unknown endpoint.")
            else:
                raise ApiError(405, "Method not allowed.")
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message, "code": exc.code})
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Internal server error."})

    # ---- static front end --------------------------------------------------
    def _serve_static(self, url_path):
        if url_path in ("/", ""):
            url_path = "/landing.html"                       # marketing/landing page
        elif url_path in ("/app", "/app/"):
            url_path = "/index.html"                         # the roster dashboard
        elif url_path in ("/login", "/login/", "/signup", "/signup/"):
            url_path = "/landing.html"                       # landing page opens the modal
        elif url_path in ("/terms", "/terms/"):
            url_path = "/terms.html"                         # Terms & Conditions of Service
        elif url_path in ("/privacy", "/privacy/"):
            url_path = "/privacy.html"                       # POPIA/GDPR-compliant privacy policy
        target = self._safe_static_path(url_path)
        if not target:
            raise ApiError(404, "Not found.")
        with open(target, "rb") as handle:
            body = handle.read()
        ext = os.path.splitext(target)[1].lower()
        self._send(200, body, STATIC_TYPES.get(ext, "application/octet-stream"))

    def _safe_static_path(self, url_path):
        rel = url_path.lstrip("/")
        if not rel:
            return None
        candidate = os.path.normpath(os.path.join(CONFIG.repo_root, rel))
        if not candidate.startswith(CONFIG.repo_root + os.sep):
            return None
        # DOCUMENTS_DIR defaults to server/uploads (already blocked below), but an
        # operator may point it elsewhere inside the checkout. Never let the
        # static handler reach a stored PDF, whatever the path is called.
        documents_root = os.path.abspath(CONFIG.documents_dir)
        if candidate == documents_root or candidate.startswith(documents_root + os.sep):
            return None
        parts = os.path.relpath(candidate, CONFIG.repo_root).split(os.sep)
        if any(part.startswith(".") for part in parts):
            return None
        if parts[0] == "server":
            return None
        if candidate.lower().endswith((".db", ".py", ".md")):
            return None
        if not os.path.isfile(candidate):
            return None
        return candidate

    # ---- mock checkout page -----------------------------------------------
    def _serve_mock_checkout(self):
        if not CONFIG.allow_mock_billing:
            raise ApiError(404, "Not found.")
        page = MOCK_CHECKOUT_HTML.replace("__PLAN_NAME__", CONFIG.plan_name).replace(
            "__PLAN_PRICE__", CONFIG.plan_price_display
        )
        self._send(200, page, "text/html; charset=utf-8")


MOCK_CHECKOUT_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Secure Checkout (local simulation)</title>
<style>
  body{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
       background:#101a36;color:#e2e8f0;display:flex;min-height:100vh;
       align-items:center;justify-content:center;padding:24px}
  .card{background:#fff;color:#0f172a;border-radius:16px;max-width:440px;width:100%;
        padding:32px;box-shadow:0 20px 50px rgba(2,6,23,.45)}
  h1{font-size:18px;margin:0 0 4px}
  .muted{color:#64748b;font-size:13px;margin:6px 0 18px;line-height:1.5}
  .row{display:flex;justify-content:space-between;font-size:13px;padding:8px 0;
       border-bottom:1px solid #e2e8f0}
  button{margin-top:20px;width:100%;padding:12px;border:0;border-radius:10px;
         background:#1d6fd8;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
  button:hover{background:#1857ad}
  .badge{display:inline-block;background:#d9ecff;color:#1857ad;font-size:11px;
         font-weight:700;padding:3px 8px;border-radius:999px;margin-bottom:12px}
  pre{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px;
      font-size:11px;overflow:auto;white-space:pre-wrap;margin-top:14px}
</style>
</head>
<body>
<div class="card">
  <img src="/assets/logo-mark.svg" alt="Invigismart Pro" width="46" height="46" style="display:block;margin-bottom:10px">
  <span class="badge">LOCAL MOCK CHECKOUT</span>
  <h1>__PLAN_NAME__</h1>
  <p class="muted">No real payment is processed. This page emits the same subscription
     event the live Stripe webhook would, so the server-side entitlement flow can be
     exercised end to end.</p>
  <div class="row"><span>Plan</span><span>__PLAN_NAME__</span></div>
  <div class="row"><span>Price</span><span>__PLAN_PRICE__</span></div>
  <div class="row"><span>Billing period</span><span>12 months</span></div>
  <button id="pay">Simulate successful payment</button>
  <pre id="out"></pre>
</div>
<script>
var TOKEN_KEY = 'invigismart_token_v1';
var out = document.getElementById('out');
document.getElementById('pay').addEventListener('click', function () {
  var token = null;
  try { token = localStorage.getItem(TOKEN_KEY); } catch (e) {}
  if (!token) { out.textContent = 'Not signed in. Return to the app and sign in first.'; return; }
  out.textContent = 'Contacting server...';
  fetch('/api/billing/mock-activate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
    body: JSON.stringify({ days: 365 })
  }).then(function (res) {
    return res.json().then(function (data) { return { ok: res.ok, status: res.status, data: data }; });
  }).then(function (r) {
    if (!r.ok) { out.textContent = 'Failed: ' + ((r.data && r.data.error) || r.status); return; }
    out.textContent = 'Payment simulated. Subscription active for ' +
      r.data.subscription.daysLeft + ' days. Redirecting...';
    setTimeout(function () { window.location.href = '/app?checkout=success'; }, 900);
  }).catch(function (err) { out.textContent = 'Network error: ' + err; });
});
</script>
</body>
</html>
"""


def startup_banner(host, port):
    """What an operator sees when the service comes up.

    Printed in a single flush()ed write: stdout is block-buffered whenever it is
    not a terminal (which is exactly how systemd runs it), so a banner written
    line by line can sit invisible in the buffer for hours on a quiet server.
    """
    lines = [
        "Invigismart Pro backend listening on http://%s:%d" % (host, port),
        "  base url        : %s%s" % (
            CONFIG.app_base_url,
            "  (public host: production mode)" if CONFIG.public_host else "",
        ),
        "  database        : %s" % CONFIG.db_path,
        "  documents       : %s (max %s per file, %d per account)" % (
            CONFIG.documents_dir,
            _megabytes(CONFIG.max_document_bytes),
            CONFIG.max_documents_per_user,
        ),
        "  gateway         : %s%s" % (
            CONFIG.billing_provider,
            "" if CONFIG.billing_configured else "  (NOT configured: cannot take a payment)",
        ),
        "  payfast         : %s" % (
            "sandbox" if CONFIG.payfast_sandbox else (
                "live" if CONFIG.payfast_configured else "NOT configured"
            )
        ),
        "  stripe          : %s" % ("configured" if CONFIG.stripe_configured else "NOT configured"),
        "  webhook secret  : %s" % (
            "configured" if CONFIG.stripe_webhook_secret else "NOT configured"
        ),
        "  mock billing    : %s" % ("enabled" if CONFIG.allow_mock_billing else "disabled"),
    ]
    if CONFIG.billing_provider == "payfast":
        lines.append("  itn verification: %s" % CONFIG.payfast_itn_verification)
    if CONFIG.jwt_secret_is_ephemeral:
        lines.append("  WARNING         : JWT_SECRET unset - sessions reset on every restart.")
    for warning in CONFIG.billing_warnings:
        lines.append("  WARNING         : %s" % warning)
    return lines


def main():
    if CONFIG.billing_fatal:
        print("Refusing to start: unsafe production billing configuration.", file=sys.stderr)
        for problem in CONFIG.billing_fatal:
            print("  * %s" % problem, file=sys.stderr)
        print(
            "  Fix the above, or set ALLOW_INSECURE_BILLING=1 for a throwaway staging host.",
            file=sys.stderr,
        )
        return 2
    host, port = CONFIG.host, CONFIG.port
    httpd = ThreadingHTTPServer((host, port), Handler)
    print("\n".join(startup_banner(host, port)), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
