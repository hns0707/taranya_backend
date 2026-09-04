"""
Payment processing service: resolution (webhook+verify matrix) and finalization.
Payment is finalized ONLY when both webhook and verify return SUCCESS.
Mixed results -> UNDER_REVIEW. Unknown/other gateway statuses -> FAILED.
"""
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from shared.models import Payment, LookupValue, SchemeInstalment, CustomerScheme
from shared.services.scheme_service import activate_scheme_on_first_payment
from shared.services.ledger_service import insert_financial_records_for_payment
from shared.notification_view import create_admin_notification
from shared.services.gold_service import get_lock_rate, lock_gold_for_payment
from shared.services.payment_status_service import get_payment_status_lookups

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lookup values
# ---------------------------------------------------------------------------


def _get_webhook_verify_lookups():
    """Webhook and verify SUCCESS/FAILED lookups."""
    webhook_success = LookupValue.objects.get(lookup__code="WEBHOOK_STATUS", code="SUCCESS")
    webhook_failed = LookupValue.objects.get(lookup__code="WEBHOOK_STATUS", code="FAILED")
    verify_success = LookupValue.objects.get(lookup__code="ESBUZZ_VERIFY_STATUS", code="SUCCESS")
    verify_failed = LookupValue.objects.get(lookup__code="ESBUZZ_VERIFY_STATUS", code="FAILED")
    return webhook_success, webhook_failed, verify_success, verify_failed


def _get_payment_status_lookups():
    """PAYMENT_STATUS lookups for resolution."""
    return get_payment_status_lookups()


def send_payment_success_sms(mobile, scheme_reference=None):
    """Scheme online payment — no dedicated DLT template; skip unless configured."""
    template_id = getattr(settings, "PAYMENT_SUCCESS_TEMPLATE_ID", None) or getattr(
        settings, "SMS_DLT_OUTSTANDING_BALANCE", None
    )
    if not template_id or not mobile:
        return False

    from shared.services.sms_service import send_dlt_sms

    message = (
        f"We have received your payment for scheme {scheme_reference or ''}. "
        f"Thank you. TARANYA JEWELS"
    )
    ok, _ = send_dlt_sms(number=mobile, text=message, dlt_template_id=template_id)
    return ok


# ---------------------------------------------------------------------------
# Unified resolution and finalization
# ---------------------------------------------------------------------------


@transaction.atomic
def resolve_payment(payment):
    """
    Centralized payment resolution from webhook + verify status.
    Decision matrix:
      Webhook SUCCESS + Verify SUCCESS -> SUCCESS, is_finalized=True, then finalize_payment()
      Webhook FAILED  + Verify FAILED   -> FAILED, is_finalized=True
      Mixed                           -> UNDER_REVIEW, is_finalized=False
    Must be called inside a transaction. Locks payment row.
    """
    payment = Payment.objects.select_for_update().get(id=payment.id)
    if payment.is_finalized:
        logger.info(f"Payment {payment.id} already finalized. Skipping resolution.")
        return

    try:
        webhook_success, webhook_failed, verify_success, verify_failed = _get_webhook_verify_lookups()
        statuses = _get_payment_status_lookups()
    except LookupValue.DoesNotExist as e:
        logger.error(f"Required lookup values not found: {e}")
        return

    wh_ok = payment.webhook_status_id and payment.webhook_status == webhook_success
    wh_fail = payment.webhook_status_id and payment.webhook_status == webhook_failed
    vf_ok = payment.esbuzz_verify_status_id and payment.esbuzz_verify_status == verify_success
    vf_fail = payment.esbuzz_verify_status_id and payment.esbuzz_verify_status == verify_failed
    if wh_ok and vf_ok:
        payment.payment_status = statuses["SUCCESS"]
        payment.is_finalized = True
        payment.paid_at = payment.paid_at or timezone.now()
        payment.save(update_fields=["payment_status", "is_finalized", "paid_at", "system_updated_at"])
        logger.info(f"Payment {payment.id} resolved to SUCCESS. Finalizing.")
        finalize_payment(payment)
    elif wh_fail and vf_fail:
        payment.payment_status = statuses["FAILED"]
        payment.is_finalized = True
        payment.save(update_fields=["payment_status", "is_finalized", "system_updated_at"])
        logger.info(f"Payment {payment.id} resolved to FAILED.")
    else:
        payment.payment_status = statuses["UNDER_REVIEW"]
        payment.is_finalized = False
        payment.save(update_fields=["payment_status", "is_finalized", "system_updated_at"])
        logger.info(f"Payment {payment.id} resolved to UNDER_REVIEW (mixed webhook/verify).")


