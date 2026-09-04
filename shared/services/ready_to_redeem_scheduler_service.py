"""
Mark schemes READY_TO_REDEEM when:
  1) Every payable (non-bonus) instalment is PAID
  2) Calendar duration from scheme start covers full tenure + bonus months
     e.g. 10+1 → 11 months; maturity = last day of that period

Run:
  python manage.py process_ready_to_redeem_schemes
  POST /internal/schemes/process-ready-to-redeem/
"""
from __future__ import annotations

import logging
from datetime import date

from django.db import transaction
from django.utils import timezone

from shared.models import CustomerScheme, LookupValue, SchemeInstalment
from shared.services.instalment_service import get_total_bonus_months
from shared.services.maturity_scheduler_service import process_one_matured_scheme
from shared.utils.scheme_date_engine import generate_scheme_schedule

logger = logging.getLogger(__name__)

SKIP_STATUS_CODES = frozenset({
    'READY_TO_REDEEM',
    'REDEEMED',
    'ABANDONED',
    'CANCELLED',
    'FAILED',
})
ELIGIBLE_STATUS_CODES = frozenset({'ACTIVE', 'COMPLETED', 'MATURED', 'PENDING'})


def _tenure_months(cs: CustomerScheme) -> int:
    return int(cs.tenure_months or (cs.scheme.tenure_months if cs.scheme_id else 0) or 0)


def _bonus_months(cs: CustomerScheme) -> int:
    return int(get_total_bonus_months(cs) or 0)


def scheme_total_months(cs: CustomerScheme) -> int:
    """Payable tenure + bonus months (10+1 → 11)."""
    tenure = _tenure_months(cs)
    bonus = _bonus_months(cs)
    return tenure + bonus if tenure else 0


def scheme_start_anchor(cs: CustomerScheme) -> date | None:
    if cs.start_date:
        return cs.start_date
    if cs.applied_at:
        return timezone.localtime(cs.applied_at).date()
    return None


def scheme_maturity_date(cs: CustomerScheme) -> date | None:
    """Last calendar day the scheme must cover (same engine as enrollment)."""
    if cs.end_date:
        return cs.end_date
    anchor = scheme_start_anchor(cs)
    tenure = _tenure_months(cs)
    if not anchor or not tenure:
        return None
    schedule = generate_scheme_schedule(anchor, tenure, _bonus_months(cs))
    return schedule['maturity_date']


def payable_instalments_all_paid(cs: CustomerScheme) -> bool:
    try:
        paid_lv = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
    except LookupValue.DoesNotExist:
        return False
    payable = SchemeInstalment.objects.filter(customer_scheme=cs, is_bonus=False)
    if not payable.exists():
        return False
    return not payable.exclude(status=paid_lv).exists()


def is_ready_to_redeem_eligible(cs: CustomerScheme, today: date | None = None) -> tuple[bool, str]:
    today = today or timezone.localdate()
    status_code = (cs.scheme_status and cs.scheme_status.code) or ''
    if status_code in SKIP_STATUS_CODES:
        return False, f'status_{status_code.lower()}'
    if status_code not in ELIGIBLE_STATUS_CODES:
        return False, f'status_{status_code.lower() or "unknown"}'
    if not payable_instalments_all_paid(cs):
        return False, 'unpaid_payable_instalments'
    maturity = scheme_maturity_date(cs)
    if not maturity:
        return False, 'no_start_or_tenure'
    if today < maturity:
        return False, f'duration_open_until_{maturity.isoformat()}'
    return True, 'ok'


@transaction.atomic
def mark_scheme_ready_to_redeem(cs: CustomerScheme) -> CustomerScheme:
    cs = CustomerScheme.objects.select_for_update().select_related('scheme', 'scheme_status', 'customer').get(id=cs.id)
    ready_lv = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='READY_TO_REDEEM')
    if cs.scheme_status_id == ready_lv.id:
        return cs

    # Apply bonus + gold lock first when still COMPLETED and not processed
    status_code = (cs.scheme_status and cs.scheme_status.code) or ''
    if status_code == 'COMPLETED' and not cs.bonus_processed:
        process_one_matured_scheme(cs)
        cs.refresh_from_db()

    cs.scheme_status = ready_lv
    cs.save(update_fields=['scheme_status', 'system_updated_at'])

    try:
        from shared.services.icici_upi_service import maybe_revoke_mandate_when_scheme_completed
        maybe_revoke_mandate_when_scheme_completed(cs.id)
    except Exception as exc:
        logger.exception('Revoke after READY_TO_REDEEM failed scheme=%s: %s', cs.id, exc)

    logger.info('Scheme %s set to READY_TO_REDEEM', cs.id)
    return cs


def process_ready_to_redeem_schemes(
    *,
    customer_scheme_id: int | None = None,
    dry_run: bool = False,
    limit: int = 500,
) -> dict:
    today = timezone.localdate()
    try:
        LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='READY_TO_REDEEM')
    except LookupValue.DoesNotExist:
        return {
            'date': str(today),
            'dry_run': dry_run,
            'error': 'Lookup SCHEME_STATUS / READY_TO_REDEEM is missing. Add it before running this job.',
            'scanned': 0,
            'marked': 0,
            'skipped': 0,
            'errors': [],
        }

    qs = (
        CustomerScheme.objects.select_related('scheme', 'scheme_status', 'customer')
        .exclude(scheme_status__code__in=list(SKIP_STATUS_CODES))
        .order_by('id')
    )
    if customer_scheme_id is not None:
        qs = qs.filter(id=customer_scheme_id)

    summary = {
        'date': str(today),
        'dry_run': dry_run,
        'scanned': 0,
        'marked': 0,
        'skipped': 0,
        'errors': [],
    }

    for cs in qs[: max(1, min(limit, 2000))]:
        summary['scanned'] += 1
        ok, reason = is_ready_to_redeem_eligible(cs, today)
        if not ok:
            summary['skipped'] += 1
            logger.debug('Skip scheme=%s reason=%s', cs.id, reason)
            continue
        try:
            if dry_run:
                summary['marked'] += 1
                logger.info(
                    'DRY RUN READY_TO_REDEEM scheme=%s tenure+bonus=%s maturity=%s',
                    cs.id,
                    scheme_total_months(cs),
                    scheme_maturity_date(cs),
                )
            else:
                mark_scheme_ready_to_redeem(cs)
                summary['marked'] += 1
        except Exception as exc:
            logger.exception('READY_TO_REDEEM failed scheme=%s: %s', cs.id, exc)
            summary['errors'].append({'customer_scheme_id': cs.id, 'error': str(exc)})

    return summary
