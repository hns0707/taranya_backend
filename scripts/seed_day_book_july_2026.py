#!/usr/bin/env python
"""
Seed July 2026 Day Book mock data for end-to-end QA.

Covers: manual opening, manual entries, POS invoices (cash / split / udhar),
scheme payments (cash / UPI), advance (JAMA) deposits, and Jul 16–20 grouping QA.

Usage (from Backend/ecom_backend):
    python scripts/seed_day_book_july_2026.py
    python scripts/seed_day_book_july_2026.py --clean   # remove only July mock rows

Re-run safe: mock POS rows use bill_to_phone 9000000071; scheme payments use
transaction_id prefix DBMOCK-JUL-.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import date, datetime, time
from decimal import Decimal

# Django setup (same pattern as insert_customer_scheme_data.py)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecom_backend.settings')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from shared.models import (  # noqa: E402
    Customer,
    CustomerLedger,
    DayBookDay,
    DayBookManualEntry,
    LookupValue,
    Payment,
    SaleInvoice,
    SchemeInstalment,
)
from shared.services.customer_store_account_service import REF_STORE_ADVANCE  # noqa: E402
from shared.services.catalogue_quote_ledger_service import (  # noqa: E402
    _create_ledger_row,
    get_or_create_catalogue_customer_scheme,
)
from shared.services.day_book_service import (  # noqa: E402
    create_manual_entry,
    set_opening_balance,
)
from shared.services.payment_service import (  # noqa: E402
    create_payment_with_collections,
    process_successful_payment,
)
from shared.services.pos_service import create_pos_invoice  # noqa: E402

YEAR = 2026
MONTH = 7
MOCK_PHONE = '9000000071'
MOCK_NAME = 'DB-MOCK DayBook Customer'
MOCK_TXN_PREFIX = 'DBMOCK-JUL-'
MOCK_ADDR = 'Day Book Mock Address, Jaipur, Rajasthan, 302001'


def july(day: int) -> date:
    return date(YEAR, MONTH, day)


def july_noon(day: int) -> datetime:
    tz = timezone.get_current_timezone()
    return timezone.make_aware(datetime.combine(july(day), time(12, 0)), tz)


def _pending_instalments(limit: int = 5):
    pending_status = LookupValue.objects.filter(
        lookup__code='INSTALLMENT_STATUS',
        code='PENDING',
    ).first()
    if not pending_status:
        return []
    return list(
        SchemeInstalment.objects.filter(status=pending_status)
        .select_related('customer_scheme__customer', 'customer_scheme__scheme')
        .order_by('id')[:limit]
    )


@transaction.atomic
def clean_july_mock() -> None:
    """Remove July 2026 day-book rows and tagged mock POS / scheme payments."""
    start = july(1)
    end = july(31)

    DayBookManualEntry.objects.filter(
        entry_date__gte=start,
        entry_date__lte=end,
    ).delete()

    DayBookDay.objects.filter(
        book_date__gte=start,
        book_date__lte=end,
    ).delete()

    mock_invoices = SaleInvoice.objects.filter(
        bill_to_phone=MOCK_PHONE,
        invoice_date__gte=start,
        invoice_date__lte=end,
    )
    inv_ids = list(mock_invoices.values_list('id', flat=True))
    if inv_ids:
        Payment.objects.filter(reference_type='SALE_INVOICE', reference_id__in=inv_ids).delete()
    mock_invoices.update(is_deleted=True)

    Payment.objects.filter(transaction_id__startswith=MOCK_TXN_PREFIX).delete()

    CustomerLedger.objects.filter(
        reference_type=REF_STORE_ADVANCE,
        entry_date__date__gte=start,
        entry_date__date__lte=end,
        customer__mobile=MOCK_PHONE,
    ).delete()

    print(f'Cleaned July {YEAR} day-book mock data.')


def _pos_invoice(
    day: int,
    *,
    total: str,
    payments: list[dict],
    label: str,
) -> SaleInvoice | None:
    payload = {
        'bill_to_name': f'{MOCK_NAME} ({label})',
        'bill_to_phone': MOCK_PHONE,
        'bill_to_address': MOCK_ADDR,
        'invoice_date': july(day).isoformat(),
        'items': [
            {
                'product_name': f'DayBook Mock Item — {label}',
                'qty': 1,
                'gross_weight': '10.000',
                'net_weight': '9.500',
                'purity': '22K',
                'making_charge': '0',
                'final_amount': total,
            }
        ],
        'payments': payments,
    }
    try:
        return create_pos_invoice(payload, created_by=None)
    except ValueError as exc:
        if 'future' in str(exc).lower():
            print(
                f'Jul {day}: skipped POS invoice ({exc}). '
                f'Re-run seed on/after {july(day).isoformat()} for auto POS rows.'
            )
            return None
        raise


def _scheme_payment(
    instalment: SchemeInstalment,
    day: int,
    amount: Decimal,
    *,
    mode: str = 'CASH',
    ref: str | None = None,
) -> Payment:
    txn = f'{MOCK_TXN_PREFIX}{day}-{instalment.id}-{uuid.uuid4().hex[:8]}'
    collections = None
    mode_code = mode
    if mode.upper() != 'CASH':
        collections = [{
            'payment_mode_code': mode.upper(),
            'amount': amount,
            'reference_number': ref or f'REF-{day}-{instalment.id}',
        }]
        mode_code = None

    payment = create_payment_with_collections(
        instalment=instalment,
        amount=amount,
        transaction_id=txn,
        payment_status_code='SUCCESS',
        payment_source='POS',
        payment_mode_code=mode_code,
        collections_data=collections,
        paid_at=july_noon(day),
        created_by=None,
    )
    process_successful_payment(payment, payment_date=july(day))
    return payment


def _mock_customer() -> Customer:
    customer, _ = Customer.objects.get_or_create(
        mobile=MOCK_PHONE,
        defaults={'full_name': MOCK_NAME, 'is_active': True},
    )
    return customer


def _mock_jama_advance(day: int, amount: str, *, mode: str = 'CASH', label: str = '') -> None:
    """Backdated STORE_ADVANCE ledger row so day book picks up POS_ADVANCE lines on `day`."""
    customer = _mock_customer()
    scheme = get_or_create_catalogue_customer_scheme(customer)
    ref_id = int(july_noon(day).timestamp()) % 2147483647
    _create_ledger_row(
        customer=customer,
        customer_scheme=scheme,
        entry_type='CREDIT',
        amount=Decimal(amount),
        reference_type=REF_STORE_ADVANCE,
        reference_id=ref_id,
        entry_date=july_noon(day),
        description=label or f'DayBook mock JAMA advance ({mode})',
        invoice=f'DBMOCK-ADV-{day}',
        source=mode.upper(),
    )


def _seed_grouping_showcase() -> None:
    """
    Jul 16–20: exercise every grouped ledger bucket in Money In and Money Out.

    Groups: Advance | Borrowing | Udhar | Lending | Misc. | HUF | HUF I
    """
    day = 16

    # --- Advance (manual + auto JAMA) ---
    create_manual_entry(
        entry_date=july(day), direction='IN', amount='3000.00',
        transaction_mode='ADVANCE', payment_mode='CASH', narration='Customer advance deposit (JAMA)',
    )
    create_manual_entry(
        entry_date=july(day), direction='OUT', amount='1500.00',
        transaction_mode='ADVANCE', payment_mode='UPI', narration='Advance refund via UPI',
    )
    _mock_jama_advance(day, '2000.00', mode='CASH', label='Counter cash advance (JAMA)')
    _mock_jama_advance(day, '1000.00', mode='UPI', label='Counter UPI advance (JAMA)')

    # --- Borrowing ---
    create_manual_entry(
        entry_date=july(day), direction='IN', amount='5000.00',
        transaction_mode='BORROWING', narration='Borrowing from relative for stock',
    )
    create_manual_entry(
        entry_date=july(day), direction='OUT', amount='2000.00',
        transaction_mode='BORROWING', narration='Borrowing partial repayment',
    )

    # --- Udhar (manual in + out; POS udhar on Jul 17 when date is not in the future) ---
    create_manual_entry(
        entry_date=july(day), direction='IN', amount='1000.00',
        transaction_mode='UDHAR', payment_mode='CASH', narration='Udhar recovery from customer',
    )
    create_manual_entry(
        entry_date=july(day), direction='OUT', amount='1200.00',
        transaction_mode='UDHAR', payment_mode='CASH', narration='Udhar settlement paid to supplier',
    )

    # --- Lending ---
    create_manual_entry(
        entry_date=july(day), direction='OUT', amount='4000.00',
        transaction_mode='LENDING', narration='Short-term lending to customer',
    )
    create_manual_entry(
        entry_date=july(day), direction='IN', amount='1000.00',
        transaction_mode='LENDING', narration='Lending repayment received',
    )

    # --- Misc. ---
    create_manual_entry(
        entry_date=july(day), direction='OUT', amount='350.00',
        transaction_mode='MISC', payment_mode='CASH', narration='Office stationery',
    )
    create_manual_entry(
        entry_date=july(day), direction='IN', amount='600.00',
        transaction_mode='MISC', payment_mode='CASH', narration='Repair charges collected',
    )
    _pos_invoice(
        day,
        total='2500.00',
        payments=[{'mode': 'CASH', 'amount': '2500.00'}],
        label='Grouping cash sale',
    )

    # --- HUF / HUF I ---
    create_manual_entry(
        entry_date=july(day), direction='OUT', amount='8000.00',
        transaction_mode='HUF', narration='HUF family account payment',
    )
    create_manual_entry(
        entry_date=july(day), direction='IN', amount='2500.00',
        transaction_mode='HUF', narration='HUF family deposit',
    )
    create_manual_entry(
        entry_date=july(day), direction='OUT', amount='3500.00',
        transaction_mode='HUF_I', narration='HUF I account payment',
    )
    create_manual_entry(
        entry_date=july(day), direction='IN', amount='1800.00',
        transaction_mode='HUF_I', narration='HUF I receipt',
    )
    print(
        'Jul 16: grouping showcase — all 7 groups (Advance, Borrowing, Udhar, Lending, '
        'Misc., HUF, HUF I) in Money In + Money Out'
    )

    # --- Jul 17: auto Udhar OUT from POS ---
    if _pos_invoice(
        17,
        total='10000.00',
        payments=[{'mode': 'CASH', 'amount': '3000.00'}],
        label='Udhar grouping',
    ):
        print('Jul 17: POS invoice Rs.10,000 (Rs.3k cash + Rs.7k udhar)')

    # --- Jul 18: auto Misc. (split invoice + scheme) ---
    if _pos_invoice(
        18,
        total='12000.00',
        payments=[
            {'mode': 'CASH', 'amount': '7000.00'},
            {'mode': 'CARD', 'amount': '5000.00'},
        ],
        label='Misc split',
    ):
        print('Jul 18: split invoice Rs.12,000 (cash + card)')
    pending = _pending_instalments(1)
    if pending:
        amt = min(Decimal(pending[0].amount), Decimal('2500.00'))
        _scheme_payment(pending[0], 18, amt, mode='UPI', ref='UPI-JUL18-GROUP')
        print(f'Jul 18: scheme UPI Rs.{amt} (Misc. auto)')
    else:
        print('Jul 18: scheme skipped (no PENDING instalment)')

    # --- Jul 19–20: lighter follow-up days ---
    create_manual_entry(
        entry_date=july(19), direction='OUT', amount='900.00',
        transaction_mode='BORROWING', narration='Borrowing interest payout',
    )
    create_manual_entry(
        entry_date=july(20), direction='IN', amount='1200.00',
        transaction_mode='HUF', narration='HUF weekend deposit',
    )
    print('Jul 19–20: extra borrowing + HUF lines added')


@transaction.atomic
def seed_july_mock() -> None:
    clean_july_mock()

    # --- Jul 1: manual opening ---
    set_opening_balance(july(1), Decimal('10000.00'), user=None)
    print('Jul 1: opening balance Rs.10,000')

    # --- Jul 2: cash-only sale Rs.5,000 ---
    _pos_invoice(2, total='5000.00', payments=[{'mode': 'CASH', 'amount': '5000.00'}], label='Cash sale')
    print('Jul 2: POS invoice Rs.5,000 (cash)')

    # --- Jul 3: split sale Rs.50,000 — cash + card + UPI ---
    _pos_invoice(
        3,
        total='50000.00',
        payments=[
            {'mode': 'CASH', 'amount': '30000.00'},
            {'mode': 'CARD', 'amount': '15000.00'},
            {'mode': 'UPI', 'amount': '5000.00'},
        ],
        label='Split sale',
    )
    print('Jul 3: POS invoice Rs.50,000 (cash/card/UPI split)')

    # --- Jul 4–5: scheme payments ---
    pending = _pending_instalments(2)
    if len(pending) >= 1:
        amt = min(Decimal(pending[0].amount), Decimal('5000.00'))
        _scheme_payment(pending[0], 4, amt, mode='UPI', ref='UPI-JUL4-MOCK')
        print(f'Jul 4: scheme payment Rs.{amt} (UPI) — {pending[0].customer_scheme.scheme.scheme_name}')
    else:
        print('Jul 4: skipped scheme payment (no PENDING instalment)')

    if len(pending) >= 2:
        amt = min(Decimal(pending[1].amount), Decimal('3000.00'))
        _scheme_payment(pending[1], 5, amt, mode='CASH')
        print(f'Jul 5: scheme payment Rs.{amt} (cash)')
    else:
        print('Jul 5: skipped scheme payment (need 2nd PENDING instalment)')

    # --- Jul 6–11: manual entries ---
    create_manual_entry(
        entry_date=july(6), direction='OUT', amount='500.00',
        transaction_mode='MISC', payment_mode='CASH', narration='Petty cash — tea & snacks',
    )
    create_manual_entry(
        entry_date=july(9), direction='IN', amount='1000.00',
        transaction_mode='BORROWING', narration='Borrowed from partner for float',
    )
    create_manual_entry(
        entry_date=july(10), direction='IN', amount='750.00',
        transaction_mode='MISC', payment_mode='CASH', narration='Repair job advance received',
    )
    create_manual_entry(
        entry_date=july(11), direction='OUT', amount='2000.00',
        transaction_mode='LENDING', narration='Short-term lend to staff',
    )
    print('Jul 6,9,10,11: manual entries added')

    # --- Jul 7: advance-style entries (manual; true JAMA auto-row uses payment date = today) ---
    create_manual_entry(
        entry_date=july(7), direction='IN', amount='2000.00',
        transaction_mode='ADVANCE', payment_mode='CASH', narration='Mock advance received (JAMA)',
    )
    create_manual_entry(
        entry_date=july(7), direction='OUT', amount='2000.00',
        transaction_mode='ADVANCE', payment_mode='UPI', narration='Mock advance via UPI (non-cash)',
    )
    print('Jul 7: manual advance simulation (IN + UPI OUT Rs.2,000)')

    # Optional: live JAMA ledger row (appears on seed run date, not Jul 7)
    customer = _mock_customer()
    try:
        from shared.services.customer_store_account_service import record_store_advance

        record_store_advance(
            customer.id,
            Decimal('500.00'),
            mode_code='CASH',
            remark='DayBook mock cash advance (today)',
        )
        print('Also recorded Rs.500 cash JAMA advance (ledger date = today)')
    except Exception as exc:
        print(f'Cash advance skipped: {exc}')

    # --- Jul 8: partial payment + udhar Rs.12,000 pending ---
    _pos_invoice(
        8,
        total='20000.00',
        payments=[{'mode': 'CASH', 'amount': '8000.00'}],
        label='Udhar partial',
    )
    print('Jul 8: POS invoice Rs.20,000 (Rs.8k cash + Rs.12k udhar)')

    # --- Jul 12: another cash sale ---
    _pos_invoice(12, total='1500.00', payments=[{'mode': 'CASH', 'amount': '1500.00'}], label='Small cash')
    print('Jul 12: POS invoice Rs.1,500 (cash)')

    # --- Jul 15: manual opening override (mismatch test vs Jul 14 closing) ---
    set_opening_balance(july(15), Decimal('50000.00'), user=None)
    print('Jul 15: manual opening Rs.50,000 (expect mismatch banner vs Jul 14 closing)')

    # --- Jul 16–20: grouped view QA (all ledger buckets) ---
    _seed_grouping_showcase()

    print()
    print('=== July 2026 Day Book mock seed complete ===')
    print('Test dates:')
    print('  Jul 1  — manual opening Rs.10,000')
    print('  Jul 2  — cash invoice')
    print('  Jul 3  — split invoice (cash + card + UPI)')
    print('  Jul 4  — scheme UPI (money in + out)')
    print('  Jul 5  — scheme cash')
    print('  Jul 6  — manual expense')
    print('  Jul 8  — udhar / partial')
    print('  Jul 9–11 — manual in/out')
    print('  Jul 12 — small cash sale')
    print('  Jul 15 — opening mismatch alert')
    print('  Jul 16 — **grouped view**: all 7 groups (best date for Grouped toggle)')
    print('  Jul 17 — POS udhar (Udhar group, Money Out)')
    print('  Jul 18 — split invoice + scheme (Misc. auto)')
    print('  Jul 19–20 — extra borrowing / HUF')
    print()
    print(f'API: GET /master/accounts/daily-book/?date={YEAR}-07-16  (grouped view QA)')


def main():
    parser = argparse.ArgumentParser(description='Seed July 2026 Day Book mock data')
    parser.add_argument('--clean', action='store_true', help='Only remove July mock data')
    args = parser.parse_args()
    if args.clean:
        clean_july_mock()
    else:
        seed_july_mock()


if __name__ == '__main__':
    main()
