"""
ICICI Orange PG (TSP): initiateSale, Payment Advice, STATUS verify, result application.
Uses webhook_status + esbuzz_verify_status columns with resolve_payment() dual-confirm.
"""
import logging
from datetime import datetime
from typing import Any, Optional

import requests
from django.conf import settings
from django.utils import timezone

from shared.orange_pg import build_secure_hash, verify_secure_hash
from shared.services.payment_status_service import map_webhook_status_to_lookup

logger = logging.getLogger(__name__)

PAYMENT_PROVIDER_ORANGE = "ORANGE_PG"

# Browser / advice success codes from Orange PG spec
_SUCCESS_CODES = {"000", "0000"}
# Initiate redirect accepted (not final payment success)
_INITIATED_CODES = {"R1000"}


def _secret() -> str:
    return (getattr(settings, "ORANGE_PG_SECRET_KEY", None) or "").strip()


def _merchant_id() -> str:
    return (getattr(settings, "ORANGE_PG_MERCHANT_ID", None) or "").strip()


def _aggregator_id() -> str:
    return (getattr(settings, "ORANGE_PG_AGGREGATOR_ID", None) or "").strip()


def is_success_response_code(code) -> bool:
    return str(code or "").strip() in _SUCCESS_CODES


def is_txn_success(data: dict) -> bool:
    """Final payment success from return/advice/STATUS payload."""
    if not isinstance(data, dict):
        return False
    txn_status = str(data.get("txnStatus") or "").strip().upper()
    if txn_status == "SUC":
        return True
    if txn_status in ("FAL", "FAIL", "FAILED", "REJ", "REJECTED"):
        return False
    code = data.get("txnResponseCode") or data.get("responseCode")
    return is_success_response_code(code)


def normalize_mobile(mobile: Optional[str]) -> str:
    digits = "".join(ch for ch in str(mobile or "") if ch.isdigit())
    if len(digits) == 10:
        return f"91{digits}"
    return digits or "910000000000"


def txn_date_now() -> str:
    return timezone.localtime().strftime("%Y%m%d%H%M%S")


def build_initiate_payload(*, merchant_txn_no, amount, customer, return_url, addl1="", addl2="") -> dict:
    first_name = (customer.full_name or "Customer").split()[0]
    email = (customer.email or "").strip() or "customer@example.com"
    payload = {
        "merchantId": _merchant_id(),
        "aggregatorID": _aggregator_id(),
        "merchantTxnNo": str(merchant_txn_no),
        "amount": f"{float(amount):.2f}",
        "currencyCode": "356",
        "payType": "0",
        "customerEmailID": email[:48],
        "transactionType": "SALE",
        "returnURL": return_url,
        "txnDate": txn_date_now(),
        "customerMobileNo": normalize_mobile(getattr(customer, "mobile", None)),
        "customerName": (customer.full_name or first_name)[:45],
        "addlParam1": str(addl1 or "")[:64],
        "addlParam2": str(addl2 or "")[:64],
    }
    # Drop empty optional addl params so hash matches bank rules
    if not payload["addlParam1"]:
        payload.pop("addlParam1")
    if not payload["addlParam2"]:
        payload.pop("addlParam2")
    if not payload["aggregatorID"]:
        payload.pop("aggregatorID")
    payload["secureHash"] = build_secure_hash(payload, _secret())
    return payload


def call_initiate_sale(payload: dict) -> tuple[Optional[dict], Optional[Exception]]:
    url = getattr(settings, "ORANGE_PG_INITIATE_URL", "") or ""
    if not url:
        return None, ValueError("ORANGE_PG_INITIATE_URL is not configured")
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=30,
        )
        try:
            data = resp.json()
        except ValueError:
            return {"raw": resp.text, "http_status": resp.status_code}, ValueError(
                f"Orange PG returned non-JSON: {resp.text[:300]}"
            )
        if resp.status_code >= 400:
            return data, ValueError(data.get("respDescription") or f"HTTP {resp.status_code}")
        return data, None
    except requests.RequestException as exc:
        return None, exc


