"""
ICICI UPI — encrypt, bank HTTP, QR3 one-time pay, MandateQR/Collect, status, callback.

Hub uses:
  create_customer_upi_mandate      → Collect (UPI ID) → CreateMandate  [mandate mode only]
  create_customer_upi_mandate_qr   → QR3 (onetime) or MandateQR (mandate) per ICICI_UPI_QR_MODE
  get_upi_mandate_status_for_customer
  process_icici_callback
  maybe_revoke_mandate_when_scheme_completed
  send_mandate_notification
  execute_mandate_instalment
  process_upi_mandate_dues (management command / internal cron)
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from shared.models import (
    LookupValue,
    Payment,
    PaymentAuditLog,
    SchemeInstalment,
    UpiMandate,
    UpiMandateExecution,
    UpiMandateNotification,
)
from shared.services.payment_helper import is_payment_already_processed
from shared.services.payment_service import create_payment_with_collections, process_successful_payment
from shared.utils.upi_mandate_dates import (
    compute_debit_day,
    debit_dates_for_instalment,
    resolve_anchor_day,
)

try:
    from Cryptodome.Cipher import AES, PKCS1_v1_5
    from Cryptodome.PublicKey import RSA
    from Cryptodome.Util.Padding import pad, unpad
except ImportError:  # pragma: no cover
    from Crypto.Cipher import AES, PKCS1_v1_5  # type: ignore
    from Crypto.PublicKey import RSA  # type: ignore
    from Crypto.Util.Padding import pad, unpad  # type: ignore

logger = logging.getLogger(__name__)

PAYMENT_PROVIDER_ICICI = 'ICICI_UPI'
QR_PREFIX = 'MQR'  # merchant_tran_id starting with this = MandateQR path
ONETIME_PREFIX = 'OQR'  # merchant_tran_id for QR3 one-time pay


def _upi_qr_mode() -> str:
    """onetime = UPI/v0 QR3 (pay once). mandate = UPI2 MandateQR (AutoPay)."""
    return (getattr(settings, 'ICICI_UPI_QR_MODE', 'mandate') or 'mandate').strip().lower()


def _is_onetime_qr_mode() -> bool:
    return _upi_qr_mode() in ('onetime', 'one_time', 'one-time', 'qr3', 'pay')


def get_icici_upi_qr_config() -> dict:
    mode = 'onetime' if _is_onetime_qr_mode() else 'mandate'
    pub = _key_path('ICICI_BANK_PUBLIC_KEY_PATH')
    priv = _key_path('ICICI_MERCHANT_PRIVATE_KEY_PATH')
    priv_info = _describe_key_file(priv)
    return {
        'mode': mode,
        'vpa_collect_enabled': mode == 'mandate',
        'label': 'One-time UPI pay' if mode == 'onetime' else 'UPI AutoPay mandate',
        'simulate': bool(getattr(settings, 'ICICI_MANDATE_SIMULATE', False)),
        'encryption_cert_ready': bool(pub and pub.is_file() and not _bank_public_key_blocking_issues()),
        'decrypt_key_ready': bool(priv_info.get('exists') and priv_info.get('has_private')),
    }


def get_icici_mandate_readiness() -> dict:
    """Ops checklist — no secrets."""
    issues: list[str] = []
    if not (getattr(settings, 'ICICI_MERCHANT_ID', '') or '').strip():
        issues.append('ICICI_MERCHANT_ID missing')
    if not (getattr(settings, 'ICICI_API_KEY', '') or '').strip():
        issues.append('ICICI_API_KEY missing')
    if not (getattr(settings, 'ICICI_MERCHANT_VPA', '') or '').strip():
        issues.append('ICICI_MERCHANT_VPA missing')
    issues.extend(_bank_public_key_issues())
    issues.extend(_merchant_private_key_issues())
    for attr in (
        'ICICI_CREATE_MANDATE_URL',
        'ICICI_MANDATE_QR_URL',
        'ICICI_EXECUTE_MANDATE_URL',
        'ICICI_TRANSACTION_STATUS_URL',
        'ICICI_TRANSACTION_STATUS_BY_CRITERIA_URL',
        'ICICI_NOTIFICATION_URL',
    ):
        if not (getattr(settings, attr, '') or '').strip():
            issues.append(f'{attr} not configured')
    callback = (getattr(settings, 'ICICI_CALLBACK_URL', '') or '').strip()
    if not callback:
        issues.append('ICICI_CALLBACK_URL not set — register with ICICI')
    return {
        'mode': _upi_qr_mode(),
        'env': getattr(settings, 'ICICI_ENV', ''),
        'merchant_id': getattr(settings, 'ICICI_MERCHANT_ID', ''),
        'merchant_vpa': getattr(settings, 'ICICI_MERCHANT_VPA', ''),
        'api_base': getattr(settings, 'ICICI_API_BASE', ''),
        'encryption_mode': _enc_mode(),
        'simulate': bool(getattr(settings, 'ICICI_MANDATE_SIMULATE', False)),
        'ready': not issues,
        'issues': issues,
    }


# =============================================================================
# Small helpers (shared)
# =============================================================================

def _pick(data: dict, *keys, default=None):
    for k in keys:
        if k in data and data[k] not in (None, ''):
            return data[k]
    return default


def _money(amount) -> str:
    return f'{Decimal(str(amount)):.2f}'


def _new_txn(prefix: str) -> str:
    return f'{prefix}{uuid.uuid4().hex}'[:35]


def _key_path(setting_name: str) -> Optional[Path]:
    raw = (getattr(settings, setting_name, '') or '').strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = (Path(settings.BASE_DIR) / p).resolve()
    return p


def _enc_mode() -> str:
    return (getattr(settings, 'ICICI_ENCRYPTION_MODE', '') or '').strip().lower()


# =============================================================================
# Encrypt / decrypt — ICICI Hybrid (Composite / Mandate / CIB PDF)
#
# Encrypt (merchant → bank):
#   1. RANDOMNO = 16-digit session key (ASCII)  [PDF also allows 32]
#   2. encryptedKey = Base64(RSA/ECB/PKCS1Padding(RANDOMNO, ICICIPubKey))
#      oaepHashingAlgorithm = "NONE"  → PKCS#1 v1.5 (NOT OAEP)
#   3. IV (PDF): "Base64encoded RANDOMNO as IV" and "exactly 16 bytes".
#      Ambiguity: Base64(16 ASCII digits) is 24 bytes. Implementation takes the
#      first 16 bytes of that Base64 encoding so AES receives a raw 16-byte IV
#      (common ICICI Java sample pattern). Do not Base64 the IV a second time
#      before AES; only Base64-encode for the optional `iv` JSON field.
#   4. AES/CBC/PKCS5Padding(payload, key=RANDOMNO, iv=IV)
#   5. Package IV (PDF allows either):
#        prepend (PDF sample / bank decrypt docs): iv="" , encryptedData=Base64(IV||cipher)
#        field (PDF "recommended"): iv=Base64(IV), encryptedData=Base64(cipher)
#   6. `service` is mandatory in the PDF field table — must come from config
#      (ICICI_HYBRID_SERVICE). Never invent or silently send "".
#
# Decrypt (bank → merchant):
#   IV = first 16 bytes of Base64Decode(encryptedData) when iv field empty
#   SessionKey = RSA decrypt encryptedKey with merchant private key
#   AES decrypt remaining bytes; skip leading IV if still present in plaintext path
# =============================================================================

def _load_rsa_key(path: Path, *, password: bytes | None = None):
    data = path.read_bytes()
    try:
        return RSA.import_key(data, passphrase=password)
    except (ValueError, IndexError, TypeError):
        # Some .cer files are raw DER
        return RSA.import_key(data, passphrase=password)


def _describe_key_file(path: Path | None) -> dict:
    """Safe metadata for ops — never logs key material."""
    if not path or not path.is_file():
        return {'exists': False, 'path': str(path or '')}
    info: dict = {'exists': True, 'path': str(path), 'size_bytes': path.stat().st_size}
    header = path.read_bytes()[:40].decode('utf-8', errors='replace').splitlines()[0]
    info['header'] = header
    try:
        key = _load_rsa_key(path)
        info['rsa_bits'] = key.size_in_bits()
        info['has_private'] = bool(key.has_private())
    except Exception as exc:
        info['rsa_error'] = str(exc)
        info['has_private'] = False
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        from datetime import timezone

        cert = x509.load_pem_x509_certificate(path.read_bytes(), default_backend())
        not_after = getattr(cert, 'not_valid_after_utc', None) or cert.not_valid_after.replace(
            tzinfo=timezone.utc
        )
        info['cert_subject'] = cert.subject.rfc4514_string()
        info['cert_not_after'] = not_after.isoformat()
        info['cert_expired'] = not_after < datetime.now(timezone.utc)
    except Exception:
        pass
    return info


def _merchant_private_key_issues() -> list[str]:
    """Validate merchant private key path — file must contain RSA private material."""
    priv = _key_path('ICICI_MERCHANT_PRIVATE_KEY_PATH')
    if not priv or not priv.is_file():
        return [
            'ICICI_MERCHANT_PRIVATE_KEY_PATH missing — set to your merchant RSA private .pem '
            '(BEGIN PRIVATE KEY / BEGIN RSA PRIVATE KEY). ICICI does not provide this; you keep it secret.'
        ]
    info = _describe_key_file(priv)
    if info.get('rsa_error'):
        return [
            f'ICICI_MERCHANT_PRIVATE_KEY_PATH is not a valid RSA key ({priv.name}): {info["rsa_error"]}'
        ]
    if not info.get('has_private'):
        subject = info.get('cert_subject', '')
        hint = f' ({subject})' if subject else ''
        return [
            f'ICICI_MERCHANT_PRIVATE_KEY_PATH points to a PUBLIC certificate{hint}, not a private key. '
            f'File: {priv.name}. Use the .pem private key that pairs with the public cert you shared '
            'with ICICI for MID onboarding — NOT rsa_apikey.txt, NOT oneashish_ssl_public.txt, '
            'NOT merchantEncryption.pem.'
        ]
    return []


def _bank_public_key_issues(*, include_expiry_warning: bool = True) -> list[str]:
    pub = _key_path('ICICI_BANK_PUBLIC_KEY_PATH')
    if not pub or not pub.is_file():
        return [f'Bank public cert missing: {getattr(settings, "ICICI_BANK_PUBLIC_KEY_PATH", "")}']
    info = _describe_key_file(pub)
    issues: list[str] = []
    if info.get('rsa_error'):
        issues.append(f'ICICI_BANK_PUBLIC_KEY_PATH is not a valid RSA public key: {info["rsa_error"]}')
    elif info.get('has_private'):
        issues.append('ICICI_BANK_PUBLIC_KEY_PATH must be ICICI bank PUBLIC cert, not your private key')
    if include_expiry_warning and info.get('cert_expired'):
        issues.append(
            f'ICICI bank public cert not_after is {info.get("cert_not_after")} (file may still work — '
            'confirm with ICICI). Path: '
            f'{getattr(settings, "ICICI_BANK_PUBLIC_KEY_PATH", "")}'
        )
    return issues


def _bank_public_key_blocking_issues() -> list[str]:
    """Hard blockers only — missing/invalid file. Expiry is advisory (ICICI prod often still accepts)."""
    pub = _key_path('ICICI_BANK_PUBLIC_KEY_PATH')
    if not pub or not pub.is_file():
        return [f'Bank public cert missing: {getattr(settings, "ICICI_BANK_PUBLIC_KEY_PATH", "")}']
    info = _describe_key_file(pub)
    issues: list[str] = []
    if info.get('rsa_error'):
        issues.append(f'ICICI_BANK_PUBLIC_KEY_PATH is not a valid RSA public key: {info["rsa_error"]}')
    elif info.get('has_private'):
        issues.append('ICICI_BANK_PUBLIC_KEY_PATH must be ICICI bank PUBLIC cert, not your private key')
    return issues


def _log_public_cert_metadata(pub_path: Path) -> None:
    """Safe diagnostics only — subject / expiry / key size. Never log key material."""
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        from datetime import datetime, timezone

        cert = x509.load_pem_x509_certificate(pub_path.read_bytes(), default_backend())
        not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(
            tzinfo=timezone.utc
        )
        expired = not_after < datetime.now(timezone.utc)
        logger.info(
            "ICICI Hybrid public cert: path=%s subject=%s not_after=%s expired=%s key_bits=%s",
            pub_path,
            cert.subject.rfc4514_string(),
            not_after.isoformat(),
            expired,
            cert.public_key().key_size,
        )
        if expired:
            # Configuration / integration note — do not treat as automatic code failure.
            # ICICI provided this file in-project; confirm with bank whether it is still
            # the Hybrid encryption public key for this MID.
            logger.warning(
                "ICICI_BANK_PUBLIC_KEY_PATH certificate not_after is in the past (%s). "
                "Confirm with ICICI that this file is still the correct Hybrid public "
                "cert for outbound encryptedKey. Replace via ICICI_BANK_PUBLIC_KEY_PATH "
                "if the bank issues a newer cert.",
                not_after.isoformat(),
            )
    except Exception:
        # Non-PEM or cryptography missing — key may still load via RSA.import_key
        logger.info("ICICI Hybrid public key file present at %s (metadata parse skipped)", pub_path)


def _hybrid_service_name() -> str:
    """
    PDF marks `service` mandatory (backend service name).
    UPI mandate APIs use "UPI" (ICICI_HYBRID_SERVICE, default UPI).
    """
    service = (getattr(settings, "ICICI_HYBRID_SERVICE", None) or "UPI").strip()
    if not service:
        raise ValueError(
            "ICICI Hybrid encryption requires ICICI_HYBRID_SERVICE (PDF: service is mandatory). "
            "Set ICICI_HYBRID_SERVICE=UPI in .env for UPI mandate APIs."
        )
    return service


def _hybrid_session_key() -> bytes:
    """PDF: Generate 16-digit (or 32-digit) random number RANDOMNO."""
    key_len = int(getattr(settings, "ICICI_HYBRID_SESSION_KEY_LEN", 16) or 16)
    if key_len not in (16, 32):
        key_len = 16
    style = (getattr(settings, "ICICI_HYBRID_SESSION_KEY_STYLE", "digit") or "digit").lower()
    if style == "hex":
        # Optional escape hatch — PDF default is digits only
        key = secrets.token_hex(key_len // 2).encode("ascii")[:key_len]
    else:
        key = "".join(str(secrets.randbelow(10)) for _ in range(key_len)).encode("ascii")
    if len(key) not in (16, 32):
        raise ValueError(f"ICICI Hybrid session key must be 16 or 32 bytes, got {len(key)}")
    return key


def _hybrid_iv_from_session_key(session_key: bytes) -> bytes:
    """
    PDF: "Base64encoded RANDOMNO as IV" + "Exactly 16 bytes".

    Base64(16 ASCII digits) yields 24 bytes. We use the first 16 bytes of that
    Base64 encoding as the raw AES IV (not a second Base64 pass before AES).
    """
    iv = base64.b64encode(session_key)[:16]
    if len(iv) != 16:
        raise ValueError(f"ICICI Hybrid IV must be exactly 16 bytes, got {len(iv)}")
    return iv


def _encrypt_for_bank(payload: dict) -> tuple[Any, dict]:
    """
    Returns (body, extra_headers).
    Hybrid  → JSON wrapper with encryptedKey + encryptedData + iv
    Asymmetric → Base64 RSA string, text/plain
    None → plain dict
    """
    mode = _enc_mode()
    if mode not in ("hybrid", "asymmetric", "rsa", "true", "1", "yes"):
        return payload, {}

    pub_path = _key_path("ICICI_BANK_PUBLIC_KEY_PATH")
    if not pub_path or not pub_path.exists():
        raise ValueError(f"Bank public key missing: {pub_path}")

    _log_public_cert_metadata(pub_path)
    public_key = _load_rsa_key(pub_path)
    plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    # --- Asymmetric: whole JSON encrypted with RSA ---
    if mode in ("asymmetric", "rsa"):
        max_len = public_key.size_in_bytes() - 11
        if len(plain) > max_len:
            raise ValueError("Payload too big for Asymmetric RSA — use Hybrid")
        cipher = base64.b64encode(PKCS1_v1_5.new(public_key).encrypt(plain)).decode("ascii")
        return cipher, {"Content-Type": "text/plain;charset=UTF-8"}

    # --- Hybrid (PDF steps) ---
    service = _hybrid_service_name()
    session_key = _hybrid_session_key()
    iv = _hybrid_iv_from_session_key(session_key)
    # Same session_key bytes for RSA wrap and AES encrypt (PDF).
    cipher_bytes = AES.new(session_key, AES.MODE_CBC, iv=iv).encrypt(pad(plain, AES.block_size))

    # PDF sample packets use empty iv + IV prepended inside encryptedData.
    # "field" keeps the PDF "recommended" Base64 IV tag.
    iv_mode = (getattr(settings, "ICICI_HYBRID_IV_MODE", "prepend") or "prepend").lower()
    if iv_mode in ("field", "tag", "recommended"):
        encrypted_data = base64.b64encode(cipher_bytes).decode("ascii")
        iv_field = base64.b64encode(iv).decode("ascii")
    else:
        # prepend | concat | embedded | sample (default) — PDF option B
        if len(iv) != 16:
            raise ValueError("ICICI Hybrid prepend requires 16-byte IV")
        encrypted_data = base64.b64encode(iv + cipher_bytes).decode("ascii")
        iv_field = ""

    # PKCS#1 v1.5 only (oaepHashingAlgorithm NONE). Not OAEP.
    encrypted_key = base64.b64encode(PKCS1_v1_5.new(public_key).encrypt(session_key)).decode(
        "ascii"
    )
    request_id = uuid.uuid4().hex[:32]
    wrapper = {
        "requestId": request_id,
        "service": service,
        "encryptedKey": encrypted_key,
        "oaepHashingAlgorithm": "NONE",
        "iv": iv_field,
        "encryptedData": encrypted_data,
        "clientInfo": "",
        "optionalParam": "",
    }
    # Safe diagnostics — lengths / algorithms only (no session key, no ciphertext dump)
    logger.info(
        "ICICI Hybrid encrypt: requestId=%s service=%s session_key_len=%s iv_len=%s "
        "iv_mode=%s rsa_bits=%s oaepHashingAlgorithm=NONE aes=CBC/PKCS5 "
        "encryptedKey_len=%s encryptedData_len=%s",
        request_id,
        service,
        len(session_key),
        len(iv),
        iv_mode,
        public_key.size_in_bits(),
        len(encrypted_key),
        len(encrypted_data),
    )
    return wrapper, {"Content-Type": "application/json"}


def _decrypt_from_bank(data: Any) -> Any:
    """Decrypt Hybrid/Asymmetric bank response per ICICI PDF."""
    mode = _enc_mode()
    if mode not in ("hybrid", "asymmetric", "rsa", "true", "1", "yes"):
        return data

    priv_path = _key_path("ICICI_MERCHANT_PRIVATE_KEY_PATH")
    if not priv_path or not priv_path.exists():
        logger.warning("Cannot decrypt ICICI response — set ICICI_MERCHANT_PRIVATE_KEY_PATH")
        return data

    password = (getattr(settings, "ICICI_MERCHANT_PRIVATE_KEY_PASSWORD", "") or "").encode(
        "utf-8"
    ) or None
    private_key = _load_rsa_key(priv_path, password=password)
    if not private_key.has_private():
        raise ValueError(
            f'ICICI_MERCHANT_PRIVATE_KEY_PATH ({priv_path.name}) is a public certificate, not a '
            'private key. Use merchant_private.pem (BEGIN PRIVATE KEY). rsa_apikey.txt and '
            'oneashish_ssl_public.txt are public certs and cannot decrypt bank responses.'
        )
    rsa = PKCS1_v1_5.new(private_key)
    sentinel = secrets.token_bytes(max(16, private_key.size_in_bytes()))

    # Plain Base64 RSA body (Asymmetric)
    if isinstance(data, str) or (
        isinstance(data, dict) and "encryptedKey" not in data and "raw" in data
    ):
        raw = data if isinstance(data, str) else str(data.get("raw") or "")
        if not raw.strip():
            return data
        plain = rsa.decrypt(base64.b64decode(raw.strip()), sentinel)
        if plain is None or plain == sentinel:
            raise ValueError("Asymmetric decrypt failed")
        try:
            return json.loads(plain.decode("utf-8"))
        except Exception:
            return plain.decode("utf-8", errors="replace")

    if not isinstance(data, dict) or "encryptedData" not in data or "encryptedKey" not in data:
        return data

    enc_key_blob = base64.b64decode(data["encryptedKey"])
    expected = private_key.size_in_bytes()
    if len(enc_key_blob) != expected:
        raise ValueError(
            f'ICICI encryptedKey is {len(enc_key_blob)} bytes but your private key is '
            f'{private_key.size_in_bits()}-bit (expects {expected} bytes). '
            'ICICI encrypted the response with a different merchant public cert than the '
            'private key in ICICI_MERCHANT_PRIVATE_KEY_PATH. Use the private key that pairs '
            'with the public cert you submitted to ICICI for MID onboarding — or ask ICICI to '
            're-register your current public cert.'
        )

    session_key = rsa.decrypt(enc_key_blob, sentinel)
    if session_key is None or session_key == sentinel:
        raise ValueError("Hybrid session-key decrypt failed — check merchant private key")

    enc = base64.b64decode(data["encryptedData"])
    iv_b64 = (data.get("iv") or "").strip()
    if iv_b64:
        # Client-style field IV (or bank sent one)
        iv = base64.b64decode(iv_b64)
        if len(iv) > 16:
            iv = iv[:16]
        cipher_bytes = enc
    else:
        # PDF: IV is always first 16 bytes of decoded encryptedData for bank responses
        iv, cipher_bytes = enc[:16], enc[16:]

    plain = unpad(AES.new(session_key, AES.MODE_CBC, iv=iv).decrypt(cipher_bytes), AES.block_size)
    # Some payloads still carry a leading IV copy — try both
    for chunk in (plain, plain[16:] if len(plain) > 16 else None):
        if chunk is None:
            continue
        try:
            return json.loads(chunk.decode("utf-8"))
        except Exception:
            continue
    raise ValueError("Hybrid decrypt: plaintext is not JSON")


# =============================================================================
# Bank HTTP — one place for all ICICI POSTs
# =============================================================================

def bank_post(url: str, payload: dict, *, label: str, txnid: str = '') -> dict:
    """
    POST to ICICI. Encrypts if Hybrid/Asymmetric. Saves audit log.
    Returns decrypted JSON dict. Raises ValueError on hard failure.
    Prints REQUEST/RESPONSE for QR3 so you can debug in gunicorn logs.
    """
    if not url:
        raise ValueError(f'{label}: URL not configured')

    if _enc_mode() in ('hybrid', 'asymmetric', 'rsa', 'true', '1', 'yes'):
        key_issues = _bank_public_key_blocking_issues()
        if key_issues:
            raise ValueError(
                f'ICICI {label}: cannot encrypt request — {"; ".join(key_issues)}'
            )

    headers = {
        'accept': '*/*',
        'cache-control': 'no-cache',
        'Content-Type': 'application/json',
    }
    api_key = getattr(settings, 'ICICI_API_KEY', '') or ''
    if api_key:
        headers['apikey'] = api_key

    body, extra = _encrypt_for_bank(payload)
    headers.update(extra)

    if label in (
        'QR3', 'MandateQR', 'CreateMandate', 'ExecuteMandate',
        'MandateNotification', 'MandateStatus', 'RevokeMandate',
    ):
        print(f'========== ICICI {label} ==========')
        print('URL:', url)
        print('REQUEST_PLAIN:', json.dumps(payload, ensure_ascii=False, default=str))

    if isinstance(body, str):
        resp = requests.post(url, data=body.encode('utf-8'), headers=headers, timeout=30)
    else:
        resp = requests.post(url, json=body, headers=headers, timeout=30)

    try:
        data = resp.json()
    except ValueError:
        data = {'raw': (resp.text or '').strip()}

    if not isinstance(data, dict):
        data = {'raw': data}

    # Decrypt if bank sent encrypted payload
    if 'encryptedData' in data or (isinstance(data.get('raw'), str) and _enc_mode() in ('asymmetric', 'rsa')):
        try:
            data = _decrypt_from_bank(data if 'encryptedData' in data else data.get('raw'))
            if not isinstance(data, dict):
                data = {'raw': data}
        except Exception as exc:
            logger.exception('ICICI decrypt failed %s: %s', label, exc)
            raise ValueError(
                'ICICI decrypt failed. Set ICICI_MERCHANT_PRIVATE_KEY_PATH to your private .pem'
            ) from exc

    PaymentAuditLog.objects.create(
        txnid=txnid or payload.get('merchantTranId') or label,
        type=f'ICICI_{label}',
        status=str(data.get('response') or resp.status_code),
        request_payload={'plain': payload},
        response_json=data,
    )

    if label in (
        'QR3', 'MandateQR', 'CreateMandate', 'ExecuteMandate',
        'MandateNotification', 'MandateStatus', 'RevokeMandate',
    ):
        print('HTTP:', resp.status_code)
        print('RESPONSE:', json.dumps(data, ensure_ascii=False, default=str)[:2000])
        print('================================')

    data['_http_status'] = resp.status_code
    return data


def _icici_response_code(data: dict) -> str:
  return str(data.get('response') or data.get('ActCode') or data.get('ResponseCode') or '').strip()


def _upi_terminal_id() -> str:
    """PDF: terminalId on UPI2 mandate APIs is MCC (5411), not merchant terminal 6012."""
    return str(getattr(settings, 'ICICI_UPI_MCC', '') or '5411').strip() or '5411'


def _bank_ok(data: dict) -> bool:
    if int(data.get('_http_status') or 0) >= 400:
        return False
    success = str(data.get('success', '')).lower()
    code = _icici_response_code(data)
    # ICICI often returns success:"true" with response 11|Invalid data — treat as failure
    if code and code not in ('0', '00', '92'):
        return False
    if success == 'false' and code not in ('0', '00', '92'):
        return False
    if success == 'true' or code in ('0', '00', '92'):
        return True
    return int(data.get('_http_status') or 0) == 200 and success != 'false'


def _bank_msg(data: dict, default: str = 'ICICI error') -> str:
    return str(
        data.get('message') or data.get('Message') or data.get('raw') or default
    )


# =============================================================================
# Intent / QR string helpers
# =============================================================================

def _upi_pay_string(*, amount, ref_id: str) -> str:
    """One-time pay (QR3) — kept if needed later. Not used for AutoPay QR."""
    pa = (getattr(settings, 'ICICI_MERCHANT_VPA', '') or '').strip()
    pn = (getattr(settings, 'ICICI_MERCHANT_NAME', '') or 'Taranya').strip()
    mc = (getattr(settings, 'ICICI_UPI_MCC', '') or '5411').strip() or '5411'
    if not pa or not ref_id:
        raise ValueError('VPA and refId required for upi://pay')
    return (
        f'upi://pay?pa={quote(pa)}&pn={quote(pn)}&tr={quote(ref_id)}'
        f'&am={_money(amount)}&cu=INR&mc={mc}'
    )


def _extract_bank_qr_string(data: dict) -> str:
    """Pull mandate QR / intent from ICICI MandateQR decrypted response."""
    for key in (
        'SignedQRData', 'signedQRData', 'signedQrData',
        'qrString', 'QRString', 'qr', 'QR', 'intent', 'Intent', 'upiString',
        'UpiString', 'mandateQr', 'MandateQr', 'mandateQR', 'MandateQR',
    ):
        val = data.get(key)
        if val is not None and str(val).strip():
            s = str(val).strip()
            if s.startswith('upi://') or s.startswith('UPI://'):
                return s
            # Some banks return raw mandate payload without scheme — prefer full URI only
    # Second pass: accept any non-empty SignedQR / qr field even if scheme check failed
    for key in ('SignedQRData', 'signedQRData', 'qrString', 'QRString'):
        val = data.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ''


def _upi_mandate_string(mandate: UpiMandate) -> str:
    """
    AutoPay Intent/QR string for MandateQR path.
    MUST use bank qrString — locally built upi://mandate is rejected by PhonePe/GPay.
    """
    stored = (getattr(mandate, 'qr_string', None) or '').strip()
    if stored:
        return stored
    pa = (getattr(settings, 'ICICI_MERCHANT_VPA', '') or '').strip()
    pn = (getattr(settings, 'ICICI_MERCHANT_NAME', '') or 'Taranya').strip()
    mc = (getattr(settings, 'ICICI_UPI_MCC', '') or '5411').strip() or '5411'
    if not pa:
        raise ValueError('Set ICICI_MERCHANT_VPA in .env')
    tid = mandate.merchant_tran_id or ''
    logger.warning(
        'Using local upi://mandate fallback for %s — PhonePe may reject. '
        'ICICI MandateQR must return qrString.',
        tid,
    )
    return (
        f'upi://mandate?pa={quote(pa)}&pn={quote(pn)}'
        f'&am={_money(mandate.amount)}&cu=INR'
        f'&tn={quote(f"Scheme-autopay-{tid}"[:50])}'
        f'&tr={quote(tid)}&mc={mc}'
    )


def _app_links(upi_url: str) -> dict:
    """GPay / PhonePe / Paytm deep links from a upi:// URL."""
    base = (upi_url or '').strip()
    out = {'generic': base, 'gpay': base, 'phonepe': base, 'paytm': base}
    if not base.startswith('upi://'):
        return out

    parsed = urlparse(base)
    kind = (parsed.netloc or 'pay').split('/')[0] or 'pay'
    q = f'?{parsed.query}' if parsed.query else ''
    pay = not kind.lower().startswith('mandate')

    path = 'upi/pay' if pay else 'upi/mandate'
    out['gpay'] = f'tez://{path}{q}'
    out['gpay_alt'] = f'gpay://{path}{q}'
    out['phonepe'] = f'phonepe://{"pay" if pay else "mandate"}{q}'
    out['phonepe_alt'] = f'ppe://upi/{"pay" if pay else "mandate"}{q}'
    out['paytm'] = f'paytmmp://{"pay" if pay else "mandate"}{q}'

    fb = quote(base, safe='')
    out['gpay_android'] = (
        f'intent://{path}{q}#Intent;scheme=tez;'
        f'package=com.google.android.apps.nbu.paisa.user;S.browser_fallback_url={fb};end'
    )
    out['phonepe_android'] = (
        f'intent://{"pay" if pay else "mandate"}{q}#Intent;scheme=phonepe;'
        f'package=com.phonepe.app;S.browser_fallback_url={fb};end'
    )
    out['paytm_android'] = (
        f'intent://{"pay" if pay else "mandate"}{q}#Intent;scheme=paytmmp;'
        f'package=net.one97.paytm;S.browser_fallback_url={fb};end'
    )
    out['generic_android'] = (
        f'intent://{kind}{q}#Intent;scheme=upi;S.browser_fallback_url={fb};end'
    )
    return out


