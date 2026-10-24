"""End-to-end tests for the Invigismart Pro backend.

Boots the real server in a subprocess against a throwaway SQLite database and
drives it over HTTP, so the tests exercise the actual auth, billing, webhook and
entitlement paths. No third-party packages required:

    python3 server/tests/test_api.py
"""
import datetime
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(HERE)
APP = os.path.join(SERVER_DIR, "app.py")

# The server modules are imported by path, not installed as a package.
sys.path.insert(0, SERVER_DIR)
import billing  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import documents  # noqa: E402
import payfast  # noqa: E402
import roster  # noqa: E402

WEBHOOK_SECRET = "whsec_test_secret"

# PayFast test material. The two signature vectors in PayFastSignatureTests are
# published by PayFast's own integration tests, so they pin the algorithm.
PAYFAST_MERCHANT_ID = "10033543"
PAYFAST_MERCHANT_KEY = "34xw0ot2cjz69"
PAYFAST_PASSPHRASE = "Swf7w0rd15753"
PAYFAST_SIGNATURE_VECTOR = [
    ("merchant_id", "10033543"),
    ("merchant_key", "34xw0ot2cjz69"),
    ("return_url", "https://www.youtube.com/watch?v=ZbZSe6N_BXs&list=RDZbZSe6N_BXs&start_radio=1"),
    ("cancel_url", "https://www.youtube.com/watch?v=ZbZSe6N_BXs&list=RDZbZSe6N_BXs&start_radio=1"),
    ("notify_url", "https://webhook.site/ca168de6-e226-4397-9a2c-8baf79fbb336"),
    ("name_first", "Test"),
    ("name_last", "Test Doe"),
    ("email_address", "test1234@test.com"),
    ("cell_number", "0230000000"),
    ("m_payment_id", "123456"),
    ("amount", "100.00"),
    ("item_name", "Product"),
    ("item_description", "Product Item"),
]

# A complete production-shaped PayFast environment.
PAYFAST_PRODUCTION = {
    "APP_BASE_URL": "https://roster.example.edu",
    "JWT_SECRET": "payfast-guard-secret",
    "BILLING_PROVIDER": "payfast",
    "PAYFAST_MERCHANT_ID": PAYFAST_MERCHANT_ID,
    "PAYFAST_MERCHANT_KEY": PAYFAST_MERCHANT_KEY,
    "PAYFAST_PASSPHRASE": PAYFAST_PASSPHRASE,
    "PAYFAST_SANDBOX": "0",
    "ALLOW_MOCK_BILLING": "0",
    "PLAN_PRICE_AMOUNT": "1200.00",
}
TMP_DIR = None
PROCESS = None
BASE = None
STDERR_LOG = None


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def request(method, path, body=None, token=None, extra_headers=None, raw_body=None, base=None):
    headers = dict(extra_headers or {})
    data = None
    if raw_body is not None:
        data = raw_body
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        (base or BASE) + path, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(payload)
            except ValueError:
                return resp.status, payload
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = {"raw": payload}
        return exc.code, parsed


def stripe_signature(payload, secret=WEBHOOK_SECRET, timestamp=None):
    ts = int(timestamp or time.time())
    signed = str(ts).encode("ascii") + b"." + payload
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (ts, digest)


def signup(school="Westridge High", email="admin@westridge.edu", password="secret123"):
    status, data = request(
        "POST",
        "/api/auth/signup",
        {"school": school, "email": email, "password": password},
    )
    return status, data


PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
)


def pdf_of_size(size):
    """A valid-looking PDF padded to exactly ``size`` bytes."""
    if size < len(PDF_BYTES):
        return PDF_BYTES[:size]
    return PDF_BYTES + b"x" * (size - len(PDF_BYTES))


def multipart_upload(
    filename="exam-timetable.pdf",
    data=PDF_BYTES,
    category="exam_timetable",
    title="October exams",
    content_type="application/pdf",
    boundary="----InvigismartProTestBoundary",
):
    """Build a browser-shaped multipart/form-data upload body."""
    chunks = []
    for name, value in (("title", title), ("category", category)):
        if value is None:
            continue
        chunks.append((
            "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
            % (boundary, name, value)
        ).encode("utf-8"))
    chunks.append((
        "--%s\r\nContent-Disposition: form-data; name=\"file\";"
        " filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
        % (boundary, filename, content_type)
    ).encode("utf-8"))
    chunks.append(data)
    chunks.append(("\r\n--%s--\r\n" % boundary).encode("ascii"))
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def sample_payload():
    teachers = [
        {"id": "t1", "name": "Ada Lovelace", "subjects": ["Maths"]},
        {"id": "t2", "name": "Alan Turing", "subjects": ["Computing"]},
        {"id": "t3", "name": "Grace Hopper", "subjects": ["Physics"]},
    ]
    exams = [
        {"id": "e1", "date": "2026-06-01", "day": "MON", "period": "AM",
         "subject": "Maths", "students": 90},
        {"id": "e2", "date": "2026-06-02", "day": "TUE", "period": "AM",
         "subject": "Physics", "students": 45},
    ]
    schedules = [{"id": "s1", "teacherId": "t3", "day": "TUE", "period": "AM", "klass": "12A"}]
    return {"teachers": teachers, "exams": exams, "schedules": schedules}


