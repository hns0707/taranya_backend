"""
Process due CrmScheduledReminder rows (WhatsApp / SMS / Call queue).
"""
import json
import logging

import requests as http_requests
from django.conf import settings as dj_settings
from django.utils import timezone

from shared.models import CommunicationLog, CrmScheduledReminder

logger = logging.getLogger(__name__)


def _log_communication(**kwargs):
    return CommunicationLog.objects.create(**kwargs)


def _normalise_phone(phone: str) -> str:
    p = phone.strip().lstrip("+").lstrip("0")
    if len(p) == 10:
        p = "91" + p
    return p


def process_due_scheduled_reminders(*, limit: int = 100, sent_by=None) -> dict:
    """
    Send / log all PENDING reminders with scheduled_at <= now.
    Returns counts: processed, sent, failed, skipped, call_queued.
    """
    from shared.services.whatsapp_service import send_whatsapp_template

    now = timezone.now()
    qs = (
        CrmScheduledReminder.objects
        .filter(status=CrmScheduledReminder.STATUS_PENDING, scheduled_at__lte=now)
        .select_related("customer")
        .order_by("scheduled_at")[: max(1, min(limit, 500))]
    )

    counts = {
        "processed": 0,
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "call_queued": 0,
        "results": [],
    }

    for row in qs:
        counts["processed"] += 1
        phone = _normalise_phone(row.phone) if row.phone else ""
        if not phone:
            row.status = CrmScheduledReminder.STATUS_SKIPPED
            row.error_detail = "No phone"
            row.processed_at = now
            row.save(update_fields=["status", "error_detail", "processed_at", "system_updated_at"])
            counts["skipped"] += 1
            counts["results"].append({"id": row.id, "status": "skipped", "reason": "no_phone"})
            continue

        try:
            if row.channel == CrmScheduledReminder.CHANNEL_WHATSAPP:
                template_name = row.template_name or getattr(
                    dj_settings, "WHATSAPP_TEMPLATE_SCHEME_REMINDER", "testtemplate"
                )
                template_language = getattr(dj_settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en_US")
                param_values = []
                if row.parameters:
                    try:
                        parsed = json.loads(row.parameters)
                        if isinstance(parsed, list):
                            param_values = [str(x) for x in parsed]
                    except (TypeError, ValueError):
                        param_values = [
                            p.strip("{} ") for p in row.parameters.split("},{") if p.strip()
                        ]
                if not param_values and row.customer:
                    param_values = [row.customer.full_name or "Customer"]

                api_resp = send_whatsapp_template(
                    phone=phone,
                    template_name=template_name,
                    template_language=template_language,
                    parameters=param_values or None,
                )
                if api_resp.get("status") == "skipped":
                    log = _log_communication(
                        channel=CommunicationLog.CHANNEL_WHATSAPP,
                        message_type=row.message_type if row.message_type in dict(CommunicationLog.TYPE_CHOICES) else CommunicationLog.TYPE_CUSTOM,
                        phone=phone,
                        status=CommunicationLog.STATUS_SKIPPED,
                        customer=row.customer,
                        template_name=template_name,
                        parameters=row.parameters,
                        message_body=row.message_body,
                        error_detail="WhatsApp not configured",
                        api_response=json.dumps(api_resp),
                        ref_instalment_id=row.ref_instalment_id,
                        ref_invoice_id=row.ref_invoice_id,
                        campaign_name=row.campaign_name,
                        sent_by=sent_by,
                    )
                    row.status = CrmScheduledReminder.STATUS_SKIPPED
                    row.communication_log = log
                    row.processed_at = now
                    row.error_detail = "WhatsApp not configured"
                    row.save(update_fields=[
                        "status", "communication_log", "processed_at", "error_detail", "system_updated_at",
                    ])
                    counts["skipped"] += 1
                    counts["results"].append({"id": row.id, "status": "skipped"})
                    continue

                log = _log_communication(
                    channel=CommunicationLog.CHANNEL_WHATSAPP,
                    message_type=row.message_type if row.message_type in dict(CommunicationLog.TYPE_CHOICES) else CommunicationLog.TYPE_CUSTOM,
                    phone=phone,
                    status=CommunicationLog.STATUS_SENT,
                    customer=row.customer,
                    template_name=template_name,
                    parameters=row.parameters,
                    message_body=row.message_body,
                    api_response=json.dumps(api_resp),
                    ref_instalment_id=row.ref_instalment_id,
                    ref_invoice_id=row.ref_invoice_id,
                    campaign_name=row.campaign_name,
                    sent_by=sent_by,
                )
                row.status = CrmScheduledReminder.STATUS_SENT
                row.communication_log = log
                row.processed_at = now
                row.save(update_fields=[
                    "status", "communication_log", "processed_at", "system_updated_at",
                ])
                counts["sent"] += 1
                counts["results"].append({"id": row.id, "status": "sent"})

            elif row.channel == CrmScheduledReminder.CHANNEL_SMS:
                from shared.services.sms_service import send_dlt_sms
                from django.conf import settings as dj_settings

                message = row.message_body or (
                    f"Dear customer, reminder from Taranya Jewels. {row.notes}".strip()
                )
                dlt_id = ""
                if row.message_type == CommunicationLog.TYPE_SCHEME_REMINDER:
                    dlt_id = dj_settings.SMS_DLT_SCHEME_DUE_REMINDER
                elif row.message_type == CommunicationLog.TYPE_UDHAR_REMINDER:
                    dlt_id = dj_settings.SMS_DLT_UDHAR_REMINDER
                elif row.message_type == CommunicationLog.TYPE_OFFER:
                    dlt_id = dj_settings.SMS_DLT_GOLD_RATE

                ok, api_text = send_dlt_sms(
                    number=phone,
                    text=message,
                    dlt_template_id=dlt_id or dj_settings.SMS_DLT_SCHEME_DUE_REMINDER,
                )
                log = _log_communication(
                    channel=CommunicationLog.CHANNEL_SMS,
                    message_type=row.message_type if row.message_type in dict(CommunicationLog.TYPE_CHOICES) else CommunicationLog.TYPE_CUSTOM,
                    phone=phone,
                    status=CommunicationLog.STATUS_SENT if ok else CommunicationLog.STATUS_FAILED,
                    customer=row.customer,
                    message_body=message,
                    api_response=api_text[:500],
                    ref_instalment_id=row.ref_instalment_id,
                    ref_invoice_id=row.ref_invoice_id,
                    campaign_name=row.campaign_name,
                    sent_by=sent_by,
                )
                row.status = CrmScheduledReminder.STATUS_SENT if ok else CrmScheduledReminder.STATUS_FAILED
                row.communication_log = log
                row.processed_at = now
                if not ok:
                    row.error_detail = api_text[:500]
                row.save(update_fields=[
                    "status", "communication_log", "processed_at", "error_detail", "system_updated_at",
                ])
                if ok:
                    counts["sent"] += 1
                else:
                    counts["failed"] += 1
                counts["results"].append({"id": row.id, "status": row.status.lower()})

            else:
                # CALL — queue as logged call reminder for staff
                body = row.message_body or row.notes or "Scheduled call reminder due"
                log = _log_communication(
                    channel=CommunicationLog.CHANNEL_CALL,
                    message_type=row.message_type if row.message_type in dict(CommunicationLog.TYPE_CHOICES) else CommunicationLog.TYPE_CUSTOM,
                    phone=phone,
                    status=CommunicationLog.STATUS_SENT,
                    customer=row.customer,
                    message_body=body,
                    parameters=json.dumps({"scheduled": True}),
                    ref_instalment_id=row.ref_instalment_id,
                    ref_invoice_id=row.ref_invoice_id,
                    campaign_name=row.campaign_name,
                    sent_by=sent_by,
                )
                row.status = CrmScheduledReminder.STATUS_SENT
                row.communication_log = log
                row.processed_at = now
                row.save(update_fields=[
                    "status", "communication_log", "processed_at", "system_updated_at",
                ])
                counts["call_queued"] += 1
                counts["sent"] += 1
                counts["results"].append({"id": row.id, "status": "call_queued"})

        except Exception as exc:
            logger.exception("Scheduled reminder %s failed", row.id)
            log = _log_communication(
                channel=row.channel if row.channel in dict(CommunicationLog.CHANNEL_CHOICES) else CommunicationLog.CHANNEL_WHATSAPP,
                message_type=row.message_type if row.message_type in dict(CommunicationLog.TYPE_CHOICES) else CommunicationLog.TYPE_CUSTOM,
                phone=phone,
                status=CommunicationLog.STATUS_FAILED,
                customer=row.customer,
                template_name=row.template_name,
                parameters=row.parameters,
                message_body=row.message_body,
                error_detail=str(exc),
                ref_instalment_id=row.ref_instalment_id,
                ref_invoice_id=row.ref_invoice_id,
                campaign_name=row.campaign_name,
                sent_by=sent_by,
            )
            row.status = CrmScheduledReminder.STATUS_FAILED
            row.communication_log = log
            row.processed_at = now
            row.error_detail = str(exc)[:2000]
            row.save(update_fields=[
                "status", "communication_log", "processed_at", "error_detail", "system_updated_at",
            ])
            counts["failed"] += 1
            counts["results"].append({"id": row.id, "status": "failed", "error": str(exc)})

    return counts
