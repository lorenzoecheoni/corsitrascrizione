"""Signed, short-lived confirmations for a bounded ordered video selection."""

import base64
import binascii
import hmac
import time
from uuid import UUID


_CONFIRMATION_ERROR = "Conferma non valida o scaduta"
_MAX_TOKEN_BYTES = 4_096
_MAX_SELECTION_SIZE = 50
_SELECTION_TTL_SECONDS = 600


def _encode(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _decode_canonical(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError(_CONFIRMATION_ERROR)
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError(_CONFIRMATION_ERROR) from exc
    if _encode(decoded) != encoded:
        raise ValueError(_CONFIRMATION_ERROR)
    return decoded


def _selection_error() -> ValueError:
    return ValueError(_CONFIRMATION_ERROR)


def sign_selection(video_ids: list[UUID], key: bytes, *, now: int | None = None) -> str:
    """Return a ten-minute HMAC-protected confirmation for ordered UUIDs."""
    try:
        if not 1 <= len(video_ids) <= _MAX_SELECTION_SIZE:
            raise _selection_error()
        if any(not isinstance(video_id, UUID) for video_id in video_ids):
            raise _selection_error()
        if len(set(video_ids)) != len(video_ids):
            raise _selection_error()
        issued_at = int(time.time()) if now is None else now
        if type(issued_at) is not int:
            raise _selection_error()
        payload = f"{issued_at + _SELECTION_TTL_SECONDS}|{','.join(str(video_id) for video_id in video_ids)}"
        encoded_payload = _encode(payload.encode("ascii"))
        signature = _encode(hmac.digest(key, encoded_payload, "sha256"))
        return f"{encoded_payload.decode('ascii')}.{signature.decode('ascii')}"
    except (TypeError, ValueError, UnicodeError):
        raise _selection_error() from None


def verify_selection(token: str, key: bytes, *, now: int | None = None) -> list[UUID]:
    """Validate a confirmation without exposing malformed-token details."""
    try:
        if not isinstance(token, str) or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
            raise _selection_error()
        parts = token.split(".")
        if len(parts) != 2:
            raise _selection_error()
        encoded_payload, encoded_signature = parts
        payload = _decode_canonical(encoded_payload)
        signature = _decode_canonical(encoded_signature)
        expected_signature = hmac.digest(key, encoded_payload.encode("ascii"), "sha256")
        if not hmac.compare_digest(signature, expected_signature):
            raise _selection_error()
        expiry_text, separator, ids_text = payload.decode("ascii").partition("|")
        if not separator or not expiry_text.isdecimal() or str(int(expiry_text)) != expiry_text:
            raise _selection_error()
        current_time = int(time.time()) if now is None else now
        if type(current_time) is not int or int(expiry_text) <= current_time:
            raise _selection_error()
        raw_ids = ids_text.split(",")
        if not 1 <= len(raw_ids) <= _MAX_SELECTION_SIZE:
            raise _selection_error()
        video_ids = [UUID(raw_id) for raw_id in raw_ids]
        if any(str(video_id) != raw_id for video_id, raw_id in zip(video_ids, raw_ids, strict=True)):
            raise _selection_error()
        if len(set(video_ids)) != len(video_ids):
            raise _selection_error()
        return video_ids
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise _selection_error() from None
