"""Contract tests for the canonical SCP license format."""

from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from sales_copilot.auth.license_format import (
    ENTERPRISE_FEATURES,
    FEATURE_CALLTAP,
    FEATURE_CENTRAL_AUDIT,
    FREE_FEATURES,
    LEGACY_HMAC_DEADLINE,
    PRO_FEATURES,
    SC_PREFIX,
    SCP_FIELD_SIZES,
    SCP_LICENSE_ID_END,
    SCP_LICENSE_ID_OFFSET,
    SCP_PAYLOAD_SLICE,
    SCP_PREFIX,
    SCP_SIGNATURE_END,
    SCP_SIGNATURE_OFFSET,
    SCP_SIGNATURE_SLICE,
    SCP_TOTAL_BYTES,
    SCP_VERSION_OFFSET,
    TIER_CODES,
    TIER_FEATURES,
)

_VECTOR_PATH = Path(__file__).parents[1] / "fixtures" / "license_pro_test_vector.json"


def _load_vector() -> dict:
    return json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))


def _decode_scp_key(key: str) -> bytes:
    assert key.startswith(f"{SCP_PREFIX}-")
    encoded = key.removeprefix(f"{SCP_PREFIX}-").replace("-", "")
    padding = "=" * ((8 - len(encoded) % 8) % 8)
    return base64.b32decode(encoded + padding)


def test_constants_are_importable_and_canonical() -> None:
    assert SCP_PREFIX == "SCP"
    assert SC_PREFIX == "SC"
    assert TIER_CODES == {"free": 0, "pro": 1, "enterprise": 2}


def test_field_sizes_and_offsets_define_exact_91_byte_layout() -> None:
    assert sum(SCP_FIELD_SIZES) == SCP_TOTAL_BYTES == 91
    assert SCP_VERSION_OFFSET == 0
    assert (SCP_LICENSE_ID_OFFSET, SCP_LICENSE_ID_END) == (11, 27)
    assert (SCP_SIGNATURE_OFFSET, SCP_SIGNATURE_END) == (27, 91)


def test_payload_slice_is_bytes_zero_through_26() -> None:
    raw = bytes(range(SCP_TOTAL_BYTES))

    assert raw[SCP_PAYLOAD_SLICE] == raw[0:27]
    assert raw[SCP_SIGNATURE_SLICE] == raw[27:91]


def test_known_good_vector_signature_and_decoded_fields() -> None:
    vector = _load_vector()
    raw = _decode_scp_key(vector["key"])
    decoded = vector["decoded"]
    public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(vector["public_key_hex"]))

    assert len(raw) == SCP_TOTAL_BYTES
    public_key.verify(raw[SCP_SIGNATURE_SLICE], raw[SCP_PAYLOAD_SLICE])
    assert raw[0] == decoded["version"]
    assert raw[1] == decoded["tier_code"] == TIER_CODES[decoded["tier"]]
    assert raw[2] == decoded["rotation_epoch"]
    assert int.from_bytes(raw[3:7], "big") == decoded["issued_at"]
    assert int.from_bytes(raw[7:11], "big") == decoded["expires_at"]
    assert raw[SCP_LICENSE_ID_OFFSET:SCP_LICENSE_ID_END].hex() == decoded["license_id_hex"]


def test_tier_feature_mappings_are_explicit() -> None:
    assert FEATURE_CALLTAP not in FREE_FEATURES
    assert FEATURE_CENTRAL_AUDIT not in FREE_FEATURES
    assert {FEATURE_CALLTAP, FEATURE_CENTRAL_AUDIT} <= PRO_FEATURES
    assert TIER_FEATURES == {
        "free": FREE_FEATURES,
        "pro": PRO_FEATURES,
        "enterprise": ENTERPRISE_FEATURES,
    }
    assert ENTERPRISE_FEATURES is not PRO_FEATURES


def test_legacy_hmac_deadline_is_a_date() -> None:
    assert isinstance(LEGACY_HMAC_DEADLINE, date)
    assert LEGACY_HMAC_DEADLINE == date(2026, 9, 1)