def _qr_png(upi_url: str) -> str:
    import qrcode
    buf = io.BytesIO()
    qrcode.make(upi_url).save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _qr_response_fields(upi_url: str) -> dict:
    return {
        'create_mode': 'QR',
        'qr_payload': upi_url,
        'intent_url': upi_url,
        'app_intents': _app_links(upi_url),
        'qr_image_base64': _qr_png(upi_url),
    }


def _onetime_status_payload(
    payment: Payment,
    instalment: SchemeInstalment,
    *,
    qr: Optional[dict] = None,
) -> dict:
    """Customer-hub shape; mandate_id holds payment.id for poll compatibility in onetime mode."""
    cs = instalment.customer_scheme
    code = payment.payment_status.code if payment.payment_status_id else 'INITIATED'
    finalized = bool(payment.is_finalized)

    if finalized and code in ('PAID', 'SUCCESS'):
        ui_status, message = 'PAYMENT_SUCCESS', 'Payment successful.'
    elif finalized or code in ('FAILED', 'REJECTED'):
        ui_status, message = 'PAYMENT_FAILED', 'Payment failed. Please try again.'
    else:
        ui_status, message = (
            'PENDING_APPROVAL',
            'Scan the QR or open your UPI app and approve the payment.',
        )

    out = {
        'mandate_id': payment.id,
        'payment_id': payment.id,
        'upi_qr_mode': 'onetime',
        'merchant_tran_id': payment.transaction_id,
        'status': code,
        'ui_status': ui_status,
        'message': message,
        'create_mode': 'QR',
        'umn': None,
        'payer_vpa': None,
        'amount': str(payment.amount),
        'customer_scheme_id': cs.id,
        'execution_id': None,
        'instalment_id': instalment.id,
        'payment_finalized': finalized,
    }
    if ui_status == 'PENDING_APPROVAL' and qr:
        out.update(qr)
    elif qr:
        out.update(qr)
    return out


