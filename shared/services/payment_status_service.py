"""
Payment status mapping: gateway status strings to LookupValue.
Centralizes all LookupValue fetching for webhook and verify flows.
Normalizes gateway status to SUCCESS or FAILED only (unknown/other -> FAILED).
"""
from shared.models import LookupValue


def normalize_gateway_status(status_string):
    """
    Normalize gateway status to "SUCCESS" or "FAILED" only.
    SUCCESS -> SUCCESS; FAILED and all other (PENDING, ERROR, CANCELLED, TIMEOUT, UNKNOWN, NULL) -> FAILED.
    """
    s = (status_string or "").strip().lower()
    if s == "success":
        return "SUCCESS"
    return "FAILED"


def map_webhook_status_to_lookup(status_string):
    """
    Map webhook status string to WEBHOOK_STATUS LookupValue.
    Uses normalize_gateway_status: only SUCCESS or FAILED.
    """
    code = normalize_gateway_status(status_string)
    return LookupValue.objects.get(lookup__code="WEBHOOK_STATUS", code=code)


def map_verify_status_to_lookup(gateway_status_string):
    """
    Map verify API gateway status string to ESBUZZ_VERIFY_STATUS LookupValue.
    Uses normalize_gateway_status: only SUCCESS or FAILED (no PENDING for resolution).
    """
    code = normalize_gateway_status(gateway_status_string)
    return LookupValue.objects.get(lookup__code="ESBUZZ_VERIFY_STATUS", code=code)


def get_verify_success_failed_lookups():
    """
    Return (success, failed) LookupValues for ESBUZZ_VERIFY_STATUS.
    Used for idempotency check: if payment already has success or failed, return early.
    """
    success = LookupValue.objects.get(lookup__code="ESBUZZ_VERIFY_STATUS", code="SUCCESS")
    failed = LookupValue.objects.get(lookup__code="ESBUZZ_VERIFY_STATUS", code="FAILED")
    return success, failed


def get_payment_status_lookups():
    """
    Return LookupValues for PAYMENT_STATUS used in resolution.
    Uses SUCCESS and FAILED for payment status; UNDER_REVIEW/REJECTED if present.
    """
    return {
        "INITIATED": LookupValue.objects.get(lookup__code="PAYMENT_STATUS", code="INITIATED"),
        "UNDER_REVIEW": LookupValue.objects.get(lookup__code="PAYMENT_STATUS", code="UNDER_REVIEW"),
        "SUCCESS": LookupValue.objects.get(lookup__code="PAYMENT_STATUS", code="SUCCESS"),
        "FAILED": LookupValue.objects.get(lookup__code="PAYMENT_STATUS", code="FAILED"),
        "REJECTED": LookupValue.objects.get(lookup__code="PAYMENT_STATUS", code="REJECTED"),
    }
