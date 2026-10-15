"""Signed tokens (HMAC-SHA256) for one-time login links and session cookies. No wallets involved.

Token format: base64url("kind|tg_id|expires|nonce") + "." + base64url(hmac)
"""
import base64
import hashlib
import hmac
import secrets
import time

LOGIN_TTL = 10 * 60          # login links expire after 10 minutes
SESSION_TTL = 7 * 24 * 3600  # sessions last 7 days


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sig(secret: str, msg: bytes) -> bytes:
    return hmac.new(secret.encode(), msg, hashlib.sha256).digest()


def sign(secret: str, kind: str, tg_id: int, ttl: int, now: float | None = None) -> str:
    if not secret:
        raise ValueError("SESSION_SECRET is not set")
    exp = int((now or time.time()) + ttl)
    msg = f"{kind}|{tg_id}|{exp}|{secrets.token_urlsafe(12)}".encode()
    return f"{_b64(msg)}.{_b64(_sig(secret, msg))}"


def verify(secret: str, token: str, kind: str, now: float | None = None) -> tuple[int, str] | None:
    """Return (tg_id, nonce) if the token is valid, of the right kind and not expired."""
    try:
        body, sig = token.split(".", 1)
        msg = _unb64(body)
        if not secret or not hmac.compare_digest(_sig(secret, msg), _unb64(sig)):
            return None
        k, tg_id, exp, nonce = msg.decode().split("|")
        if k != kind or int(exp) < (now or time.time()):
            return None
        return int(tg_id), nonce
    except (ValueError, UnicodeDecodeError):
        return None


def gate_cookie(secret: str, password: str) -> str:
    """Cookie value proving the visitor knew the site password (changes if the password changes)."""
    return _b64(_sig(secret, f"gate|{password}".encode()))


def login_url(base_url: str, secret: str, tg_id: int) -> str:
    return f"{base_url}/login?t={sign(secret, 'login', tg_id, LOGIN_TTL)}"