def days_between(earlier, later):
    """Whole days between two ISO-8601 timestamps as the app writes them."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        datetime.datetime.strptime(later, fmt) - datetime.datetime.strptime(earlier, fmt)
    ).days


def payfast_itn(user_id, payment_id, amount="1200.00", status="COMPLETE", token="tok_annual_1"):
    """A PayFast ITN shaped like the real thing, signed the way PayFast signs."""
    fields = {
        "m_payment_id": "inv-%s-1770000000" % user_id,
        "pf_payment_id": str(payment_id),
        "payment_status": status,
        "item_name": "Annual School Unlimited",
        "item_description": "R1200 / year",
        "amount_gross": amount,
        "amount_fee": "-30.00",
        "amount_net": "1170.00",
        "custom_str1": user_id,
        "custom_str2": "annual_school_unlimited",
        "name_first": "Westridge High",
        "email_address": "billing@westridge.edu",
        "merchant_id": PAYFAST_MERCHANT_ID,
        "token": token,
        "subscription_type": "1",
    }
    fields["signature"] = payfast.signature(fields, PAYFAST_PASSPHRASE)
    return fields


def post_itn(fields, base=None, signature=None):
    """POST an ITN the way PayFast does: form-encoded, never JSON."""
    if signature is not None:
        fields = dict(fields, signature=signature)
    body = urllib.parse.urlencode(fields).encode("utf-8")
    return request(
        "POST",
        "/api/billing/webhook",
        raw_body=body,
        extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
        base=base,
    )


def setUpModule():
    global TMP_DIR, PROCESS, BASE, STDERR_LOG
    TMP_DIR = tempfile.mkdtemp(prefix="invigismart-test-")
    port = free_port()
    BASE = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env.update({
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "DB_PATH": os.path.join(TMP_DIR, "test.db"),
        "DOCUMENTS_DIR": os.path.join(TMP_DIR, "documents"),
        "JWT_SECRET": "test-jwt-secret",
        "STRIPE_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "ALLOW_MOCK_BILLING": "1",
        "TRIAL_DAYS": "14",
    })
    STDERR_LOG = open(os.path.join(TMP_DIR, "server.log"), "w+")
    PROCESS = subprocess.Popen(
        [sys.executable, APP], env=env, stdout=STDERR_LOG, stderr=STDERR_LOG
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if PROCESS.poll() is not None:
            STDERR_LOG.seek(0)
            raise RuntimeError("server exited early:\n" + STDERR_LOG.read())
        try:
            status, _ = request("GET", "/api/health")
            if status == 200:
                return
        except Exception:
            time.sleep(0.15)
    raise RuntimeError("server did not become healthy in time")


def tearDownModule():
    if PROCESS and PROCESS.poll() is None:
        PROCESS.terminate()
        try:
            PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            PROCESS.kill()
    if STDERR_LOG:
        STDERR_LOG.close()


def spawn_server(extra_env):
    """Boot a throwaway server instance with a custom environment.

    The production-guard tests need configurations the shared test server must
    never run with (a public https host, mock endpoints force-disabled, a
    deliberately incomplete Stripe setup), so each one gets its own process and
    its own database.
    """
    tmp = tempfile.mkdtemp(prefix="invigismart-extra-")
    port = free_port()
    env = dict(os.environ)
    env.update({
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "DB_PATH": os.path.join(tmp, "extra.db"),
        "DOCUMENTS_DIR": os.path.join(tmp, "documents"),
    })
    env.update(extra_env)
    log = open(os.path.join(tmp, "extra.log"), "w+")
    process = subprocess.Popen([sys.executable, APP], env=env, stdout=log, stderr=log)
    return process, "http://127.0.0.1:%d" % port, log


def stop_server(process, log):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    log.close()


def wait_healthy(base, process, log, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            log.seek(0)
            raise AssertionError("server exited early:\n" + log.read())
        try:
            status, _ = request("GET", "/api/health", base=base)
            if status == 200:
                return
        except Exception:
            time.sleep(0.15)
    raise AssertionError("server did not become healthy in time")


class AuthTests(unittest.TestCase):
    def test_health_reports_configuration(self):
        status, data = request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["config"]["mockBilling"])
        self.assertFalse(data["config"]["stripeConfigured"])
        # The plan copy is served from config so the pages never hard-code it.
        self.assertEqual(data["config"]["planName"], "Annual School Unlimited")
        self.assertEqual(data["config"]["planPrice"], "R1200 / year")
        # Contact details are served the same way, so a school sees one address
        # to ask about a payment wherever they are on the site.
        self.assertEqual(data["config"]["supportEmail"], "daniel@invigismart.online")
        self.assertEqual(data["config"]["supportPhone"], "+27 76 319 3749")
        self.assertEqual(data["config"]["supportPhoneDial"], "+27763193749")
        self.assertEqual(data["config"]["supportWhatsappUrl"], "https://wa.me/27763193749")

    def test_signup_grants_trial_and_generates_roster(self):
        status, data = signup(email="trial@example.com")
        self.assertEqual(status, 201)
        self.assertTrue(data["token"])
        self.assertEqual(data["user"]["email"], "trial@example.com")
        self.assertTrue(data["subscription"]["entitled"])
        self.assertEqual(data["subscription"]["source"], "trial")

        status, result = request(
            "POST", "/api/roster/generate", sample_payload(), token=data["token"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["summary"]["exams"], 2)
        self.assertEqual(len(result["assignments"]), 2)

        # Rule 2: Ada (Maths) never invigilates her own subject.
        maths = next(a for a in result["assignments"] if a["examId"] == "e1")
        self.assertNotIn("t1", maths["teacherIds"])
        # Rule 3: Grace is teaching Physics on TUE AM, so she is excluded.
        physics = next(a for a in result["assignments"] if a["examId"] == "e2")
        self.assertNotIn("t3", physics["teacherIds"])
        # e1 needs 3 invigilators, only 2 were eligible -> one warning.
        self.assertEqual(len(result["warnings"]), 1)
        self.assertTrue(result["generatedAt"])

    def test_signup_validation_and_duplicate_email(self):
        status, _ = signup(email="shortpw@example.com", password="123")
        self.assertEqual(status, 400)
        status, _ = signup(email="not-an-email", password="secret123")
        self.assertEqual(status, 400)
        status, _ = signup(email="dupe@example.com", password="secret123")
        self.assertEqual(status, 201)
        status, data = signup(email="DUPE@example.com", password="secret123")
        self.assertEqual(status, 409)

    def test_login_flow(self):
        status, _ = signup(email="login@example.com", password="secret123")
        self.assertEqual(status, 201)
        status, _ = request(
            "POST", "/api/auth/login",
            {"email": "login@example.com", "password": "definitely-wrong"},
        )
        self.assertEqual(status, 401)
        status, data = request(
            "POST", "/api/auth/login",
            {"email": "LOGIN@example.com", "password": "secret123"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["token"])
        status, me = request("GET", "/api/me", token=data["token"])
        self.assertEqual(status, 200)
        self.assertEqual(me["user"]["email"], "login@example.com")

    def test_password_hash_is_never_exposed(self):
        status, data = signup(email="privacy@example.com")
        self.assertEqual(status, 201)
        self.assertNotIn("pw_hash", json.dumps(data))
        self.assertNotIn("secret123", json.dumps(data))

    def test_authentication_required(self):
        for path, body in (("/api/me", None), ("/api/roster/generate", sample_payload())):
            status, _ = request("POST" if body else "GET", path, body)
            self.assertEqual(status, 401, msg=path)
        status, _ = request("GET", "/api/me", token="not.a.token")
        self.assertEqual(status, 401)

    def test_tampered_token_is_rejected(self):
        status, data = signup(email="tamper@example.com")
        header, payload, signature = data["token"].split(".")
        forged = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB")
        status, _ = request(
            "GET", "/api/me", token="%s.%s.%s" % (header, forged, signature)
        )
        self.assertEqual(status, 401)


class RosterSubjectAliasTests(unittest.TestCase):
    """A timetable grid stores full subject names, but exams often use codes.

    The no-own-subject rule has to survive that mismatch, otherwise a teacher
    who teaches "English Home Language" is silently rostered onto an "ENGHL"
    paper simply because the strings differ.
    """

    def test_codes_and_short_names_do_not_bypass_the_own_subject_rule(self):
        teachers = [
            {"id": "t1", "name": "Ada", "subjects": ["English Home Language"]},
            {"id": "t2", "name": "Brian", "subjects": ["Maths Lit"]},
            {"id": "t3", "name": "Cara", "subjects": ["Physical Sciences"]},
            {"id": "t4", "name": "Dumi", "subjects": ["History"]},
        ]
        exams = [
            {"id": "e1", "day": "MON", "period": "AM", "subject": "ENGHL", "students": 30},
            {"id": "e2", "day": "MON", "period": "PM", "subject": "Mathematical Literacy Gr12", "students": 30},
            {"id": "e3", "day": "TUE", "period": "AM", "subject": "Physics", "students": 30},
        ]
        result = roster.generate_roster(teachers, [], exams)
        assigned = {a["examId"]: a["teacherIds"] for a in result["assignments"]}

        self.assertTrue(assigned["e1"])
        self.assertNotIn("t1", assigned["e1"])
        self.assertNotIn("t2", assigned["e2"])
        self.assertNotIn("t3", assigned["e3"])
        # The History teacher is unrelated to all three papers, so the solver
        # still has someone to roster and does not report a shortfall.
        self.assertIn("t4", [tid for ids in assigned.values() for tid in ids])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["summary"]["assigned"], 3)

    def test_unknown_subjects_still_match_when_spelled_identically(self):
        teachers = [
            {"id": "t1", "name": "Ada", "subjects": ["Robotics"]},
            {"id": "t2", "name": "Brian", "subjects": ["Geography"]},
        ]
        exams = [
            {"id": "e1", "day": "TUE", "period": "PM", "subject": "Robotics Gr10", "students": 20},
        ]
        result = roster.generate_roster(teachers, [], exams, ratio=1)
        assigned = result["assignments"][0]["teacherIds"]

        self.assertNotIn("t1", assigned)
        self.assertIn("t2", assigned)

    def test_subject_key_drops_grade_markers_and_joins_aliases(self):
        self.assertEqual(roster._subject_key("Mathematical Literacy Gr 12"), "mathematical literacy")
        self.assertEqual(roster._subject_key("Maths Lit"), "mathematical literacy")
        self.assertEqual(roster._subject_key("ENGHL"), "english")
        self.assertEqual(roster._subject_key("English First Additional Language"), "english")
        self.assertEqual(roster._subject_key("Robotics Gr10"), "robotics")
        self.assertEqual(roster._subject_key(""), "")


class BillingTests(unittest.TestCase):
    def _new_account(self, email):
        status, data = signup(email=email)
        self.assertEqual(status, 201)
        return data["user"]["id"], data["token"]

    def test_expired_subscription_blocks_roster_generation(self):
        _, token = self._new_account("expired@example.com")
        status, me = request("POST", "/api/billing/dev-expire", token=token)
        self.assertEqual(status, 200)
        self.assertFalse(me["subscription"]["entitled"])
        status, data = request(
            "POST", "/api/roster/generate", sample_payload(), token=token
        )
        self.assertEqual(status, 402)
        self.assertEqual(data["code"], "subscription_required")

    def test_mock_checkout_reactivates_account(self):
        _, token = self._new_account("checkout@example.com")
        request("POST", "/api/billing/dev-expire", token=token)

        status, session = request("POST", "/api/billing/checkout", {}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(session["mode"], "mock")
        self.assertIn("/mock-checkout", session["url"])

        status, data = request(
            "POST", "/api/billing/mock-activate", {"days": 365}, token=token
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["subscription"]["entitled"])
        self.assertEqual(data["subscription"]["source"], "subscription")
        self.assertGreaterEqual(data["subscription"]["daysLeft"], 364)

        status, result = request(
            "POST", "/api/roster/generate", sample_payload(), token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(result["assignments"]), 2)

    def test_checkout_requires_authentication(self):
        status, _ = request("POST", "/api/billing/checkout", {})
        self.assertEqual(status, 401)


class TrialWatermarkTests(unittest.TestCase):
    """The evaluation window runs the real solver, but it never yields a
    usable artifact: the roster is flagged as a trial artifact and the clean,
    unwatermarked export is a paid-only path."""

    def _trial_account(self, email):
        status, data = signup(email=email)
        self.assertEqual(status, 201)
        return data["token"]

    def _export_body(self, assignments):
        payload = sample_payload()
        return {
            "teachers": payload["teachers"],
            "exams": payload["exams"],
            "assignments": assignments,
        }

    def test_trial_roster_is_flagged_as_a_trial_artifact(self):
        token = self._trial_account("marked@example.com")
        status, result = request(
            "POST", "/api/roster/generate", sample_payload(), token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["entitlement"]["source"], "trial")
        self.assertTrue(result["trialArtifact"])

    def test_paid_roster_is_not_flagged(self):
        token = self._trial_account("clean@example.com")
        status, _ = request(
            "POST", "/api/billing/mock-activate", {"days": 365}, token=token
        )
        self.assertEqual(status, 200)
        status, result = request(
            "POST", "/api/roster/generate", sample_payload(), token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["entitlement"]["source"], "subscription")
        self.assertFalse(result["trialArtifact"])

    def test_trial_account_cannot_fetch_the_clean_export(self):
        token = self._trial_account("nofile@example.com")
        status, body = request(
            "POST", "/api/roster/export", self._export_body([]), token=token
        )
        self.assertEqual(status, 402)
        self.assertEqual(body["code"], "subscription_required")

    def test_lapsed_licence_also_loses_the_clean_export(self):
        token = self._trial_account("lapsed@example.com")
        status, _ = request("POST", "/api/billing/dev-expire", token=token)
        self.assertEqual(status, 200)
        status, body = request(
            "POST", "/api/roster/export", self._export_body([]), token=token
        )
        self.assertEqual(status, 402)
        self.assertEqual(body["code"], "subscription_required")

    def test_paying_school_gets_a_clean_csv(self):
        token = self._trial_account("paid@example.com")
        request("POST", "/api/billing/mock-activate", {"days": 365}, token=token)
        status, result = request(
            "POST", "/api/roster/generate", sample_payload(), token=token
        )
        self.assertEqual(status, 200)

        status, export = request(
            "POST", "/api/roster/export", self._export_body(result["assignments"]), token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(export["filename"], "invigilation_roster.csv")
        lines = export["csv"].split("\r\n")
        self.assertEqual(lines[0], "Date,Day,Period,Subject,Students,Required,Invigilators")
        # One header row plus one row per exam, with no evaluation stamp.
        self.assertEqual(len(lines), 1 + len(result["assignments"]))
        self.assertNotIn("TRIAL", export["csv"])
        self.assertIn("Ada Lovelace", export["csv"])
        # e1 is Maths (90 students) -> the 1:30 rule asks for 3 invigilators.
        self.assertIn("2026-06-01,MON,AM,Maths,90,3,", export["csv"])

    def test_export_requires_authentication(self):
        status, _ = request("POST", "/api/roster/export", self._export_body([]))
        self.assertEqual(status, 401)

    def test_export_validates_its_payload(self):
        token = self._trial_account("validate@example.com")
        request("POST", "/api/billing/mock-activate", {"days": 365}, token=token)
        status, body = request(
            "POST",
            "/api/roster/export",
            {"teachers": [{"id": "t1", "name": "Ada"}], "exams": [{"id": "e1"}], "assignments": "nope"},
            token=token,
        )
        self.assertEqual(status, 400)
        self.assertIn("assignments", body["error"])


class DocumentLibraryTests(unittest.TestCase):
    """The per-account PDF library: upload, list, download, delete."""

    def _session(self, email):
        status, data = signup(email=email)
        self.assertEqual(201, status)
        return data["token"]

    def _upload(self, token, base=None, **kwargs):
        body, content_type = multipart_upload(**kwargs)
        return request(
            "POST",
            "/api/documents",
            raw_body=body,
            token=token,
            extra_headers={"Content-Type": content_type},
            base=base,
        )

    def test_trial_account_can_upload_list_download_and_delete(self):
        # The library is reference material, not a generated artifact, so the
        # trial can use it. It is still account-scoped and server-verified.
        token = self._session("docs-owner@example.com")
        status, data = self._upload(token, title="Grade 12 exams", category="exam_timetable")
        self.assertEqual(201, status, msg=data)
        doc = data["document"]
        self.assertEqual("Grade 12 exams", doc["title"])
        self.assertEqual("exam_timetable", doc["category"])
        self.assertEqual("exam-timetable.pdf", doc["filename"])
        self.assertEqual(len(PDF_BYTES), doc["size"])
        self.assertEqual(1, data["limits"]["count"])
        doc_id = doc["id"]

        status, listing = request("GET", "/api/documents", token=token)
        self.assertEqual(200, status)
        self.assertEqual([doc_id], [row["id"] for row in listing["documents"]])
        self.assertEqual(1, listing["limits"]["count"])

        status, body = request("GET", "/api/documents/" + doc_id, token=token)
        self.assertEqual(200, status)
        self.assertTrue(body.startswith("%PDF-1.4"))

        status, body = request(
            "GET", "/api/documents/%s?download=1" % doc_id, token=token
        )
        self.assertEqual(200, status)
        self.assertTrue(body.startswith("%PDF-1.4"))

        status, removed = request("DELETE", "/api/documents/" + doc_id, token=token)
        self.assertEqual(200, status)
        self.assertTrue(removed["deleted"])
        status, _ = request("GET", "/api/documents/" + doc_id, token=token)
        self.assertEqual(404, status)
        status, listing = request("GET", "/api/documents", token=token)
        self.assertEqual([], listing["documents"])

    def test_upload_rejects_non_pdf_payloads(self):
        token = self._session("docs-bad@example.com")
        for case in (
            {"filename": "notes.txt", "content_type": "text/plain"},
            {"filename": "notes.pdf", "data": b"<html>not a pdf</html>"},
            {"filename": "notes.pdf", "data": b""},
        ):
            status, data = self._upload(token, **case)
            self.assertEqual(415, status, msg=case)
            self.assertEqual("pdf_required", data["code"], msg=case)
        status, listing = request("GET", "/api/documents", token=token)
        self.assertEqual([], listing["documents"])

    def test_unknown_category_is_rejected(self):
        token = self._session("docs-category@example.com")
        status, data = self._upload(token, category="holiday-photos")
        self.assertEqual(400, status)
        self.assertEqual("invalid_category", data["code"])

    def test_documents_are_private_to_the_owning_account(self):
        owner = self._session("docs-private-a@example.com")
        other = self._session("docs-private-b@example.com")
        status, data = self._upload(owner, title="Owner only")
        self.assertEqual(201, status)
        doc_id = data["document"]["id"]

        status, listing = request("GET", "/api/documents", token=other)
        self.assertEqual(200, status)
        self.assertEqual([], listing["documents"])

        # "Not yours" and "does not exist" must look identical.
        status, _ = request("GET", "/api/documents/" + doc_id, token=other)
        self.assertEqual(404, status)
        status, _ = request("DELETE", "/api/documents/" + doc_id, token=other)
        self.assertEqual(404, status)

        # ...and the owner still has the document.
        status, body = request("GET", "/api/documents/" + doc_id, token=owner)
        self.assertEqual(200, status)
        self.assertTrue(body.startswith("%PDF-1.4"))

    def test_document_endpoints_require_authentication(self):
        body, content_type = multipart_upload()
        status, _ = request(
            "POST",
            "/api/documents",
            raw_body=body,
            extra_headers={"Content-Type": content_type},
        )
        self.assertEqual(401, status)
        status, _ = request("GET", "/api/documents")
        self.assertEqual(401, status)
        unknown = "a" * 32
        status, _ = request("GET", "/api/documents/" + unknown)
        self.assertEqual(401, status)
        status, _ = request("DELETE", "/api/documents/" + unknown)
        self.assertEqual(401, status)


class DocumentLimitTests(unittest.TestCase):
    """Quotas and per-file limits, on a server configured small for the test."""

    @classmethod
    def setUpClass(cls):
        cls.process, cls.base, cls.log = spawn_server({
            "JWT_SECRET": "document-limit-secret",
            "ALLOW_MOCK_BILLING": "1",
            "MAX_DOCUMENT_BYTES": "1024",
            "MAX_DOCUMENTS_PER_USER": "2",
            "MAX_DOCUMENT_STORAGE_BYTES": "1500",
        })
        wait_healthy(cls.base, cls.process, cls.log)

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.process, cls.log)

    def _session(self, email):
        status, data = request(
            "POST",
            "/api/auth/signup",
            {"school": "Westridge High", "email": email, "password": "secret123"},
            base=self.base,
        )
        self.assertEqual(201, status)
        return data["token"]

    def _upload(self, token, **kwargs):
        body, content_type = multipart_upload(**kwargs)
        return request(
            "POST",
            "/api/documents",
            raw_body=body,
            token=token,
            extra_headers={"Content-Type": content_type},
            base=self.base,
        )

    def test_file_count_limit_is_enforced(self):
        token = self._session("doc-limit-count@example.com")
        for index in range(2):
            status, data = self._upload(token, title="Doc %d" % index, data=pdf_of_size(400))
            self.assertEqual(201, status, msg=data)
        status, data = self._upload(token, title="One too many", data=pdf_of_size(400))
        self.assertEqual(409, status)
        self.assertEqual("document_limit_reached", data["code"])

    def test_per_file_size_limit_is_enforced(self):
        token = self._session("doc-limit-size@example.com")
        status, data = self._upload(token, data=pdf_of_size(1100))
        self.assertEqual(413, status)
        self.assertEqual("document_too_large", data["code"])

    def test_storage_allowance_is_enforced(self):
        token = self._session("doc-limit-storage@example.com")
        status, _ = self._upload(token, title="First", data=pdf_of_size(900))
        self.assertEqual(201, status)
        status, data = self._upload(token, title="Second", data=pdf_of_size(700))
        self.assertEqual(409, status)
        self.assertEqual("storage_limit_reached", data["code"])


class DocumentHelperTests(unittest.TestCase):
    """The multipart parser and validation, without a running server."""

    def test_parser_keeps_binary_bytes_and_utf8_filenames(self):
        boundary = "----InvigismartHelper"
        pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
        head = (
            "--%s\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n"
            "Timetable \xc3\xb1\r\n"
            "--%s\r\nContent-Disposition: form-data; name=\"file\";"
            " filename=\"timetable-\xc3\xb1.pdf\"\r\n"
            "Content-Type: application/pdf\r\n\r\n" % (boundary, boundary)
        ).encode("latin-1")
        body = head + pdf + ("\r\n--%s--\r\n" % boundary).encode("ascii")
        parsed = documents.parse_multipart(
            "multipart/form-data; boundary=" + boundary, body
        )
        self.assertEqual("Timetable \xf1", parsed["fields"]["title"])
        part = parsed["files"][0]
        self.assertEqual("timetable-\xf1.pdf", part["filename"])
        self.assertEqual(pdf, part["data"])
        self.assertIsNone(documents.pdf_problem(part["data"]))

    def test_only_pdf_magic_is_accepted(self):
        self.assertIn("empty", documents.pdf_problem(b""))
        self.assertIsNotNone(documents.pdf_problem(b"<html>no</html>"))
        self.assertIsNone(documents.pdf_problem(b"%PDF-1.7\n..."))

    def test_filenames_cannot_escape_or_impersonate(self):
        self.assertEqual("timetable.pdf", documents.clean_filename("../../timetable.pdf"))
        # The sanitizer always yields a .pdf name; the API separately rejects a
        # non-PDF *original* extension before this runs, so a renamed .txt never
        # reaches storage.
        self.assertEqual("notes.txt.pdf", documents.clean_filename("notes.txt"))
        self.assertEqual("document.pdf", documents.clean_filename("..."))
        with self.assertRaises(ValueError):
            documents.storage_path("/tmp", "../etc", "a" * 32)
        with self.assertRaises(ValueError):
            documents.storage_path("/tmp", "a" * 32, "../../etc/passwd")

    def test_content_disposition_is_utf8_safe(self):
        header = documents.content_disposition("timetable-\xf1.pdf", attachment=True)
        self.assertTrue(header.startswith("attachment;"))
        self.assertIn("filename*=UTF-8''timetable-%C3%B1.pdf", header)


class DocumentStaticIsolationTests(unittest.TestCase):
    """A DOCUMENTS_DIR inside the checkout is still unreachable as a static file."""

    @classmethod
    def setUpClass(cls):
        cls.docs_dir = tempfile.mkdtemp(
            prefix="uploads-test-", dir=os.path.dirname(SERVER_DIR)
        )
        cls.process, cls.base, cls.log = spawn_server({
            "JWT_SECRET": "document-static-secret",
            "ALLOW_MOCK_BILLING": "1",
            "DOCUMENTS_DIR": cls.docs_dir,
        })
        wait_healthy(cls.base, cls.process, cls.log)

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.process, cls.log)
        shutil.rmtree(cls.docs_dir, ignore_errors=True)

    def test_uploaded_pdf_is_not_served_without_a_token(self):
        status, session = request(
            "POST",
            "/api/auth/signup",
            {"school": "Westridge High", "email": "doc-static@example.com",
             "password": "secret123"},
            base=self.base,
        )
        self.assertEqual(201, status)
        user_id, token = session["user"]["id"], session["token"]
        body, content_type = multipart_upload()
        status, data = request(
            "POST",
            "/api/documents",
            raw_body=body,
            token=token,
            extra_headers={"Content-Type": content_type},
            base=self.base,
        )
        self.assertEqual(201, status)

        doc_id = data["document"]["id"]
        relative = "/%s/%s/%s.pdf" % (
            os.path.basename(self.docs_dir), user_id, doc_id
        )
        status, _ = request("GET", relative, base=self.base)
        self.assertEqual(404, status)

        # The authenticated endpoint still serves it.
        status, downloaded = request(
            "GET", "/api/documents/" + doc_id, token=token, base=self.base
        )
        self.assertEqual(200, status)
        self.assertTrue(downloaded.startswith("%PDF-1.4"))


class WebhookTests(unittest.TestCase):
    def _post_webhook(self, event, signature):
        body = json.dumps(event).encode("utf-8")
        return request(
            "POST",
            "/api/billing/webhook",
            raw_body=body,
            extra_headers={
                "Content-Type": "application/json",
                "Stripe-Signature": signature if signature is not None else stripe_signature(body),
            },
        )

    def test_valid_webhook_updates_subscription(self):
        status, data = signup(email="webhook@example.com")
        self.assertEqual(status, 201)
        user_id, token = data["user"]["id"], data["token"]
        request("POST", "/api/billing/dev-expire", token=token)

        event = {
            "id": "evt_valid_1",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_1",
                    "customer": "cus_1",
                    "status": "active",
                    "current_period_end": int(time.time()) + 400 * 86400,
                    "metadata": {"user_id": user_id},
                }
            },
        }
        status, resp = self._post_webhook(event, None)
        self.assertEqual(status, 200)
        self.assertEqual(resp["action"], "updated")
        self.assertTrue(resp["subscription"]["entitled"])

        status, me = request("GET", "/api/me", token=token)
        self.assertTrue(me["subscription"]["entitled"])

        status, result = request("POST", "/api/roster/generate", sample_payload(), token=token)
        self.assertEqual(status, 200)

    def test_webhook_rejects_bad_signature(self):
        event = {"id": "evt_bad", "type": "customer.subscription.deleted",
                 "data": {"object": {}}}
        status, _ = self._post_webhook(
            event, "t=%d,v1=%s" % (int(time.time()), "0" * 64)
        )
        self.assertEqual(status, 400)

    def test_webhook_rejects_stale_signature(self):
        event = {"id": "evt_stale", "type": "invoice.paid", "data": {"object": {}}}
        body = json.dumps(event).encode("utf-8")
        stale = stripe_signature(body, timestamp=int(time.time()) - 4000)
        status, _ = self._post_webhook(event, stale)
        self.assertEqual(status, 400)

    def test_webhook_is_idempotent(self):
        status, data = signup(email="idempotent@example.com")
        self.assertEqual(status, 201)
        user_id, token = data["user"]["id"], data["token"]
        event = {
            "id": "evt_idem",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {"user_id": user_id},
                    "customer": "cus_idem",
                    "subscription": "sub_idem",
                    "current_period_end": int(time.time()) + 400 * 86400,
                }
            },
        }
        first_status, first = self._post_webhook(event, None)
        second_status, second = self._post_webhook(event, None)
        self.assertEqual(first_status, 200)
        self.assertEqual(first["action"], "activated")
        self.assertEqual(second_status, 200)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["action"], "duplicate")

    def test_subscription_deleted_revokes_access(self):
        status, data = signup(email="cancel@example.com")
        self.assertEqual(status, 201)
        user_id, token = data["user"]["id"], data["token"]

        activate = {
            "id": "evt_activate_cancel",
            "type": "customer.subscription.created",
            "data": {"object": {"id": "sub_c", "customer": "cus_c", "status": "active",
                                "current_period_end": int(time.time()) + 400 * 86400,
                                "metadata": {"user_id": user_id}}},
        }
        self._post_webhook(activate, None)
        status, me = request("GET", "/api/me", token=token)
        self.assertTrue(me["subscription"]["entitled"])

        cancel = {
            "id": "evt_delete_cancel",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_c", "customer": "cus_c", "status": "canceled",
                                "metadata": {"user_id": user_id}}},
        }
        self._post_webhook(cancel, None)
        status, me = request("GET", "/api/me", token=token)
        self.assertFalse(me["subscription"]["entitled"])
        status, _ = request("POST", "/api/roster/generate", sample_payload(), token=token)
        self.assertEqual(status, 402)


class _EmptyStore:
    """Minimal Store stand-in: an account that has never completed a checkout."""

    def get_subscription(self, user_id):
        return None


class BillingPortalTests(unittest.TestCase):
    """POST /api/billing/portal - self-service invoices and cancellation."""

    def test_portal_requires_authentication(self):
        status, data = request("POST", "/api/billing/portal")
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", data["code"])

    def test_portal_requires_configured_billing(self):
        # The shared test server runs in mock mode, so there is no Stripe customer
        # and no portal to open: the endpoint must say so instead of failing open.
        status, session = signup(school="Portal High", email="portal@example.edu")
        self.assertEqual(201, status)
        status, data = request("POST", "/api/billing/portal", token=session["token"])
        self.assertEqual(503, status)
        self.assertEqual("not_configured", data["code"])
        self.assertIn("not configured", data["error"])

    def test_portal_needs_a_linked_stripe_customer(self):
        # With Stripe configured but no checkout completed there is genuinely no
        # customer to manage, and the client - not the server - must act on that.
        portal_cfg = config.Config(env={
            "APP_BASE_URL": "https://roster.example.edu",
            "JWT_SECRET": "portal-test-secret",
            "STRIPE_SECRET_KEY": "sk_live_portal",
            "STRIPE_PRICE_ID": "price_portal",
            "STRIPE_WEBHOOK_SECRET": "whsec_portal",
        })
        with self.assertRaises(billing.BillingError) as caught:
            billing.create_portal_session(
                _EmptyStore(), {"id": "u1"}, portal_cfg
            )
        self.assertEqual("no_customer", caught.exception.code)


class FrontEndTests(unittest.TestCase):
    def test_landing_page_is_served_at_root(self):
        status, html = request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIsInstance(html, str)
        # The landing page must present the product, the pricing options and the
        # entry points for registering, buying and logging in.
        self.assertIn("Invigismart Pro", html)
        self.assertIn('id="pricing"', html)
        self.assertIn("Annual School Unlimited", html)
        self.assertIn('data-plan-price', html)
        self.assertIn("R1200 / year", html)
        self.assertIn("startTrial()", html)
        self.assertIn("buyNow()", html)
        self.assertIn('id="authForm"', html)
        # It must talk to the same API and share the dashboard's session key.
        self.assertIn("/api/auth/", html)
        self.assertIn("invigismart_token_v1", html)

    def test_dashboard_is_served_at_app(self):
        for path in ("/app", "/app/"):
            status, html = request("GET", path)
            self.assertEqual(status, 200, msg=path)
            self.assertIsInstance(html, str)
            self.assertIn("Exam Invigilation", html)
            self.assertIn('id="rosterOutput"', html)
            # The PDF document library is part of the dashboard, not a bolt-on
            # page: upload, list and the API client all ship with it.
            self.assertIn('id="content-documents"', html)
            self.assertIn('id="documentFile"', html)
            self.assertIn('accept="application/pdf,.pdf"', html)
            self.assertIn("uploadDocument(event)", html)
            self.assertIn("/api/documents", html)

    def test_login_and_signup_paths_serve_the_landing_page(self):
        for path in ("/login", "/signup"):
            status, html = request("GET", path)
            self.assertEqual(status, 200, msg=path)
            self.assertIn("Invigismart Pro", html)

    def test_terms_and_privacy_pages_are_served(self):
        # The signup consent checkbox and both footers link to these pages, so
        # the legal copy has to resolve at /terms and /privacy (with or without
        # a trailing slash) and carry the operator's real details.
        status, terms = request("GET", "/terms")
        self.assertEqual(status, 200)
        self.assertIn("Terms and Conditions of Service", terms)
        self.assertIn("Refund &amp; Cancellation Policy", terms)
        self.assertIn("Prinsloo Consulting", terms)
        self.assertIn("daniel@invigismart.online", terms)

        status, privacy = request("GET", "/privacy")
        self.assertEqual(status, 200)
        self.assertIn("Privacy Policy", privacy)
        self.assertIn("POPIA", privacy)
        self.assertIn("Information Officer", privacy)
        self.assertIn("daniel@invigismart.online", privacy)

        for path in ("/terms/", "/privacy/"):
            status, _ = request("GET", path)
            self.assertEqual(status, 200, msg=path)

    def test_landing_page_links_to_the_legal_pages(self):
        # The marketing footer and the register form must both reach the legal
        # pages, and signup must carry an explicit consent checkbox.
        status, html = request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('href="/terms"', html)
        self.assertIn('href="/privacy"', html)
        self.assertIn('id="authTerms"', html)
        self.assertIn("Please accept the Terms", html)

    def test_landing_page_walkthrough_covers_every_dashboard_tab(self):
        # The walkthrough is the self-service onboarding path, so it has to name
        # the real dashboard tabs, in the order a school meets them, and quote
        # the real button labels instead of inventing its own.
        status, html = request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('id="walkthrough"', html)
        self.assertIn('href="#walkthrough"', html)          # reachable from the nav
        for step in range(1, 8):
            self.assertIn('id="walk-step-%d"' % step, html, msg=step)
        for tab in (
            "Teachers &amp; Subjects",
            "Teacher Schedules",
            "Exam Timetable",
            "Roster Generator",
            "Teacher View",
            "Subscription &amp; Billing",
        ):
            self.assertIn(tab, html, msg=tab)

        # Every label the guide tells the user to look for must actually exist in
        # the dashboard, so the guide cannot drift away from the product.
        shared_labels = (
            "Add Busy Slot",
            "Add Exam Session",
            "Generate Balanced Roster",
            "Print Duty Sheet",
            "Export Roster CSV",
            "Load Sample Data",
        )
        status, app = request("GET", "/app")
        self.assertEqual(status, 200)
        for label in shared_labels:
            self.assertIn(label, html, msg=label)
            self.assertIn(label, app, msg=label)

    def test_mock_checkout_page_is_served(self):
        status, html = request("GET", "/mock-checkout")
        self.assertEqual(status, 200)
        self.assertIn("LOCAL MOCK CHECKOUT", html)
        # A simulated payment must land the customer back in the dashboard.
        self.assertIn("/app?checkout=success", html)

    def test_staff_timetable_importer_is_wired_into_the_dashboard(self):
        # The grid parser is a separate browser asset: both upload buttons share
        # it, and the schedules table exposes a per-teacher filter for the
        # thousand-odd busy slots a real school timetable produces.
        status, js = request("GET", "/assets/timetable-import.js")
        self.assertEqual(status, 200)
        self.assertIsInstance(js, str)
        self.assertIn("parseStaffTimetable", js)
        self.assertIn("isStaffTimetableGrid", js)
        self.assertIn("applyToWorkspace", js)

        status, html = request("GET", "/app")
        self.assertEqual(status, 200)
        self.assertIn("/assets/timetable-import.js", html)
        self.assertIn("importStaffTimetable(rows)", html)
        self.assertIn('id="scheduleFilter"', html)

    def test_brand_assets_are_served(self):
        # Every page links the logo mark (header/favicon); it must be served as SVG.
        status, body = request("GET", "/assets/logo-mark.svg")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, str)
        self.assertIn("<svg", body)
        self.assertIn("Invigismart Pro", body)
        status, body = request("GET", "/assets/logo.svg")
        self.assertEqual(status, 200)
        self.assertIn("Invigismart", body)

    def test_pages_use_the_brand_palette(self):
        for path in ("/", "/app", "/terms", "/privacy"):
            status, html = request("GET", path)
            self.assertEqual(status, 200, msg=path)
            self.assertIn("/assets/logo-mark.svg", html)
            self.assertIn("tailwind.config", html)

    def test_pages_publish_the_support_contact(self):
        # A school that cannot complete a payment, or that is locked out of a
        # lapsed account, has to find a human from whichever page they are on.
        for path in ("/", "/app", "/terms", "/privacy"):
            status, html = request("GET", path)
            self.assertEqual(status, 200, msg=path)
            self.assertIn("daniel@invigismart.online", html, msg=path)
            self.assertIn("+27 76 319 3749", html, msg=path)
            self.assertIn("mailto:daniel@invigismart.online", html, msg=path)
            self.assertIn("https://wa.me/27763193749", html, msg=path)
            # ...plus the hooks the server's copy is written into.
            self.assertIn("data-support-email", html, msg=path)
            self.assertIn("data-support-wa-link", html, msg=path)

    def test_unknown_api_endpoint_returns_404(self):
        status, _ = request("GET", "/api/nope")
        self.assertEqual(status, 404)

    def test_server_sources_and_database_are_not_public(self):
        for path in (
            "/server/app.py",
            "/server/db.py",
            "/server/tests/test_api.py",
            "/server/invigismart.db",
            "/.gitignore",
            "/.env",
            "/.env.example",
            "/.env.production.example",
            "/DEPLOY.md",
            "/README.md",
        ):
            status, _ = request("GET", path)
            self.assertEqual(status, 404, msg=path)


class SupportContactTests(unittest.TestCase):
    """The contact details printed across the site.

    They are served through /api/health exactly like the plan copy, so the
    landing page, the dashboard and the billing panel cannot drift apart, and an
    operator can change them without touching HTML.
    """

    def test_defaults_are_the_live_contact_details(self):
        cfg = config.Config(env={})
        self.assertEqual("daniel@invigismart.online", cfg.support_email)
        self.assertEqual("+27 76 319 3749", cfg.support_phone)
        self.assertEqual("+27763193749", cfg.support_phone_dial)
        self.assertEqual("https://wa.me/27763193749", cfg.support_whatsapp_url)

    def test_phone_is_dialable_however_it_is_written(self):
        # A tel:/wa.me link needs the international form, so the operator should
        # be able to write the number the way it appears on a business card.
        for written in ("076 319 3749", "0763193749", "+27 76 319 3749",
                        "+27763193749", "27763193749", "0027 76 319 3749",
                        "(076) 319-3749"):
            cfg = config.Config(env={"SUPPORT_PHONE": written})
            self.assertEqual("+27763193749", cfg.support_phone_dial, msg=written)
            self.assertEqual(
                "https://wa.me/27763193749", cfg.support_whatsapp_url, msg=written
            )

    def test_blank_values_fall_back_to_the_live_details(self):
        cfg = config.Config(env={"SUPPORT_EMAIL": "   ", "SUPPORT_PHONE": ""})
        self.assertEqual("daniel@invigismart.online", cfg.support_email)
        self.assertEqual("+27763193749", cfg.support_phone_dial)

    def test_both_details_can_be_overridden(self):
        cfg = config.Config(env={
            "SUPPORT_EMAIL": "help@westridge.edu",
            "SUPPORT_PHONE": "+27 11 111 1111",
        })
        self.assertEqual("help@westridge.edu", cfg.describe()["supportEmail"])
        self.assertEqual("https://wa.me/27111111111", cfg.describe()["supportWhatsappUrl"])

    def test_the_running_server_serves_the_configured_details(self):
        process, base, log = spawn_server({
            "SUPPORT_EMAIL": "billing@westridge.edu",
            "SUPPORT_PHONE": "021 555 0100",
        })
        try:
            wait_healthy(base, process, log)
            status, data = request("GET", "/api/health", base=base)
            self.assertEqual(status, 200)
            cfg = data["config"]
            self.assertEqual("billing@westridge.edu", cfg["supportEmail"])
            self.assertEqual("021 555 0100", cfg["supportPhone"])
            # A Cape Town landline ("021 ...") must still become dialable.
            self.assertEqual("+27215550100", cfg["supportPhoneDial"])
        finally:
            stop_server(process, log)


class ConfigGuardTests(unittest.TestCase):
    """The production billing guard.

    server/config.py treats any public https host as production, because that is
    where the two silent failures live: a school pays and the licence never
    unlocks, or a signed-in visitor mints themself a free year through the mock
    endpoints. These tests pin the rules down in-process and then prove them end
    to end by booting real servers with deliberately broken environments.
    """

    PRODUCTION = {
        "APP_BASE_URL": "https://roster.example.edu",
        "JWT_SECRET": "guard-test-jwt-secret",
        "STRIPE_SECRET_KEY": "sk_live_guard",
        "STRIPE_PRICE_ID": "price_guard",
        "STRIPE_WEBHOOK_SECRET": "whsec_guard",
    }

    def config_for(self, overrides):
        env = dict(self.PRODUCTION)
        env.update(overrides)
        for name in [k for k, v in env.items() if v is None]:
            del env[name]
        return config.Config(env=env)

    def test_local_http_development_stays_permissive(self):
        cfg = config.Config(env={})
        self.assertFalse(cfg.public_host)
        self.assertEqual([], cfg.billing_fatal)
        self.assertTrue(cfg.allow_mock_billing)
        self.assertEqual("", cfg.stripe_webhook_secret)

    def test_complete_production_configuration_is_clean(self):
        cfg = self.config_for({})
        self.assertTrue(cfg.public_host)
        self.assertTrue(cfg.stripe_configured)
        self.assertEqual([], cfg.billing_fatal)
        self.assertFalse(cfg.allow_mock_billing)

    def test_public_https_without_webhook_secret_is_fatal(self):
        # The webhook secret is no longer borrowed from the API key, so its
        # absence is caught here instead of turning every delivery into a 400.
        cfg = self.config_for({"STRIPE_WEBHOOK_SECRET": None})
        self.assertTrue(cfg.stripe_configured)
        self.assertEqual("", cfg.stripe_webhook_secret)
        self.assertIn("STRIPE_WEBHOOK_SECRET", " ".join(cfg.billing_fatal))

    def test_public_https_without_stripe_is_fatal(self):
        cfg = self.config_for({
            "STRIPE_SECRET_KEY": None,
            "STRIPE_PRICE_ID": None,
            "STRIPE_WEBHOOK_SECRET": None,
        })
        self.assertFalse(cfg.stripe_configured)
        self.assertTrue(cfg.billing_fatal)

    def test_public_https_without_jwt_secret_is_fatal(self):
        cfg = self.config_for({"JWT_SECRET": None})
        self.assertIn("JWT_SECRET", " ".join(cfg.billing_fatal))

    def test_public_https_always_disables_mock_billing(self):
        cfg = self.config_for({"ALLOW_MOCK_BILLING": "1", "ALLOW_INSECURE_BILLING": "1"})
        self.assertFalse(cfg.allow_mock_billing)
        self.assertEqual([], cfg.billing_fatal)     # waived on purpose
        self.assertTrue(cfg.billing_warnings)

    def test_incomplete_production_config_refuses_to_boot(self):
        broken = dict(self.PRODUCTION)
        del broken["STRIPE_WEBHOOK_SECRET"]
        process, base, log = spawn_server(broken)
        try:
            code = process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            stop_server(process, log)
            self.fail("server started on %s despite a fatal billing config" % base)
        self.assertEqual(2, code)
        log.seek(0)
        output = log.read()
        log.close()
        self.assertIn("Refusing to start", output)
        self.assertIn("STRIPE_WEBHOOK_SECRET", output)

    def test_insecure_override_still_hides_the_mock_endpoints(self):
        process, base, log = spawn_server({
            "APP_BASE_URL": "https://staging.example.edu",
            "JWT_SECRET": "guard-test-jwt-secret",
            "ALLOW_MOCK_BILLING": "1",
            "ALLOW_INSECURE_BILLING": "1",
        })
        try:
            wait_healthy(base, process, log)
            # The banner is flushed on boot, so whoever is reading journalctl
            # sees the effective billing mode immediately instead of after the
            # next 8 KB of output.
            log.seek(0)
            banner = log.read()
            self.assertIn("public host: production mode", banner)
            self.assertIn("mock billing    : disabled", banner)
            status, data = request("GET", "/api/health", base=base)
            self.assertEqual(200, status)
            self.assertTrue(data["config"]["publicHost"])
            self.assertFalse(data["config"]["mockBilling"])
            # No amount of ALLOW_MOCK_BILLING may unlock a free licence here.
            status, _ = request("GET", "/mock-checkout", base=base)
            self.assertEqual(404, status)
            status, session = request(
                "POST",
                "/api/auth/signup",
                {"school": "Guard High", "email": "guard@example.edu", "password": "secret123"},
                base=base,
            )
            self.assertEqual(201, status)
            status, body = request(
                "POST", "/api/billing/mock-activate", {}, token=session["token"], base=base
            )
            self.assertEqual(403, status)
            self.assertIn("disabled", body["error"])
            # An unsigned webhook body must also be refused, because this host is
            # not in mock mode even though no Stripe keys are present.
            status, _ = request(
                "POST",
                "/api/billing/webhook",
                raw_body=b'{"id":"evt_forged","type":"customer.subscription.deleted"}',
                extra_headers={"Content-Type": "application/json"},
                base=base,
            )
            self.assertEqual(400, status)
        finally:
            stop_server(process, log)


class PayFastSignatureTests(unittest.TestCase):
    """The signature is the one thing that has to be byte-perfect.

    Both vectors are the ones PayFast's own integration tests assert, so this
    pins the algorithm - including PHP's urlencode quirks (a space becomes "+",
    and "~" is escaped, unlike Python's quote_plus).
    """

    def test_published_vector_with_passphrase(self):
        self.assertEqual(
            "afced4f3501fb6a8362e436c90611563",
            payfast.signature(PAYFAST_SIGNATURE_VECTOR, PAYFAST_PASSPHRASE),
        )

    def test_published_vector_without_passphrase(self):
        self.assertEqual(
            "5c3a00245be1906d733293cb5a060a06",
            payfast.signature(PAYFAST_SIGNATURE_VECTOR, ""),
        )

    def test_signature_ignores_empty_values(self):
        # PayFast signs only the fields that are actually sent, so a field that is
        # sent empty must produce the same digest as a field that is omitted.
        present = dict(PAYFAST_SIGNATURE_VECTOR)
        omitted = {k: v for k, v in present.items() if k != "cell_number"}
        self.assertEqual(
            payfast.signature(omitted, PAYFAST_PASSPHRASE),
            payfast.signature(dict(omitted, cell_number=""), PAYFAST_PASSPHRASE),
        )

    def test_php_urlencode_matches_php(self):
        self.assertEqual("Annual+School+Unlimited", payfast.php_urlencode("Annual School Unlimited"))
        self.assertEqual("admin%40westridge.edu", payfast.php_urlencode("admin@westridge.edu"))
        self.assertEqual("R199%2Fyear%3B+50%25", payfast.php_urlencode("R199/year; 50%"))
        self.assertEqual("%7Etilde%7E", payfast.php_urlencode("~tilde~"))


class PayFastCheckoutTests(unittest.TestCase):
    """The signed payment form: annual ZAR, and the account carried out in it."""

    def setUp(self):
        self.cfg = config.Config(env=dict(PAYFAST_PRODUCTION, PAYFAST_SANDBOX="1"))
        self.user = {"id": "u1", "school": "Westridge High", "email": "admin@westridge.edu"}

    def test_annual_subscription_form_is_complete_and_signed(self):
        fields = payfast.checkout_fields(self.cfg, self.user, now=1770000000)
        self.assertEqual("6", fields["frequency"])            # annual
        self.assertEqual("0", fields["cycles"])               # until cancelled
        self.assertEqual("1", fields["subscription_type"])
        self.assertEqual("1200.00", fields["amount"])
        self.assertEqual("1200.00", fields["recurring_amount"])
        self.assertEqual("u1", fields["custom_str1"])
        self.assertTrue(fields["m_payment_id"].startswith("inv-u1-"))
        self.assertEqual("https://roster.example.edu/api/billing/webhook", fields["notify_url"])
        # Documented order, with the signature appended last.
        expected = [k for k in payfast.CHECKOUT_FIELD_ORDER if k in fields] + ["signature"]
        self.assertEqual(expected, list(fields))
        unsigned = {k: v for k, v in fields.items() if k != "signature"}
        self.assertEqual(payfast.signature(unsigned, PAYFAST_PASSPHRASE), fields["signature"])

    def test_sandbox_and_live_endpoints(self):
        self.assertEqual(
            "https://sandbox.payfast.co.za/eng/process", payfast.process_url(self.cfg)
        )
        live = config.Config(env=dict(PAYFAST_PRODUCTION, PAYFAST_SANDBOX="0"))
        self.assertEqual("https://www.payfast.co.za/eng/process", payfast.process_url(live))
        self.assertEqual("https://api.payfast.co.za", payfast.api_base(live))

    def test_cancel_signs_content_length_when_there_is_no_body(self):
        seen = {}

        def stub(url, method, headers, payload):
            seen.update({"url": url, "method": method, "headers": headers})
            return 200, "{}"

        payfast.cancel_subscription(self.cfg, "tok 1", http_request=stub)
        self.assertEqual(
            "https://sandbox.payfast.co.za/subscriptions/tok%201/cancel?testing=true",
            seen["url"],
        )
        self.assertEqual("PUT", seen["method"])
        # PayFast's own plugin signs content-length=0 for a bodiless cancel.
        self.assertEqual("0", seen["headers"]["content-length"])
        signed = {
            k: v for k, v in seen["headers"].items() if k not in ("signature", "Content-Length")
        }
        self.assertEqual(
            seen["headers"]["signature"], payfast.api_signature(signed, {}, PAYFAST_PASSPHRASE)
        )

    def test_unreachable_gateway_raises_instead_of_pretending(self):
        def refuse(*_args):
            raise OSError("gateway down")

        with self.assertRaises(payfast.PayfastError) as caught:
            payfast.cancel_subscription(self.cfg, "tok", http_request=refuse)
        self.assertEqual("upstream", caught.exception.code)

    def test_form_needs_credentials(self):
        with self.assertRaises(payfast.PayfastError) as caught:
            payfast.checkout_fields(config.Config(env={}), self.user)
        self.assertEqual("not_configured", caught.exception.code)


class PayFastCancelTests(unittest.TestCase):
    """cancel_licence stops the next charge and keeps the paid-for period."""

    def _store_with_licence(self, name):
        store = db.Store(os.path.join(TMP_DIR, name))
        user = store.create_user("Westridge High", "cancel@example.com", "hash", None)
        store.upsert_subscription(
            user["id"],
            status="active",
            current_period_end="2099-01-01T00:00:00Z",
            stripe_subscription_id="tok_annual_1",
            provider="payfast",
        )
        return store, user

    def test_cancel_stops_renewal_but_keeps_access(self):
        cfg = config.Config(env=dict(PAYFAST_PRODUCTION))
        store, user = self._store_with_licence("cancel.db")
        called = []

        def stub(_cfg, token):
            called.append(token)
            return {}

        result = billing.cancel_licence(store, user, cfg, cancel_call=stub)
        self.assertEqual(["tok_annual_1"], called)
        self.assertTrue(result["cancelled"])
        # Still licensed today: the school keeps the year it paid for, and only
        # the next automatic charge is stopped.
        self.assertTrue(result["subscription"]["entitled"])
        self.assertTrue(result["subscription"]["cancelAtPeriodEnd"])
        self.assertEqual("2099-01-01T00:00:00Z", result["periodEnd"])
        self.assertEqual("canceled", store.get_subscription(user["id"])["status"])

    def test_cancel_without_a_linked_subscription_is_refused(self):
        cfg = config.Config(env=dict(PAYFAST_PRODUCTION))
        store = db.Store(os.path.join(TMP_DIR, "cancel-none.db"))
        user = store.create_user("Westridge High", "none@example.com", "hash", None)
        with self.assertRaises(billing.BillingError) as caught:
            billing.cancel_licence(store, user, cfg)
        self.assertEqual("no_subscription", caught.exception.code)

    def test_cancel_on_stripe_points_at_the_portal(self):
        cfg = config.Config(env={
            "APP_BASE_URL": "https://roster.example.edu",
            "JWT_SECRET": "stripe-cancel-secret",
            "STRIPE_SECRET_KEY": "sk_live_x",
            "STRIPE_PRICE_ID": "price_x",
            "STRIPE_WEBHOOK_SECRET": "whsec_x",
        })
        store = db.Store(os.path.join(TMP_DIR, "cancel-stripe.db"))
        user = store.create_user("Westridge High", "stripe@example.com", "hash", None)
        with self.assertRaises(billing.BillingError) as caught:
            billing.cancel_licence(store, user, cfg)
        self.assertEqual("unsupported", caught.exception.code)


class PayFastWebhookTests(unittest.TestCase):
    """The ITN path, end to end, against a real server on the PayFast provider.

    PAYFAST_ITN_VERIFICATION=signature keeps this offline - it is the same md5
    check PayFast performs, with no call back to them. PAYFAST_API_BASE points at
    a closed port so the cancellation test proves a dead gateway fails loudly.
    """

    @classmethod
    def setUpClass(cls):
        cls.process, cls.base, cls.log = spawn_server(dict(
            PAYFAST_PRODUCTION,
            PAYFAST_SANDBOX="1",
            PAYFAST_ITN_VERIFICATION="signature",
            PAYFAST_API_BASE="http://127.0.0.1:1",
        ))
        wait_healthy(cls.base, cls.process, cls.log)

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.process, cls.log)

    def _signup(self, email):
        status, data = request(
            "POST",
            "/api/auth/signup",
            {"school": "Westridge High", "email": email, "password": "secret123"},
            base=self.base,
        )
        self.assertEqual(201, status)
        return data

    def test_complete_itn_activates_and_unlocks_the_solver(self):
        session = self._signup("payfast-activate@example.com")
        user_id, token = session["user"]["id"], session["token"]
        self.assertEqual("trial", session["subscription"]["source"])

        status, body = post_itn(payfast_itn(user_id, "7000001"), base=self.base)
        self.assertEqual(200, status)
        self.assertEqual("activated", body["action"])
        self.assertTrue(body["subscription"]["entitled"])
        self.assertEqual("subscription", body["subscription"]["source"])
        handover = body["subscription"]["currentPeriodEnd"]

        status, me = request("GET", "/api/me", token=token, base=self.base)
        self.assertEqual("subscription", me["subscription"]["source"])

        # The real proof: the server-side paywall is lifted for the solver.
        status, _ = request(
            "POST", "/api/roster/generate", sample_payload(), token=token, base=self.base
        )
        self.assertEqual(200, status)

        # A retried notification must not buy a second year.
        status, again = post_itn(payfast_itn(user_id, "7000001"), base=self.base)
        self.assertEqual(200, status)
        self.assertTrue(again.get("duplicate"))
        status, me = request("GET", "/api/me", token=token, base=self.base)
        self.assertEqual(handover, me["subscription"]["currentPeriodEnd"])

    def test_successful_itn_records_a_payment_and_hides_the_portal(self):
        session = self._signup("payfast-summary@example.com")
        user_id, token = session["user"]["id"], session["token"]

        status, summary = request("GET", "/api/billing/summary", token=token, base=self.base)
        self.assertEqual(200, status)
        self.assertFalse(summary["canCancel"])          # nothing paid yet
        self.assertEqual([], summary["payments"])

        post_itn(payfast_itn(user_id, "7000002"), base=self.base)
        status, summary = request("GET", "/api/billing/summary", token=token, base=self.base)
        self.assertEqual("payfast", summary["provider"])
        self.assertFalse(summary["supportsPortal"])     # PayFast has no hosted portal
        self.assertTrue(summary["canCancel"])
        self.assertEqual("1200.00", summary["payments"][0]["amount"])
        self.assertEqual("COMPLETE", summary["payments"][0]["status"])


class PayFastEdgeCaseTests(unittest.TestCase):
    """Renewals, refusals and a dead gateway, against the same real server."""

    @classmethod
    def setUpClass(cls):
        cls.process, cls.base, cls.log = spawn_server(dict(
            PAYFAST_PRODUCTION,
            PAYFAST_SANDBOX="1",
            PAYFAST_ITN_VERIFICATION="signature",
            PAYFAST_API_BASE="http://127.0.0.1:1",
        ))
        wait_healthy(cls.base, cls.process, cls.log)

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.process, cls.log)

    def _signup(self, email):
        status, data = request(
            "POST",
            "/api/auth/signup",
            {"school": "Westridge High", "email": email, "password": "secret123"},
            base=self.base,
        )
        self.assertEqual(201, status)
        return data

    def test_renewal_extends_the_year_already_paid_for(self):
        session = self._signup("payfast-renew@example.com")
        user_id, token = session["user"]["id"], session["token"]
        post_itn(payfast_itn(user_id, "7100001"), base=self.base)
        status, me = request("GET", "/api/me", token=token, base=self.base)
        first = me["subscription"]["currentPeriodEnd"]

        post_itn(payfast_itn(user_id, "7100002"), base=self.base)
        status, me = request("GET", "/api/me", token=token, base=self.base)
        second = me["subscription"]["currentPeriodEnd"]
        self.assertGreater(second, first)
        self.assertGreater(days_between(first, second), 360)

    def test_tampered_or_empty_itn_changes_nothing(self):
        session = self._signup("payfast-forged@example.com")
        user_id, token = session["user"]["id"], session["token"]
        status, _ = post_itn(payfast_itn(user_id, "7100003"), base=self.base, signature="0" * 32)
        self.assertEqual(400, status)
        status, _ = request(
            "POST",
            "/api/billing/webhook",
            raw_body=b"",
            extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
            base=self.base,
        )
        self.assertEqual(400, status)
        status, me = request("GET", "/api/me", token=token, base=self.base)
        self.assertEqual("trial", me["subscription"]["source"])

    def test_underpaid_payment_is_recorded_but_grants_nothing(self):
        session = self._signup("payfast-short@example.com")
        user_id, token = session["user"]["id"], session["token"]
        status, body = post_itn(payfast_itn(user_id, "7100004", amount="10.00"), base=self.base)
        self.assertEqual(200, status)
        self.assertEqual("underpaid", body["action"])
        self.assertEqual("trial", body["subscription"]["source"])
        status, summary = request("GET", "/api/billing/summary", token=token, base=self.base)
        # The short payment is in the school's history (so it can be queried with
        # the gateway) but it did not buy any time.
        self.assertEqual("10.00", summary["payments"][0]["amount"])

    def test_failed_payment_never_grants_a_licence(self):
        session = self._signup("payfast-failed@example.com")
        user_id = session["user"]["id"]
        status, body = post_itn(payfast_itn(user_id, "7100005", status="FAILED"), base=self.base)
        self.assertEqual(200, status)
        self.assertEqual("payment_failed", body["action"])
        self.assertEqual("trial", body["subscription"]["source"])

    def test_cancel_fails_loudly_when_the_gateway_is_unreachable(self):
        session = self._signup("payfast-cancel@example.com")
        user_id, token = session["user"]["id"], session["token"]
        post_itn(payfast_itn(user_id, "7100006"), base=self.base)
        # Telling the school it was cancelled when it was not is the failure this
        # guards against, so a dead gateway must be an error, not a success.
        status, body = request("POST", "/api/billing/cancel", token=token, base=self.base)
        self.assertEqual(502, status)
        self.assertIn("PayFast", body["error"])
        status, me = request("GET", "/api/me", token=token, base=self.base)
        self.assertTrue(me["subscription"]["entitled"])     # still licensed, as promised


class PayFastGuardTests(unittest.TestCase):
    """The boot guard, for the gateway a South African deployment actually uses."""

    def config_for(self, overrides):
        env = dict(PAYFAST_PRODUCTION)
        env.update(overrides)
        for name in [k for k, v in env.items() if v is None]:
            del env[name]
        return config.Config(env=env)

    def test_complete_payfast_configuration_is_clean(self):
        cfg = self.config_for({})
        self.assertEqual("payfast", cfg.billing_provider)
        self.assertTrue(cfg.billing_configured)
        self.assertTrue(cfg.payfast_configured)
        self.assertEqual([], cfg.billing_fatal)
        self.assertFalse(cfg.allow_mock_billing)

    def test_missing_merchant_credentials_are_fatal(self):
        cfg = self.config_for({"PAYFAST_MERCHANT_ID": None, "PAYFAST_MERCHANT_KEY": None})
        self.assertFalse(cfg.billing_configured)
        self.assertIn("PAYFAST_MERCHANT_ID", " ".join(cfg.billing_fatal))

    def test_missing_passphrase_is_fatal_unless_deliberately_waived(self):
        cfg = self.config_for({"PAYFAST_PASSPHRASE": None})
        self.assertIn("PAYFAST_PASSPHRASE", " ".join(cfg.billing_fatal))
        waived = self.config_for({"PAYFAST_PASSPHRASE": None, "PAYFAST_ALLOW_NO_PASSPHRASE": "1"})
        self.assertEqual([], waived.billing_fatal)

    def test_unreadable_price_is_fatal(self):
        cfg = self.config_for({"PLAN_PRICE_AMOUNT": "R1200"})
        self.assertIn("PLAN_PRICE_AMOUNT", " ".join(cfg.billing_fatal))

    def test_sandbox_on_a_public_host_warns_but_still_boots(self):
        cfg = self.config_for({"PAYFAST_SANDBOX": "1"})
        self.assertEqual([], cfg.billing_fatal)
        self.assertIn("sandbox", " ".join(cfg.billing_warnings))

    def test_incomplete_payfast_configuration_refuses_to_boot(self):
        broken = dict(PAYFAST_PRODUCTION)
        del broken["PAYFAST_PASSPHRASE"]
        process, base, log = spawn_server(broken)
        try:
            code = process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            stop_server(process, log)
            self.fail("server started on %s without a PayFast passphrase" % base)
        self.assertEqual(2, code)
        log.seek(0)
        output = log.read()
        log.close()
        self.assertIn("Refusing to start", output)
        self.assertIn("PAYFAST_PASSPHRASE", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