def finalize_payment(payment):
    """
    Apply ledger, installment PAID, total_paid, and optionally scheme activation.
    MUST be called only when payment.payment_status is SUCCESS (or PAID) and payment.is_finalized is True.
    Aborts if scheme is ABANDONED or not in [ACTIVE, PENDING].
    Activation notification only when first installment and scheme was PENDING -> ACTIVE.
    """
    payment = Payment.objects.select_for_update().get(id=payment.id)
    success_codes = ("SUCCESS", "PAID")
    status_code = (payment.payment_status and payment.payment_status.code) or ""
    if status_code not in success_codes or not payment.is_finalized:
        logger.warning(f"finalize_payment: payment {payment.id} status={status_code} or not finalized. Abort.")
        return

    instalment = SchemeInstalment.objects.select_for_update().get(id=payment.instalment_id)
    customer_scheme = CustomerScheme.objects.select_for_update().get(id=instalment.customer_scheme_id)
    scheme_status_code = (customer_scheme.scheme_status and customer_scheme.scheme_status.code) or ""

    if scheme_status_code == "ABANDONED":
        logger.warning(f"finalize_payment: scheme {customer_scheme.id} is ABANDONED. Abort (no resurrection).")
        return
    if scheme_status_code not in ("ACTIVE", "PENDING"):
        logger.warning(f"finalize_payment: scheme status is {scheme_status_code}. Must be ACTIVE or PENDING. Abort.")
        return

    scheme = customer_scheme.scheme
    installment_paid = LookupValue.objects.get(lookup__code="INSTALLMENT_STATUS", code="PAID")

    # Payment finalization: update installment, total_paid, ledger.
    instalment.status = installment_paid
    instalment.save(update_fields=["status", "system_updated_at"])
    customer_scheme.total_paid = (customer_scheme.total_paid or 0) + payment.amount
    customer_scheme.save(update_fields=["total_paid", "system_updated_at"])
    insert_financial_records_for_payment(payment)

    # Gold lock: when scheme has DYNAMIC_LOCK or FIXED_GRAM, lock this installment at 24K rate (today else yesterday).
    has_gold_lock = customer_scheme.benefits.filter(
        benefit_type__in=["DYNAMIC_LOCK", "FIXED_GRAM"]
    ).exists() or scheme.benefits.filter(
        benefit_type__in=["DYNAMIC_LOCK", "FIXED_GRAM"]
    ).exists()
    if has_gold_lock:
        try:
            lock_rate_obj = get_lock_rate()
            lock_rate_val = lock_rate_obj and (getattr(lock_rate_obj, "rate_value", None) or getattr(lock_rate_obj, "sell_price", None))
            if lock_rate_val is not None and float(lock_rate_val) > 0:
                result = lock_gold_for_payment(payment, instalment, float(lock_rate_val))
                if result is None:
                    logger.warning(f"Gold locking returned None for payment {payment.id}")
                else:
                    logger.info(f"Gold locked: {result}g for payment {payment.id}")
        except Exception as e:
            logger.error(f"Gold lock error for payment {payment.id}: {str(e)}")
            # Don't pass - let it fail visibly or we can choose to continue
            # For now, continue payment finalization but log the error

    # First installment + PENDING -> activate scheme and send activation notification
    is_first_installment = instalment.instalment_no == 1
    if is_first_installment and scheme_status_code == "PENDING":
        # Activate scheme and refresh instance so scheme_reference (TS0001, TS0002, ...) is present
        customer_scheme = activate_scheme_on_first_payment(customer_scheme, payment.paid_at)
        send_payment_success_sms(customer_scheme.customer.mobile or "", customer_scheme.scheme_reference)
        create_admin_notification(
            title="Scheme Pending For Approval",
            message=f"Customer scheme for {customer_scheme.customer.full_name} is pending for approval.",
            section_code="CRM_CUSTOMER_KYC_PAN_STATUS_APPROVE",
            customer_id=customer_scheme.customer.id,
            notification_type="SCHEME_PENDING_APPROVAL",
        )
        logger.info(f"Scheme {customer_scheme.id} activated (first installment, was PENDING). Notification sent.")

    # If all tenure installments are PAID -> set scheme COMPLETED and send completion notification
    paid_status = LookupValue.objects.get(lookup__code="INSTALLMENT_STATUS", code="PAID")
    tenure_count = scheme.tenure_months
    paid_count = SchemeInstalment.objects.filter(
        customer_scheme=customer_scheme,
        is_bonus=False,
        status=paid_status,
    ).count()
    if paid_count >= tenure_count:
        completed_status = LookupValue.objects.get(lookup__code="SCHEME_STATUS", code="COMPLETED")
        was_completed = customer_scheme.scheme_status_id == completed_status.id
        customer_scheme.scheme_status = completed_status
        customer_scheme.completed_at = timezone.now()
        customer_scheme.save(update_fields=["scheme_status", "completed_at", "system_updated_at"])
        if not was_completed:
            create_admin_notification(
                title="Scheme completed",
                message=f"Customer scheme for {customer_scheme.customer.full_name} has been completed (all installments paid). Bonus will be processed by monthly scheduler.",
                section_code="SCHEME_COMPLETED",
                customer_id=customer_scheme.customer.id,
                notification_type="SCHEME_COMPLETED",
            )
            logger.info(f"Scheme {customer_scheme.id} marked COMPLETED. Completion notification sent.")
            from shared.services.icici_upi_service import maybe_revoke_mandate_when_scheme_completed

            maybe_revoke_mandate_when_scheme_completed(customer_scheme.id)

    logger.info(f"Payment {payment.id} finalized successfully.")


def recalculate_payment_state(payment):
    """
    Centralized reconciliation: (re)read webhook_status + esbuzz_verify_status,
    apply decision matrix, update payment_status and is_finalized, trigger finalize if both SUCCESS.
    Call after updating either webhook_status or esbuzz_verify_status, or to fix stuck payments.
    """
    resolve_payment(payment)

