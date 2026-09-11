import base64
import hashlib
import hmac
from uuid import UUID

import pytest

from app.selection import sign_selection, verify_selection


KEY = b"selection-test-key-32-bytes-long!"
FIRST_VIDEO = UUID("12345678-1234-4abc-8def-123456789abc")
SECOND_VIDEO = UUID("abcdefab-cdef-4abc-8def-abcdefabcdef")
INVALID_CONFIRMATION = "Conferma non valida o scaduta"


def _signed_payload(payload: str) -> str:
    encoded = base64.urlsafe_b64encode(payload.encode("ascii")).rstrip(b"=")
    signature = hmac.digest(KEY, encoded, "sha256")
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return f"{encoded.decode('ascii')}.{encoded_signature.decode('ascii')}"


def test_selection_token_round_trips_unique_canonical_uuids_in_order() -> None:
    token = sign_selection([FIRST_VIDEO, SECOND_VIDEO], KEY, now=1_000)

    assert verify_selection(token, KEY, now=1_599) == [FIRST_VIDEO, SECOND_VIDEO]


def test_selection_token_expires_after_ten_minutes() -> None:
    token = sign_selection([FIRST_VIDEO], KEY, now=1_000)

    with pytest.raises(ValueError, match=f"^{INVALID_CONFIRMATION}$"):
        verify_selection(token, KEY, now=1_600)


def test_selection_token_rejects_a_tampered_signature() -> None:
    token = sign_selection([FIRST_VIDEO], KEY, now=1_000)
    payload, _ = token.split(".")

    with pytest.raises(ValueError, match=f"^{INVALID_CONFIRMATION}$"):
        verify_selection(f"{payload}.AAAAAAAA", KEY, now=1_001)


@pytest.mark.parametrize(
    "payload",
    [
        "1600|not-a-uuid",
        "1600|",
        f"1600|{FIRST_VIDEO},{FIRST_VIDEO}",
        "1600|" + ",".join(
            f"00000000-0000-4000-8000-{number:012d}" for number in range(51)
        ),
    ],
    ids=["malformed-uuid", "zero-items", "duplicate-uuid", "too-many-items"],
)
def test_selection_token_rejects_malformed_or_out_of_range_payloads(payload: str) -> None:
    with pytest.raises(ValueError, match=f"^{INVALID_CONFIRMATION}$"):
        verify_selection(_signed_payload(payload), KEY, now=1_001)
