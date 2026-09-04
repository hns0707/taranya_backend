"""
Function-based views for payments in the customer app.
"""

import json
import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.http import HttpResponseRedirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from customer.auth.customer_auth import CustomerAuthentication
from shared.models import Payment, SchemeInstalment, LookupValue, PaymentAuditLog
from shared.services import orange_pg_service
from shared.services.payment_helper import (
    get_locked_payment_by_transaction_id,
    is_payment_already_processed,
    update_status_and_process,
)
from shared.services.payment_processor import resolve_payment
from shared.services.payment_service import create_payment_with_collections
from shared.services.payment_status_service import (
    get_verify_success_failed_lookups,
    map_verify_status_to_lookup,
)

logger = logging.getLogger(__name__)



@api_view(["GET"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def payment_list(request):
    payments = Payment.objects.filter(
        instalment__customer_scheme__customer=request.user
    ).prefetch_related('collections__payment_mode')

    def _payment_mode_display(p):
        if p.collections.exists():
            modes = list(p.collections.values_list('payment_mode__code', flat=True))
            return ','.join(modes) if len(modes) == 1 else 'SPLIT'
        return p.payment_mode.code if p.payment_mode else None

    data = [{
        "id": payment.id,
        "instalment_id": payment.instalment.id,
        "amount": str(payment.amount) if payment.amount is not None else None,
        "payment_mode": _payment_mode_display(payment),
        "receipt_no": payment.receipt_no,
        "transaction_id": payment.transaction_id,
        "payment_status": payment.payment_status.code,
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
    } for payment in payments]

    return Response(data, status=status.HTTP_200_OK)


@api_view(["GET"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def payment_detail(request, payment_id):
    try:
        payment = Payment.objects.prefetch_related('collections__payment_mode').get(
            id=payment_id,
            instalment__customer_scheme__customer=request.user
        )
    except Payment.DoesNotExist:
        return Response(
            {"error": "Payment not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    def _mode_display(p):
        if p.collections.exists():
            modes = list(p.collections.values_list('payment_mode__code', flat=True))
            return ','.join(modes) if len(modes) == 1 else 'SPLIT'
        return p.payment_mode.code if p.payment_mode else None
    data = {
        "id": payment.id,
        "instalment_id": payment.instalment.id,
        "amount": str(payment.amount) if payment.amount is not None else None,
        "payment_mode": _mode_display(payment),
        "receipt_no": payment.receipt_no,
        "transaction_id": payment.transaction_id,
        "payment_status": payment.payment_status.code,
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
    }

    return Response(data, status=status.HTTP_200_OK)



@api_view(["POST"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def initiate_payment(request):
    """
    Initiates ICICI Orange PG payment for a scheme instalment (Standard redirect).
    Requires authenticated customer.
    """
    customer = request.user  # This is a Custom Customer object (not Django User)
    
    # Validate input data
    instalment_id = request.data.get("instalment_id")
    if not instalment_id:
        return Response(
            {"error": "instalment_id is required"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Get instalment and verify it belongs to the authenticated customer
    try:
        instalment = SchemeInstalment.objects.select_related(
            'customer_scheme', 'customer_scheme__scheme_status', 'customer_scheme__scheme'
        ).get(
            id=instalment_id,
            customer_scheme__customer=customer
        )
        scheme_status_code = (instalment.customer_scheme.scheme_status and instalment.customer_scheme.scheme_status.code) or ""
        blocked_statuses = ("ABANDONED", "COMPLETED", "CANCELLED", "FAILED")
        if scheme_status_code in blocked_statuses:
            return Response(
                {"error": f"Payment is not allowed for this scheme (status: {scheme_status_code})."},
                status=status.HTTP_400_BAD_REQUEST
            )
        # First installment allowed when PENDING; other installments require ACTIVE
        if instalment.instalment_no == 1:
            if scheme_status_code not in ("PENDING", "ACTIVE"):
                return Response(
                    {"error": "First installment is only allowed when scheme is PENDING or ACTIVE."},
                    status=status.HTTP_400_BAD_REQUEST
                )
        else:
            if scheme_status_code != "ACTIVE":
                return Response(
                    {"error": "Scheme is not active. Payment is not allowed for this scheme."},
                    status=status.HTTP_400_BAD_REQUEST
                )
        if instalment.is_bonus:
            return Response(
                {"error": "Bonus installments do not require payment"},
                status=status.HTTP_400_BAD_REQUEST
            )
        if instalment.status.code == "PAID":
            return Response(
                {"error": "Instalment already paid"},
                status=status.HTTP_400_BAD_REQUEST
            )
    except SchemeInstalment.DoesNotExist:
        return Response(
            {"error": "Instalment not found"},
            status=status.HTTP_404_NOT_FOUND
        )


    # Generate unique transaction ID
    txnid = str(uuid.uuid4()).replace("-", "")[:20]

    # Create payment record (INITIATED) with receipt_no and PaymentCollection
    try:
        payment = create_payment_with_collections(
            instalment=instalment,
            amount=instalment.amount,
            transaction_id=txnid,
            payment_status_code='INITIATED',
            payment_source='CP',
            payment_mode_code='ONLINE',
            paid_at=timezone.now(),
            payment_provider=orange_pg_service.PAYMENT_PROVIDER_ORANGE,
        )
    except LookupValue.DoesNotExist as e:
        return Response({"error": "Required lookup values not found"}, status=status.HTTP_404_NOT_FOUND)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return_url = (getattr(settings, "ORANGE_PG_RETURN_URL", None) or "").strip()
    if not return_url:
        return_url = request.build_absolute_uri("/customer/payments/orange/return/")

    if not getattr(settings, "ORANGE_PG_MERCHANT_ID", None) or not getattr(settings, "ORANGE_PG_SECRET_KEY", None):
        payment_status_lv = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code='FAILED')
        payment.payment_status = payment_status_lv
        payment.save(update_fields=["payment_status", "system_updated_at"])
        return Response(
            {"error": "Orange PG is not configured (merchant id / secret key)."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    payload = orange_pg_service.build_initiate_payload(
        merchant_txn_no=txnid,
        amount=instalment.amount,
        customer=customer,
        return_url=return_url,
        addl1=str(payment.id),
        addl2=str(instalment.id),
    )

    response_data, request_error = orange_pg_service.call_initiate_sale(payload)
    try:
        PaymentAuditLog.objects.create(
            txnid=txnid,
            type="ORANGE_PG_INITIATE",
            status=str((response_data or {}).get("responseCode") or (request_error and "ERROR")),
            request_payload=payload,
            response_json=response_data if isinstance(response_data, dict) else {"raw": response_data},
        )
    except Exception as e:
        logger.warning("Orange initiate audit log failed: %s", e)

    if request_error is not None:
        payment_status_lv = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code='FAILED')
        payment.payment_status = payment_status_lv
        payment.save(update_fields=["payment_status", "system_updated_at"])
        return Response(
            {"error": f"Failed to initiate payment: {request_error}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    response_code = str((response_data or {}).get("responseCode") or "")
    # R1000 = redirect flow started; 000/0000 also acceptable if bank returns them
    if response_code not in ("R1000", "000", "0000"):
        payment_status_lv = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code='FAILED')
        payment.payment_status = payment_status_lv
        payment.save(update_fields=["payment_status", "system_updated_at"])
        return Response(
            {
                "error": (response_data or {}).get("respDescription")
                or f"Orange PG initiate failed ({response_code or 'unknown'})",
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )

    try:
        payment_url = orange_pg_service.build_payment_url(response_data or {})
    except ValueError as e:
        payment_status_lv = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code='FAILED')
        payment.payment_status = payment_status_lv
        payment.save(update_fields=["payment_status", "system_updated_at"])
        return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    return Response({
        "payment_url": payment_url,
        "txnid": txnid,
        "payment_id": payment.id,
        "inst_no": instalment.instalment_no,
        "provider": orange_pg_service.PAYMENT_PROVIDER_ORANGE,
    }, status=status.HTTP_200_OK)



@api_view(["GET"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def payment_status(request, payment_id):
    """
    API endpoint to get payment status for polling after redirect.
    This API only reads current state and has no side effects.
    """
    try:
        payment = Payment.objects.get(
            id=payment_id,
            instalment__customer_scheme__customer=request.user
        )
    except Payment.DoesNotExist:
        return Response(
            {"detail": "Payment not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    # Determine status and message
    status_map = {
        "INITIATED": ("PENDING", "Payment is still in progress"),
        "UNDER_REVIEW": ("PENDING", "Payment is under review"),
        "SUCCESS": ("SUCCESS", "Payment completed successfully"),
        "SUCCESS": ("SUCCESS", "Payment completed successfully"),
        "FAILED": ("FAILED", "Payment failed or was cancelled"),
        "REJECTED": ("FAILED", "Payment was rejected"),
        "REFUNDED": ("FAILED", "Payment was refunded"),
        "PAID": ("SUCCESS", "Payment completed successfully"),
    }

    status_code, message = status_map.get(payment.payment_status.code, ("PENDING", "Payment status unknown"))

    # Build response
    response_data = {
        "payment_id": payment.id,
        "status": status_code,
        "message": message,
        "transaction_id": payment.transaction_id,
        "webhook_status": payment.webhook_status.code if payment.webhook_status else "PENDING",
        "gateway_verify_status": payment.esbuzz_verify_status.code if payment.esbuzz_verify_status else "PENDING",
        "esbuzz_verify_status": payment.esbuzz_verify_status.code if payment.esbuzz_verify_status else "PENDING",
        "updated_at": payment.system_updated_at.isoformat()
    }

    return Response(response_data, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
@transaction.atomic
def verify_payment(request):
    """
    Confirm Orange PG payment via STATUS API (dual-confirm with Payment Advice / return).
    POST /customer/payments/verify/  body: { transaction_id }
    """
    transaction_id = request.data.get("transaction_id")
    if not transaction_id:
        return Response(
            {"error": "transaction_id is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        payment = get_locked_payment_by_transaction_id(transaction_id)
    except Payment.DoesNotExist:
        return Response({"error": "Payment not found"}, status=404)

    if payment.instalment.customer_scheme.customer != request.user:
        return Response({"error": "Payment not found"}, status=404)

    scheme_status_code = (payment.instalment.customer_scheme.scheme_status and payment.instalment.customer_scheme.scheme_status.code) or ""
    if scheme_status_code not in ("ACTIVE", "PENDING"):
        return Response(
            {"error": "Scheme is not active or pending. Payment verification is not allowed for this scheme."},
            status=status.HTTP_400_BAD_REQUEST
        )

    if is_payment_already_processed(payment):
        return Response({
            "message": "Payment already finalized",
            "transaction_id": transaction_id,
            "webhook_status": payment.webhook_status.code if payment.webhook_status else "PENDING",
            "gateway_verify_status": payment.esbuzz_verify_status.code if payment.esbuzz_verify_status else "PENDING",
            "esbuzz_verify_status": payment.esbuzz_verify_status.code if payment.esbuzz_verify_status else "PENDING",
            "payment_status": payment.payment_status.code,
        }, status=200)

    verify_success, verify_failed = get_verify_success_failed_lookups()
    if payment.esbuzz_verify_status in (verify_success, verify_failed):
        return Response({
            "message": "Payment already verified",
            "transaction_id": transaction_id,
            "webhook_status": payment.webhook_status.code if payment.webhook_status else "PENDING",
            "gateway_verify_status": payment.esbuzz_verify_status.code,
            "esbuzz_verify_status": payment.esbuzz_verify_status.code,
            "payment_status": payment.payment_status.code,
        }, status=200)

    raw_response_text, verify_response, gateway_status, request_error = orange_pg_service.call_status_api(
        transaction_id
    )
    audit_payload = orange_pg_service.build_status_payload(transaction_id)

    if request_error is not None:
        return Response({
            "error": "STATUS API failed",
            "transaction_id": transaction_id,
            "detail": str(request_error),
        }, status=502)

    try:
        PaymentAuditLog.objects.create(
            txnid=transaction_id,
            type="ORANGE_PG_STATUS",
            status=gateway_status,
            request_payload=audit_payload,
            response_json=verify_response if isinstance(verify_response, dict) else {"raw": raw_response_text},
        )
    except Exception as e:
        logger.error(f"Audit log failed: {str(e)}")

    if gateway_status == "UNKNOWN":
        return Response({
            "message": "Payment still pending at gateway",
            "transaction_id": transaction_id,
            "webhook_status": payment.webhook_status.code if payment.webhook_status else "PENDING",
            "gateway_verify_status": payment.esbuzz_verify_status.code if payment.esbuzz_verify_status else "PENDING",
            "esbuzz_verify_status": payment.esbuzz_verify_status.code if payment.esbuzz_verify_status else "PENDING",
            "payment_status": payment.payment_status.code,
        }, status=200)

    if gateway_status == "SUCCESS" and isinstance(verify_response, dict):
        txn_id = verify_response.get("txnID") or verify_response.get("paymentID")
        if txn_id:
            payment.gateway_transaction_id = str(txn_id)
            payment.save(update_fields=["gateway_transaction_id", "system_updated_at"])
        # If Payment Advice not yet received, STATUS success fills webhook_status too
        if not payment.webhook_status_id:
            orange_pg_service.apply_advice_or_return_result(payment, verify_response)

    verify_lookup = map_verify_status_to_lookup(gateway_status)
    update_status_and_process(payment, "esbuzz_verify_status", verify_lookup)

    return Response({
        "transaction_id": transaction_id,
        "webhook_status": payment.webhook_status.code if payment.webhook_status else "PENDING",
        "gateway_verify_status": payment.esbuzz_verify_status.code if payment.esbuzz_verify_status else "PENDING",
        "esbuzz_verify_status": payment.esbuzz_verify_status.code if payment.esbuzz_verify_status else "PENDING",
        "payment_status": payment.payment_status.code,
    }, status=200)


def _parse_orange_callback_data(request) -> dict:
    try:
        data = request.data.dict() if hasattr(request.data, "dict") else dict(request.data)
    except Exception:
        data = {}
    if not data and request.body:
        try:
            data = json.loads(request.body.decode("utf-8"))
        except Exception:
            data = {}
    # Flatten QueryDict list values
    flat = {}
    for k, v in (data or {}).items():
        if isinstance(v, (list, tuple)):
            flat[k] = v[0] if v else ""
        else:
            flat[k] = v
    return flat


def _hub_redirect_for_payment(payment, *, status_hint: str):
    base = (getattr(settings, "ORANGE_PG_HUB_RETURN_BASE", None) or "").rstrip("/")
    if not base:
        base = "http://127.0.0.1:8080/payment-status"
    amount = payment.amount
    return (
        f"{base}?paymentId={payment.id}"
        f"&txnid={payment.transaction_id}"
        f"&amount={amount}"
        f"&status={status_hint}"
        f"&verify=1"
    )


@csrf_exempt
@api_view(["POST", "GET"])
@authentication_classes([])
@permission_classes([])
@transaction.atomic
def orange_pg_return(request):
    """
    Browser return from Orange PG (form POST to returnURL).
    Verifies hash, updates webhook_status when final, redirects to customer hub.
    """
    data = _parse_orange_callback_data(request)
    if request.method == "GET" and not data:
        data = {k: request.GET.get(k) for k in request.GET.keys()}

    txnid = data.get("merchantTxnNo") or data.get("merchantTxnNo".lower()) or request.GET.get("merchantTxnNo")
    logger.info("Orange PG return received txnid=%s keys=%s", txnid, list(data.keys()))

    try:
        PaymentAuditLog.objects.create(
            txnid=txnid or "UNKNOWN",
            type="ORANGE_PG_RETURN",
            status=str(data.get("responseCode") or ""),
            response_json=data,
        )
    except Exception as e:
        logger.warning("Orange return audit failed: %s", e)

    hub_base = (getattr(settings, "ORANGE_PG_HUB_RETURN_BASE", None) or "http://127.0.0.1:8080/payment-status").rstrip("/")
    if not txnid:
        return HttpResponseRedirect(f"{hub_base}?status=failure")

    try:
        payment = get_locked_payment_by_transaction_id(txnid)
    except Payment.DoesNotExist:
        return HttpResponseRedirect(f"{hub_base}?status=failure&txnid={txnid}")

    if data.get("secureHash") or data.get("securehash"):
        if not orange_pg_service.validate_callback_hash(data):
            logger.error("Orange return hash mismatch for %s", txnid)
            return HttpResponseRedirect(_hub_redirect_for_payment(payment, status_hint="failure"))

    if not is_payment_already_processed(payment):
        if orange_pg_service.validate_callback_amount(payment, data):
            orange_pg_service.apply_advice_or_return_result(payment, data)
            resolve_payment(payment)
        else:
            logger.error("Orange return amount mismatch for %s", txnid)

    hint = "success" if orange_pg_service.is_txn_success(data) else (
        "pending" if str(data.get("responseCode") or "") in ("R1000",) else "failure"
    )
    if payment.is_finalized and payment.payment_status and payment.payment_status.code == "SUCCESS":
        hint = "success"
    return HttpResponseRedirect(_hub_redirect_for_payment(payment, status_hint=hint))


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
@transaction.atomic
def orange_pg_advice(request):
    """
    Orange PG Payment Advice (server-to-server). Always HTTP 200.
    POST /customer/payments/orange/advice/
    """
    data = _parse_orange_callback_data(request)
    txnid = data.get("merchantTxnNo")
    logger.info("Orange PG advice received: %s", data)

    try:
        PaymentAuditLog.objects.create(
            txnid=txnid or "UNKNOWN",
            type="ORANGE_PG_ADVICE",
            status=str(data.get("responseCode") or data.get("txnStatus") or ""),
            response_json=data,
        )
    except Exception as e:
        logger.warning("Orange advice audit failed: %s", e)

    if not txnid:
        return Response({"message": "Ignored"}, status=200)

    try:
        payment = get_locked_payment_by_transaction_id(txnid)
    except Payment.DoesNotExist:
        logger.warning("Orange advice payment not found: %s", txnid)
        return Response({"message": "Ignored"}, status=200)

    try:
        if is_payment_already_processed(payment):
            return Response({"message": "Already processed"}, status=200)

        if data.get("secureHash") or data.get("securehash"):
            if not orange_pg_service.validate_callback_hash(data):
                logger.error("Orange advice hash mismatch for %s", txnid)
                return Response({"message": "Invalid hash"}, status=200)

        if not orange_pg_service.validate_callback_amount(payment, data):
            logger.error("Orange advice amount mismatch for %s", txnid)
            return Response({"message": "Amount mismatch"}, status=200)

        orange_pg_service.apply_advice_or_return_result(payment, data)
        resolve_payment(payment)
        return Response({"message": "Advice processed"}, status=200)
    except Exception as e:
        logger.exception("Orange advice error: %s", e)
        return Response({"message": "Error handled"}, status=200)


@api_view(["POST"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def create_upi_mandate(request):
    """
    Create ICICI UPI mandate (Collect) for next unpaid instalment.
    POST /customer/payments/upi-mandate/create/
    body: { instalment_id, payer_vpa }
    """
    from shared.services.icici_upi_service import create_customer_upi_mandate

    instalment_id = request.data.get("instalment_id")
    payer_vpa = request.data.get("payer_vpa")
    if not instalment_id:
        return Response({"error": "instalment_id is required"}, status=status.HTTP_400_BAD_REQUEST)
    if not payer_vpa:
        return Response({"error": "payer_vpa is required"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        data = create_customer_upi_mandate(
            customer=request.user,
            instalment_id=int(instalment_id),
            payer_vpa=str(payer_vpa),
        )
        return Response({"message": "UPI mandate initiated", "data": data}, status=status.HTTP_201_CREATED)
    except SchemeInstalment.DoesNotExist:
        return Response({"error": "Instalment not found"}, status=status.HTTP_404_NOT_FOUND)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.exception("create_upi_mandate failed: %s", e)
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def upi_qr_config(request):
    """GET /customer/payments/upi-qr/config/ — onetime vs mandate mode from server env."""
    from shared.services.icici_upi_service import get_icici_upi_qr_config

    return Response(get_icici_upi_qr_config(), status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def create_upi_mandate_qr(request):
    """
    Create ICICI UPI QR — mode from ICICI_UPI_QR_MODE:
      onetime → UPI/v0 QR3 (pay once)
      mandate → UPI2 MandateQR (AutoPay)
    POST /customer/payments/upi-mandate/create-qr/
    body: { instalment_id }
    """
    from shared.services.icici_upi_service import create_customer_upi_mandate_qr

    instalment_id = request.data.get("instalment_id")
    if not instalment_id:
        return Response({"error": "instalment_id is required"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        data = create_customer_upi_mandate_qr(
            customer=request.user,
            instalment_id=int(instalment_id),
        )
        return Response({"message": "UPI mandate QR ready", "data": data}, status=status.HTTP_201_CREATED)
    except SchemeInstalment.DoesNotExist:
        return Response({"error": "Instalment not found"}, status=status.HTTP_404_NOT_FOUND)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.exception("create_upi_mandate_qr failed: %s", e)
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def upi_mandate_status(request, mandate_id):
    """
    Poll mandate + first-debit status for customer hub.
    GET /customer/payments/upi-mandate/<id>/status/
    """
    from shared.models import UpiMandate, Payment
    from shared.services.icici_upi_service import get_upi_mandate_status_for_customer

    try:
        data = get_upi_mandate_status_for_customer(request.user, int(mandate_id))
        return Response(data, status=status.HTTP_200_OK)
    except Payment.DoesNotExist:
        return Response({"error": "Payment not found"}, status=status.HTTP_404_NOT_FOUND)
    except UpiMandate.DoesNotExist:
        return Response({"error": "Mandate not found"}, status=status.HTTP_404_NOT_FOUND)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def icici_upi_callback(request):
    """
    ICICI UPI Mandate callback (public, no auth).
    POST /customer/payments/callback/
    Always returns HTTP 200.
    """
    from shared.services.icici_upi_service import process_icici_callback

    result = process_icici_callback(request)
    return Response(result, status=status.HTTP_200_OK)