def _finalize_onetime_payment(payment: Payment, *, gateway_response=None, bank_rrn=None) -> Payment:
    if is_payment_already_processed(payment):
        return payment
    if bank_rrn:
        payment.gateway_transaction_id = bank_rrn
        payment.save(update_fields=['gateway_transaction_id', 'system_updated_at'])
    process_successful_payment(payment, payment_date=timezone.now())
    if gateway_response is not None:
        PaymentAuditLog.objects.create(
            txnid=payment.transaction_id,
            type='ICICI_QR3_SUCCESS',
            status='SUCCESS',
            request_payload=gateway_response if isinstance(gateway_response, dict) else {'raw': gateway_response},
            response_json={'payment_id': payment.id},
        )
    return payment


def _fail_onetime_payment(payment: Payment, *, gateway_response=None) -> Payment:
    if is_payment_already_processed(payment):
        return payment
    failed = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code='FAILED')
    payment.payment_status = failed
    payment.is_finalized = True
    payment.save(update_fields=['payment_status', 'is_finalized', 'system_updated_at'])
    if gateway_response is not None:
        PaymentAuditLog.objects.create(
            txnid=payment.transaction_id,
            type='ICICI_QR3_FAILED',
            status='FAILED',
            request_payload=gateway_response if isinstance(gateway_response, dict) else {'raw': gateway_response},
            response_json={'payment_id': payment.id},
        )
    return payment


