"""
Daily processor for ICICI UPI mandate PDN + recurring ExecuteMandate.

Run via cron:
  python manage.py process_upi_mandate_dues

Or internal API:
  POST /internal/upi-mandates/process-dues/
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from shared.models import UpiMandate, UpiMandateExecution, UpiMandateNotification
from shared.services.icici_upi_service import (
    _next_unpaid_instalment,
    execute_mandate_instalment,
    maybe_revoke_mandate_when_scheme_completed,
    send_mandate_notification,
)
from shared.utils.upi_mandate_dates import debit_dates_for_instalment

logger = logging.getLogger(__name__)

PDN_MIN_HOURS = 24


def process_upi_mandate_dues(
    *,
    mandate_id: int | None = None,
    dry_run: bool = False,
    notify_only: bool = False,
    execute_only: bool = False,
    limit: int = 200,
) -> dict:
    """
    For each APPROVED mandate with UMN:
      - notification_date == today  -> MandateNotification (instalment 2+)
      - debit_date == today         -> ExecuteMandate (after PDN success + 24h, or instalment 1 retry)
    """
    today = timezone.localdate()
    now = timezone.now()

    qs = (
        UpiMandate.objects.filter(status=UpiMandate.STATUS_APPROVED)
        .exclude(umn__isnull=True)
        .exclude(umn='')
        .select_related('customer_scheme')
        .order_by('id')
    )
    if mandate_id is not None:
        qs = qs.filter(id=mandate_id)

    summary = {
        'date': str(today),
        'dry_run': dry_run,
        'mandates_scanned': 0,
        'notifications_sent': 0,
        'notifications_skipped': 0,
        'executions_sent': 0,
        'executions_skipped': 0,
        'revokes_attempted': 0,
        'errors': [],
    }

    for mandate in qs[: max(1, min(limit, 1000))]:
        summary['mandates_scanned'] += 1
        instalment = _next_unpaid_instalment(mandate.customer_scheme_id)
        if not instalment:
            # All payable (non-bonus) instalments paid — revoke AutoPay (retry if payment hook missed)
            if dry_run:
                summary['revokes_attempted'] += 1
                logger.info('DRY RUN revoke mandate=%s (no unpaid payable instalments)', mandate.id)
            else:
                try:
                    maybe_revoke_mandate_when_scheme_completed(mandate.customer_scheme_id)
                    summary['revokes_attempted'] += 1
                except Exception as exc:
                    logger.exception('Revoke on schedule failed mandate=%s: %s', mandate.id, exc)
                    summary['errors'].append({
                        'mandate_id': mandate.id,
                        'instalment_id': None,
                        'error': f'revoke: {exc}',
                    })
            continue

        notif_date, debit_date = debit_dates_for_instalment(
            instalment,
            mandate.customer_scheme,
            debit_day=mandate.debit_day,
        )

        try:
            if not execute_only and today == notif_date and instalment.instalment_no >= 2:
                existing = UpiMandateNotification.objects.filter(
                    scheme_instalment=instalment,
                    status=UpiMandateNotification.STATUS_SUCCESS,
                ).exists()
                if existing:
                    summary['notifications_skipped'] += 1
                elif dry_run:
                    summary['notifications_sent'] += 1
                    logger.info(
                        'DRY RUN notify mandate=%s instalment=%s amount=%s',
                        mandate.id,
                        instalment.id,
                        instalment.amount,
                    )
                else:
                    send_mandate_notification(mandate, instalment)
                    summary['notifications_sent'] += 1

            if not notify_only and today == debit_date:
                can_execute = False
                skip_reason = ''

                if instalment.instalment_no == 1:
                    # First instalment: no PDN — setup path or retry failed first debit
                    latest = (
                        UpiMandateExecution.objects.filter(
                            upi_mandate=mandate,
                            scheme_instalment=instalment,
                        )
                        .order_by('-id')
                        .first()
                    )
                    if latest and latest.txn_status == UpiMandateExecution.TXN_SUCCESS:
                        skip_reason = 'already_paid'
                    else:
                        can_execute = True
                else:
                    pdn = (
                        UpiMandateNotification.objects.filter(scheme_instalment=instalment)
                        .order_by('-id')
                        .first()
                    )
                    if not pdn:
                        skip_reason = 'no_pdn'
                    elif pdn.status != UpiMandateNotification.STATUS_SUCCESS:
                        skip_reason = f'pdn_{pdn.status.lower()}'
                    elif not pdn.notified_at:
                        skip_reason = 'pdn_no_timestamp'
                    elif pdn.notified_at > now - timedelta(hours=PDN_MIN_HOURS):
                        skip_reason = 'pdn_within_24h'
                    else:
                        latest = (
                            UpiMandateExecution.objects.filter(scheme_instalment=instalment)
                            .order_by('-id')
                            .first()
                        )
                        if latest and latest.txn_status in (
                            UpiMandateExecution.TXN_SUCCESS,
                            UpiMandateExecution.TXN_INITIATED,
                            UpiMandateExecution.TXN_PENDING,
                        ):
                            skip_reason = f'execution_{latest.txn_status.lower()}'
                        else:
                            can_execute = True

                if not can_execute:
                    summary['executions_skipped'] += 1
                    logger.debug(
                        'Skip execute mandate=%s instalment=%s reason=%s',
                        mandate.id,
                        instalment.id,
                        skip_reason,
                    )
                    continue

                if dry_run:
                    summary['executions_sent'] += 1
                    logger.info(
                        'DRY RUN execute mandate=%s instalment=%s amount=%s',
                        mandate.id,
                        instalment.id,
                        instalment.amount,
                    )
                else:
                    require_pdn = instalment.instalment_no >= 2
                    execute_mandate_instalment(
                        mandate,
                        instalment,
                        require_notification=require_pdn,
                    )
                    summary['executions_sent'] += 1

        except Exception as exc:
            logger.exception(
                'UPI mandate scheduler mandate=%s instalment=%s: %s',
                mandate.id,
                instalment.id,
                exc,
            )
            summary['errors'].append({
                'mandate_id': mandate.id,
                'instalment_id': instalment.id,
                'error': str(exc),
            })

    return summary
