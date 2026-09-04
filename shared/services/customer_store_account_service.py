"""
Store cash account per customer (catalogue scheme bucket).

running_balance semantics (shop ledger):
  positive = customer owes (UDHAR)
  negative = customer has credit / advance (JAMA)

Customer-facing signed_balance = -running_balance:
  positive signed = JAMA available
  negative signed = UDHAR outstanding
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from shared.models import Customer, CustomerLedger
from shared.services.catalogue_quote_ledger_service import (
    get_or_create_catalogue_customer_scheme,
    compute_scheme_cash_running,
    _create_ledger_row,
    _d,
    TWOPLACES,
)

REF_STORE_ADVANCE = 'STORE_ADVANCE'


def balance_snapshot_for_storage(balance: dict | None) -> dict:
    """Ensure store balance dict is safe for Django JSONField (no Decimal)."""
    if not balance:
        return {}
    return {
        key: (str(value) if isinstance(value, Decimal) else value)
        for key, value in balance.items()
    }


def get_customer_store_balance(customer_id: int) -> dict:
    customer = Customer.objects.get(pk=customer_id)
    scheme = get_or_create_catalogue_customer_scheme(customer)
    running = compute_scheme_cash_running(customer, scheme)
    last = (
        CustomerLedger.objects.filter(customer=customer, customer_scheme=scheme)
        .order_by('-id')
        .first()
    )
    gold = last.running_gold_balance if last else Decimal('0.0000')
    silver = last.running_silver_balance if last else Decimal('0.0000')

    jama = abs(running) if running < 0 else Decimal('0.00')
    udhar = running if running > 0 else Decimal('0.00')
    signed = -running

    if signed > 0:
        label = 'JAMA'
    elif signed < 0:
        label = 'UDHAR'
    else:
        label = 'CLEAR'

    udhar_signed = (-udhar).quantize(TWOPLACES) if udhar > 0 else Decimal('0.00')

    return {
        'customer_id': customer_id,
        'scheme_reference': scheme.scheme_reference,
        'running_balance': str(running.quantize(TWOPLACES)),
        'signed_balance': str(signed.quantize(TWOPLACES)),
        'label': label,
        'jama_available': str(jama.quantize(TWOPLACES)),
        'udhar_outstanding': str(udhar.quantize(TWOPLACES)),
        'udhar_signed': str(udhar_signed),
        'gold_grams': str(gold),
        'silver_grams': str(silver),
    }


@transaction.atomic
def record_store_advance(
    customer_id: int,
    amount,
    *,
    mode_code: str = 'CASH',
    remark: str = '',
    admin_user=None,
) -> dict:
    """Customer deposits advance at counter (no bill). Creates JAMA (negative running)."""
    amount = _d(amount)
    if amount <= 0:
        raise ValueError('Advance amount must be greater than zero.')

    customer = Customer.objects.get(pk=customer_id)
    scheme = get_or_create_catalogue_customer_scheme(customer)
    ref_id = int(timezone.now().timestamp() * 1000) % 2147483647

    entry = _create_ledger_row(
        customer=customer,
        customer_scheme=scheme,
        entry_type='CREDIT',
        amount=amount,
        reference_type=REF_STORE_ADVANCE,
        reference_id=ref_id,
        entry_date=timezone.now(),
        description=remark.strip() or 'Advance deposit (JAMA)',
        invoice='',
        source=(mode_code or 'CASH').upper(),
    )

    bal = get_customer_store_balance(customer_id)
    return {
        'ledger_id': entry.id,
        'amount': str(amount),
        **{k: bal[k] for k in ('signed_balance', 'label', 'jama_available', 'udhar_outstanding')},
    }
