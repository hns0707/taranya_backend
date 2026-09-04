"""
CRM Communication & Reminder views.

Endpoints:
  POST  /master/crm/reminders/send-whatsapp/          — WhatsApp scheme instalment reminders
  POST  /master/crm/reminders/send-sms/               — SMS scheme instalment reminders
  POST  /master/crm/reminders/send-udhar-sms/         — SMS udhar payment reminders
  POST  /master/crm/reminders/send-gold-rate-sms/     — SMS today's gold/silver rates
  POST  /master/crm/reminders/send-offer-whatsapp/    — marketing / offer WhatsApp
  POST  /master/crm/reminders/log-call/               — log on-call reminder
  GET/POST /master/crm/reminders/scheduled/           — list / create scheduled reminders
  POST  /master/crm/reminders/scheduled/<pk>/cancel/  — cancel pending schedule
  POST  /master/crm/reminders/process-scheduled/      — process due schedules
  POST  /master/pos/invoice/<pk>/send-whatsapp/       — invoice PDF via WhatsApp
  GET   /master/crm/communication-logs/               — message history
  GET   /master/crm/communication-logs/analytics/     — WhatsApp + telecom analytics
"""
import json
import logging
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from master.permissions.permission_checker import admin_auth
from shared.models import CommunicationLog, Customer, SchemeInstalment, SaleInvoice

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_communication(
    *,
    channel: str,
    message_type: str,
    phone: str,
    status_val: str,
    customer=None,
    template_name: str = "",
    parameters: str = "",
    message_body: str = "",
    api_response: str = "",
    error_detail: str = "",
    ref_invoice_id: int = None,
    ref_instalment_id: int = None,
    campaign_name: str = "",
    sent_by=None,
) -> CommunicationLog:
    return CommunicationLog.objects.create(
        channel=channel,
        message_type=message_type,
        phone=phone,
        status=status_val,
        customer=customer,
        template_name=template_name,
        parameters=parameters,
        message_body=message_body,
        api_response=api_response,
        error_detail=error_detail,
        ref_invoice_id=ref_invoice_id,
        ref_instalment_id=ref_instalment_id,
        campaign_name=campaign_name,
        sent_by=sent_by,
    )


def _normalise_phone(phone: str) -> str:
    """Ensure country code prefix (India default 91)."""
    p = phone.strip().lstrip("+").lstrip("0")
    if len(p) == 10:
        p = "91" + p
    return p


# ---------------------------------------------------------------------------
# Send WhatsApp reminder (scheme instalments)
# ---------------------------------------------------------------------------

