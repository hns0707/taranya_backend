"""
Active savings-scheme balances available for catalogue bill settlement (kitty redeem).
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from shared.models import Customer, CustomerScheme, LookupValue, SchemeInstalment
from shared.services.catalogue_quote_ledger_service import (
    CATALOGUE_SCHEME_CODE,
    compute_scheme_cash_running,
    _d,
    TWOPLACES,
)

FULL_REDEEM_TOLERANCE = Decimal('0.01')
TERMINAL_SCHEME_STATUS_CODES = frozenset({
    'ABANDONED',
    'COMPLETED',
    'MATURED',
    'REDEEMED',
    'CANCELLED',
})


def _is_cash_savings_scheme(customer_scheme: CustomerScheme) -> bool:
    """Gold-lock schemes redeem as metal, not cash on POS bill."""
    if customer_scheme.scheme.scheme_code == CATALOGUE_SCHEME_CODE:
        return False
    return not customer_scheme.benefits.filter(
        benefit_type__in=['FIXED_GRAM', 'DYNAMIC_LOCK']
    ).exists()


def get_customer_scheme_redeem_options(customer_id: int) -> dict:
    """
    List active customer schemes with cash available to apply on a catalogue bill.
    """
    customer = Customer.objects.get(pk=customer_id)
    try:
        active_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='ACTIVE')
        paid_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
    except LookupValue.DoesNotExist:
        return {'customer_id': customer_id, 'schemes': []}

    schemes_qs = (
        CustomerScheme.objects.filter(customer=customer, scheme_status=active_status)
        .exclude(scheme__scheme_code=CATALOGUE_SCHEME_CODE)
        .select_related('scheme')
        .order_by('-system_created_at')
    )

    rows = []
    for cs in schemes_qs:
        stats = SchemeInstalment.objects.filter(customer_scheme=cs).aggregate(
            paid_installments=Count('id', filter=Q(status=paid_status)),
            total_paid_amount=Sum('amount', filter=Q(status=paid_status)),
        )
        total_paid = _d(stats['total_paid_amount'] or 0)
        running = compute_scheme_cash_running(customer, cs)
        cash_available = running if running > 0 else Decimal('0.00')
        if not _is_cash_savings_scheme(cs):
            cash_available = Decimal('0.00')

        redeemable = min(cash_available, total_paid) if total_paid > 0 else cash_available
        if redeemable <= 0:
            continue

        rows.append({
            'customer_scheme_id': cs.id,
            'scheme_code': cs.scheme.scheme_code,
            'scheme_name': cs.scheme.scheme_name,
            'monthly_amount': str(cs.monthly_amount.quantize(TWOPLACES)),
            'paid_installments': stats['paid_installments'] or 0,
            'total_paid_amount': str(total_paid),
            'cash_available': str(redeemable.quantize(TWOPLACES)),
            'is_cash_scheme': True,
        })

    return {
        'customer_id': customer_id,
        'customer_name': customer.full_name,
        'schemes': rows,
    }


def parse_scheme_settlements_payload(raw) -> list[dict]:
    """Normalize [{customerSchemeId, amount}, ...] from API body."""
    if not raw:
        return []
    items = raw if isinstance(raw, list) else []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cs_id = item.get('customerSchemeId') or item.get('customer_scheme_id')
        amt = item.get('amount')
        if cs_id is None or amt is None:
            continue
        amount = _d(amt)
        if amount <= 0:
            continue
        out.append({
            'customer_scheme_id': int(cs_id),
            'amount': str(amount.quantize(TWOPLACES)),
        })
    return out


def total_scheme_settlement_amount(settlements: list[dict]) -> Decimal:
    return sum((_d(s['amount']) for s in settlements), Decimal('0')).quantize(TWOPLACES)


def validate_scheme_settlements(customer_id: int, settlements: list[dict]) -> str | None:
    if not settlements:
        return None
    options = {s['customer_scheme_id']: _d(s['cash_available']) for s in get_customer_scheme_redeem_options(customer_id)['schemes']}
    for item in settlements:
        cs_id = item['customer_scheme_id']
        amount = _d(item['amount'])
        available = options.get(cs_id)
        if available is None:
            return f'Scheme {cs_id} is not eligible for cash redemption on this bill.'
        if amount > available:
            return (
                f'Kitty redeem ({amount}) exceeds available balance ({available}) '
                f'for scheme {cs_id}.'
            )
    return None


def close_scheme_if_fully_redeemed(
    customer: Customer,
    customer_scheme: CustomerScheme,
    *,
    quote_number: str = '',
) -> bool:
    """
    After kitty redeem ledger posts, close the customer scheme when no cash balance remains.
    Sets terminal status (REDEEMED if configured, else MATURED) and cancels pending instalments.
    """
    customer_scheme = CustomerScheme.objects.select_related('scheme_status').get(
        pk=customer_scheme.pk,
    )
    status_code = (customer_scheme.scheme_status.code or '').upper()
    if status_code in TERMINAL_SCHEME_STATUS_CODES:
        return False

    remaining = compute_scheme_cash_running(customer, customer_scheme)
    if remaining > FULL_REDEEM_TOLERANCE:
        return False

    try:
        redeemed_status = LookupValue.objects.filter(
            lookup__code='SCHEME_STATUS',
            code='REDEEMED',
        ).first()
        matured_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='MATURED')
        pending_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
        cancelled_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='CANCELLED')
        paid_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
    except LookupValue.DoesNotExist:
        return False

    terminal_status = redeemed_status or matured_status
    now = timezone.now()

    paid_aggregate = SchemeInstalment.objects.filter(
        customer_scheme=customer_scheme,
        status=paid_inst,
    ).aggregate(total=Sum('amount'))
    total_paid = _d(paid_aggregate.get('total') or 0)

    reason = f'Fully redeemed at catalogue POS{f" ({quote_number})" if quote_number else ""}'
    customer_scheme.scheme_status = terminal_status
    customer_scheme.closed_at = now
    customer_scheme.maturity_amount = total_paid
    customer_scheme.bonus_processed = True
    customer_scheme.processed_at = now
    update_fields = [
        'scheme_status',
        'closed_at',
        'maturity_amount',
        'bonus_processed',
        'processed_at',
        'system_updated_at',
    ]
    if terminal_status.code == 'REDEEMED':
        customer_scheme.abandoned_reason = reason
        customer_scheme.abandoned_at = now
        update_fields.extend(['abandoned_reason', 'abandoned_at'])

    customer_scheme.save(update_fields=update_fields)

    SchemeInstalment.objects.filter(
        customer_scheme=customer_scheme,
        status=pending_inst,
    ).update(status=cancelled_inst)

    return True
