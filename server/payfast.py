"""PayFast gateway (South Africa): hosted checkout, ITN validation, recurring API.

Why this module exists
----------------------
Stripe is only available to South African businesses through its "extended
network", so the local gateway is the practical route to taking ZAR. PayFast is
used here because it supports **annual** recurring billing and exposes an API to
pause/cancel/view subscriptions, which is what a 12-month school licence needs.

Three things are PayFast-specific and all three live in this file:

  1. the *custom integration* checkout - the browser POSTs a signed form to
     PayFast instead of following a redirect URL;
  2. the ITN ("Instant Transaction Notification") webhook - form-encoded, with a
     signature that must match PayFast's PHP `urlencode` + `md5` exactly;
  3. the recurring payments API used to fetch/pause/cancel a subscription.

The signature algorithm is reproduced from PayFast's own documentation and is
pinned by two published test vectors in server/tests/test_api.py, so a change
here breaks a test instead of silently rejecting live payments.

Nothing in this module imports from the rest of the app: it takes plain values
and returns plain dicts, which keeps it unit-testable without a server.
"""
import datetime
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- endpoints
# Live and sandbox hosts. Overridable through config so a staging host can be
# pointed at the sandbox without a code change.
PROCESS_URL_LIVE = "https://www.payfast.co.za/eng/process"
PROCESS_URL_SANDBOX = "https://sandbox.payfast.co.za/eng/process"
VALIDATE_URL_LIVE = "https://www.payfast.co.za/eng/query/validate"
VALIDATE_URL_SANDBOX = "https://sandbox.payfast.co.za/eng/query/validate"
API_BASE_LIVE = "https://api.payfast.co.za"
API_BASE_SANDBOX = "https://sandbox.payfast.co.za"

# ------------------------------------------------------------ field ordering
# PayFast signs the fields *in the order they are documented*, not in sorted
# order, so this tuple is the contract. Subscriptions extend the same form:
# subscription_type, recurring_amount, frequency and cycles are appended where
# the documentation places them.
#
# If PayFast ever rejects a signature, the fix is a one-line reorder here -
# see "If the signature is rejected" in DEPLOY.md.
CHECKOUT_FIELD_ORDER = (
    "merchant_id",
    "merchant_key",
    "return_url",
    "cancel_url",
    "notify_url",
    "name_first",
    "name_last",
    "email_address",
    "cell_number",
    "m_payment_id",
    "amount",
    "item_name",
    "item_description",
    "custom_int1",
    "custom_int2",
    "custom_int3",
    "custom_int4",
    "custom_int5",
    "custom_str1",
    "custom_str2",
    "custom_str3",
    "custom_str4",
    "custom_str5",
    "email_confirmation",
    "confirmation_address",
    "payment_method",
    "subscription_type",
    "billing_date",
    "recurring_amount",
    "frequency",
    "cycles",
)

# PayFast's documented recurring frequency codes. The annual licence uses
# "annual"; confirm the cadence with a sandbox transaction before going live -
# the PayFast dashboard shows the next scheduled collection date.
FREQUENCY_DAILY = "1"
FREQUENCY_WEEKLY = "2"
FREQUENCY_MONTHLY = "3"
FREQUENCY_QUARTERLY = "4"
FREQUENCY_BIANNUAL = "5"
FREQUENCY_ANNUAL = "6"
FREQUENCY_CHOICES = {
    "daily": FREQUENCY_DAILY,
    "weekly": FREQUENCY_WEEKLY,
    "monthly": FREQUENCY_MONTHLY,
    "quarterly": FREQUENCY_QUARTERLY,
    "biannual": FREQUENCY_BIANNUAL,
    "annual": FREQUENCY_ANNUAL,
}

# cycle value meaning "repeat until cancelled" rather than a fixed count.
CYCLES_INDEFINITE = "0"

