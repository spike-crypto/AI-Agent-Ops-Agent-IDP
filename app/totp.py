"""TOTP (RFC 6238) and recovery codes, standard library only."""

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time


def new_secret() -> str:
    """A 160-bit base32 secret, the usual size for authenticator apps."""
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _normalise(secret: str) -> bytes:
    secret = secret.strip().replace(" ", "").upper()
    padding = "=" * (-len(secret) % 8)
    return base64.b32decode(secret + padding)


def code_at(secret: str, counter: int) -> str:
    digest = hmac.new(_normalise(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def current_code(secret: str) -> str:
    return code_at(secret, int(time.time()) // 30)


def verify(secret: str, code: str, window: int = 1) -> bool:
    """Accepts the current 30-second step and `window` steps either side (clock drift)."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6 or not secret:
        return False
    counter = int(time.time()) // 30
    return any(hmac.compare_digest(code_at(secret, counter + drift), code)
               for drift in range(-window, window + 1))


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    from urllib.parse import quote

    label = quote(f"{issuer}:{account}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        "&algorithm=SHA1&digits=6&period=30"
    )


def new_recovery_codes(count: int = 8) -> list[str]:
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(count)]


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).hexdigest()
