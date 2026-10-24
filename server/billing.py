"""Server-side billing.

Two code paths share one subscription-state handler:
  * live Stripe  - Checkout Sessions are created via the Stripe REST API and
                   subscription changes arrive over a signed webhook.
  * mock billing - a local "checkout" page that emits the same event shape, so
                   the whole flow is runnable and testable without live keys.

Entitlement is always derived on the server from the database, never from
anything the browser stores.
"""
import datetime
import hashlib
import hmac
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import payfast

STRIPE_API_VERSION = "2024-06-20"
ACTIVE_STATUSES = ("active", "trialing")
# Statuses that still grant access while the paid-for period has not elapsed. A
# cancelled subscription belongs here: cancellation stops the *next* charge, it
# does not confiscate time the school has already paid for. (Stripe clears the
# period end when a subscription genuinely ends, so that case denies access.)
ACCESS_STATUSES = ACTIVE_STATUSES + ("canceled",)


class BillingError(Exception):
    """Raised when billing cannot be completed (config or upstream error)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def to_iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def from_epoch(value):
    if value in (None, ""):
        return None
    try:
        return datetime.datetime.fromtimestamp(int(value), datetime.timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def entitlement(store, user, cfg):
    """Compute the authoritative entitlement for a user from the database."""
    now = utcnow()
    sub = store.get_subscription(user["id"]) or {}
    status = sub.get("status") or "none"
    plan = sub.get("plan") or "annual_school_unlimited"
    period_end = from_iso(sub.get("current_period_end"))

    entitled = False
    source = "none"
    effective_end = period_end

    if status in ACCESS_STATUSES and period_end and period_end > now:
        entitled, source = True, "subscription"

    if not entitled:
        trial_end = from_iso(user.get("trial_ends_at"))
        if trial_end and trial_end > now:
            entitled, source, status = True, "trial", "trialing"
            effective_end = trial_end

    days_left = 0
    if entitled and effective_end:
        days_left = max(0, math.ceil((effective_end - now).total_seconds() / 86400.0))

    return {
        "entitled": entitled,
        "plan": plan,
        "status": status,
        "source": source,
        "currentPeriodEnd": to_iso(effective_end),
        "daysLeft": days_left,
        "planName": cfg.plan_name,
        "planPrice": cfg.plan_price_display,
        "mockBilling": cfg.allow_mock_billing and not cfg.billing_configured,
        "provider": cfg.billing_provider,
        "cancelAtPeriodEnd": status == "canceled" and entitled,
    }


def public_user(user):
    return {"id": user["id"], "school": user["school"], "email": user["email"]}


def default_period_end(cfg=None, days=365):
    return utcnow() + datetime.timedelta(days=days)


# --------------------------------------------------------------- webhook sig
def _signature_candidates(sig_header):
    timestamp = None
    signatures = []
    for item in (sig_header or "").split(","):
        if "=" not in item:
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        if key == "t":
            timestamp = value.strip()
        elif key == "v1":
            signatures.append(value.strip())
    return timestamp, signatures


def verify_stripe_signature(payload, sig_header, secret, tolerance=300):
    """Verify a Stripe `Stripe-Signature` header against the raw request body."""
    if not sig_header or not secret:
        return False
    timestamp, signatures = _signature_candidates(sig_header)
    if not timestamp or not signatures:
        return False
    try:
        if abs(time.time() - int(timestamp)) > tolerance:
            return False
    except (TypeError, ValueError):
        return False
    signed_payload = timestamp.encode("ascii") + b"." + payload
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


# ---------------------------------------------------------------- checkout
def _stripe_post(cfg, path, params, label):
    """POST form-encoded parameters to the Stripe REST API, return the parsed body."""
    payload = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        cfg.stripe_api_base + path,
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + cfg.stripe_secret_key,
            "Content-Type": "application/x-www-form-urlencoded",
            "Stripe-Version": STRIPE_API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise BillingError(
            "Stripe rejected the %s request (%s): %s" % (label, exc.code, detail)
        )
    except Exception as exc:  # network / DNS / TLS / non-JSON body
        raise BillingError("Could not reach Stripe: %s" % exc)


def create_checkout_session(store, user, cfg, plan=None):
    """Describe the hosted checkout for whichever gateway is live.

    Stripe answers with a URL to redirect to. PayFast answers with a URL *and*
    the signed fields that must be POSTed to it, because its custom integration
    is a form submission rather than a redirect.
    """
    if cfg.billing_provider == "payfast":
        try:
            fields = payfast.checkout_fields(cfg, user, plan)
        except payfast.PayfastError as exc:
            raise BillingError(str(exc), code=exc.code)
        return {"url": payfast.process_url(cfg), "fields": fields, "mode": "payfast"}

    if cfg.stripe_configured:
        session = _stripe_post(
            cfg,
            "/v1/checkout/sessions",
            {
                "mode": "subscription",
                "line_items[0][price]": cfg.stripe_price_id,
                "line_items[0][quantity]": "1",
                "success_url": cfg.app_base_url + "/app?checkout=success",
                "cancel_url": cfg.app_base_url + "/app?checkout=cancel",
                "client_reference_id": user["id"],
                "metadata[user_id]": user["id"],
                "customer_email": user["email"],
            },
            "checkout",
        )
        if not session.get("url"):
            raise BillingError("Stripe did not return a checkout URL.")
        return {"url": session["url"], "id": session.get("id"), "mode": "stripe"}

    if not cfg.allow_mock_billing:
        raise BillingError("Billing is not configured on this server.", code="not_configured")
    session_id = "mock_" + uuid.uuid4().hex
    return {
        "url": cfg.app_base_url + "/mock-checkout?session=" + session_id,
        "id": session_id,
        "mode": "mock",
    }


# ------------------------------------------------------------ billing portal
def create_portal_session(store, user, cfg):
    """Return {'url', 'mode'} for Stripe's hosted Billing Portal.

    The school downloads its invoices, updates the card or cancels the licence
    there, so this app never stores card details and never has to field a
    cancellation by hand. The portal must be enabled once in the Stripe
    dashboard (Settings -> Billing -> Customer portal) before it will work.
    """
    if cfg.billing_provider == "payfast":
        raise BillingError(
            "PayFast has no hosted billing portal. Use the Billing tab in the app "
            "for your invoices and to cancel.",
            code="unsupported",
        )
    if not cfg.stripe_configured:
        raise BillingError(
            "Billing is not configured on this server, so there is no portal to open.",
            code="not_configured",
        )
    subscription = store.get_subscription(user["id"]) or {}
    customer_id = subscription.get("stripe_customer_id")
    if not customer_id:
        raise BillingError(
            "This account is not linked to a Stripe customer yet. Complete a checkout "
            "first, or contact us to link your existing invoice.",
            code="no_customer",
        )
    session = _stripe_post(
        cfg,
        "/v1/billing_portal/sessions",
        {
            "customer": customer_id,
            "return_url": cfg.app_base_url + "/app?billing=return",
        },
        "portal",
    )
    if not session.get("url"):
        raise BillingError("Stripe did not return a portal URL.")
    return {"url": session["url"], "mode": "stripe"}


# ------------------------------------------------- authoritative state update
def _end_trial(store, user_id):
    """Once a paid subscription exists, the trial must no longer grant access."""
    if user_id:
        store.set_trial_end(user_id, to_iso(utcnow() - datetime.timedelta(days=1)))


def _resolve_user_id(store, event, obj):
    for candidate in (
        (obj.get("metadata") or {}).get("user_id"),
        (event.get("metadata") or {}).get("user_id"),
        obj.get("client_reference_id"),
    ):
        if candidate and store.get_user_by_id(candidate):
            return candidate
    sub = store.find_subscription_by_customer(obj.get("customer"))
    if sub:
        return sub["user_id"]
    return None


def apply_subscription_event(store, event, cfg=None):
    """Apply a Stripe-shaped event to the subscription store.

    Shared by the live webhook and the mock checkout, so both exercise the same
    code path. Returns (user_id, action).
    """
    etype = event.get("type") or ""
    if etype.startswith("subscription."):
        # A provider-neutral event, e.g. PayFast's ITN (see payfast.canonical_event).
        return _apply_canonical_event(store, event, cfg)
    obj = (event.get("data") or {}).get("object") or {}
    user_id = _resolve_user_id(store, event, obj)

    if etype == "checkout.session.completed":
        if not user_id:
            return None, "no-user"
        period_end = from_epoch(obj.get("current_period_end")) or default_period_end(cfg)
        store.upsert_subscription(
            user_id,
            status="active",
            current_period_end=to_iso(period_end),
            stripe_customer_id=obj.get("customer"),
            stripe_subscription_id=obj.get("subscription"),
        )
        _end_trial(store, user_id)
        return user_id, "activated"

    if etype in ("customer.subscription.created", "customer.subscription.updated", "invoice.paid"):
        if not user_id:
            return None, "no-user"
        period_end = from_epoch(obj.get("current_period_end")) or default_period_end(cfg)
        store.upsert_subscription(
            user_id,
            status=obj.get("status") or "active",
            current_period_end=to_iso(period_end),
            stripe_customer_id=obj.get("customer"),
            stripe_subscription_id=obj.get("id") or obj.get("subscription"),
        )
        if (obj.get("status") or "active") in ACTIVE_STATUSES:
            _end_trial(store, user_id)
        return user_id, "updated"

    if etype == "customer.subscription.deleted":
        if not user_id:
            return None, "no-user"
        store.upsert_subscription(user_id, status="canceled", clear_period_end=True)
        return user_id, "canceled"

    return None, "ignored"


def mock_checkout_event(user, cfg, days=365):
    """Build an event identical in shape to a real Stripe subscription event."""
    return {
        "id": "evt_mock_" + uuid.uuid4().hex,
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_mock_" + uuid.uuid4().hex,
                "customer": "cus_mock_" + uuid.uuid4().hex,
                "status": "active",
                "current_period_end": int(
                    (utcnow() + datetime.timedelta(days=days)).timestamp()
                ),
                "metadata": {"user_id": user["id"]},
            }
        },
    }


# ------------------------------------------- provider-neutral (e.g. PayFast)
def _price_amount(cfg):
    """The price as a number, or None when it cannot be read."""
    try:
        value = float(getattr(cfg, "plan_price_amount", "") or "")
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _to_amount(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _amount_is_enough(event, cfg):
    """Reject a payment that is materially less than the price.

    By the time this runs the notification is already verified as genuine, so it
    catches a wrong amount (a stale price, or a form someone tampered with)
    rather than a forged notification. A missing amount never blocks a payment.
    """
    expected = _price_amount(cfg)
    paid = _to_amount(event.get("amount"))
    if expected is None or paid is None:
        return True
    return paid + 0.01 >= expected


def _extended_period_end(store, user_id, cfg):
    """The end of the paid period after this payment.

    A renewal extends the period already paid for, so paying early never
    shortens cover; a first payment starts today.
    """
    days = getattr(cfg, "subscription_days", 365) or 365
    existing = store.get_subscription(user_id) or {}
    base = utcnow()
    current_end = from_iso(existing.get("current_period_end"))
    if current_end and current_end > base:
        base = current_end
    return base + datetime.timedelta(days=days)


def _record_event_payment(store, user_id, event, period_end=None):
    """Add the payment to the school's invoice history (never extends access)."""
    return store.record_payment(
        user_id,
        provider=event.get("provider") or "unknown",
        provider_payment_id=event.get("provider_reference"),
        provider_subscription_id=event.get("provider_subscription_id"),
        amount=event.get("amount"),
        currency=event.get("currency"),
        status=event.get("status") or "unknown",
        item_name=event.get("item_name"),
        email=event.get("email"),
        period_end=to_iso(period_end) if period_end else None,
    )