SUBSCRIPTION_TYPE = "1"      # 1 = recurring subscription, 2 = tokenisation only
CURRENCY = "ZAR"


# --------------------------------------------------------------- signature
def php_urlencode(value):
    """Reproduce PHP's urlencode(), which is what PayFast signs with.

    Differences from urllib.parse.quote_plus that bite in practice:
      * PHP leaves only A-Za-z0-9 - _ . unencoded, so "~" becomes "%7E";
      * a space becomes "+";
      * every other byte of the UTF-8 encoding becomes %XX with upper-case hex.
    """
    text = "" if value is None else str(value)
    encoded = urllib.parse.quote_plus(text, safe="")
    return encoded.replace("~", "%7E")


def build_parameter_string(fields, passphrase=""):
    """Join the fields as PHP would, adding the passphrase when one is set.

    Empty values are skipped: PayFast signs only the parameters that are
    actually sent, and an empty parameter would change the digest.
    """
    if isinstance(fields, dict):
        pairs = [(key, fields[key]) for key in CHECKOUT_FIELD_ORDER if key in fields]
        # Anything outside the documented order (ITN extras) is appended in the
        # order it arrived, which is how PayFast sends it back to us.
        pairs += [
            (key, value) for key, value in fields.items() if key not in CHECKOUT_FIELD_ORDER
        ]
    else:
        pairs = list(fields)

    parts = []
    for key, value in pairs:
        if value is None or str(value) == "":
            continue
        parts.append("%s=%s" % (key, php_urlencode(str(value).strip())))
    if passphrase:
        parts.append("passphrase=%s" % php_urlencode(str(passphrase).strip()))
    return "&".join(parts)


def signature(fields, passphrase=""):
    """Lower-case md5 hex digest of the parameter string."""
    return hashlib.md5(
        build_parameter_string(fields, passphrase).encode("utf-8")
    ).hexdigest()


