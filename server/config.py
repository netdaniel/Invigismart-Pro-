"""Runtime configuration for the Invigismart Pro backend.

All values come from environment variables so the same code runs in dev and
production. Standard library only - no third-party packages required.
"""
import os
import secrets

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, os.pardir))


def _env_bool(name, default=False, env=None):
    raw = (env if env is not None else os.environ).get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default, env=None):
    raw = (env if env is not None else os.environ).get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_path(name, default, env=None):
    """Absolute path for a path-valued setting.

    A relative value is resolved against the repository root, never against the
    process working directory. Under systemd and under Passenger (cPanel's
    Python apps) the working directory is chosen by the server, not by us, so a
    relative database path would quietly open a *different*, empty database.
    """
    raw = (env if env is not None else os.environ).get(name)
    if not raw or not raw.strip():
        return default
    value = os.path.expanduser(raw.strip())
    if not os.path.isabs(value):
        value = os.path.join(REPO_ROOT, value)
    return os.path.normpath(value)


class Config:
    """Server configuration resolved once at start-up."""

    def __init__(self, env=None):
        env = env if env is not None else os.environ
        self.host = env.get("HOST", "127.0.0.1")
        self.port = _env_int("PORT", 8000, env)
        self.repo_root = REPO_ROOT
        self.db_path = _env_path("DB_PATH", os.path.join(HERE, "invigismart.db"), env)

        # --- uploaded documents (the per-account PDF library) -----------------
        # Metadata lives in SQLite; the PDFs themselves live in this directory,
        # outside the static web root. Production should point DOCUMENTS_DIR at a
        # persistent volume beside the database.
        self.documents_dir = _env_path("DOCUMENTS_DIR", os.path.join(HERE, "uploads"), env)
        self.max_document_bytes = max(
            1024, _env_int("MAX_DOCUMENT_BYTES", 10 * 1024 * 1024, env)
        )
        self.max_documents_per_user = max(
            1, _env_int("MAX_DOCUMENTS_PER_USER", 50, env)
        )
        # The per-account total can never be smaller than a single allowed file.
        self.max_document_storage_bytes = max(
            self.max_document_bytes,
            _env_int("MAX_DOCUMENT_STORAGE_BYTES", 100 * 1024 * 1024, env),
        )
        # Multipart overhead (boundary + form fields) rides on top of the file.
        self.max_request_bytes = self.max_document_bytes + 1024 * 1024

        # Sessions are signed with HMAC-SHA256 (JWT). A random secret is fine for
        # dev; production must set JWT_SECRET or every restart invalidates tokens.
        self.jwt_secret = env.get("JWT_SECRET") or secrets.token_hex(32)
        self.jwt_secret_is_ephemeral = not env.get("JWT_SECRET")
        self.access_token_ttl = _env_int("ACCESS_TOKEN_TTL", 3600, env)
        self.trial_days = _env_int("TRIAL_DAYS", 14, env)

        # --- which gateway is live --------------------------------------------
        # "stripe", "payfast" or "mock". PayFast is the South African gateway and
        # is what a ZAR-priced annual licence actually uses.
        self.billing_provider = (env.get("BILLING_PROVIDER", "") or "").strip().lower()

        # Stripe (kept for an entity Stripe does support)
        self.stripe_secret_key = env.get("STRIPE_SECRET_KEY", "").strip()
        self.stripe_price_id = env.get("STRIPE_PRICE_ID", "").strip()
        # Deliberately NOT falling back to the API key: Stripe signs deliveries with
        # the endpoint's own signing secret, so using the API key here would turn
        # every real webhook into a 400 and silently withhold paid licences.
        self.stripe_webhook_secret = env.get("STRIPE_WEBHOOK_SECRET", "").strip()
        self.stripe_webhook_secret_is_set = bool(self.stripe_webhook_secret)
        self.stripe_api_base = env.get("STRIPE_API_BASE", "https://api.stripe.com").rstrip("/")
        self.stripe_configured = bool(self.stripe_secret_key and self.stripe_price_id)

        # --- PayFast (South Africa) -------------------------------------------
        self.payfast_merchant_id = (env.get("PAYFAST_MERCHANT_ID", "") or "").strip()
        self.payfast_merchant_key = (env.get("PAYFAST_MERCHANT_KEY", "") or "").strip()
        # Must match the passphrase set on the PayFast account: the signature over
        # the payment form includes it, so a mismatch means the checkout is
        # rejected and the school cannot pay at all.
        self.payfast_passphrase = env.get("PAYFAST_PASSPHRASE", "") or ""
        self.payfast_sandbox = _env_bool("PAYFAST_SANDBOX", default=False, env=env)
        # 6 = annual, which has to match the recurring plan the school agrees to.
        self.payfast_frequency = (env.get("PAYFAST_FREQUENCY", "") or "6").strip()
        self.payfast_cycles = (env.get("PAYFAST_CYCLES", "") or "0").strip()  # 0 = until cancelled
        self.payfast_item_name = (env.get("PAYFAST_ITEM_NAME", "") or "").strip()
        # api | signature | api+signature | off - see server/payfast.py
        self.payfast_itn_verification = (
            env.get("PAYFAST_ITN_VERIFICATION", "") or "api"
        ).strip().lower()
        self.payfast_ip_allowlist = [
            part.strip()
            for part in (env.get("PAYFAST_IP_ALLOWLIST", "") or "").split(",")
            if part.strip()
        ]
        self.payfast_allow_no_passphrase = _env_bool(
            "PAYFAST_ALLOW_NO_PASSPHRASE", env=env
        )
        # Endpoint overrides, so staging can run against the sandbox unchanged.
        self.payfast_process_url = (env.get("PAYFAST_PROCESS_URL", "") or "").strip()
        self.payfast_validate_url = (env.get("PAYFAST_VALIDATE_URL", "") or "").strip()
        self.payfast_api_base = (env.get("PAYFAST_API_BASE", "") or "").strip()
        self.payfast_configured = bool(self.payfast_merchant_id and self.payfast_merchant_key)

        # Resolve the provider: prefer whichever gateway is really configured, so a
        # half-finished environment cannot silently select the wrong one.
        if self.billing_provider not in ("stripe", "payfast", "mock"):
            if self.payfast_configured:
                self.billing_provider = "payfast"
            elif self.stripe_configured:
                self.billing_provider = "stripe"
            else:
                self.billing_provider = "mock"

        # A configured gateway is the only thing that may switch mock billing off,
        # and mock endpoints are never on by default when a gateway is configured.
        self.billing_configured = (
            (self.billing_provider == "payfast" and self.payfast_configured)
            or (self.billing_provider == "stripe" and self.stripe_configured)
        )

        # Mock billing lets the whole flow run locally without live keys.
        self.allow_mock_billing = _env_bool(
            "ALLOW_MOCK_BILLING",
            default=(self.billing_provider == "mock"),
            env=env,
        )

        base = env.get("APP_BASE_URL")
        if not base:
            display_host = "localhost" if self.host in ("0.0.0.0", "127.0.0.1") else self.host
            base = "http://%s:%d" % (display_host, self.port)
        self.app_base_url = base.rstrip("/")

        self.plan_name = env.get("PLAN_NAME", "Annual School Unlimited")
        self.plan_price_display = env.get("PLAN_PRICE_DISPLAY", "R1200 / year")
        # The amount actually sent to the gateway, in ZAR. PLAN_PRICE_DISPLAY is
        # only what the pages print; this is what is charged. A local gateway takes
        # the amount from the request, so a wrong value here charges the wrong price.
        self.plan_price_amount = (env.get("PLAN_PRICE_AMOUNT", "") or "1200.00").strip()
        # How long a paid period lasts when the gateway does not tell us.
        self.subscription_days = _env_int("SUBSCRIPTION_DAYS", 365, env)

        # --- public contact details --------------------------------------------
        # Printed on the landing page, the dashboard footer and the billing panel,
        # and served through /api/health exactly like the plan copy, so the pages
        # cannot drift apart. A school that cannot pay, or that is locked out of
        # a lapsed account, needs a human to reach: these default to the real
        # support address rather than being blank.
        self.support_email = (env.get("SUPPORT_EMAIL", "") or "").strip() \
            or "daniel@invigismart.online"
        self.support_phone = (env.get("SUPPORT_PHONE", "") or "").strip() \
            or "+27 76 319 3749"
        # A tel:/wa.me link needs a dialable number, so normalise it once here
        # instead of asking every page to guess how the operator wrote it down.
        self.support_phone_dial = self._dial_number(self.support_phone)
        self.support_whatsapp_url = (
            "https://wa.me/" + self.support_phone_dial.lstrip("+")
            if self.support_phone_dial else ""
        )

        # --- production safety review ------------------------------------------
        # A public https host is treated as a production deployment and is held to
        # two rules. Both guard a *silent* failure: the school pays and nothing
        # unlocks, or a signed-in visitor mints themself a free licence.
        self.allow_insecure_billing = _env_bool("ALLOW_INSECURE_BILLING", env=env)
        self.public_host = self.app_base_url.lower().startswith("https://")
        self.billing_fatal = []
        self.billing_warnings = []
        self._review_billing_safety()

    def _review_billing_safety(self):
        """Record the billing misconfigurations that must stop a public deployment."""
        if not self.public_host:
            return

        # Rule 0: mock endpoints can never be reachable from a public https host.
        # /api/billing/mock-activate grants a free subscription to any caller who
        # holds a valid session token, so it is force-disabled rather than trusted.
        if self.allow_mock_billing:
            self.allow_mock_billing = False
            self.billing_warnings.append(
                "ALLOW_MOCK_BILLING ignored: a public https host never exposes "
                "/mock-checkout or the demo activate/expire endpoints."
            )

        if self.allow_insecure_billing:
            self.billing_warnings.append(
                "ALLOW_INSECURE_BILLING=1 - production billing checks are being skipped."
            )
            return

        if self.jwt_secret_is_ephemeral:
            self.billing_fatal.append(
                "JWT_SECRET is not set: every restart would sign out every customer. "
                "Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
            )

        if self.billing_provider == "payfast":
            self._review_payfast()
        elif self.billing_provider == "stripe":
            self._review_stripe()
        else:
            self.billing_fatal.append(
                "No payment gateway is configured (BILLING_PROVIDER=mock). Set "
                "PAYFAST_MERCHANT_ID + PAYFAST_MERCHANT_KEY (South Africa: PayFast -> "
                "Settings -> Integration) or STRIPE_SECRET_KEY + STRIPE_PRICE_ID, or "
                "this host cannot take a payment at all."
            )

    def _review_stripe(self):
        if not self.stripe_configured:
            self.billing_fatal.append(
                "Stripe is not configured. Set STRIPE_SECRET_KEY and STRIPE_PRICE_ID to the "
                "yearly R1200 (ZAR) price, or this host cannot take a payment at all."
            )
        elif not self.stripe_webhook_secret_is_set:
            self.billing_fatal.append(
                "STRIPE_WEBHOOK_SECRET is not set. Stripe's deliveries would fail signature "
                "verification, so customers would pay and never receive their licence."
            )

    def _review_payfast(self):
        """PayFast-specific silent failures.

        These are all "the school cannot pay" or "the school paid and nothing
        unlocked" traps, so they stop the boot rather than waiting for a customer.
        """
        if not self.payfast_configured:
            self.billing_fatal.append(
                "PayFast is not configured. Set PAYFAST_MERCHANT_ID and "
                "PAYFAST_MERCHANT_KEY from your PayFast account, or this host cannot "
                "take a payment at all."
            )
            return

        # PayFast validates the signature on the payment form using the account
        # passphrase, so a missing or wrong passphrase is rejected at their end.
        if not self.payfast_passphrase and not self.payfast_allow_no_passphrase:
            self.billing_fatal.append(
                "PAYFAST_PASSPHRASE is not set. PayFast signs the payment form with it, "
                "so without the exact passphrase from your PayFast account the checkout "
                "is rejected. Set it (and the same value in PayFast), or set "
                "PAYFAST_ALLOW_NO_PASSPHRASE=1 if your account genuinely has none."
            )

        try:
            amount = float(self.plan_price_amount)
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            self.billing_fatal.append(
                "PLAN_PRICE_AMOUNT must be a positive number such as 1200.00: it is the "
                "amount actually charged, and an unreadable value means the payment form "
                "cannot be built."
            )

        if self.payfast_itn_verification == "signature" and not self.payfast_passphrase:
            self.billing_fatal.append(
                "PAYFAST_ITN_VERIFICATION=signature needs PAYFAST_PASSPHRASE, or every "
                "notification would be rejected."
            )

        if self.payfast_sandbox:
            self.billing_warnings.append(
                "PAYFAST_SANDBOX=1 on a public host: checkouts go to the PayFast sandbox "
                "and no real money moves."
            )

    @staticmethod
    def _dial_number(raw):
        """International dial string for a phone number written any local way.

        `076 319 3749` (South African local), `+27 76 319 3749` and
        `0027763193749` all resolve to `+27763193749`, which is what `tel:` and
        `wa.me` links require. Returns "" when nothing dialable is left.
        """
        cleaned = "".join(ch for ch in (raw or "") if ch.isdigit() or ch == "+")
        if cleaned.startswith("+"):
            return "+" + "".join(ch for ch in cleaned if ch.isdigit())
        digits = "".join(ch for ch in cleaned if ch.isdigit())
        if digits.startswith("00"):           # international access prefix
            digits = digits[2:]
        elif digits.startswith("0"):           # 076... -> 2776...
            digits = "27" + digits[1:]
        return ("+" + digits) if digits else ""

    def describe(self):
        return {
            "host": self.host,
            "port": self.port,
            "dbPath": self.db_path,
            "billingProvider": self.billing_provider,
            "billingConfigured": self.billing_configured,
            "payfastConfigured": self.payfast_configured,
            "payfastSandbox": self.payfast_sandbox,
            "stripeConfigured": self.stripe_configured,
            "mockBilling": self.allow_mock_billing,
            "publicHost": self.public_host,
            "billingWarnings": list(self.billing_warnings),
            "ephemeralJwtSecret": self.jwt_secret_is_ephemeral,
            "planName": self.plan_name,
            "planPrice": self.plan_price_display,
            "trialDays": self.trial_days,
            "supportEmail": self.support_email,
            "supportPhone": self.support_phone,
            "supportPhoneDial": self.support_phone_dial,
            "supportWhatsappUrl": self.support_whatsapp_url,
            "documentMaxBytes": self.max_document_bytes,
            "documentMaxCount": self.max_documents_per_user,
            "documentMaxStorageBytes": self.max_document_storage_bytes,
        }
