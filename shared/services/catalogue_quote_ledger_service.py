"""
Post catalogue quotation / booking / order amounts to customer_ledger (cash udhar + payments).

Ledger convention:
  - DEBIT CASH  = catalogue sale (udhar / NAAM) — increases outstanding (positive running_balance)
  - CREDIT CASH = customer payment (JAMA) — reduces outstanding
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from shared.models import (
    CatalogueQuote,
    CatalogueQuotePayment,
    Customer,
    CustomerLedger,
    CustomerScheme,
    LookupValue,
    Redemption,
    SchemeMaster,
)
from shared.utils.ledger_utils import create_ledger_entry
from shared.services.catalogue_invoice_service import (
    PAYMENT_REF_CATALOGUE,
    ensure_catalogue_quote_payments,
    ensure_catalogue_sale_invoice,
    get_invoice_receipt_number,
)

logger = logging.getLogger(__name__)

CATALOGUE_SCHEME_CODE = 'STORE_CATALOGUE'
CATALOGUE_SCHEME_NAME = 'Store Catalogue Sales'
REF_SALE = 'CATALOGUE_QUOTE'
REF_PAYMENT = 'CATALOGUE_QUOTE_PAYMENT'
REF_JAMA_SETTLEMENT = 'STORE_JAMA_SETTLEMENT'
REF_SCHEME_REDEEM = 'SCHEME_REDEEM'
REF_STORE_ADVANCE = 'STORE_ADVANCE'

CATALOGUE_CASH_REFS = frozenset({
    REF_SALE,
    REF_PAYMENT,
    REF_JAMA_SETTLEMENT,
    REF_SCHEME_REDEEM,
    REF_STORE_ADVANCE,
})

TWOPLACES = Decimal('0.01')


def _d(value) -> Decimal:
    if value is None:
        return Decimal('0')
    return Decimal(str(value)).quantize(TWOPLACES)


def _get_active_scheme_status():
    return LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='ACTIVE')


def get_or_create_catalogue_customer_scheme(customer: Customer) -> CustomerScheme:
    """
    One logical "store catalogue" bucket per customer for ledger running balances.
    """
    scheme, _ = SchemeMaster.objects.get_or_create(
        scheme_code=CATALOGUE_SCHEME_CODE,
        defaults={
            'scheme_name': CATALOGUE_SCHEME_NAME,
            'tenure_months': 12,
            'min_instalment': Decimal('1.00'),
            'max_instalment': Decimal('9999999.00'),
            'is_active': False,
            'scheme_description': 'Internal ledger bucket for catalogue POS quotations (not a savings scheme).',
        },
    )

    existing = (
        CustomerScheme.objects.filter(customer=customer, scheme=scheme)
        .order_by('id')
        .first()
    )
    if existing:
        return existing

    active_status = _get_active_scheme_status()
    ref = f'CAT-{customer.id}'
    suffix = 1
    while CustomerScheme.objects.filter(scheme_reference=ref).exists():
        ref = f'CAT-{customer.id}-{suffix}'
        suffix += 1

    return CustomerScheme.objects.create(
        customer=customer,
        scheme=scheme,
        monthly_amount=Decimal('1.00'),
        scheme_reference=ref,
        tenure_months=12,
        total_instalments=0,
        scheme_status=active_status,
        activated_at=timezone.now(),
    )


def _is_catalogue_store_scheme(customer_scheme: CustomerScheme) -> bool:
    try:
        return customer_scheme.scheme.scheme_code == CATALOGUE_SCHEME_CODE
    except Exception:
        return False


def compute_scheme_cash_running(customer: Customer, customer_scheme: CustomerScheme) -> Decimal:
    """
    Recompute cash running from ledger rows (source of truth for display).
    Store catalogue: + = UDHAR, − = JAMA.
  Other schemes: + = prepaid (legacy scheme instalments).
    """
    entries = (
        CustomerLedger.objects.filter(
            customer=customer,
            customer_scheme=customer_scheme,
            value_type='CASH',
        )
        .exclude(reference_type='GOLD_LOCK')
        .order_by('entry_date', 'id')
    )
    running = Decimal('0.00')
    catalogue_scheme = _is_catalogue_store_scheme(customer_scheme)

    for entry in entries:
        ref = entry.reference_type or ''
        use_catalogue_rules = catalogue_scheme or ref in CATALOGUE_CASH_REFS
        if use_catalogue_rules:
            if entry.entry_type == 'DEBIT':
                running += entry.amount or Decimal('0')
            elif entry.entry_type == 'CREDIT':
                running -= entry.amount or Decimal('0')
        elif entry.entry_type == 'CREDIT':
            running += entry.amount or Decimal('0')
        elif entry.entry_type == 'DEBIT':
            running -= entry.amount or Decimal('0')
    return running.quantize(TWOPLACES)


def repair_scheme_running_balances(customer: Customer, customer_scheme: CustomerScheme) -> None:
    """Persist running_balance on each row from recomputed cash (fixes legacy inverted values)."""
    entries = (
        CustomerLedger.objects.filter(customer=customer, customer_scheme=customer_scheme)
        .order_by('entry_date', 'id')
    )
    running_cash = Decimal('0.00')
    catalogue_scheme = _is_catalogue_store_scheme(customer_scheme)

    for entry in entries:
        if entry.value_type == 'CASH':
            ref = entry.reference_type or ''
            use_catalogue_rules = catalogue_scheme or ref in CATALOGUE_CASH_REFS
            if use_catalogue_rules:
                if entry.entry_type == 'DEBIT':
                    running_cash += entry.amount or Decimal('0')
                elif entry.entry_type == 'CREDIT':
                    running_cash -= entry.amount or Decimal('0')
            elif entry.entry_type == 'CREDIT':
                running_cash += entry.amount or Decimal('0')
            elif entry.entry_type == 'DEBIT':
                running_cash -= entry.amount or Decimal('0')
            CustomerLedger.objects.filter(pk=entry.pk).update(
                running_balance=running_cash.quantize(TWOPLACES)
            )


def _last_balances(customer: Customer, customer_scheme: CustomerScheme):
    last = (
        CustomerLedger.objects.filter(customer=customer, customer_scheme=customer_scheme)
        .order_by('-id')
        .first()
    )
    if not last:
        return Decimal('0.00'), Decimal('0.0000'), Decimal('0.0000')

    cash = compute_scheme_cash_running(customer, customer_scheme)
    return (
        cash,
        last.running_gold_balance or Decimal('0.0000'),
        last.running_silver_balance or Decimal('0.0000'),
    )


def _create_ledger_row(
    *,
    customer: Customer,
    customer_scheme: CustomerScheme,
    entry_type: str,
    amount: Decimal,
    reference_type: str,
    reference_id: int,
    entry_date,
    description: str,
    invoice: str = '',
    source: str = '',
) -> CustomerLedger:
    prev_cash, prev_gold, prev_silver = _last_balances(customer, customer_scheme)
    amount = _d(amount)

    # running_balance: + = UDHAR (owed to shop), − = JAMA (advance with shop)
    if entry_type == 'DEBIT':
        new_cash = prev_cash + amount
    else:
        new_cash = prev_cash - amount

    return CustomerLedger.objects.create(
        customer=customer,
        customer_scheme=customer_scheme,
        entry_type=entry_type,
        value_type='CASH',
        amount=amount,
        gold_grams=Decimal('0.0000'),
        silver_grams=Decimal('0.0000'),
        running_balance=new_cash,
        running_gold_balance=prev_gold,
        running_silver_balance=prev_silver,
        reference_type=reference_type,
        reference_id=reference_id,
        invoice=invoice or '',
        source=source or '',
        entry_date=entry_date,
        description=description,
        admin_remark=None,
    )


def _ensure_sale_debit(quote: CatalogueQuote, customer_scheme: CustomerScheme) -> None:
    if CustomerLedger.objects.filter(
        reference_type=REF_SALE,
        reference_id=quote.id,
        entry_type='DEBIT',
    ).exists():
        return

    amount = _d(quote.grand_total)
    if amount <= 0:
        return

    _create_ledger_row(
        customer=quote.customer,
        customer_scheme=customer_scheme,
        entry_type='DEBIT',
        amount=amount,
        reference_type=REF_SALE,
        reference_id=quote.id,
        entry_date=quote.system_created_at or timezone.now(),
        description=f'Catalogue sale — {get_invoice_receipt_number(quote)}',
        invoice=get_invoice_receipt_number(quote),
        source='CATALOGUE',
    )
    logger.info('Customer ledger DEBIT %s for quote %s', amount, quote.quote_number)


def _payment_entry_date(quote: CatalogueQuote):
    sale_dt = quote.system_created_at or timezone.now()
    pay_dt = quote.system_updated_at or sale_dt
    if pay_dt <= sale_dt:
        return sale_dt + timedelta(seconds=1)
    return pay_dt


def _ensure_scheme_settlements(quote: CatalogueQuote, catalogue_scheme: CustomerScheme) -> None:
    """Apply kitty / scheme cash against bill; debit savings scheme, credit store catalogue."""
    settlements = getattr(quote, 'scheme_settlements', None) or []
    if not settlements:
        return

    sale_dt = quote.system_created_at or timezone.now()
    entry_date = sale_dt + timedelta(milliseconds=750)

    for item in settlements:
        cs_id = int(item.get('customer_scheme_id') or 0)
        amount = _d(item.get('amount') or 0)
        if cs_id <= 0 or amount <= 0:
            continue

        ref_id = quote.id * 10000 + cs_id
        if CustomerLedger.objects.filter(
            reference_type=REF_SCHEME_REDEEM,
            reference_id=ref_id,
            customer_scheme_id=cs_id,
        ).exists():
            continue

        try:
            savings_scheme = CustomerScheme.objects.select_related('scheme', 'customer').get(
                pk=cs_id,
                customer_id=quote.customer_id,
            )
        except CustomerScheme.DoesNotExist:
            logger.warning('Scheme settlement skipped: customer_scheme %s not found', cs_id)
            continue

        if _is_catalogue_store_scheme(savings_scheme):
            continue

        create_ledger_entry(
            customer=quote.customer,
            customer_scheme=savings_scheme,
            entry_type='DEBIT',
            value_type='CASH',
            amount=amount,
            reference_type=REF_SCHEME_REDEEM,
            reference_id=ref_id,
            description=f'Kitty redeemed on {quote.quote_number}',
        )

        _create_ledger_row(
            customer=quote.customer,
            customer_scheme=catalogue_scheme,
            entry_type='CREDIT',
            amount=amount,
            reference_type=REF_SCHEME_REDEEM,
            reference_id=ref_id,
            entry_date=entry_date,
            description=f'Kitty redeem — {savings_scheme.scheme.scheme_name} ({get_invoice_receipt_number(quote)})',
            invoice=get_invoice_receipt_number(quote),
            source='SCHEME',
        )

        repair_scheme_running_balances(quote.customer, savings_scheme)
        remaining_after = compute_scheme_cash_running(quote.customer, savings_scheme)
        Redemption.objects.update_or_create(
            customer_scheme=savings_scheme,
            jewellery_order_id=quote.quote_number,
            defaults={
                'scheme_amount_used': amount,
                'remaining_balance': max(Decimal('0'), remaining_after),
            },
        )
        from shared.services.customer_scheme_redeem_service import (
            close_scheme_if_fully_redeemed,
        )

        if close_scheme_if_fully_redeemed(
            quote.customer,
            savings_scheme,
            quote_number=quote.quote_number,
        ):
            logger.info(
                'Customer scheme %s closed after full kitty redeem on %s',
                savings_scheme.id,
                quote.quote_number,
            )


def _ensure_jama_settlement(quote: CatalogueQuote, customer_scheme: CustomerScheme) -> None:
    settle = _d(getattr(quote, 'settle_from_jama', 0) or 0)
    if settle <= 0:
        return
    if CustomerLedger.objects.filter(
        reference_type=REF_JAMA_SETTLEMENT,
        reference_id=quote.id,
    ).exists():
        return

    sale_dt = quote.system_created_at or timezone.now()
    _create_ledger_row(
        customer=quote.customer,
        customer_scheme=customer_scheme,
        entry_type='CREDIT',
        amount=settle,
        reference_type=REF_JAMA_SETTLEMENT,
        reference_id=quote.id,
        entry_date=sale_dt + timedelta(milliseconds=500),
        description=f'JAMA adjusted on {get_invoice_receipt_number(quote)}',
        invoice=get_invoice_receipt_number(quote),
        source='JAMA',
    )


def _ensure_payment_credits(
    quote: CatalogueQuote,
    customer_scheme: CustomerScheme,
    invoice,
    payment_records: list,
) -> None:
    """Post cash credits linked to Payment rows (receipt_no = tax invoice number)."""
    entry_date = _payment_entry_date(quote)
    receipt_no = invoice.invoice_number

    if payment_records:
        for payment in payment_records:
            if CustomerLedger.objects.filter(
                reference_type='PAYMENT',
                reference_id=payment.id,
            ).exists():
                continue
            amt = _d(payment.amount)
            if amt <= 0:
                continue
            mode_label = ''
            if payment.payment_mode_id:
                try:
                    mode_label = payment.payment_mode.code
                except Exception:
                    mode_label = ''
            _create_ledger_row(
                customer=quote.customer,
                customer_scheme=customer_scheme,
                entry_type='CREDIT',
                amount=amt,
                reference_type='PAYMENT',
                reference_id=payment.id,
                entry_date=entry_date,
                description=f'Payment on {receipt_no}' + (f' ({mode_label})' if mode_label else ''),
                invoice=receipt_no,
                source=(mode_label or 'CATALOGUE').upper(),
            )
        return

    # Legacy rows: catalogue quote payment id as reference (pre-Payment migration)
    for qp in quote.payments.order_by('id'):
        if CustomerLedger.objects.filter(
            reference_type=REF_PAYMENT,
            reference_id=qp.id,
        ).exists():
            continue
        amt = _d(qp.amount)
        if amt <= 0:
            continue
        _create_ledger_row(
            customer=quote.customer,
            customer_scheme=customer_scheme,
            entry_type='CREDIT',
            amount=amt,
            reference_type=REF_PAYMENT,
            reference_id=qp.id,
            entry_date=entry_date,
            description=f'Payment on {receipt_no} ({qp.mode_name or qp.mode_code})',
            invoice=receipt_no,
            source=(qp.mode_code or 'CATALOGUE').upper(),
        )


@transaction.atomic
def sync_catalogue_quote_to_customer_ledger(quote: CatalogueQuote) -> None:
    """
    Create customer_ledger rows when quote is a booking or order.
    Idempotent — safe to call after create or status change.
    """
    quote = (
        CatalogueQuote.objects.select_related('customer')
        .prefetch_related('payments')
        .get(pk=quote.pk)
    )

    if quote.status not in (CatalogueQuote.STATUS_BOOKING, CatalogueQuote.STATUS_ORDER):
        return

    if _d(quote.grand_total) <= 0:
        return

    admin_user = getattr(quote, 'updated_by', None) or getattr(quote, 'created_by', None)
    invoice = ensure_catalogue_sale_invoice(quote, created_by=admin_user)
    quote.refresh_from_db(fields=['sale_invoice_id'])
    payment_records = ensure_catalogue_quote_payments(quote, invoice, created_by=admin_user)

    customer_scheme = get_or_create_catalogue_customer_scheme(quote.customer)
    _ensure_sale_debit(quote, customer_scheme)
    _ensure_scheme_settlements(quote, customer_scheme)
    _ensure_jama_settlement(quote, customer_scheme)
    _ensure_payment_credits(quote, customer_scheme, invoice, payment_records)
    repair_scheme_running_balances(quote.customer, customer_scheme)

    logger.info(
        'Ledger synced for %s: grand=%s paid=%s pending=%s customer=%s',
        quote.quote_number,
        quote.grand_total,
        quote.paid_amount,
        quote.pending_amount,
        quote.customer_id,
    )


@transaction.atomic
def rebuild_catalogue_ledger_for_customer(customer_id: int) -> int:
    """
    Delete catalogue ledger rows for a customer and re-post from booking/order quotes.
    Use after fixing running-balance semantics for existing data.
    Returns number of quotes re-synced.
    """
    customer = Customer.objects.get(pk=customer_id)
    customer_scheme = get_or_create_catalogue_customer_scheme(customer)
    CustomerLedger.objects.filter(
        customer_id=customer_id,
        customer_scheme=customer_scheme,
        reference_type__in=[REF_SALE, REF_PAYMENT, REF_JAMA_SETTLEMENT, REF_SCHEME_REDEEM],
    ).delete()

    quotes = CatalogueQuote.objects.filter(
        customer_id=customer_id,
        status__in=(CatalogueQuote.STATUS_BOOKING, CatalogueQuote.STATUS_ORDER),
    )
    count = 0
    for quote in quotes:
        sync_catalogue_quote_to_customer_ledger(quote)
        count += 1
    repair_scheme_running_balances(customer, customer_scheme)
    return count
