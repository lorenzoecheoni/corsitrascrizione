import base64
import binascii
import hmac
import secrets
import time


SESSION_COOKIE = "bvr_session"
SESSION_TTL_SECONDS = 43_200


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii") + b"=" * (-len(value) % 4),
                            altchars=b"-_", validate=True)


def issue_session(secret: bytes, *, now: int | None = None) -> str:
    expiry = int(time.time()) if now is None else now
    payload = f"team:{expiry + SESSION_TTL_SECONDS}".encode("ascii")
    signature = hmac.digest(secret, payload, "sha256")
    return f"{_encode(payload)}.{_encode(signature)}"


def session_is_valid(value: str | None, secret: bytes, *, now: int | None = None) -> bool:
    if not value:
        return False
    try:
        encoded_payload, encoded_signature = value.split(".")
        payload = _decode(encoded_payload)
        signature = _decode(encoded_signature)
        if _encode(payload) != encoded_payload or _encode(signature) != encoded_signature:
            return False
        expected = hmac.digest(secret, payload, "sha256")
        subject, expiry_text = payload.decode("ascii").split(":")
        expiry = int(expiry_text)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    current_time = int(time.time()) if now is None else now
    return (subject == "team" and expiry_text.isdecimal() and expiry > current_time
            and secrets.compare_digest(signature, expected))


def credentials_are_valid(username: str, password: str, expected_password: str) -> bool:
    username_matches = secrets.compare_digest(username, "team")
    password_matches = secrets.compare_digest(password, expected_password)
    return username_matches and password_matches