def _poll_onetime_qr_bank_status(payment: Payment) -> Optional[dict]:
    """TransactionStatus3 for QR3 one-time pay."""
    if payment.is_finalized:
        return None
    url = getattr(settings, 'ICICI_UPI_TXN_STATUS3_URL', '') or ''
    if not url:
        return None
    mcc = (getattr(settings, 'ICICI_UPI_MCC', '') or '5411').strip() or '5411'
    body = {
        'merchantId': str(settings.ICICI_MERCHANT_ID),
        'subMerchantId': str(
            getattr(settings, 'ICICI_SUB_MERCHANT_ID', None) or settings.ICICI_MERCHANT_ID
        ),
        'terminalId': str(mcc),
        'merchantTranId': payment.transaction_id,
    }
    try:
        return bank_post(url, body, label='TxnStatus3', txnid=payment.transaction_id)
    except Exception:
        logger.exception('ICICI TxnStatus3 poll failed payment_id=%s', payment.id)
        return None


def _apply_onetime_bank_status(payment: Payment, data: dict) -> Payment:
    txn_status = str(
        _pick(data, 'TxnStatus', 'txnStatus', 'status', 'Status', default='') or ''
    ).upper().replace(' ', '-')
    bank_rrn = _pick(data, 'BankRRN', 'bankRRN', 'OriginalBankRRN', 'rrn')

    if _bank_ok(data) and (
        txn_status in ('SUCCESS', 'TXN-SUCCESS', 'TXN_SUCCESS', 'COMPLETED', '0')
        or (txn_status.endswith('SUCCESS') and 'FAIL' not in txn_status)
    ):
        return _finalize_onetime_payment(payment, gateway_response=data, bank_rrn=bank_rrn)
    if 'FAIL' in txn_status or txn_status in ('REJECTED', 'CANCELLED', 'EXPIRED', 'TIMEOUT'):
        return _fail_onetime_payment(payment, gateway_response=data)
    return payment


# =============================================================================
# Mandate / instalment helpers
# =============================================================================

def _next_unpaid_instalment(customer_scheme_id: int):
    return (
        SchemeInstalment.objects.filter(
            customer_scheme_id=customer_scheme_id,
            is_bonus=False,
        )
        .exclude(status__code='PAID')
        .order_by('instalment_no')
        .first()
    )


def _mandate_debit_day(mandate: UpiMandate, customer_scheme) -> int:
    if mandate.debit_day:
        return int(mandate.debit_day)
    return compute_debit_day(resolve_anchor_day(customer_scheme))


def _mandate_debit_day_str(mandate: UpiMandate, customer_scheme) -> str:
    return str(_mandate_debit_day(mandate, customer_scheme))


def _apply_mandate_debit_fields(mandate: UpiMandate, customer_scheme, instalment: SchemeInstalment) -> UpiMandate:
    """Persist debit_day and latest instalment amount on the mandate row."""
    mandate.debit_day = compute_debit_day(resolve_anchor_day(customer_scheme))
    mandate.amount = instalment.amount
    mandate.save(update_fields=['debit_day', 'amount', 'system_updated_at'])
    return mandate


def _mandate_seq_no_for_instalment(instalment: SchemeInstalment) -> str:
    return str(instalment.instalment_no)


def _sync_execution_amount_from_instalment(execution: UpiMandateExecution, instalment: SchemeInstalment) -> None:
    """Always debit the current instalment amount (scheme amount may change month to month)."""
    instalment.refresh_from_db(fields=['amount'])
    if execution.amount != instalment.amount:
        execution.amount = instalment.amount
        execution.save(update_fields=['amount', 'system_updated_at'])


def _execute_payload(mandate: UpiMandate, instalment: SchemeInstalment, execution: UpiMandateExecution) -> dict:
    return {
        'merchantId': str(settings.ICICI_MERCHANT_ID),
        'subMerchantId': str(getattr(settings, 'ICICI_SUB_MERCHANT_ID', None) or settings.ICICI_MERCHANT_ID),
        'terminalId': _upi_terminal_id(),
        'merchantName': getattr(settings, 'ICICI_MERCHANT_NAME', 'Taranya'),
        'subMerchantName': getattr(settings, 'ICICI_SUB_MERCHANT_NAME', 'Jewellery Scheme'),
        'amount': _money(execution.amount),
        'merchantTranId': execution.merchant_tran_id,
        'billNumber': re.sub(r'[^A-Za-z0-9]', '', f'CS{mandate.customer_scheme_id}E{instalment.instalment_no}')[:50],
        'remark': f'Scheme instalment {instalment.instalment_no}',
        'retryCount': str(execution.retry_count or 0),
        'mandateSeqNo': execution.mandate_seq_no or _mandate_seq_no_for_instalment(instalment),
        'UMN': mandate.umn,
        'purpose': 'RECURRING',
    }


def _notification_payload(
    mandate: UpiMandate,
    instalment: SchemeInstalment,
    notification: UpiMandateNotification,
) -> dict:
    return {
        'merchantId': str(settings.ICICI_MERCHANT_ID),
        'subMerchantId': str(getattr(settings, 'ICICI_SUB_MERCHANT_ID', None) or settings.ICICI_MERCHANT_ID),
        'terminalId': _upi_terminal_id(),
        'merchantName': getattr(settings, 'ICICI_MERCHANT_NAME', 'Taranya'),
        'subMerchantName': getattr(settings, 'ICICI_SUB_MERCHANT_NAME', 'Jewellery Scheme'),
        'amount': _money(notification.amount),
        'merchantTranId': notification.merchant_tran_id,
        'billNumber': re.sub(r'[^A-Za-z0-9]', '', f'CS{mandate.customer_scheme_id}N{instalment.instalment_no}')[:50],
        'remark': f'Scheme instalment {instalment.instalment_no} PDN',
        'mandateSeqNo': notification.mandate_seq_no,
        'UMN': mandate.umn,
        'purpose': 'RECURRING',
    }


def _validate_instalment(customer, instalment_id: int):
    instalment = SchemeInstalment.objects.select_related(
        'customer_scheme', 'customer_scheme__scheme', 'customer_scheme__scheme_status', 'status',
    ).get(id=instalment_id, customer_scheme__customer=customer)

    cs = instalment.customer_scheme
    scheme_status = (cs.scheme_status and cs.scheme_status.code) or ''
    if scheme_status in ('ABANDONED', 'COMPLETED', 'CANCELLED', 'FAILED', 'REDEEMED'):
        raise ValueError(f'Cannot set up auto-pay for scheme status {scheme_status}')
    if instalment.is_bonus:
        raise ValueError('Bonus installments do not require payment')
    if instalment.status and instalment.status.code == 'PAID':
        raise ValueError('This installment is already paid')

    next_one = _next_unpaid_instalment(cs.id)
    if not next_one or next_one.id != instalment.id:
        raise ValueError('UPI auto-pay must start from the next unpaid installment')
    return instalment, cs


def _fail_other_pending(cs, *, keep_if=None) -> Optional[UpiMandate]:
    existing = (
        UpiMandate.objects.filter(
            customer_scheme=cs,
            status__in=[UpiMandate.STATUS_PENDING, UpiMandate.STATUS_APPROVED],
        )
        .order_by('-id')
        .first()
    )
    if not existing:
        return None
    if existing.status == UpiMandate.STATUS_APPROVED:
        raise ValueError('Auto-pay is already active for this scheme')
    if keep_if and keep_if(existing):
        return existing
    existing.status = UpiMandate.STATUS_FAILED
    existing.save(update_fields=['status', 'system_updated_at'])
    return None


def _is_qr(mandate: UpiMandate) -> bool:
    return (mandate.merchant_tran_id or '').startswith(QR_PREFIX)


def _status_payload(mandate: UpiMandate, *, qr: Optional[dict] = None) -> dict:
    latest = (
        UpiMandateExecution.objects.filter(upi_mandate=mandate)
        .select_related('payment', 'scheme_instalment')
        .order_by('-id')
        .first()
    )
    payment = latest.payment if latest else None
    mode = 'QR' if _is_qr(mandate) else 'COLLECT'

    ui_status = 'PENDING_APPROVAL'
    message = (
        'Scan QR or open GPay/PhonePe — approve AutoPay, then the first instalment is debited.'
        if mode == 'QR'
        else 'Approve the mandate request in your UPI app.'
    )

    if mandate.status == UpiMandate.STATUS_FAILED:
        ui_status, message = 'FAILED', 'Mandate setup failed. Please try again.'
    elif mandate.status == UpiMandate.STATUS_REVOKED:
        ui_status, message = 'REVOKED', 'Mandate was revoked.'
    elif mandate.status == UpiMandate.STATUS_APPROVED:
        if latest and latest.txn_status == UpiMandateExecution.TXN_SUCCESS and payment and payment.is_finalized:
            ui_status, message = 'PAYMENT_SUCCESS', 'Payment successful.'
        elif latest and latest.txn_status == UpiMandateExecution.TXN_FAILED:
            ui_status, message = 'PAYMENT_FAILED', 'Mandate approved, but debit failed.'
        elif latest and latest.txn_status in (UpiMandateExecution.TXN_INITIATED, UpiMandateExecution.TXN_PENDING):
            ui_status, message = 'DEBITING', 'Mandate approved. Debiting…'
        else:
            ui_status, message = 'APPROVED', 'Mandate approved. Auto-pay is active.'

    out = {
        'mandate_id': mandate.id,
        'merchant_tran_id': mandate.merchant_tran_id,
        'status': mandate.status,
        'ui_status': ui_status,
        'message': message,
        'create_mode': mode,
        'umn': mandate.umn,
        'payer_vpa': mandate.payer_vpa,
        'amount': str(mandate.amount),
        'customer_scheme_id': mandate.customer_scheme_id,
        'execution_id': latest.id if latest else None,
        'instalment_id': latest.scheme_instalment_id if latest else None,
        'payment_id': payment.id if payment else None,
        'payment_finalized': bool(payment and payment.is_finalized),
    }
    if ui_status == 'PENDING_APPROVAL' and mode == 'QR':
        if qr:
            out.update(qr)
        else:
            # Rebuild mandate Intent/QR while waiting for customer approval
            try:
                out.update(_qr_response_fields(_upi_mandate_string(mandate)))
            except ValueError:
                pass
    elif qr:
        out.update(qr)
    return out