@api_view(["POST"])
@admin_auth(
    "CRM_CUSTOMER_UPCOMING_REMINDERS",
    "CRM_CUSTOMER_PAST_DUE_REMINDERS",
    "CRM_COMMUNICATION_SEND",
)
def send_whatsapp_reminder(request):
    """
    POST body:
    {
        "instalment_ids": [1, 2, 3],
        "template_name": "scheme_payment_reminder",   // optional, defaults to setting
        "template_language": "en",
        "campaign_name": ""
    }
    Sends a WhatsApp template message to each instalment's customer.
    """
    from django.conf import settings as dj_settings
    from shared.services.whatsapp_service import send_whatsapp_template

    instalment_ids = request.data.get("instalment_ids", [])
    template_name = request.data.get(
        "template_name",
        getattr(dj_settings, "WHATSAPP_TEMPLATE_SCHEME_REMINDER", "testtemplate"),
    )
    template_language = request.data.get(
        "template_language",
        getattr(dj_settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en_US"),
    )
    campaign_name = request.data.get("campaign_name", "")

    if not instalment_ids:
        return Response({"error": "instalment_ids is required"}, status=status.HTTP_400_BAD_REQUEST)

    instalments = SchemeInstalment.objects.filter(id__in=instalment_ids).select_related(
        "customer_scheme__customer", "customer_scheme__scheme"
    )

    results = []
    for inst in instalments:
        customer = inst.customer_scheme.customer
        scheme = inst.customer_scheme.scheme
        phone_raw = customer.mobile or ""
        phone = _normalise_phone(phone_raw)

        if not phone_raw:
            _log_communication(
                channel=CommunicationLog.CHANNEL_WHATSAPP,
                message_type=CommunicationLog.TYPE_SCHEME_REMINDER,
                phone="",
                status_val=CommunicationLog.STATUS_SKIPPED,
                customer=customer,
                template_name=template_name,
                error_detail="No mobile number on customer record",
                ref_instalment_id=inst.id,
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({"instalment_id": inst.id, "status": "skipped", "reason": "no_phone"})
            continue

        # Template parameters — Mart2Meta expects "{v1},{v2},..."
        parameters = f"{{{customer.full_name}}},{{{scheme.scheme_name}}},{{{inst.due_date}}},{{Rs.{inst.amount}}}"
        param_values = [
            customer.full_name or "",
            scheme.scheme_name or "",
            str(inst.due_date),
            f"Rs.{inst.amount}",
        ]

        try:
            api_resp = send_whatsapp_template(
                phone=phone,
                template_name=template_name,
                template_language=template_language,
                parameters=param_values,
            )
            if api_resp.get("status") == "skipped":
                _log_communication(
                    channel=CommunicationLog.CHANNEL_WHATSAPP,
                    message_type=CommunicationLog.TYPE_SCHEME_REMINDER,
                    phone=phone,
                    status_val=CommunicationLog.STATUS_SKIPPED,
                    customer=customer,
                    template_name=template_name,
                    parameters=parameters,
                    api_response=json.dumps(api_resp),
                    error_detail="WhatsApp not configured",
                    ref_instalment_id=inst.id,
                    campaign_name=campaign_name,
                    sent_by=request.user,
                )
                results.append({
                    "instalment_id": inst.id,
                    "customer": customer.full_name,
                    "status": "skipped",
                    "reason": "not_configured",
                })
                continue

            _log_communication(
                channel=CommunicationLog.CHANNEL_WHATSAPP,
                message_type=CommunicationLog.TYPE_SCHEME_REMINDER,
                phone=phone,
                status_val=CommunicationLog.STATUS_SENT,
                customer=customer,
                template_name=template_name,
                parameters=parameters,
                api_response=json.dumps(api_resp),
                ref_instalment_id=inst.id,
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({"instalment_id": inst.id, "customer": customer.full_name, "status": "sent"})
        except Exception as exc:
            _log_communication(
                channel=CommunicationLog.CHANNEL_WHATSAPP,
                message_type=CommunicationLog.TYPE_SCHEME_REMINDER,
                phone=phone,
                status_val=CommunicationLog.STATUS_FAILED,
                customer=customer,
                template_name=template_name,
                parameters=parameters,
                error_detail=str(exc),
                ref_instalment_id=inst.id,
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({"instalment_id": inst.id, "customer": customer.full_name, "status": "failed", "error": str(exc)})

    sent = sum(1 for r in results if r["status"] == "sent")
    failed = sum(1 for r in results if r["status"] == "failed")
    return Response({
        "message": f"WhatsApp reminders sent: {sent}, failed: {failed}, skipped: {len(results) - sent - failed}",
        "results": results,
    })


# ---------------------------------------------------------------------------
# Send SMS reminder (scheme instalments)
# ---------------------------------------------------------------------------

@api_view(["POST"])
@admin_auth(
    "CRM_CUSTOMER_UPCOMING_REMINDERS",
    "CRM_CUSTOMER_PAST_DUE_REMINDERS",
    "CRM_COMMUNICATION_SEND",
)
def send_sms_reminder(request):
    """
    POST body: { "instalment_ids": [1,2], "campaign_name": "" }
    Sends otpmsg.in DLT SMS reminder to each instalment's customer.
    """
    from shared.services.sms_service import send_scheme_due_reminder_sms

    instalment_ids = request.data.get("instalment_ids", [])
    campaign_name = request.data.get("campaign_name", "")

    if not instalment_ids:
        return Response({"error": "instalment_ids is required"}, status=status.HTTP_400_BAD_REQUEST)

    instalments = SchemeInstalment.objects.filter(id__in=instalment_ids).select_related(
        "customer_scheme__customer", "customer_scheme__scheme"
    )

    results = []
    for inst in instalments:
        customer = inst.customer_scheme.customer
        phone_raw = customer.mobile or ""
        phone = _normalise_phone(phone_raw)

        if not phone_raw:
            _log_communication(
                channel=CommunicationLog.CHANNEL_SMS,
                message_type=CommunicationLog.TYPE_SCHEME_REMINDER,
                phone="",
                status_val=CommunicationLog.STATUS_SKIPPED,
                customer=customer,
                error_detail="No mobile number",
                ref_instalment_id=inst.id,
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({"instalment_id": inst.id, "status": "skipped", "reason": "no_phone"})
            continue

        try:
            ok, api_text = send_scheme_due_reminder_sms(
                mobile=phone,
                customer_name=customer.full_name or "Customer",
                amount=inst.amount,
                due_date=inst.due_date,
            )
            message = (
                f"Scheme instalment reminder — {customer.full_name}, "
                f"Rs {inst.amount}, due {inst.due_date}"
            )
            _log_communication(
                channel=CommunicationLog.CHANNEL_SMS,
                message_type=CommunicationLog.TYPE_SCHEME_REMINDER,
                phone=phone,
                status_val=CommunicationLog.STATUS_SENT if ok else CommunicationLog.STATUS_FAILED,
                customer=customer,
                message_body=message,
                api_response=api_text,
                ref_instalment_id=inst.id,
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({"instalment_id": inst.id, "customer": customer.full_name, "status": "sent" if ok else "failed"})
        except Exception as exc:
            _log_communication(
                channel=CommunicationLog.CHANNEL_SMS,
                message_type=CommunicationLog.TYPE_SCHEME_REMINDER,
                phone=phone,
                status_val=CommunicationLog.STATUS_FAILED,
                customer=customer,
                error_detail=str(exc),
                ref_instalment_id=inst.id,
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({"instalment_id": inst.id, "status": "failed", "error": str(exc)})

    sent = sum(1 for r in results if r["status"] == "sent")
    return Response({
        "message": f"SMS reminders sent: {sent}, failed/skipped: {len(results) - sent}",
        "results": results,
    })


@api_view(["POST"])
@admin_auth(
    "CRM_CUSTOMER_VIEW",
    "CRM_COMMUNICATION_SEND",
)
def send_udhar_sms_reminder(request):
    """
    POST body: { "customer_ids": [1, 2], "campaign_name": "" }
    Sends udhar payment reminder SMS to customers with outstanding store balance.
    """
    from decimal import Decimal
    from shared.services.customer_store_account_service import get_customer_store_balance
    from shared.services.sms_service import send_udhar_payment_reminder_sms

    customer_ids = request.data.get("customer_ids", [])
    campaign_name = request.data.get("campaign_name", "")

    if not customer_ids:
        return Response({"error": "customer_ids is required"}, status=status.HTTP_400_BAD_REQUEST)

    customers = Customer.objects.filter(id__in=customer_ids)
    results = []

    for customer in customers:
        phone_raw = customer.mobile or ""
        phone = _normalise_phone(phone_raw)
        if not phone_raw:
            _log_communication(
                channel=CommunicationLog.CHANNEL_SMS,
                message_type=CommunicationLog.TYPE_UDHAR_REMINDER,
                phone="",
                status_val=CommunicationLog.STATUS_SKIPPED,
                customer=customer,
                error_detail="No mobile number",
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({"customer_id": customer.id, "status": "skipped", "reason": "no_phone"})
            continue

        bal = get_customer_store_balance(customer.id)
        outstanding = Decimal(str(bal.get("udhar_outstanding") or "0"))
        if outstanding <= 0:
            _log_communication(
                channel=CommunicationLog.CHANNEL_SMS,
                message_type=CommunicationLog.TYPE_UDHAR_REMINDER,
                phone=phone,
                status_val=CommunicationLog.STATUS_SKIPPED,
                customer=customer,
                error_detail="No udhar outstanding",
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({"customer_id": customer.id, "status": "skipped", "reason": "no_udhar"})
            continue

        try:
            ok, api_text = send_udhar_payment_reminder_sms(mobile=phone, balance_amount=outstanding)
            _log_communication(
                channel=CommunicationLog.CHANNEL_SMS,
                message_type=CommunicationLog.TYPE_UDHAR_REMINDER,
                phone=phone,
                status_val=CommunicationLog.STATUS_SENT if ok else CommunicationLog.STATUS_FAILED,
                customer=customer,
                message_body=f"Udhar reminder — Rs {outstanding}",
                api_response=api_text,
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({
                "customer_id": customer.id,
                "customer": customer.full_name,
                "status": "sent" if ok else "failed",
            })
        except Exception as exc:
            _log_communication(
                channel=CommunicationLog.CHANNEL_SMS,
                message_type=CommunicationLog.TYPE_UDHAR_REMINDER,
                phone=phone,
                status_val=CommunicationLog.STATUS_FAILED,
                customer=customer,
                error_detail=str(exc),
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({"customer_id": customer.id, "status": "failed", "error": str(exc)})

    sent = sum(1 for r in results if r["status"] == "sent")
    return Response({
        "message": f"Udhar SMS sent: {sent}, failed/skipped: {len(results) - sent}",
        "results": results,
    })


def _gold_rate_targets(request) -> tuple[list[tuple[str, Customer | None]], Response | None]:
    phones = request.data.get("phones") or request.query_params.getlist("phones") or []
    if isinstance(phones, str):
        phones = [p for p in phones.replace(",", " ").split() if p]
    customer_ids = request.data.get("customer_ids") or request.query_params.getlist("customer_ids") or []
    if isinstance(customer_ids, str):
        customer_ids = [customer_ids]

    targets: list[tuple[str, Customer | None]] = []
    seen = set()
    for raw in phones:
        if not raw:
            continue
        phone = _normalise_phone(str(raw))
        if phone in seen:
            continue
        seen.add(phone)
        targets.append((phone, None))

    ids = []
    for cid in customer_ids:
        try:
            ids.append(int(cid))
        except (TypeError, ValueError):
            continue
    if ids:
        for customer in Customer.objects.filter(id__in=ids):
            if not customer.mobile:
                continue
            phone = _normalise_phone(customer.mobile)
            if phone in seen:
                continue
            seen.add(phone)
            targets.append((phone, customer))

    if not targets:
        return [], Response(
            {"error": "Provide at least one phone or customer_id with mobile"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return targets, None


@api_view(["POST"])
@admin_auth("CRM_METAL_RATE", "CRM_COMMUNICATION_SEND")
def send_gold_rate_sms(request):
    """
    POST body: {
      "phones": ["9876543210"],          // optional
      "customer_ids": [1, 2],            // optional
      "branch_id": null,                 // optional — for branch rate lookup
      "campaign_name": ""
    }
    At least one of phones or customer_ids is required.
    """
    from shared.services.sms_service import send_gold_rate_sms as _send_gold_rate_sms

    targets, err = _gold_rate_targets(request)
    if err:
        return err
    branch_id = request.data.get("branch_id")
    campaign_name = request.data.get("campaign_name", "")

    results = []
    for phone, customer in targets:
        try:
            ok, api_text, sent_text = _send_gold_rate_sms(mobile=phone, branch_id=branch_id)
            _log_communication(
                channel=CommunicationLog.CHANNEL_SMS,
                message_type=CommunicationLog.TYPE_OFFER,
                phone=phone,
                status_val=CommunicationLog.STATUS_SENT if ok else CommunicationLog.STATUS_FAILED,
                customer=customer,
                message_body=sent_text or "Today's gold/silver rate SMS",
                api_response=api_text,
                campaign_name=campaign_name or "gold_rate",
                sent_by=request.user,
            )
            results.append({
                "phone": phone,
                "customer_id": customer.id if customer else None,
                "status": "sent" if ok else "failed",
                "text": sent_text,
                "detail": api_text,
            })
        except Exception as exc:
            _log_communication(
                channel=CommunicationLog.CHANNEL_SMS,
                message_type=CommunicationLog.TYPE_OFFER,
                phone=phone,
                status_val=CommunicationLog.STATUS_FAILED,
                customer=customer,
                error_detail=str(exc),
                campaign_name=campaign_name or "gold_rate",
                sent_by=request.user,
            )
            results.append({"phone": phone, "status": "failed", "error": str(exc)})

    sent = sum(1 for r in results if r["status"] == "sent")
    return Response({
        "message": f"Gold rate SMS sent: {sent}, failed/skipped: {len(results) - sent}",
        "results": results,
    })


# ---------------------------------------------------------------------------
# Send invoice via WhatsApp (PDF → S3 → WhatsApp)
# ---------------------------------------------------------------------------

@api_view(["POST"])
@admin_auth(
    "CRM_STORES_POS_VIEW",
    "CRM_ACCOUNTS_INVOICE_VIEW",
    "CRM_COMMUNICATION_SEND",
)
def send_invoice_whatsapp(request, pk: int):
    """
    POST /master/pos/invoice/<pk>/send-whatsapp/
    Body (optional): { "phone": "919876543210" }  — override recipient number.

    Uses approved Mart2Meta template `testtemplate` (en_US):
      Header : document (invoice PDF)
      Body   : Your Invoice is ready {{1}}{{2}}
               {{1}} = invoice number, {{2}} = remaining balance (pending amount)
    """
    from io import BytesIO
    from django.conf import settings as dj_settings
    from django.shortcuts import get_object_or_404
    from shared.services.pos_receipt_pdf import build_pos_invoice_pdf_bytes
    from shared.services.s3_service import upload_file_to_s3, build_public_object_url
    from shared.services.whatsapp_service import send_whatsapp_template, WhatsAppAPIError

    invoice = get_object_or_404(
        SaleInvoice.objects.prefetch_related("items").filter(is_deleted=False), pk=pk
    )

    phone_override = request.data.get("phone", "").strip()
    phone_raw = phone_override or invoice.bill_to_phone or ""
    if not phone_raw:
        return Response({"error": "No phone number for this invoice"}, status=status.HTTP_400_BAD_REQUEST)

    phone = _normalise_phone(phone_raw)

    # Build PDF bytes
    try:
        pdf_bytes = build_pos_invoice_pdf_bytes(invoice)
    except Exception as exc:
        return Response({"error": f"PDF generation failed: {exc}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Upload PDF to S3 (required — template has a document header)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in invoice.invoice_number)
    s3_key = f"invoices/{safe_name}.pdf"
    pdf_file = BytesIO(pdf_bytes)
    pdf_file.content_type = "application/pdf"
    pdf_file.name = f"{safe_name}.pdf"

    uploaded = upload_file_to_s3(pdf_file, s3_key)
    if not uploaded:
        return Response(
            {"error": "Failed to upload invoice PDF to S3. Cannot send document template without a public URL."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    pdf_url = build_public_object_url(s3_key)

    # Match approved template: testtemplate / en_US / {{1}} {{2}}
    # Mart2Meta format: parameters = "{value1},{value2}"
    template_name = getattr(dj_settings, "WHATSAPP_TEMPLATE_INVOICE", "testtemplate")
    template_language = getattr(dj_settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en_US")
    inv_no = str(invoice.invoice_number).replace(",", " ")
    amount = f"Rs.{invoice.pending_amount}".replace(",", " ")
    param_values = [inv_no, amount]
    parameters = f"{{{inv_no}}},{{{amount}}}"

    customer = invoice.customer
    try:
        api_resp = send_whatsapp_template(
            phone=phone,
            template_name=template_name,
            template_language=template_language,
            parameters=param_values,
            media_type="document",
            media_url=pdf_url,
        )
        if api_resp.get("status") == "skipped":
            _log_communication(
                channel=CommunicationLog.CHANNEL_WHATSAPP,
                message_type=CommunicationLog.TYPE_INVOICE,
                phone=phone,
                status_val=CommunicationLog.STATUS_SKIPPED,
                customer=customer,
                template_name=template_name,
                parameters=parameters,
                api_response=json.dumps(api_resp),
                error_detail="WhatsApp not configured",
                ref_invoice_id=invoice.id,
                sent_by=request.user,
            )
            return Response({"error": "WhatsApp not configured", "detail": api_resp}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        _log_communication(
            channel=CommunicationLog.CHANNEL_WHATSAPP,
            message_type=CommunicationLog.TYPE_INVOICE,
            phone=phone,
            status_val=CommunicationLog.STATUS_SENT,
            customer=customer,
            template_name=template_name,
            parameters=parameters,
            api_response=json.dumps(api_resp),
            ref_invoice_id=invoice.id,
            sent_by=request.user,
        )
        return Response({
            "message": "Invoice sent via WhatsApp",
            "phone": phone,
            "invoice_number": invoice.invoice_number,
            "template_name": template_name,
            "pdf_url": pdf_url,
            "api_response": api_resp,
        })
    except WhatsAppAPIError as exc:
        _log_communication(
            channel=CommunicationLog.CHANNEL_WHATSAPP,
            message_type=CommunicationLog.TYPE_INVOICE,
            phone=phone,
            status_val=CommunicationLog.STATUS_FAILED,
            customer=customer,
            template_name=template_name,
            parameters=parameters,
            api_response=exc.response_text[:2000],
            error_detail=str(exc),
            ref_invoice_id=invoice.id,
            sent_by=request.user,
        )
        return Response(
            {
                "error": "WhatsApp send failed",
                "status_code": exc.status_code,
                "detail": exc.response_text[:1000],
                "payload_sent": {
                    "template_name": template_name,
                    "template_language": template_language,
                    "parameters": parameters,
                    "media_type": "document",
                    "url": pdf_url,
                    "phone": phone,
                },
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )
    except Exception as exc:
        _log_communication(
            channel=CommunicationLog.CHANNEL_WHATSAPP,
            message_type=CommunicationLog.TYPE_INVOICE,
            phone=phone,
            status_val=CommunicationLog.STATUS_FAILED,
            customer=customer,
            template_name=template_name,
            parameters=parameters,
            error_detail=str(exc),
            ref_invoice_id=invoice.id,
            sent_by=request.user,
        )
        return Response({"error": f"WhatsApp send failed: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)


# ---------------------------------------------------------------------------
# Communication log list
# ---------------------------------------------------------------------------

@api_view(["GET"])
@admin_auth(
    "CRM_COMMUNICATION_VIEW",
    "CRM_CUSTOMER_UPCOMING_REMINDERS",
    "CRM_CUSTOMER_PAST_DUE_REMINDERS",
    "CRM_CUSTOMER_LIST",
)
def communication_log_list(request):
    """
    GET /master/crm/communication-logs/
    Query params: channel, message_type, status, customer_id, page, page_size
    """
    qs = CommunicationLog.objects.select_related("customer", "sent_by")

    channel = request.GET.get("channel")
    if channel:
        qs = qs.filter(channel=channel)

    message_type = request.GET.get("message_type")
    if message_type:
        qs = qs.filter(message_type=message_type)

    log_status = request.GET.get("status")
    if log_status:
        qs = qs.filter(status=log_status)

    customer_id = request.GET.get("customer_id")
    if customer_id:
        qs = qs.filter(customer_id=customer_id)

    campaign = request.GET.get("campaign_name")
    if campaign:
        qs = qs.filter(campaign_name__icontains=campaign)

    # Pagination
    page = max(1, int(request.GET.get("page", 1)))
    page_size = min(200, max(1, int(request.GET.get("page_size", 50))))
    total = qs.count()
    start = (page - 1) * page_size
    items = qs[start: start + page_size]

    data = []
    for log in items:
        data.append({
            "id": log.id,
            "channel": log.channel,
            "message_type": log.message_type,
            "status": log.status,
            "phone": log.phone,
            "customer_name": log.customer.full_name if log.customer else "",
            "customer_id": log.customer_id,
            "template_name": log.template_name,
            "parameters": log.parameters,
            "message_body": log.message_body,
            "api_response": log.api_response,
            "error_detail": log.error_detail,
            "campaign_name": log.campaign_name,
            "ref_invoice_id": log.ref_invoice_id,
            "ref_instalment_id": log.ref_instalment_id,
            "sent_at": log.sent_at.isoformat(),
            "sent_by": log.sent_by.username if log.sent_by else "",
        })

    return Response({
        "count": total,
        "page": page,
        "page_size": page_size,
        "results": data,
    })


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@api_view(["GET"])
@admin_auth(
    "CRM_COMMUNICATION_VIEW",
    "CRM_CUSTOMER_UPCOMING_REMINDERS",
    "CRM_CUSTOMER_PAST_DUE_REMINDERS",
    "CRM_CUSTOMER_LIST",
)
def communication_analytics(request):
    """
    GET /master/crm/communication-logs/analytics/
    Returns channel/status breakdown and campaign summaries.
    """
    days = int(request.GET.get("days", 30))
    since = timezone.now() - timedelta(days=days)

    qs = CommunicationLog.objects.filter(sent_at__gte=since)

    # Channel breakdown
    channel_stats = list(
        qs.values("channel", "status").annotate(count=Count("id")).order_by("channel", "status")
    )

    # Message type breakdown
    type_stats = list(
        qs.values("message_type", "status").annotate(count=Count("id")).order_by("message_type", "status")
    )

    # Campaign summary
    campaign_stats = list(
        qs.exclude(campaign_name="")
        .values("campaign_name", "channel", "status")
        .annotate(count=Count("id"))
        .order_by("campaign_name", "channel", "status")
    )

    # Totals
    total_sent = qs.filter(status=CommunicationLog.STATUS_SENT).count()
    total_failed = qs.filter(status=CommunicationLog.STATUS_FAILED).count()
    total_skipped = qs.filter(status=CommunicationLog.STATUS_SKIPPED).count()
    total = qs.count()

    whatsapp_qs = qs.filter(channel=CommunicationLog.CHANNEL_WHATSAPP)
    telecom_qs = qs.filter(channel__in=[CommunicationLog.CHANNEL_SMS, CommunicationLog.CHANNEL_CALL])
    wa_sent = whatsapp_qs.filter(status=CommunicationLog.STATUS_SENT).count()
    telecom_sent = telecom_qs.filter(status=CommunicationLog.STATUS_SENT).count()

    return Response({
        "period_days": days,
        "totals": {
            "total": total,
            "sent": total_sent,
            "failed": total_failed,
            "skipped": total_skipped,
            "delivery_rate": round(total_sent / total * 100, 1) if total else 0,
        },
        "whatsapp": {
            "total": whatsapp_qs.count(),
            "sent": wa_sent,
            "failed": whatsapp_qs.filter(status=CommunicationLog.STATUS_FAILED).count(),
            "delivery_rate": round(wa_sent / whatsapp_qs.count() * 100, 1) if whatsapp_qs.count() else 0,
        },
        "telecom": {
            "total": telecom_qs.count(),
            "sent": telecom_sent,
            "failed": telecom_qs.filter(status=CommunicationLog.STATUS_FAILED).count(),
            "calls": telecom_qs.filter(channel=CommunicationLog.CHANNEL_CALL).count(),
            "sms": telecom_qs.filter(channel=CommunicationLog.CHANNEL_SMS).count(),
            "delivery_rate": round(telecom_sent / telecom_qs.count() * 100, 1) if telecom_qs.count() else 0,
        },
        "by_channel": channel_stats,
        "by_message_type": type_stats,
        "by_campaign": campaign_stats,
    })


# ---------------------------------------------------------------------------
# Log a call reminder (manual / on-call)
# ---------------------------------------------------------------------------

@api_view(["POST"])
@admin_auth(
    "CRM_CUSTOMER_UPCOMING_REMINDERS",
    "CRM_CUSTOMER_PAST_DUE_REMINDERS",
    "CRM_COMMUNICATION_SEND",
    "CRM_CUSTOMER_LIST",
)
def log_call_reminder(request):
    """
    POST /master/crm/reminders/log-call/
    Body: {
      customer_id?, phone?, instalment_id?, message_type?, campaign_name?,
      notes?, outcome? (reached|no_answer|callback|other)
    }
    Logs an on-call reminder / outbound call attempt in CommunicationLog.
    """
    from shared.models import SchemeInstalment as SI

    customer_id = request.data.get("customer_id")
    phone_raw = (request.data.get("phone") or "").strip()
    instalment_id = request.data.get("instalment_id")
    message_type = request.data.get("message_type") or CommunicationLog.TYPE_SCHEME_REMINDER
    campaign_name = request.data.get("campaign_name") or ""
    notes = (request.data.get("notes") or "").strip()
    outcome = (request.data.get("outcome") or "other").strip()

    customer = None
    ref_instalment_id = None

    if instalment_id:
        try:
            inst = SI.objects.select_related("customer_scheme__customer").get(id=instalment_id)
            customer = inst.customer_scheme.customer
            ref_instalment_id = inst.id
            if not phone_raw:
                phone_raw = customer.mobile or ""
        except SI.DoesNotExist:
            return Response({"error": "instalment not found"}, status=status.HTTP_404_NOT_FOUND)

    if customer_id and not customer:
        try:
            customer = Customer.objects.get(id=customer_id)
            if not phone_raw:
                phone_raw = customer.mobile or ""
        except Customer.DoesNotExist:
            return Response({"error": "customer not found"}, status=status.HTTP_404_NOT_FOUND)

    if not phone_raw:
        return Response({"error": "phone is required"}, status=status.HTTP_400_BAD_REQUEST)

    phone = _normalise_phone(phone_raw)
    body = notes or f"Call reminder logged (outcome={outcome})"

    log = _log_communication(
        channel=CommunicationLog.CHANNEL_CALL,
        message_type=message_type if message_type in dict(CommunicationLog.TYPE_CHOICES) else CommunicationLog.TYPE_CUSTOM,
        phone=phone,
        status_val=CommunicationLog.STATUS_SENT,
        customer=customer,
        message_body=body,
        parameters=json.dumps({"outcome": outcome}),
        ref_instalment_id=ref_instalment_id,
        campaign_name=campaign_name,
        sent_by=request.user,
    )
    return Response({
        "message": "Call reminder logged",
        "id": log.id,
        "phone": phone,
        "customer_id": customer.id if customer else None,
        "outcome": outcome,
    }, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Schedule reminders
# ---------------------------------------------------------------------------

def _serialize_scheduled(row):
    return {
        "id": row.id,
        "customer_id": row.customer_id,
        "customer_name": row.customer.full_name if row.customer else "",
        "phone": row.phone,
        "channel": row.channel,
        "message_type": row.message_type,
        "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
        "status": row.status,
        "template_name": row.template_name,
        "parameters": row.parameters,
        "message_body": row.message_body,
        "campaign_name": row.campaign_name,
        "ref_instalment_id": row.ref_instalment_id,
        "ref_invoice_id": row.ref_invoice_id,
        "notes": row.notes,
        "processed_at": row.processed_at.isoformat() if row.processed_at else None,
        "error_detail": row.error_detail,
        "created_at": row.system_created_at.isoformat() if row.system_created_at else None,
    }


@api_view(["GET", "POST"])
@admin_auth(
    "CRM_CUSTOMER_UPCOMING_REMINDERS",
    "CRM_CUSTOMER_PAST_DUE_REMINDERS",
    "CRM_COMMUNICATION_SEND",
    "CRM_COMMUNICATION_VIEW",
    "CRM_CUSTOMER_LIST",
)
def scheduled_reminders(request):
    """
    GET  /master/crm/reminders/scheduled/  — list (status, channel, page)
    POST /master/crm/reminders/scheduled/  — create one or many
    """
    from django.utils.dateparse import parse_datetime
    from shared.models import CrmScheduledReminder, SchemeInstalment as SI

    if request.method == "GET":
        qs = CrmScheduledReminder.objects.select_related("customer").all()
        status_filter = request.GET.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        channel = request.GET.get("channel")
        if channel:
            qs = qs.filter(channel=channel)
        page = max(1, int(request.GET.get("page", 1)))
        page_size = min(200, max(1, int(request.GET.get("page_size", 50))))
        total = qs.count()
        start = (page - 1) * page_size
        items = [_serialize_scheduled(r) for r in qs[start: start + page_size]]
        return Response({"count": total, "page": page, "page_size": page_size, "results": items})

    # POST create
    payload = request.data
    items = payload if isinstance(payload, list) else [payload]
    created = []
    errors = []

    for idx, item in enumerate(items):
        phone_raw = (item.get("phone") or "").strip()
        customer_id = item.get("customer_id")
        instalment_id = item.get("instalment_id") or item.get("ref_instalment_id")
        scheduled_raw = item.get("scheduled_at")
        channel = (item.get("channel") or CrmScheduledReminder.CHANNEL_WHATSAPP).upper()
        message_type = item.get("message_type") or CrmScheduledReminder.TYPE_SCHEME_REMINDER

        customer = None
        ref_instalment_id = None
        if instalment_id:
            try:
                inst = SI.objects.select_related("customer_scheme__customer").get(id=instalment_id)
                customer = inst.customer_scheme.customer
                ref_instalment_id = inst.id
                if not phone_raw:
                    phone_raw = customer.mobile or ""
            except SI.DoesNotExist:
                errors.append({"index": idx, "error": "instalment not found"})
                continue

        if customer_id and not customer:
            try:
                customer = Customer.objects.get(id=customer_id)
                if not phone_raw:
                    phone_raw = customer.mobile or ""
            except Customer.DoesNotExist:
                errors.append({"index": idx, "error": "customer not found"})
                continue

        if not phone_raw:
            errors.append({"index": idx, "error": "phone is required"})
            continue
        if not scheduled_raw:
            errors.append({"index": idx, "error": "scheduled_at is required"})
            continue

        scheduled_at = parse_datetime(str(scheduled_raw))
        if scheduled_at is None:
            errors.append({"index": idx, "error": "invalid scheduled_at"})
            continue
        if timezone.is_naive(scheduled_at):
            scheduled_at = timezone.make_aware(scheduled_at, timezone.get_current_timezone())

        if channel not in dict(CrmScheduledReminder.CHANNEL_CHOICES):
            errors.append({"index": idx, "error": f"invalid channel: {channel}"})
            continue

        row = CrmScheduledReminder.objects.create(
            customer=customer,
            phone=_normalise_phone(phone_raw),
            channel=channel,
            message_type=message_type if message_type in dict(CrmScheduledReminder.TYPE_CHOICES) else CrmScheduledReminder.TYPE_CUSTOM,
            scheduled_at=scheduled_at,
            template_name=item.get("template_name") or "",
            parameters=item.get("parameters") or "",
            message_body=item.get("message_body") or "",
            campaign_name=item.get("campaign_name") or "",
            ref_instalment_id=ref_instalment_id,
            ref_invoice_id=item.get("ref_invoice_id") or item.get("invoice_id"),
            notes=item.get("notes") or "",
            created_by=request.user,
            updated_by=request.user,
        )
        created.append(_serialize_scheduled(row))

    return Response(
        {"message": f"Scheduled {len(created)} reminder(s)", "created": created, "errors": errors},
        status=status.HTTP_201_CREATED if created else status.HTTP_400_BAD_REQUEST,
    )


@api_view(["POST"])
@admin_auth(
    "CRM_CUSTOMER_UPCOMING_REMINDERS",
    "CRM_CUSTOMER_PAST_DUE_REMINDERS",
    "CRM_COMMUNICATION_SEND",
    "CRM_CUSTOMER_LIST",
)
def cancel_scheduled_reminder(request, pk: int):
    """POST /master/crm/reminders/scheduled/<pk>/cancel/"""
    from shared.models import CrmScheduledReminder

    try:
        row = CrmScheduledReminder.objects.get(pk=pk)
    except CrmScheduledReminder.DoesNotExist:
        return Response({"error": "not found"}, status=status.HTTP_404_NOT_FOUND)

    if row.status != CrmScheduledReminder.STATUS_PENDING:
        return Response({"error": f"cannot cancel status={row.status}"}, status=status.HTTP_400_BAD_REQUEST)

    row.status = CrmScheduledReminder.STATUS_CANCELLED
    row.updated_by = request.user
    row.save(update_fields=["status", "updated_by", "system_updated_at"])
    return Response({"message": "Cancelled", "id": row.id, "status": row.status})


@api_view(["POST"])
@admin_auth(
    "CRM_CUSTOMER_UPCOMING_REMINDERS",
    "CRM_CUSTOMER_PAST_DUE_REMINDERS",
    "CRM_COMMUNICATION_SEND",
    "CRM_CUSTOMER_LIST",
)
def process_scheduled_reminders(request):
    """
    POST /master/crm/reminders/process-scheduled/
    Body optional: { "limit": 100 }
    Processes due PENDING scheduled reminders (WhatsApp send / call queue log / SMS).
    """
    from shared.services.crm_reminder_service import process_due_scheduled_reminders

    limit = int(request.data.get("limit", 100))
    result = process_due_scheduled_reminders(limit=limit, sent_by=request.user)
    return Response(result)


# ---------------------------------------------------------------------------
# Send offer / marketing WhatsApp (template) to customer IDs
# ---------------------------------------------------------------------------

@api_view(["POST"])
@admin_auth(
    "CRM_COMMUNICATION_SEND",
    "CRM_CUSTOMER_LIST",
    "CRM_CUSTOMER_UPCOMING_REMINDERS",
)
def send_offer_whatsapp(request):
    """
    POST /master/crm/reminders/send-offer-whatsapp/
    Body: {
      customer_ids: [1,2],
      phones?: ["91..."],
      template_name?, template_language?,
      parameters?: ["v1","v2"],
      campaign_name?, message_type?: "OFFER"
    }
    """
    from django.conf import settings as dj_settings
    from shared.services.whatsapp_service import send_whatsapp_template

    customer_ids = request.data.get("customer_ids") or []
    phones = request.data.get("phones") or []
    template_name = request.data.get(
        "template_name",
        getattr(dj_settings, "WHATSAPP_TEMPLATE_OFFER", None)
        or getattr(dj_settings, "WHATSAPP_TEMPLATE_SCHEME_REMINDER", "testtemplate"),
    )
    template_language = request.data.get(
        "template_language",
        getattr(dj_settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en_US"),
    )
    param_values = request.data.get("parameters") or []
    campaign_name = request.data.get("campaign_name") or "Offer"
    message_type = request.data.get("message_type") or CommunicationLog.TYPE_OFFER
    if message_type not in dict(CommunicationLog.TYPE_CHOICES):
        message_type = CommunicationLog.TYPE_OFFER

    recipients = []
    if customer_ids:
        for c in Customer.objects.filter(id__in=customer_ids):
            recipients.append({"customer": c, "phone": c.mobile or ""})
    for p in phones:
        recipients.append({"customer": None, "phone": str(p)})

    if not recipients:
        return Response({"error": "customer_ids or phones required"}, status=status.HTTP_400_BAD_REQUEST)

    results = []
    for rec in recipients:
        phone_raw = rec["phone"]
        customer = rec["customer"]
        if not phone_raw:
            results.append({"customer_id": customer.id if customer else None, "status": "skipped", "reason": "no_phone"})
            continue
        phone = _normalise_phone(phone_raw)
        values = list(param_values) if param_values else [customer.full_name if customer else "Customer"]
        try:
            api_resp = send_whatsapp_template(
                phone=phone,
                template_name=template_name,
                template_language=template_language,
                parameters=values,
            )
            if api_resp.get("status") == "skipped":
                _log_communication(
                    channel=CommunicationLog.CHANNEL_WHATSAPP,
                    message_type=message_type,
                    phone=phone,
                    status_val=CommunicationLog.STATUS_SKIPPED,
                    customer=customer,
                    template_name=template_name,
                    parameters=json.dumps(values),
                    api_response=json.dumps(api_resp),
                    error_detail="WhatsApp not configured",
                    campaign_name=campaign_name,
                    sent_by=request.user,
                )
                results.append({"phone": phone, "status": "skipped", "reason": "not_configured"})
                continue
            _log_communication(
                channel=CommunicationLog.CHANNEL_WHATSAPP,
                message_type=message_type,
                phone=phone,
                status_val=CommunicationLog.STATUS_SENT,
                customer=customer,
                template_name=template_name,
                parameters=json.dumps(values),
                api_response=json.dumps(api_resp),
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({"phone": phone, "customer_id": customer.id if customer else None, "status": "sent"})
        except Exception as exc:
            _log_communication(
                channel=CommunicationLog.CHANNEL_WHATSAPP,
                message_type=message_type,
                phone=phone,
                status_val=CommunicationLog.STATUS_FAILED,
                customer=customer,
                template_name=template_name,
                parameters=json.dumps(values),
                error_detail=str(exc),
                campaign_name=campaign_name,
                sent_by=request.user,
            )
            results.append({"phone": phone, "status": "failed", "error": str(exc)})

    sent = sum(1 for r in results if r["status"] == "sent")
    return Response({
        "message": f"Offer WhatsApp: sent={sent}, other={len(results) - sent}",
        "results": results,
    })
