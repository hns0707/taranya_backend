"""
ICICI Orange / PhiCommerce PG — HMAC-SHA256 secureHash (Hash Calculation V1).
"""
import hashlib
import hmac
from typing import Any, Mapping


def build_secure_hash(params: Mapping[str, Any], secret_key: str) -> str:
    """
    Hash V1 (PDF):
    1. Non-null / non-empty params
    2. Sort by parameter name ascending
    3. Concatenate values only
    4. HMAC-SHA256 with merchant secret → lowercase hex
    """
    parts = []
    for key in sorted(params.keys()):
        if key in ("secureHash", "securehash"):
            continue
        value = params.get(key)
        if value is None:
            continue
        text = str(value)
        if text == "":
            continue
        parts.append(text)
    msg = "".join(parts)
    digest = hmac.new(
        secret_key.encode("utf-8"),
        msg.encode("ascii"),
        hashlib.sha256,
    )
    return digest.hexdigest().lower()


def verify_secure_hash(params: Mapping[str, Any], secret_key: str, received_hash: str | None) -> bool:
    if not received_hash:
        return False
    expected = build_secure_hash(params, secret_key)
    return hmac.compare_digest(expected, str(received_hash).strip().lower())