# =============================================================================
# Payment finalize (shared by callback + execute)
# =============================================================================

def _get_or_create_payment(execution: UpiMandateExecution) -> Payment:
    if execution.payment_id:
        return Payment.objects.select_for_update().get(id=execution.payment_id)
    payment = create_payment_with_collections(
        instalment=execution.scheme_instalment,
        amount=execution.amount,
        transaction_id=execution.merchant_tran_id or uuid.uuid4().hex[:20],
        payment_status_code='INITIATED',
        payment_source='CP',
        payment_mode_code='UPI',
        paid_at=None,
        payment_provider=PAYMENT_PROVIDER_ICICI,
        upi_execution=execution,
    )
    execution.payment = payment
    execution.save(update_fields=['payment', 'system_updated_at'])
    return payment


def _mark_paid(execution: UpiMandateExecution, *, gateway_response=None, bank_rrn=None) -> Payment:
    payment = _get_or_create_payment(execution)
    if is_payment_already_processed(payment):
        return payment
    if bank_rrn:
        payment.gateway_transaction_id = bank_rrn
        payment.save(update_fields=['gateway_transaction_id', 'system_updated_at'])
    process_successful_payment(payment, payment_date=timezone.now())
    execution.txn_status = UpiMandateExecution.TXN_SUCCESS
    execution.executed_at = timezone.now()
    if bank_rrn:
        execution.bank_rrn = bank_rrn
    if gateway_response is not None:
        execution.gateway_response = gateway_response
    execution.save(update_fields=['txn_status', 'executed_at', 'bank_rrn', 'gateway_response', 'system_updated_at'])
    return payment


def _mark_failed(execution: UpiMandateExecution, *, gateway_response=None, bank_rrn=None) -> Payment:
    payment = _get_or_create_payment(execution)
    if is_payment_already_processed(payment):
        return payment
    failed = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code='FAILED')
    payment.payment_status = failed
    payment.is_finalized = True
    payment.save(update_fields=['payment_status', 'is_finalized', 'system_updated_at'])
    execution.txn_status = UpiMandateExecution.TXN_FAILED
    execution.executed_at = timezone.now()
    if bank_rrn:
        execution.bank_rrn = bank_rrn
    if gateway_response is not None:
        execution.gateway_response = gateway_response
    execution.save(update_fields=['txn_status', 'executed_at', 'bank_rrn', 'gateway_response', 'system_updated_at'])
    return payment


def _create_execution(mandate: UpiMandate, instalment: SchemeInstalment, *, merchant_tran_id=None):
    existing = UpiMandateExecution.objects.filter(scheme_instalment=instalment).first()
    if existing:
        if existing.amount != instalment.amount:
            existing.amount = instalment.amount
            existing.save(update_fields=['amount', 'system_updated_at'])
        return existing
    return UpiMandateExecution.objects.create(
        upi_mandate=mandate,
        scheme_instalment=instalment,
        merchant_tran_id=merchant_tran_id or f'ICICI{mandate.id}{instalment.id}{uuid.uuid4().hex[:8]}'[:20],
        amount=instalment.amount,
        txn_status=UpiMandateExecution.TXN_PENDING,
    )


def _apply_execute_mandate_response(execution: UpiMandateExecution, data: dict) -> UpiMandateExecution:
    """
    Handle ExecuteMandate response per Collect PDF:
    - response 0 / status SUCCESS → debit success
    - response 92 → initiated, wait for EXECUTE-SUCCESS callback
    - other codes (e.g. 11) → failed even if success:true
    """
    code = _icici_response_code(data)
    bank_rrn = _pick(data, 'BankRRN', 'bankRRN', 'OriginalBankRRN')
    status = str(_pick(data, 'status', 'Status', 'TxnStatus', default='')).upper().replace(' ', '-')

    if code in ('0', '00') or status == 'SUCCESS' or 'EXECUTE-SUCCESS' in status:
        _mark_paid(execution, gateway_response=data, bank_rrn=bank_rrn)
    elif code == '92' or status in ('INITIATED', 'PENDING', 'CREATE-INITIATED'):
        execution.txn_status = UpiMandateExecution.TXN_PENDING
        execution.gateway_response = data
        if bank_rrn:
            execution.bank_rrn = str(bank_rrn)
        execution.save(update_fields=['txn_status', 'gateway_response', 'bank_rrn', 'system_updated_at'])
    elif _bank_ok(data) and not code:
        # Ambiguous 200 — wait for callback rather than failing
        execution.txn_status = UpiMandateExecution.TXN_PENDING
        execution.gateway_response = data
        execution.save(update_fields=['txn_status', 'gateway_response', 'system_updated_at'])
    else:
        _mark_failed(execution, gateway_response=data, bank_rrn=bank_rrn)
    execution.refresh_from_db()
    return execution


def _apply_notification_response(
    notification: UpiMandateNotification,
    data: dict,
) -> UpiMandateNotification:
    code = _icici_response_code(data)
    status = str(_pick(data, 'status', 'Status', 'TxnStatus', default='')).upper().replace(' ', '-')

    if code in ('0', '00') or status == 'SUCCESS' or 'NOTIFICATION-SUCCESS' in status:
        notification.status = UpiMandateNotification.STATUS_SUCCESS
    elif code == '92' or status in ('INITIATED', 'PENDING'):
        notification.status = UpiMandateNotification.STATUS_SENT
    elif _bank_ok(data) and not code:
        notification.status = UpiMandateNotification.STATUS_SENT
    else:
        notification.status = UpiMandateNotification.STATUS_FAILED

    notification.gateway_response = data
    notification.notified_at = notification.notified_at or timezone.now()
    notification.save(update_fields=['status', 'gateway_response', 'notified_at', 'system_updated_at'])
    notification.refresh_from_db()
    return notification


def _get_or_create_notification(mandate: UpiMandate, instalment: SchemeInstalment) -> UpiMandateNotification:
    existing = UpiMandateNotification.objects.filter(scheme_instalment=instalment).first()
    if existing:
        existing.amount = instalment.amount
        existing.save(update_fields=['amount', 'system_updated_at'])
        return existing

    notif_date, debit_date = debit_dates_for_instalment(
        instalment,
        mandate.customer_scheme,
        debit_day=mandate.debit_day,
    )
    return UpiMandateNotification.objects.create(
        upi_mandate=mandate,
        scheme_instalment=instalment,
        merchant_tran_id=_new_txn('PDN'),
        mandate_seq_no=_mandate_seq_no_for_instalment(instalment),
        amount=instalment.amount,
        debit_date=debit_date,
        notification_date=notif_date,
        status=UpiMandateNotification.STATUS_PENDING,
    )


def send_mandate_notification(mandate: UpiMandate, instalment: SchemeInstalment) -> UpiMandateNotification:
    """
    ICICI MandateNotification (PDN) — required >= 24h before Execute for instalment 2+.
    Uses the current instalment.amount (not mandate.amount frozen at setup).
    """
    if mandate.status != UpiMandate.STATUS_APPROVED or not mandate.umn:
        raise ValueError('Mandate must be APPROVED with UMN before notification')

    instalment.refresh_from_db(fields=['amount', 'instalment_no', 'status'])
    notification = _get_or_create_notification(mandate, instalment)
    if notification.status == UpiMandateNotification.STATUS_SUCCESS:
        return notification

    notification.amount = instalment.amount
    notification.status = UpiMandateNotification.STATUS_PENDING
    notification.save(update_fields=['amount', 'status', 'system_updated_at'])

    if getattr(settings, 'ICICI_MANDATE_SIMULATE', False):
        notification.status = UpiMandateNotification.STATUS_SUCCESS
        notification.notified_at = timezone.now()
        notification.gateway_response = {'simulated': True}
        notification.save(update_fields=['status', 'notified_at', 'gateway_response', 'system_updated_at'])
        return notification

    url = getattr(settings, 'ICICI_NOTIFICATION_URL', '') or ''
    if not url:
        raise ValueError('ICICI_NOTIFICATION_URL not configured')

    payload = _notification_payload(mandate, instalment, notification)
    try:
        data = bank_post(url, payload, label='MandateNotification', txnid=notification.merchant_tran_id)
        notification.status = UpiMandateNotification.STATUS_SENT
        notification.notified_at = timezone.now()
        notification.gateway_response = data
        notification.save(update_fields=['status', 'notified_at', 'gateway_response', 'system_updated_at'])
        return _apply_notification_response(notification, data)
    except Exception as exc:
        logger.exception('MandateNotification failed: %s', exc)
        notification.status = UpiMandateNotification.STATUS_FAILED
        notification.gateway_response = {'error': str(exc)}
        notification.notified_at = timezone.now()
        notification.save(update_fields=['status', 'gateway_response', 'notified_at', 'system_updated_at'])
        raise


def execute_mandate_instalment(
    mandate: UpiMandate,
    instalment: SchemeInstalment,
    *,
    require_notification: bool = True,
) -> UpiMandateExecution | None:
    """
    Debit a scheme instalment via ExecuteMandate.
    require_notification=False for first instalment within 5 min of mandate approval.
    """
    if mandate.status != UpiMandate.STATUS_APPROVED or not mandate.umn:
        return None

    instalment.refresh_from_db(fields=['amount', 'instalment_no', 'status'])
    if instalment.status and instalment.status.code == 'PAID':
        return None

    if require_notification and instalment.instalment_no >= 2:
        pdn = (
            UpiMandateNotification.objects.filter(scheme_instalment=instalment)
            .order_by('-id')
            .first()
        )
        if not pdn or pdn.status != UpiMandateNotification.STATUS_SUCCESS:
            raise ValueError(
                f'Pre-debit notification not successful for instalment {instalment.instalment_no}'
            )

    execution = _create_execution(mandate, instalment)
    _sync_execution_amount_from_instalment(execution, instalment)
    execution.mandate_seq_no = _mandate_seq_no_for_instalment(instalment)
    execution.txn_status = UpiMandateExecution.TXN_INITIATED
    execution.save(update_fields=['mandate_seq_no', 'amount', 'txn_status', 'system_updated_at'])

    if getattr(settings, 'ICICI_MANDATE_SIMULATE', False):
        return execution

    url = getattr(settings, 'ICICI_EXECUTE_MANDATE_URL', '') or ''
    if not url:
        return execution

    payload = _execute_payload(mandate, instalment, execution)
    try:
        data = bank_post(url, payload, label='ExecuteMandate', txnid=execution.merchant_tran_id)
        return _apply_execute_mandate_response(execution, data)
    except Exception as exc:
        logger.exception('ExecuteMandate failed: %s', exc)
        execution.txn_status = UpiMandateExecution.TXN_FAILED
        execution.gateway_response = {'error': str(exc)}
        execution.save(update_fields=['txn_status', 'gateway_response', 'system_updated_at'])
    return execution


