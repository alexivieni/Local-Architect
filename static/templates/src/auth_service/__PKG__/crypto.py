"""Password hashing and signed tokens. Nothing here stores state."""
import base64
import hashlib
import hmac
import json
import os
import time

ITERATIONS = 200_000


def hash_password(password, salt=None, iterations=ITERATIONS):
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        algo, iters, salt, want = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
    return hmac.compare_digest(dk.hex(), want)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload, secret, ttl_s=900, now=None):
    now = int(time.time() if now is None else now)
    body = _b64(json.dumps(dict(payload, exp=now + ttl_s), separators=(",", ":")).encode())
    mac = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def verify(token, secret, now=None):
    """-> payload dict, or None when the token is malformed, tampered with or expired."""
    try:
        body, mac = token.split(".")
        want = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, want):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, AttributeError):
        return None
    if payload.get("exp", 0) < (time.time() if now is None else now):
        return None
    return payload


def new_session_token():
    return _b64(os.urandom(32))


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()