def _apply_canonical_event(store, event, cfg):
    """Apply a provider-neutral event, e.g. one built from a PayFast ITN.

    Only a COMPLETE payment grants time, and a payment that falls short of the
    price is recorded but does not extend the licence.
    """
    etype = event.get("type") or ""
    user_id = event.get("user_id")

    if etype in ("subscription.activated", "subscription.renewed"):
        if not user_id:
            return None, "no-user"
        if not _amount_is_enough(event, cfg):
            _record_event_payment(store, user_id, event)
            return user_id, "underpaid"
        period_end = _extended_period_end(store, user_id, cfg)
        store.upsert_subscription(
            user_id,
            status="active",
            current_period_end=to_iso(period_end),
            stripe_subscription_id=event.get("provider_subscription_id"),
            provider=event.get("provider") or cfg.billing_provider,
        )
        _end_trial(store, user_id)
        _record_event_payment(store, user_id, event, period_end)
        return user_id, "activated"

    if etype == "subscription.canceled":
        if not user_id:
            return None, "no-user"
        # Cancellation stops the next charge; it does not confiscate the period
        # the school has already paid for.
        store.upsert_subscription(user_id, status="canceled")
        return user_id, "canceled"

    if not user_id:
        return None, "no-user"
    _record_event_payment(store, user_id, event)
    return user_id, etype.replace("subscription.", "") or "ignored"