def build_payment_url(initiate_response: dict) -> str:
    redirect_uri = (
        initiate_response.get("redirectURI")
        or initiate_response.get("redirectUri")
        or ""
    ).strip()
    tran_ctx = (initiate_response.get("tranCtx") or "").strip()
    if not redirect_uri or not tran_ctx:
        raise ValueError("Orange PG initiate response missing redirectURI/tranCtx")
    sep = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{sep}tranCtx={tran_ctx}"


def build_status_payload(merchant_txn_no: str) -> dict:
    payload = {
        "merchantId": _merchant_id(),
        "merchantTxnNo": str(merchant_txn_no),
        "originalTxnNo": str(merchant_txn_no),
        "transactionType": "STATUS",
    }
    agg = _aggregator_id()
    if agg:
        payload["aggregatorID"] = agg
    payload["secureHash"] = build_secure_hash(payload, _secret())
    return payload


def extract_status_from_response(data: Optional[dict]) -> str:
    """Return SUCCESS | FAILED | UNKNOWN from STATUS / return payload."""
    if not isinstance(data, dict):
        return "UNKNOWN"
    txn_status = str(data.get("txnStatus") or "").strip().upper()
    if txn_status == "SUC" or is_txn_success(data):
        return "SUCCESS"
    if txn_status in ("FAL", "FAIL", "FAILED", "REJ", "REJECTED"):
        return "FAILED"
    code = str(data.get("txnResponseCode") or data.get("responseCode") or "").strip()
    if code in _INITIATED_CODES or txn_status in ("PEN", "PENDING", "INI", "INIT", ""):
        if code and code not in _SUCCESS_CODES and code not in _INITIATED_CODES:
            return "FAILED"
        return "UNKNOWN"
    if is_success_response_code(code):
        return "SUCCESS"
    if code:
        return "FAILED"
    return "UNKNOWN"


def call_status_api(merchant_txn_no: str):
    """
    Returns: (raw_text, response_dict_or_None, gateway_status SUCCESS|FAILED|UNKNOWN, error_or_None)
    """
    url = getattr(settings, "ORANGE_PG_COMMAND_URL", "") or ""
    payload = build_status_payload(merchant_txn_no)
    raw = ""
    data = None
    try:
        resp = requests.post(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        raw = resp.text
        try:
            data = resp.json()
        except ValueError:
            data = None
        resp.raise_for_status()
        return raw, data, extract_status_from_response(data), None
    except requests.RequestException as exc:
        return raw, data, "UNKNOWN", exc


def validate_callback_hash(data: dict) -> bool:
    return verify_secure_hash(data, _secret(), data.get("secureHash") or data.get("securehash"))


def validate_callback_amount(payment, data: dict) -> bool:
    try:
        expected = float(payment.instalment.amount)
        actual = float(data.get("amount") or data.get("Amount") or 0)
    except (TypeError, ValueError):
        return False
    if actual == 0:
        # Some return payloads omit amount; allow and rely on STATUS
        return True
    return round(expected, 2) == round(actual, 2)


def apply_advice_or_return_result(payment, data: dict):
    """
    Map Orange return/advice into webhook_status (dual-confirm left half).
    Stores gateway txn ids on success.
    """
    success = is_txn_success(data)
    status_str = "success" if success else "failure"
    # UPI out-of-band initiated — keep pending (no webhook success/fail yet)
    code = str(data.get("responseCode") or "").strip()
    if code in _INITIATED_CODES and not success:
        return payment

    lookup = map_webhook_status_to_lookup(status_str)
    payment.webhook_status = lookup
    if success:
        payment.gateway_transaction_id = str(
            data.get("txnID") or data.get("paymentID") or payment.gateway_transaction_id or ""
        ) or payment.gateway_transaction_id
    payment.save()
    return payment


def apply_status_gateway_string(gateway_status: str) -> str:
    """Normalize to success/failure for map_verify_status_to_lookup."""
    s = (gateway_status or "").strip().upper()
    if s == "SUCCESS":
        return "success"
    if s == "FAILED":
        return "failure"
    return "failure"