# -------------------------------------------------------------- exceptions
class PayfastError(Exception):
    """PayFast could not complete the request (config, network or rejection)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class ItnError(PayfastError):
    """The ITN could not be trusted.

    The caller must answer 400 and change nothing: an unverified notification
    that is applied would hand out a free licence.
    """


# ---------------------------------------------------------------- endpoints
def _override(cfg, attribute):
    return (getattr(cfg, attribute, "") or "").strip()


def process_url(cfg):
    """Where the browser POSTs the signed payment form."""
    override = _override(cfg, "payfast_process_url")
    if override:
        return override
    return PROCESS_URL_SANDBOX if cfg.payfast_sandbox else PROCESS_URL_LIVE


def validate_url(cfg):
    """Where we ask PayFast whether an ITN it apparently sent is genuine."""
    override = _override(cfg, "payfast_validate_url")
    if override:
        return override
    return VALIDATE_URL_SANDBOX if cfg.payfast_sandbox else VALIDATE_URL_LIVE


def api_base(cfg):
    """Base URL of the recurring payments API."""
    override = _override(cfg, "payfast_api_base")
    if override:
        return override.rstrip("/")
    return API_BASE_SANDBOX if cfg.payfast_sandbox else API_BASE_LIVE


# ------------------------------------------------------------- references
def payment_ref(user_id, now=None):
    """Our own unique reference for a payment attempt.

    PayFast echoes it back as m_payment_id, so it is the second way we recover
    the account if custom_str1 is ever lost.
    """
    stamp = int(now if now is not None else time.time())
    return ("inv-%s-%d" % (str(user_id), stamp))[:100]


def user_id_from_ref(reference):
    """Recover the account id from an m_payment_id produced by payment_ref()."""
    if not reference:
        return None
    text = str(reference).strip()
    if not text.startswith("inv-"):
        return None
    body = text[4:].rsplit("-", 1)[0]
    return body or None


# ----------------------------------------------------------------- checkout
def checkout_fields(cfg, user, plan_id=None, now=None):
    """Build the signed field set the browser must POST to PayFast.

    The dictionary is returned in PayFast's documented order and the signature
    is appended last, so the payload verifies whether PayFast re-signs in
    documented order or in the order it received the fields.

    The account is carried out to PayFast in custom_str1 (primary) and
    m_payment_id (fallback); the ITN brings one of them back, which is how a
    payment finds the school it belongs to.
    """
    if not cfg.payfast_configured:
        raise PayfastError(
            "PayFast is not configured. Set PAYFAST_MERCHANT_ID and "
            "PAYFAST_MERCHANT_KEY, or this server cannot take a payment at all.",
            code="not_configured",
        )

    values = {
        "merchant_id": cfg.payfast_merchant_id,
        "merchant_key": cfg.payfast_merchant_key,
        "return_url": cfg.app_base_url + "/app?checkout=success",
        "cancel_url": cfg.app_base_url + "/app?checkout=cancel",
        "notify_url": cfg.app_base_url + "/api/billing/webhook",
        "name_first": (user.get("school") or "School")[:100],
        "email_address": user.get("email") or "",
        "m_payment_id": payment_ref(user["id"], now),
        "amount": cfg.plan_price_amount,
        "item_name": (cfg.payfast_item_name or cfg.plan_name)[:100],
        "item_description": cfg.plan_price_display,
        "custom_str1": user["id"],
        "custom_str2": plan_id or "annual_school_unlimited",
        "email_confirmation": "1",
        "confirmation_address": user.get("email") or "",
        "subscription_type": SUBSCRIPTION_TYPE,
        "recurring_amount": cfg.plan_price_amount,
        "frequency": cfg.payfast_frequency,
        "cycles": cfg.payfast_cycles,
    }

    ordered = {}
    for key in CHECKOUT_FIELD_ORDER:
        value = values.get(key)
        if value is None or str(value) == "":
            continue
        ordered[key] = str(value).strip()
    ordered["signature"] = signature(ordered, cfg.payfast_passphrase)
    return ordered


# --------------------------------------------------------------------- ITN
# How an incoming notification is checked. "api" asks PayFast directly and is
# the default: it is authoritative and independent of field ordering.
# "signature" re-computes the digest locally, which needs the ITN field order to
# match PayFast's documentation exactly - enable it only after comparing against
# a real sandbox ITN. "api+signature" requires both. "off" is for tests only.
ITN_VERIFY_API = "api"
ITN_VERIFY_SIGNATURE = "signature"
ITN_VERIFY_BOTH = "api+signature"
ITN_VERIFY_OFF = "off"

# PayFast sends notifications from these hosts (their own plugin resolves them).
ITN_HOSTS = (
    "www.payfast.co.za",
    "sandbox.payfast.co.za",
    "w1w.payfast.co.za",
    "w2w.payfast.co.za",
)


def form_to_dict(raw_body):
    """Parse a form-encoded body, keeping PayFast's field order."""
    if isinstance(raw_body, bytes):
        text = raw_body.decode("utf-8", "replace")
    else:
        text = raw_body or ""
    return dict(urllib.parse.parse_qsl(text, keep_blank_values=True))


def client_ip(remote_ip, headers=None):
    """The real caller, even though the app sits behind a reverse proxy."""
    forwarded = ""
    if headers:
        try:
            forwarded = headers.get("X-Forwarded-For") or ""
        except AttributeError:
            forwarded = ""
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return remote_ip


def resolve_itn_hosts():
    """Resolve PayFast's notification hostnames, as their plugin does."""
    import socket

    found = []
    for host in ITN_HOSTS:
        try:
            found += socket.gethostbyname_ex(host)[2]
        except OSError:
            continue
    return sorted(set(found))