def create_billing_summary(store, user, cfg):
    """Everything the in-app Billing tab needs.

    A local gateway has no hosted portal, so the app itself shows the invoice
    history and offers cancellation.
    """
    subscription = store.get_subscription(user["id"]) or {}
    entitlement_info = entitlement(store, user, cfg)
    token = subscription.get("stripe_subscription_id")
    provider = cfg.billing_provider
    return {
        "provider": provider,
        "subscription": entitlement_info,
        "supportsPortal": provider == "stripe" and cfg.stripe_configured,
        "canCancel": bool(token)
        and provider == "payfast"
        and entitlement_info["entitled"]
        and subscription.get("status") != "canceled",
        "cancelAtPeriodEnd": subscription.get("status") == "canceled"
        and entitlement_info["entitled"],
        "periodEnd": subscription.get("current_period_end"),
        "payments": [
            {
                "id": row.get("id"),
                "date": row.get("created_at"),
                "amount": row.get("amount"),
                "currency": row.get("currency"),
                "status": row.get("status"),
                "itemName": row.get("item_name"),
                "periodEnd": row.get("period_end"),
            }
            for row in store.list_payments(user["id"], 24)
        ],
    }


def cancel_licence(store, user, cfg, cancel_call=None):
    """Stop the next charge, keeping access until the paid period ends.

    Cancelling immediately would remove time the school has already paid for,
    which is unfair and exactly what consumer-protection rules exist to prevent.
    `cancel_call` is injectable so the gateway call can be stubbed in tests.
    """
    if cfg.billing_provider != "payfast":
        raise BillingError(
            "Cancellations are handled in the billing portal on this server.",
            code="unsupported",
        )
    subscription = store.get_subscription(user["id"]) or {}
    token = subscription.get("stripe_subscription_id")
    if not token:
        raise BillingError(
            "This licence is not linked to a PayFast subscription yet, so it cannot "
            "be cancelled here. Complete a checkout first.",
            code="no_subscription",
        )
    try:
        (cancel_call or payfast.cancel_subscription)(cfg, token)
    except payfast.PayfastError as exc:
        raise BillingError(str(exc), code=exc.code)
    store.upsert_subscription(user["id"], status="canceled", provider="payfast")
    return {
        "cancelled": True,
        "subscription": entitlement(store, user, cfg),
        "periodEnd": subscription.get("current_period_end"),
    }
