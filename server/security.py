"""Password hashing and JWT (HS256) helpers. Standard library only.

Passwords are stored as PBKDF2-HMAC-SHA256 digests with a per-user random salt.
Session tokens are compact, signed JWTs that the server verifies on every
sensitive request - the client can never mint one.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import time

PBKDF2_ALGO = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 200_000
_SALT_BYTES = 16


def _b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text):
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password):
    """Return a self-describing hash string: algo$iterations$salt$digest."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "%s$%d$%s$%s" % (PBKDF2_ALGO, PBKDF2_ITERATIONS, salt.hex(), digest.hex())


def verify_password(password, stored):
    """Constant-time check of a candidate password against a stored hash."""
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != PBKDF2_ALGO:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(candidate, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def sign_jwt(claims, secret):
    header = {"alg": "HS256", "typ": "JWT"}
    segments = [
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    segments.append(_b64url_encode(signature))
    return ".".join(segments)


def decode_jwt(token, secret, leeway=0):
    """Return the claims dict for a valid token, otherwise None."""
    if not token or token.count(".") != 2:
        return None
    header_b64, payload_b64, signature_b64 = token.split(".")
    signing_input = (".".join([header_b64, payload_b64])).encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    try:
        provided = _b64url_decode(signature_b64)
    except (binascii.Error, ValueError):
        return None
    if not hmac.compare_digest(provided, expected):
        return None
    try:
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if exp is not None:
        try:
            if time.time() > float(exp) + leeway:
                return None
        except (TypeError, ValueError):
            return None
    return claims


def issue_access_token(user, secret, ttl_seconds):
    now = int(time.time())
    return sign_jwt(
        {
            "sub": user["id"],
            "email": user["email"],
            "iat": now,
            "exp": now + int(ttl_seconds),
        },
        secret,
    )