def ip_allowed(remote_ip, allowlist):
    """True when remote_ip matches the allowlist (exact address or CIDR)."""
    if not allowlist:
        return True
    if not remote_ip:
        return False
    import ipaddress

    try:
        candidate = ipaddress.ip_address(str(remote_ip).strip())
    except ValueError:
        return False
    for entry in allowlist:
        item = str(entry).strip()
        if not item:
            continue
        try:
            if "/" in item:
                if candidate in ipaddress.ip_network(item, strict=False):
                    return True
            elif candidate == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False


def _default_post(url, raw_body, timeout=20):
    """POST the raw body back to PayFast and return the response text."""
    request = urllib.request.Request(
        url,
        data=raw_body if isinstance(raw_body, bytes) else (raw_body or "").encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def verify_itn(fields, raw_body, cfg, http_post=None, remote_ip=None, headers=None):
    """Raise ItnError unless this notification is provably from PayFast.

    Both checks exist because they fail differently: the API check is
    authoritative but needs the network, and the local signature check needs
    PayFast's exact field order. An allowlist, when configured, is a hard gate
    and is evaluated first.
    """
    caller = client_ip(remote_ip, headers)
    if not ip_allowed(caller, getattr(cfg, "payfast_ip_allowlist", ()) or ()):
        raise ItnError("ITN came from %s, which is not in PAYFAST_IP_ALLOWLIST." % caller)

    mode = (getattr(cfg, "payfast_itn_verification", "") or ITN_VERIFY_API).strip().lower()
    if mode == ITN_VERIFY_OFF:
        return True

    if mode in (ITN_VERIFY_SIGNATURE, ITN_VERIFY_BOTH):
        presented = (fields.get("signature") or "").strip()
        passphrase = getattr(cfg, "payfast_passphrase", "") or ""
        if not passphrase and mode == ITN_VERIFY_SIGNATURE:
            raise ItnError(
                "PAYFAST_ITN_VERIFICATION=signature needs PAYFAST_PASSPHRASE, or "
                "the signature cannot be checked at all."
            )
        if passphrase:
            if not presented:
                raise ItnError("ITN carries no signature.")
            unsigned = {k: v for k, v in fields.items() if k != "signature"}
            if not hmac.compare_digest(presented.lower(), signature(unsigned, passphrase)):
                raise ItnError("ITN signature does not match the passphrase.")

    if mode in (ITN_VERIFY_API, ITN_VERIFY_BOTH):
        poster = http_post or _default_post
        try:
            answer = poster(validate_url(cfg), raw_body)
        except (urllib.error.URLError, OSError) as exc:
            raise ItnError("Could not confirm the ITN with PayFast: %s" % exc)
        if str(answer or "").strip().upper() != "VALID":
            raise ItnError("PayFast did not confirm this ITN as valid.")
    return True


def canonical_event(fields, resolve_user=None):
    """Turn a *verified* ITN into the provider-neutral event our store applies.

    Only a COMPLETE payment grants time. Pending, failed and cancelled
    notifications are recorded for the audit trail but must never extend a
    licence: that is the difference between "the school paid" and "the school
    tried to pay".
    """
    status = (fields.get("payment_status") or "").strip().upper()
    token = (fields.get("token") or "").strip() or None
    payment_id = (fields.get("pf_payment_id") or "").strip()

    user_id = None
    if resolve_user:
        for candidate in (
            fields.get("custom_str1"),
            user_id_from_ref(fields.get("m_payment_id")),
            fields.get("email_address"),
        ):
            if not candidate:
                continue
            user_id = resolve_user(candidate)
            if user_id:
                break

    by_status = {
        "COMPLETE": "subscription.activated",
        "CANCELLED": "subscription.payment_cancelled",
        "FAILED": "subscription.payment_failed",
        "PENDING": "subscription.payment_pending",
    }
    event_type = by_status.get(status, "subscription.payment_unknown")
    fallback = "%s|%s|%s" % (token or "", status, fields.get("m_payment_id") or "")

    return {
        "id": "pf_pay_%s" % (payment_id or hashlib.md5(
            fallback.encode("utf-8")
        ).hexdigest()),
        "type": event_type,
        "provider": "payfast",
        "user_id": user_id,
        "provider_reference": payment_id,
        "provider_subscription_id": token,
        "amount": (fields.get("amount_gross") or fields.get("amount") or "").strip(),
        "amount_fee": (fields.get("amount_fee") or "").strip(),
        "amount_net": (fields.get("amount_net") or "").strip(),
        "currency": CURRENCY,
        "item_name": fields.get("item_name") or "",
        "email": fields.get("email_address") or "",
        "status": status,
        "raw": dict(fields),
    }


# ----------------------------------------------------- recurring payments API
def _api_timestamp(now=None):
    """PayFast expects ISO-8601 in South African time (UTC+2, no DST)."""
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    return (current + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+02:00")


def api_signature(headers, body=None, passphrase=""):
    """Sign the request headers plus the body, in that order (per PayFast)."""
    merged = {}
    merged.update(headers)
    merged.update(body or {})
    return signature(merged, passphrase)


def _default_request(url, method, headers, body_bytes, timeout=30):
    request = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def subscription_request(cfg, command, token, body=None, method="PUT", http_request=None):
    """Call /subscriptions/{token}/{command}.

    Commands: fetch, pause, unpause, cancel, update, adhoc. The signature covers
    merchant-id, timestamp, version and the body - and for a bodiless cancel it
    also covers content-length=0, exactly as PayFast's own plugin does, which is
    why that header is added *before* signing rather than after.
    """
    if not cfg.payfast_configured:
        raise PayfastError(
            "PayFast is not configured, so subscriptions cannot be managed.",
            code="not_configured",
        )
    clean_token = str(token or "").strip()
    if not clean_token:
        raise PayfastError(
            "This licence is not linked to a PayFast subscription yet, so it "
            "cannot be managed here. Complete a checkout first.",
            code="no_subscription",
        )

    url = "%s/subscriptions/%s/%s" % (
        api_base(cfg),
        urllib.parse.quote(clean_token, safe=""),
        command,
    )
    if cfg.payfast_sandbox:
        url += "?testing=true"

    body = dict(body or {})
    signed = {
        "merchant-id": cfg.payfast_merchant_id,
        "timestamp": _api_timestamp(),
        "version": "v1",
    }
    if command == "cancel" and not body:
        signed["content-length"] = "0"
    signed["signature"] = api_signature(signed, body, cfg.payfast_passphrase)

    payload = urllib.parse.urlencode(body).encode("utf-8") if body else b""
    headers = dict(signed)
    if method != "GET":
        headers.setdefault("Content-Length", str(len(payload)))
    if payload:
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    sender = http_request or _default_request
    try:
        status, text = sender(url, method, headers, None if method == "GET" else payload)
    except (urllib.error.URLError, OSError) as exc:
        # A gateway that cannot be reached must surface as a clear failure, never
        # as a silent success: the school is told the cancellation did not happen.
        raise PayfastError(
            "Could not reach PayFast to %s the subscription: %s" % (command, exc),
            code="upstream",
        )
    if status != 200:
        raise PayfastError(
            "PayFast returned HTTP %s for the %s request: %s"
            % (status, command, (text or "").strip()[:200]),
            code="upstream",
        )
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {"raw": text}


def fetch_subscription(cfg, token, http_request=None):
    return subscription_request(cfg, "fetch", token, method="GET", http_request=http_request)


def cancel_subscription(cfg, token, http_request=None):
    """Stop future charges. Access lasts until the period the school paid for ends."""
    return subscription_request(cfg, "cancel", token, method="PUT", http_request=http_request)


def pause_subscription(cfg, token, http_request=None):
    return subscription_request(cfg, "pause", token, method="PUT", http_request=http_request)


def unpause_subscription(cfg, token, http_request=None):
    return subscription_request(cfg, "unpause", token, method="PUT", http_request=http_request)