def _execute_first(mandate: UpiMandate):
    """After CREATE-SUCCESS — debit first unpaid instalment within 5 min (no PDN needed per PDF)."""
    if mandate.status != UpiMandate.STATUS_APPROVED or not mandate.umn:
        return None
    instalment = _next_unpaid_instalment(mandate.customer_scheme_id)
    if not instalment:
        return None
    return execute_mandate_instalment(mandate, instalment, require_notification=False)


# =============================================================================
# Customer hub — Create Collect (UPI ID)
# =============================================================================

def create_customer_upi_mandate(*, customer, instalment_id: int, payer_vpa: str) -> dict:
    """Collect (UPI ID) → CreateMandate. Disabled when ICICI_UPI_QR_MODE=onetime."""
    if _is_onetime_qr_mode():
        raise ValueError(
            'UPI ID auto-pay is not available in one-time pay mode. Use Scan QR or UPI apps.'
        )
    payer_vpa = (payer_vpa or '').strip().lower()
    if not payer_vpa or '@' not in payer_vpa:
        raise ValueError('Enter a valid UPI ID (example: name@oksbi)')

    instalment, cs = _validate_instalment(customer, instalment_id)
    _fail_other_pending(
        cs,
        keep_if=lambda m: (not _is_qr(m)) and (m.payer_vpa or '').lower() == payer_vpa and m.status == UpiMandate.STATUS_PENDING,
    )

    today = timezone.localdate()
    tenure = cs.tenure_months or cs.scheme.tenure_months or 11
    end = today.replace(year=today.year + max(1, (tenure // 12) + 1))
    debit_day = compute_debit_day(resolve_anchor_day(cs))

    mandate = UpiMandate.objects.create(
        customer_scheme=cs,
        merchant_tran_id=_new_txn('MND'),
        payer_vpa=payer_vpa,
        payer_mobile=customer.mobile,
        payer_name=customer.full_name,
        amount=instalment.amount,
        debit_day=debit_day,
        frequency='MT',
        start_date=today,
        end_date=end,
        status=UpiMandate.STATUS_PENDING,
        mandate_created_at=timezone.now(),
    )

    if getattr(settings, 'ICICI_MANDATE_SIMULATE', False):
        mandate.umn = f'sim{uuid.uuid4().hex[:28]}@upi'
        mandate.status = UpiMandate.STATUS_APPROVED
        mandate.mandate_approved_at = timezone.now()
        mandate.save()
        execution = _execute_first(mandate)
        if execution:
            _mark_paid(execution, gateway_response={'simulated': True}, bank_rrn=f'SIM{uuid.uuid4().hex[:10].upper()}')
        return _status_payload(UpiMandate.objects.get(id=mandate.id))

    url = getattr(settings, 'ICICI_CREATE_MANDATE_URL', '') or ''
    if not url:
        return _status_payload(mandate)

    collect_by = timezone.localtime() + timedelta(days=2)
    payload = {
        'merchantId': settings.ICICI_MERCHANT_ID,
        'subMerchantId': getattr(settings, 'ICICI_SUB_MERCHANT_ID', None) or settings.ICICI_MERCHANT_ID,
        'terminalId': getattr(settings, 'ICICI_TERMINAL_ID', '6012'),
        'merchantName': getattr(settings, 'ICICI_MERCHANT_NAME', 'Taranya'),
        'subMerchantName': getattr(settings, 'ICICI_SUB_MERCHANT_NAME', 'Jewellery Scheme'),
        'payerVa': mandate.payer_vpa,
        'amount': _money(instalment.amount),
        'note': f'Scheme instalment {instalment.instalment_no}',
        'collectByDate': collect_by.strftime('%d/%m/%Y %I:%M %p'),
        'merchantTranId': mandate.merchant_tran_id,
        'billNumber': f'CS{mandate.customer_scheme_id}',
        'validityStartDate': today.strftime('%d/%m/%Y'),
        'validityEndDate': end.strftime('%d/%m/%Y'),
        'amountLimit': 'F',
        'remark': f'CustomerScheme{mandate.customer_scheme_id}',
        'autoExecute': 'N',
        'requestType': 'C',
        'frequency': 'MT',
        'debitDay': _mandate_debit_day_str(mandate, cs),
        'debitRule': 'ON',
        'revokable': 'Y',
        'blockfund': 'N',
        'purpose': 'RECURRING',
        'validatePayerAccFlag': 'N',
    }
    data = bank_post(url, payload, label='CreateMandate', txnid=mandate.merchant_tran_id)
    if not _bank_ok(data):
        mandate.status = UpiMandate.STATUS_FAILED
        mandate.save(update_fields=['status', 'system_updated_at'])
        raise ValueError(f"CreateMandate failed: {_bank_msg(data)}")
    if data.get('BankRRN'):
        mandate.bank_rrn = str(data['BankRRN'])
        mandate.save(update_fields=['bank_rrn', 'system_updated_at'])
    return _status_payload(mandate)


# =============================================================================
# Customer hub — QR3 one-time pay (UPI/v0) OR MandateQR (UPI2) via ICICI_UPI_QR_MODE
# =============================================================================

def create_customer_upi_onetime_qr(*, customer, instalment_id: int) -> dict:
    """
    One-time UPI pay via QR3 (UPI/v0):
      POST …/QR3/{MID} → refId / qrString → upi://pay?tr=refId
      Poll TransactionStatus3 or bank callback SUCCESS → finalize payment.
    """
    instalment, cs = _validate_instalment(customer, instalment_id)

    pending = (
        Payment.objects.filter(
            instalment=instalment,
            payment_provider=PAYMENT_PROVIDER_ICICI,
            is_finalized=False,
            payment_status__code='INITIATED',
        )
        .order_by('-id')
        .first()
    )
    if pending and pending.transaction_id:
        upi = _upi_pay_string(amount=pending.amount, ref_id=pending.transaction_id)
        return _onetime_status_payload(pending, instalment, qr=_qr_response_fields(upi))

    merchant_tran_id = re.sub(r'[^A-Za-z0-9]', '', _new_txn(ONETIME_PREFIX))[:35]
    payment = create_payment_with_collections(
        instalment=instalment,
        amount=instalment.amount,
        transaction_id=merchant_tran_id,
        payment_status_code='INITIATED',
        payment_source='CP',
        payment_mode_code='UPI',
        payment_provider=PAYMENT_PROVIDER_ICICI,
    )

    if getattr(settings, 'ICICI_MANDATE_SIMULATE', False):
        upi = _upi_pay_string(amount=instalment.amount, ref_id=merchant_tran_id)
        return _onetime_status_payload(payment, instalment, qr=_qr_response_fields(upi))

    url = getattr(settings, 'ICICI_UPI_QR3_URL', '') or ''
    if not url:
        raise ValueError('ICICI_UPI_QR3_URL not set — check ICICI_API_BASE and ICICI_MERCHANT_ID in .env')

    mcc = (getattr(settings, 'ICICI_UPI_MCC', '') or '5411').strip() or '5411'
    bill_number = re.sub(r'[^A-Za-z0-9]', '', f'CS{cs.id}I{instalment.instalment_no}')[:35]
    payload = {
        'merchantId': str(settings.ICICI_MERCHANT_ID),
        'subMerchantId': str(
            getattr(settings, 'ICICI_SUB_MERCHANT_ID', None) or settings.ICICI_MERCHANT_ID
        ),
        'terminalId': str(mcc),
        'amount': _money(instalment.amount),
        'merchantTranId': merchant_tran_id,
        'billNumber': bill_number,
    }
    data = bank_post(url, payload, label='QR3', txnid=merchant_tran_id)

    if not _bank_ok(data):
        _fail_onetime_payment(payment, gateway_response=data)
        msg = _bank_msg(data)
        hint = ''
        if str(data.get('response')) == '8000' or 'encrypt' in msg.lower():
            hint = ' (code 8000 = wrong/expired bank public cert for this MID)'
        raise ValueError(f'QR3 failed: {msg}{hint}')

    ref_id = str(
        _pick(data, 'refId', 'RefId', 'referenceId', 'ReferenceId', 'merchantTranId')
        or merchant_tran_id
    )
    bank_rrn = _pick(data, 'BankRRN', 'bankRRN')
    if bank_rrn:
        payment.gateway_transaction_id = str(bank_rrn)
        payment.save(update_fields=['gateway_transaction_id', 'system_updated_at'])

    bank_qr = str(
        _pick(data, 'qrString', 'QRString', 'qr', 'QR', 'intent', 'Intent', 'upiString') or ''
    )
    upi = bank_qr if bank_qr.startswith('upi://') else _upi_pay_string(
        amount=instalment.amount, ref_id=ref_id
    )
    print('QR3 intent/QR string:', upi)
    return _onetime_status_payload(payment, instalment, qr=_qr_response_fields(upi))


def create_customer_upi_mandate_qr(*, customer, instalment_id: int) -> dict:
    """Route to QR3 one-time pay or MandateQR AutoPay based on ICICI_UPI_QR_MODE."""
    if _is_onetime_qr_mode():
        return create_customer_upi_onetime_qr(customer=customer, instalment_id=instalment_id)
    return _create_customer_upi_mandate_qr(customer=customer, instalment_id=instalment_id)


def _create_customer_upi_mandate_qr(*, customer, instalment_id: int) -> dict:
    """
    Scan QR or open GPay/PhonePe:
      1) POST MandateQR → bank returns upi://mandate… (or we build it)
      2) Customer approves AutoPay in app
      3) Callback CREATE-SUCCESS → we call ExecuteMandate → first instalment paid
      4) Later months: Notification + Execute (same as UPI-ID Collect)

    This is AutoPay, not one-time QR3 pay.
    """
    instalment, cs = _validate_instalment(customer, instalment_id)

    reused = _fail_other_pending(
        cs,
        keep_if=lambda m: _is_qr(m) and m.status == UpiMandate.STATUS_PENDING,
    )
    if reused:
        stored_qr = (getattr(reused, 'qr_string', None) or '').strip()
        if stored_qr:
            return _status_payload(reused, qr=_qr_response_fields(stored_qr))
        mandate = reused
    else:
        today = timezone.localdate()
        tenure = cs.tenure_months or cs.scheme.tenure_months or 11
        end = today.replace(year=today.year + max(1, (tenure // 12) + 1))
        debit_day = compute_debit_day(resolve_anchor_day(cs))

        mandate = UpiMandate.objects.create(
            customer_scheme=cs,
            merchant_tran_id=_new_txn(QR_PREFIX),
            payer_mobile=customer.mobile,
            payer_name=customer.full_name,
            amount=instalment.amount,
            debit_day=debit_day,
            frequency='MT',
            start_date=today,
            end_date=end,
            status=UpiMandate.STATUS_PENDING,
            mandate_created_at=timezone.now(),
        )

    # Local simulate: wait for poll (~6s) then approve + debit
    if getattr(settings, 'ICICI_MANDATE_SIMULATE', False):
        upi = _upi_mandate_string(mandate)
        return _status_payload(mandate, qr=_qr_response_fields(upi))

    url = getattr(settings, 'ICICI_MANDATE_QR_URL', '') or ''
    if not url:
        raise ValueError('ICICI_MANDATE_QR_URL not set — check ICICI_API_BASE in .env')

    txn = re.sub(r'[^A-Za-z0-9]', '', mandate.merchant_tran_id)[:35]
    if txn != mandate.merchant_tran_id:
        mandate.merchant_tran_id = txn
        mandate.save(update_fields=['merchant_tran_id', 'system_updated_at'])

    today = mandate.start_date or timezone.localdate()
    end = mandate.end_date or today.replace(year=today.year + 2)
    collect_by = timezone.localtime() + timedelta(days=2)
    _apply_mandate_debit_fields(mandate, cs, instalment)
    # Same fields as CreateMandate, but no payerVa — customer VPA comes from scan/callback
    payload = {
        'merchantId': str(settings.ICICI_MERCHANT_ID),
        'subMerchantId': str(getattr(settings, 'ICICI_SUB_MERCHANT_ID', None) or settings.ICICI_MERCHANT_ID),
        'terminalId': str(getattr(settings, 'ICICI_TERMINAL_ID', '6012') or '6012'),
        'merchantName': getattr(settings, 'ICICI_MERCHANT_NAME', 'Taranya'),
        'subMerchantName': getattr(settings, 'ICICI_SUB_MERCHANT_NAME', 'Jewellery Scheme'),
        'amount': _money(instalment.amount),
        'note': f'Scheme autopay {instalment.instalment_no}',
        'collectByDate': collect_by.strftime('%d/%m/%Y %I:%M %p'),
        'merchantTranId': txn,
        'billNumber': re.sub(r'[^A-Za-z0-9]', '', f'CS{cs.id}')[:35],
        'validityStartDate': today.strftime('%d/%m/%Y'),
        'validityEndDate': end.strftime('%d/%m/%Y'),
        'amountLimit': 'F',
        'remark': f'CustomerScheme{cs.id}',
        'autoExecute': 'N',
        'requestType': 'C',
        'frequency': 'MT',
        'debitDay': _mandate_debit_day_str(mandate, cs),
        'debitRule': 'ON',
        'revokable': 'Y',
        'blockfund': 'N',
        'purpose': 'RECURRING',
    }
    data = bank_post(url, payload, label='MandateQR', txnid=txn)

    if not _bank_ok(data):
        mandate.status = UpiMandate.STATUS_FAILED
        mandate.save(update_fields=['status', 'system_updated_at'])
        msg = _bank_msg(data)
        hint = ''
        if str(data.get('response')) == '8000' or 'encrypt' in msg.lower():
            hint = ' (code 8000 = wrong/expired bank public .pem)'
        raise ValueError(f'MandateQR failed: {msg}{hint}')

    if data.get('BankRRN'):
        mandate.bank_rrn = str(data['BankRRN'])
        mandate.save(update_fields=['bank_rrn', 'system_updated_at'])

    bank_qr = _extract_bank_qr_string(data)
    if not bank_qr:
        mandate.status = UpiMandate.STATUS_FAILED
        mandate.save(update_fields=['status', 'system_updated_at'])
        logger.error(
            'MandateQR success but no QR payload in response keys=%s',
            list(data.keys()),
        )
        raise ValueError(
            'MandateQR succeeded but ICICI did not return SignedQRData/qrString. '
            'Check MandateQR RESPONSE in server logs.'
        )

    ref_id = _pick(data, 'refId', 'RefId', 'referenceId')
    if ref_id and not mandate.bank_rrn:
        mandate.bank_rrn = str(ref_id)

    mandate.qr_string = bank_qr
    mandate.save(update_fields=['qr_string', 'bank_rrn', 'system_updated_at'])
    print('MandateQR intent/QR string:', bank_qr[:200])
    return _status_payload(mandate, qr=_qr_response_fields(bank_qr))


# =============================================================================
# Status poll
# =============================================================================

def get_upi_onetime_qr_status_for_customer(customer, payment_id: int) -> dict:
    """Poll QR3 payment — payment_id is passed as mandate_id from the hub for compatibility."""
    payment = Payment.objects.select_related(
        'instalment', 'instalment__customer_scheme', 'payment_status'
    ).get(
        id=payment_id,
        instalment__customer_scheme__customer=customer,
        payment_provider=PAYMENT_PROVIDER_ICICI,
    )
    instalment = payment.instalment
    simulate = getattr(settings, 'ICICI_MANDATE_SIMULATE', False)

    if simulate and not payment.is_finalized:
        created = payment.system_created_at
        if created and (timezone.now() - created).total_seconds() >= 6:
            _finalize_onetime_payment(
                payment, gateway_response={'simulated': True}, bank_rrn=f'SIM{uuid.uuid4().hex[:10].upper()}'
            )
            payment.refresh_from_db()

    if not simulate and not payment.is_finalized:
        data = _poll_onetime_qr_bank_status(payment)
        if data:
            payment = _apply_onetime_bank_status(payment, data)
            payment.refresh_from_db()

    upi = None
    if not payment.is_finalized and payment.transaction_id:
        upi = _qr_response_fields(
            _upi_pay_string(amount=payment.amount, ref_id=payment.transaction_id)
        )
    return _onetime_status_payload(payment, instalment, qr=upi)


def get_upi_mandate_status_for_customer(customer, mandate_id: int) -> dict:
    if _is_onetime_qr_mode():
        return get_upi_onetime_qr_status_for_customer(customer, mandate_id)

    mandate = UpiMandate.objects.select_related('customer_scheme').get(
        id=mandate_id, customer_scheme__customer=customer,
    )
    simulate = getattr(settings, 'ICICI_MANDATE_SIMULATE', False)

    # Simulate QR: auto-approve ~6s after create
    if simulate and _is_qr(mandate) and mandate.status == UpiMandate.STATUS_PENDING:
        created = mandate.mandate_created_at or mandate.system_created_at
        if created and (timezone.now() - created).total_seconds() >= 6:
            mandate.payer_vpa = mandate.payer_vpa or 'simuser@upi'
            mandate.umn = f'sim{uuid.uuid4().hex[:28]}@upi'
            mandate.status = UpiMandate.STATUS_APPROVED
            mandate.mandate_approved_at = timezone.now()
            mandate.save()
            execution = _execute_first(mandate)
            if execution:
                _mark_paid(execution, gateway_response={'simulated': True}, bank_rrn=mandate.bank_rrn)
            mandate = UpiMandate.objects.get(id=mandate.id)

    if not simulate and mandate.status == UpiMandate.STATUS_PENDING:
        # Poll TransactionStatus first (reliable); ByCriteria often 500 on prod — optional fallback.
        for url_attr in ('ICICI_TRANSACTION_STATUS_URL', 'ICICI_TRANSACTION_STATUS_BY_CRITERIA_URL'):
            url = getattr(settings, url_attr, '') or ''
            if not url:
                continue
            body = {
                'merchantId': str(settings.ICICI_MERCHANT_ID),
                'subMerchantId': str(
                    getattr(settings, 'ICICI_SUB_MERCHANT_ID', None) or settings.ICICI_MERCHANT_ID
                ),
                'terminalId': str(getattr(settings, 'ICICI_TERMINAL_ID', '5411') or '5411'),
                'merchantTranId': mandate.merchant_tran_id,
                'transactionType': 'M',
            }
            try:
                data = bank_post(url, body, label='MandateStatus', txnid=mandate.merchant_tran_id)
            except Exception as exc:
                logger.warning('ICICI MandateStatus failed (%s): %s', url_attr, exc)
                if 'cannot encrypt' in str(exc).lower() or '8000' in str(exc):
                    break
                continue
            if str(data.get('response')) == '8000':
                logger.error(
                    'ICICI MandateStatus: Invalid Encrypted Request (8000) — '
                    'ICICI_BANK_PUBLIC_KEY_PATH must be the current Hybrid public cert from ICICI '
                    '(not merchantEncryption.pem / oneashish_ssl_public.txt).'
                )
                break
            if int(data.get('_http_status') or 0) >= 500:
                logger.warning('ICICI MandateStatus %s returned HTTP %s — trying next endpoint',
                               url_attr, data.get('_http_status'))
                continue
            st = str(_pick(data, 'TxnStatus', 'txnStatus', 'status', 'Status', default='')).upper().replace(' ', '-')
            umn = _pick(data, 'UMN', 'umn')
            if 'CREATE-SUCCESS' in st or (umn and 'FAIL' not in st):
                mandate.umn = str(umn or mandate.umn or '')
                mandate.bank_rrn = str(
                    _pick(data, 'OriginalBankRRN', 'BankRRN', 'bankRRN') or mandate.bank_rrn or ''
                ) or mandate.bank_rrn
                mandate.payer_vpa = _pick(data, 'payerVA', 'PayerVA') or mandate.payer_vpa
                mandate.status = UpiMandate.STATUS_APPROVED
                mandate.mandate_approved_at = timezone.now()
                mandate.save()
                _execute_first(mandate)  # first instalment debit after AutoPay approve
                mandate = UpiMandate.objects.get(id=mandate.id)
                break
            if 'CREATE-FAIL' in st or st in ('CREATE-FAILURE', 'FAIL', 'FAILURE'):
                mandate.status = UpiMandate.STATUS_FAILED
                mandate.save(update_fields=['status', 'system_updated_at'])
                break
            if st == 'CREATE-INITIATED':
                # QR/mandate created at bank — customer must approve in UPI app; keep polling.
                break

    return _status_payload(mandate)


# =============================================================================
# Bank callback
# =============================================================================

def process_icici_callback(request) -> dict:
    """ICICI posts encrypted/plain JSON here after mandate approve / debit."""
    # Read body
    try:
        body = request.data if hasattr(request, 'data') else json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        body = {}
    if isinstance(body, dict) and 'encryptedData' in body:
        try:
            body = _decrypt_from_bank(body)
        except Exception as exc:
            logger.exception('Callback decrypt failed: %s', exc)
            return {'message': 'decrypt failed', 'error': str(exc)}
    if not isinstance(body, dict):
        body = {'raw': body}

    txn_status = str(_pick(body, 'TxnStatus', 'txnStatus', 'status', 'Status', default='') or '').upper().replace(' ', '-')
    merchant_tran_id = _pick(body, 'merchantTranId', 'MerchantTranId')
    umn = _pick(body, 'UMN', 'umn')
    bank_rrn = _pick(body, 'BankRRN', 'bankRRN', 'OriginalBankRRN')
    payer_vpa = _pick(body, 'PayerVA', 'payerVA', 'PayerVa')

    logger.info('ICICI callback TxnStatus=%s merchantTranId=%s', txn_status, merchant_tran_id)

    try:
        with transaction.atomic():
            result = {}

            # QR3 one-time pay (UPI/v0) — SUCCESS / FAILURE (not mandate CREATE-*)
            if merchant_tran_id and (
                txn_status in ('SUCCESS', 'TXN-SUCCESS', 'TXN_SUCCESS', 'COMPLETED')
                or (txn_status.endswith('SUCCESS') and 'CREATE' not in txn_status and 'EXECUTE' not in txn_status)
            ):
                payment = (
                    Payment.objects.select_for_update()
                    .filter(
                        transaction_id=merchant_tran_id,
                        payment_provider=PAYMENT_PROVIDER_ICICI,
                        is_finalized=False,
                    )
                    .first()
                )
                if payment:
                    pay = _finalize_onetime_payment(payment, gateway_response=body, bank_rrn=bank_rrn)
                    result = {'payment_id': pay.id, 'onetime': True}
                    PaymentAuditLog.objects.create(
                        txnid=merchant_tran_id,
                        type='ICICI_UPI_CALLBACK',
                        status=txn_status or 'SUCCESS',
                        request_payload=body,
                        response_json={'result': result},
                    )
                    return {'message': 'Callback processed', 'type': txn_status, 'result': result}

            if merchant_tran_id and (
                'FAIL' in txn_status
                and 'CREATE' not in txn_status
                and 'EXECUTE' not in txn_status
            ):
                payment = (
                    Payment.objects.select_for_update()
                    .filter(
                        transaction_id=merchant_tran_id,
                        payment_provider=PAYMENT_PROVIDER_ICICI,
                        is_finalized=False,
                    )
                    .first()
                )
                if payment:
                    pay = _fail_onetime_payment(payment, gateway_response=body)
                    result = {'payment_id': pay.id, 'onetime': True, 'failed': True}
                    PaymentAuditLog.objects.create(
                        txnid=merchant_tran_id,
                        type='ICICI_UPI_CALLBACK',
                        status=txn_status or 'FAILED',
                        request_payload=body,
                        response_json={'result': result},
                    )
                    return {'message': 'Callback processed', 'type': txn_status, 'result': result}

            if 'CREATE-SUCCESS' in txn_status or txn_status == 'CREATE_SUCCESS':
                mandate = UpiMandate.objects.select_for_update().get(merchant_tran_id=merchant_tran_id)
                if mandate.status != UpiMandate.STATUS_APPROVED or not mandate.umn:
                    mandate.umn = umn or mandate.umn
                    mandate.bank_rrn = bank_rrn or mandate.bank_rrn
                    mandate.payer_vpa = payer_vpa or mandate.payer_vpa
                    mandate.payer_name = _pick(body, 'PayerName') or mandate.payer_name
                    mandate.payer_mobile = _pick(body, 'PayerMobile') or mandate.payer_mobile
                    mandate.status = UpiMandate.STATUS_APPROVED
                    mandate.mandate_approved_at = timezone.now()
                    mandate.save()
                    _execute_first(mandate)
                result = {'mandate_id': mandate.id}

            elif 'CREATE-FAIL' in txn_status or 'CREATE-FAILURE' in txn_status:
                mandate = UpiMandate.objects.select_for_update().get(merchant_tran_id=merchant_tran_id)
                mandate.status = UpiMandate.STATUS_FAILED
                mandate.save(update_fields=['status', 'system_updated_at'])
                result = {'mandate_id': mandate.id}

            elif 'NOTIFICATION-SUCCESS' in txn_status or txn_status in ('NOTIFICATION_SUCCESS',):
                notification = UpiMandateNotification.objects.select_for_update().get(
                    merchant_tran_id=merchant_tran_id
                )
                notification.status = UpiMandateNotification.STATUS_SUCCESS
                notification.gateway_response = body
                notification.notified_at = notification.notified_at or timezone.now()
                notification.save(update_fields=['status', 'gateway_response', 'notified_at', 'system_updated_at'])
                result = {'notification_id': notification.id}

            elif 'NOTIFICATION-FAIL' in txn_status or 'NOTIFICATION-FAILURE' in txn_status:
                notification = UpiMandateNotification.objects.select_for_update().get(
                    merchant_tran_id=merchant_tran_id
                )
                notification.status = UpiMandateNotification.STATUS_FAILED
                notification.gateway_response = body
                notification.notified_at = notification.notified_at or timezone.now()
                notification.save(update_fields=['status', 'gateway_response', 'notified_at', 'system_updated_at'])
                result = {'notification_id': notification.id, 'failed': True}

            elif 'EXECUTE-SUCCESS' in txn_status or txn_status in ('SUCCESS', 'EXECUTE_SUCCESS'):
                try:
                    execution = UpiMandateExecution.objects.select_for_update().get(
                        merchant_tran_id=merchant_tran_id
                    )
                except UpiMandateExecution.DoesNotExist:
                    mandate = UpiMandate.objects.select_for_update().get(merchant_tran_id=merchant_tran_id)
                    instalment = _next_unpaid_instalment(mandate.customer_scheme_id)
                    if not instalment:
                        raise ValueError('No pending instalment for EXECUTE callback')
                    execution = _create_execution(mandate, instalment, merchant_tran_id=merchant_tran_id)
                payment = _mark_paid(execution, gateway_response=body, bank_rrn=bank_rrn)
                result = {'execution_id': execution.id, 'payment_id': payment.id}

            elif 'EXECUTE-FAIL' in txn_status or 'EXECUTE-FAILURE' in txn_status:
                execution = UpiMandateExecution.objects.select_for_update().get(
                    merchant_tran_id=merchant_tran_id
                )
                payment = _mark_failed(execution, gateway_response=body, bank_rrn=bank_rrn)
                result = {'execution_id': execution.id, 'payment_id': payment.id, 'failed': True}

            elif 'REVOKE-SUCCESS' in txn_status:
                mandate = (
                    UpiMandate.objects.select_for_update().filter(merchant_tran_id=merchant_tran_id).first()
                    or UpiMandate.objects.select_for_update().filter(umn=umn).first()
                )
                if mandate:
                    mandate.status = UpiMandate.STATUS_REVOKED
                    mandate.revoked_at = timezone.now()
                    mandate.save(update_fields=['status', 'revoked_at', 'system_updated_at'])
                    result = {'mandate_id': mandate.id}

            else:
                result = {'skipped': True, 'reason': txn_status or 'unknown'}

        PaymentAuditLog.objects.create(
            txnid=merchant_tran_id or umn or 'UNKNOWN',
            type='ICICI_UPI_CALLBACK',
            status=txn_status or 'UNKNOWN',
            request_payload=body,
            response_json={'result': result},
        )
        return {'message': 'Callback processed', 'type': txn_status, 'result': result}
    except Exception as exc:
        logger.exception('ICICI callback error: %s', exc)
        return {'message': 'Callback received', 'type': txn_status, 'error': str(exc)}


# =============================================================================
# Revoke when scheme fully paid
# =============================================================================

def _payable_instalments_fully_paid(customer_scheme_id: int) -> bool:
    """
    True when every customer-payable instalment is PAID.
    Bonus months (10+1 / 11+1) are is_bonus=True and never count toward autopay / revoke.
    Works whether customer started autopay on instalment 1 or mid-scheme (e.g. 4th).
    """
    try:
        paid_lv = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
    except LookupValue.DoesNotExist:
        return False

    payable = SchemeInstalment.objects.filter(
        customer_scheme_id=customer_scheme_id,
        is_bonus=False,
    )
    payable_count = payable.count()
    if payable_count <= 0:
        return False
    unpaid = payable.exclude(status=paid_lv).exists()
    return not unpaid


def maybe_revoke_mandate_when_scheme_completed(customer_scheme_id: int) -> None:
    """
    After the last payable instalment is paid (not the bonus month), revoke ICICI AutoPay.
    Safe for mid-scheme start: cash months 1–3 + autopay 4–10 → revoke when 10th is paid.
    """
    if not _payable_instalments_fully_paid(customer_scheme_id):
        return

    mandate = (
        UpiMandate.objects.filter(customer_scheme_id=customer_scheme_id, status=UpiMandate.STATUS_APPROVED)
        .order_by('-id')
        .first()
    )
    if not mandate or not mandate.umn:
        return
    try:
        request_upi_mandate_revoke(mandate)
    except Exception as exc:
        logger.exception('Revoke failed mandate_id=%s: %s', mandate.id, exc)


@transaction.atomic
def request_upi_mandate_revoke(mandate: UpiMandate) -> dict:
    mandate = UpiMandate.objects.select_for_update().get(id=mandate.id)
    if mandate.status != UpiMandate.STATUS_APPROVED:
        return {'skipped': True, 'reason': f'status_{mandate.status}'}
    if not mandate.umn:
        raise ValueError('UMN is required to revoke')

    if getattr(settings, 'ICICI_MANDATE_SIMULATE', False):
        mandate.status = UpiMandate.STATUS_REVOKED
        mandate.revoked_at = timezone.now()
        mandate.save(update_fields=['status', 'revoked_at', 'system_updated_at'])
        return {'simulated': True, 'mandate_id': mandate.id}

    url = getattr(settings, 'ICICI_CREATE_MANDATE_URL', '') or ''
    if not url:
        return {'skipped': True, 'reason': 'not_configured'}

    today = timezone.localdate()
    start = mandate.start_date or today
    end = mandate.end_date or today
    revoke_tran = _new_txn('RVK')
    payload = {
        'merchantId': settings.ICICI_MERCHANT_ID,
        'subMerchantId': getattr(settings, 'ICICI_SUB_MERCHANT_ID', None) or settings.ICICI_MERCHANT_ID,
        'terminalId': getattr(settings, 'ICICI_TERMINAL_ID', '6012'),
        'merchantName': getattr(settings, 'ICICI_MERCHANT_NAME', 'Taranya'),
        'subMerchantName': getattr(settings, 'ICICI_SUB_MERCHANT_NAME', 'Jewellery Scheme'),
        'payerVa': mandate.payer_vpa or '',
        'amount': _money(mandate.amount),
        'note': f'Revoke scheme {mandate.customer_scheme_id}',
        'collectByDate': (timezone.localtime() + timedelta(days=2)).strftime('%d/%m/%Y %I:%M %p'),
        'merchantTranId': revoke_tran,
        'billNumber': f'RVK{mandate.customer_scheme_id}{uuid.uuid4().hex[:6]}',
        'validityStartDate': start.strftime('%d/%m/%Y'),
        'validityEndDate': end.strftime('%d/%m/%Y'),
        'amountLimit': 'F',
        'remark': f'RevokeCS{mandate.customer_scheme_id}',
        'autoExecute': 'N',
        'requestType': 'R',
        'frequency': mandate.frequency or 'MT',
        'debitDay': _mandate_debit_day_str(mandate, mandate.customer_scheme),
        'debitRule': 'ON',
        'revokable': 'Y',
        'blockfund': 'N',
        'purpose': 'RECURRING',
        'UMN': mandate.umn,
        'validatePayerAccFlag': 'N',
    }
    data = bank_post(url, payload, label='RevokeMandate', txnid=revoke_tran)
    if not _bank_ok(data):
        raise ValueError(_bank_msg(data, 'Revoke failed'))
    return {'mandate_id': mandate.id, 'revoke_merchant_tran_id': revoke_tran, 'initiated': True, 'response': data}
