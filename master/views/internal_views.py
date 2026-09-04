"""
Internal endpoints for cron/scheduler (e.g. monthly process-matured, payment reconciliation).
Protected by shared secret header.
"""
import logging

from django.conf import settings
from django.db import transaction
from django.views.decorators.http import require_http_methods
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from shared.models import Payment, LookupValue
from shared.services.maturity_scheduler_service import process_matured_schemes
from shared.services.payment_processor import resolve_payment

logger = logging.getLogger(__name__)

INTERNAL_SECRET_HEADER = "X-Internal-Secret"


def _is_internal_request(request):
    secret = getattr(settings, "INTERNAL_SECRET", None)
    if not secret:
        return False
    return request.headers.get(INTERNAL_SECRET_HEADER) == secret


@api_view(["POST"])
@require_http_methods(["POST"])
def process_matured_schemes_view(request):
    """
    POST /internal/schemes/process-matured/
    Trigger monthly maturity processing: COMPLETED schemes -> bonus + gold lock + MATURED.
    Call from cron/celery on 1st of every month.
    Requires X-Internal-Secret header to match settings.INTERNAL_SECRET.
    """
    if not _is_internal_request(request):
        logger.warning("process_matured_schemes: rejected (missing or invalid internal secret)")
        return Response({"error": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)
    try:
        count = process_matured_schemes()
        return Response(
            {"message": "Maturity processing completed", "processed_count": count},
            status=status.HTTP_200_OK,
        )
    except Exception as e:
        logger.exception(f"process_matured_schemes: {e}")
        return Response(
            {"error": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@require_http_methods(["POST"])
def reconcile_payments_view(request):
    """
    POST /internal/payments/reconcile/
    Find payments where webhook_status=SUCCESS and esbuzz_verify_status=SUCCESS
    but payment_status still INITIATED (or not finalized), and run resolve_payment for each.
    Use to fix stuck payments.
    """
    try:
        webhook_success = LookupValue.objects.get(lookup__code="WEBHOOK_STATUS", code="SUCCESS")
        verify_success = LookupValue.objects.get(lookup__code="ESBUZZ_VERIFY_STATUS", code="SUCCESS")
    except LookupValue.DoesNotExist as e:
        return Response({"error": f"Lookup missing: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    stuck_ids = list(
        Payment.objects.filter(
            webhook_status=webhook_success,
            esbuzz_verify_status=verify_success,
            is_finalized=False,
        ).values_list("id", flat=True)
    )
    count = 0
    for payment_id in stuck_ids:
        try:
            with transaction.atomic():
                payment = Payment.objects.select_for_update().get(id=payment_id)
                resolve_payment(payment)
                count += 1
        except Exception as e:
            logger.exception(f"reconcile payment {payment_id}: {e}")
    return Response(
        {"message": "Reconciliation completed", "reconciled_count": count, "candidates": len(stuck_ids)},
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@require_http_methods(["POST"])
def process_upi_mandate_dues_view(request):
    """
    POST /internal/upi-mandates/process-dues/
    Send MandateNotification (T-1) and ExecuteMandate on debit_date.
    Requires X-Internal-Secret header.
    """
    if not _is_internal_request(request):
        logger.warning("process_upi_mandate_dues: rejected (missing or invalid internal secret)")
        return Response({"error": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)
    from shared.services.upi_mandate_scheduler_service import process_upi_mandate_dues

    try:
        payload = request.data if hasattr(request, "data") else {}
        mandate_id = payload.get("mandate_id")
        if mandate_id is not None:
            mandate_id = int(mandate_id)
        dry_run = str(payload.get("dry_run", "")).lower() in ("1", "true", "yes")
        result = process_upi_mandate_dues(
            mandate_id=mandate_id,
            dry_run=dry_run,
            notify_only=str(payload.get("notify_only", "")).lower() in ("1", "true", "yes"),
            execute_only=str(payload.get("execute_only", "")).lower() in ("1", "true", "yes"),
        )
        return Response({"message": "UPI mandate dues processed", **result}, status=status.HTTP_200_OK)
    except Exception as e:
        logger.exception("process_upi_mandate_dues: %s", e)
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
@require_http_methods(["POST"])
def process_ready_to_redeem_schemes_view(request):
    """
    POST /internal/schemes/process-ready-to-redeem/
    All payable instalments paid + full tenure (e.g. 11 months) from start → READY_TO_REDEEM.
    Requires X-Internal-Secret header.
    """
    if not _is_internal_request(request):
        logger.warning("process_ready_to_redeem_schemes: rejected (missing or invalid internal secret)")
        return Response({"error": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)
    from shared.services.ready_to_redeem_scheduler_service import process_ready_to_redeem_schemes

    try:
        payload = request.data if hasattr(request, "data") else {}
        scheme_id = payload.get("scheme_id") or payload.get("customer_scheme_id")
        if scheme_id is not None:
            scheme_id = int(scheme_id)
        dry_run = str(payload.get("dry_run", "")).lower() in ("1", "true", "yes")
        result = process_ready_to_redeem_schemes(
            customer_scheme_id=scheme_id,
            dry_run=dry_run,
        )
        return Response({"message": "Ready-to-redeem processing completed", **result}, status=status.HTTP_200_OK)
    except Exception as e:
        logger.exception("process_ready_to_redeem_schemes: %s", e)
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
