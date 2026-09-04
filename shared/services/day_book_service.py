"""
Day Book — daily cash ledger with manual entries and POS auto-reconciliation.

POS rule: full receipt value in Money In; non-cash payment modes (+ udhar) in Money Out.
Closing = opening + sum(money_in) - sum(money_out).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from shared.models import (
    CustomerLedger,
    DayBookDay,
    DayBookManualEntry,
    DayBookManualPayment,
    Payment,
    PaymentCollection,
    SaleInvoice,
)
from shared.day_book_groups import (
    classify_day_book_entry,
    clear_day_book_group_cache,
    day_book_group_label,
    is_allowed_manual_entry_type,
    normalize_manual_entry_type,
)
from shared.services.customer_store_account_service import REF_STORE_ADVANCE

TWOPLACES = Decimal('0.01')
CASH_MODES = frozenset({'CASH'})

# In-request memo for closing balances while building a day or a short date range.
_closing_memo: dict[date, Decimal] = {}


def _clear_closing_memo() -> None:
    _closing_memo.clear()


def _memo_get(book_date: date) -> Decimal | None:
    return _closing_memo.get(book_date)


def _memo_set(book_date: date, closing: Decimal) -> None:
    _closing_memo[book_date] = closing


def _d(value) -> Decimal:
    if value is None:
        return Decimal('0')
    return Decimal(str(value)).quantize(TWOPLACES)


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES)


def _as_datetime(value) -> datetime | None:
    """Normalize date / datetime / None into a timezone-aware-or-naive datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    return None


def _payment_occurred_at(payment: Payment | None, fallback=None) -> datetime | None:
    if payment is None:
        return _as_datetime(fallback)
    return (
        _as_datetime(getattr(payment, 'paid_at', None))
        or _as_datetime(getattr(payment, 'system_created_at', None))
        or _as_datetime(fallback)
    )


def _line_sort_key(line: 'DayBookLine') -> tuple:
    """Earliest → latest; stable secondary keys."""
    occurred = line.occurred_at or datetime.combine(line.entry_date, time.min)
    # Naive compare: strip tz if mixed (MySQL often stores naive)
    if getattr(occurred, 'tzinfo', None) is not None:
        occurred = occurred.replace(tzinfo=None)
    return (occurred, line.entry_date, line.id)


def _sort_lines_chronological(lines: list['DayBookLine']) -> list['DayBookLine']:
    return sorted(lines, key=_line_sort_key)


def _norm_code(value: str | None) -> str:
    if not value:
        return ''
    return (
        str(value)
        .strip()
        .upper()
        .replace('-', '_')
        .replace('.', '')
        .replace('/', '_')
        .replace(' ', '_')
    )


def _resolve_money_out_group(
    payment_mode: str | None,
    transaction_mode: str | None = None,
) -> str:
    """Money Out groups by mode of payment (CASH, UPI, CARD, …)."""
    pay = _norm_code(payment_mode)
    if pay and pay != 'SPLIT':
        return pay
    mode = _norm_code(transaction_mode)
    if mode and mode != 'SPLIT':
        return mode
    return 'CASH'


def _manual_expense_affects_cash(payment_mode: str | None) -> bool:
    """
    Manual Money Out: only CASH reduces the cash drawer / closing balance.
    Card, UPI, etc. are recorded for reporting but do not leave cash.
    """
    pay = _norm_code(payment_mode)
    return (not pay) or pay in CASH_MODES or pay == 'SPLIT'


@dataclass
class DayBookLine:
    id: str
    source: str
    direction: str
    amount: Decimal
    transaction_mode: str
    narration: str
    reference: str
    entry_date: date
    is_editable: bool
    payment_mode: str | None = None
    source_id: int | None = None
    ledger_group: str | None = None
    occurred_at: datetime | None = None
    payment_collections: list[dict[str, Any]] | None = None
    # When False, Money Out is shown but excluded from cash closing.
    affects_cash: bool = True

    def to_dict(self) -> dict[str, Any]:
        # Money In → Type / Day Book group. Money Out → mode of payment.
        if self.direction == DayBookManualEntry.DIRECTION_OUT:
            group = _resolve_money_out_group(self.payment_mode, self.transaction_mode)
        else:
            group = self.ledger_group or classify_day_book_entry(
                source=self.source,
                transaction_mode=self.transaction_mode,
                payment_mode=self.payment_mode,
                narration=self.narration,
            )
        occurred = self.occurred_at or datetime.combine(self.entry_date, time.min)
        occurred_iso = occurred.isoformat()
        payload: dict[str, Any] = {
            'id': self.id,
            'source': self.source,
            'direction': self.direction,
            'amount': str(self.amount),
            'transaction_mode': self.transaction_mode,
            'narration': self.narration,
            'reference': self.reference,
            'entry_date': self.entry_date.isoformat(),
            'occurred_at': occurred_iso,
            'is_editable': self.is_editable,
            'payment_mode': self.payment_mode,
            'source_id': self.source_id,
            'ledger_group': group,
            'affects_cash': self.affects_cash,
        }
        if self.payment_collections is not None:
            payload['payment_collections'] = self.payment_collections
        return payload


def _cash_impact_out_total(lines: list['DayBookLine']) -> Decimal:
    """Money Out amounts that reduce cash closing (excludes non-cash manual expenses)."""
    return _round_money(sum(
        (
            ln.amount
            for ln in lines
            if ln.direction == DayBookManualEntry.DIRECTION_OUT and ln.affects_cash
        ),
        Decimal('0'),
    ))


def _normalize_payment_collections(
    *,
    amount: Decimal,
    payment_mode: str | None = None,
    payment_collections: list | None = None,
) -> list[dict[str, Any]]:
    """
    Normalize split payment rows. Sum must equal entry amount.
    Accepts legacy single payment_mode when collections omitted.
    """
    cols: list[dict[str, Any]] = []
    if payment_collections:
        for raw in payment_collections:
            if not isinstance(raw, dict):
                continue
            mode = str(raw.get('payment_mode') or raw.get('mode') or '').strip().upper()
            amt = _d(raw.get('amount'))
            if not mode or amt <= 0:
                continue
            cols.append({'payment_mode': mode, 'amount': amt})
    if not cols:
        pay = (payment_mode or 'CASH').strip().upper() or 'CASH'
        cols = [{'payment_mode': pay, 'amount': amount}]

    total = sum((c['amount'] for c in cols), Decimal('0')).quantize(TWOPLACES)
    if total != amount.quantize(TWOPLACES):
        raise ValueError(
            f'Payment modes must sum to entry amount (₹{amount}). Got ₹{total}.'
        )
    return cols


def _primary_payment_mode(cols: list[dict[str, Any]]) -> str:
    if len(cols) == 1:
        return cols[0]['payment_mode']
    return 'SPLIT'


def _replace_manual_payments(
    entry: DayBookManualEntry,
    cols: list[dict[str, Any]],
    *,
    user=None,
) -> None:
    entry.payments.all().delete()
    DayBookManualPayment.objects.bulk_create([
        DayBookManualPayment(
            entry=entry,
            payment_mode=c['payment_mode'],
            amount=c['amount'],
            sort_order=idx,
            created_by=user,
            updated_by=user,
        )
        for idx, c in enumerate(cols)
    ])


def _entry_payment_cols(entry: DayBookManualEntry) -> list[dict[str, Any]]:
    rows = list(entry.payments.all())
    if rows:
        return [
            {'payment_mode': (r.payment_mode or 'CASH').upper(), 'amount': _d(r.amount)}
            for r in rows
        ]
    # Legacy rows before split table / backfill
    return [{
        'payment_mode': (entry.payment_mode or 'CASH').upper() or 'CASH',
        'amount': _d(entry.amount),
    }]



def _mode_label(mode) -> str:
    if mode is None:
        return ''
    return (getattr(mode, 'label', None) or getattr(mode, 'code', None) or '').strip()


def _mode_code(mode) -> str:
    if mode is None:
        return ''
    return (getattr(mode, 'code', None) or '').strip().upper()


def _earliest_activity_date(on_or_before: date | None = None) -> date | None:
    """First date with any day-book activity (never defaults to year 2000)."""
    candidates: list[date] = []

    inv_qs = SaleInvoice.objects.filter(is_deleted=False)
    if on_or_before:
        inv_qs = inv_qs.filter(invoice_date__lte=on_or_before)
    inv_min = inv_qs.aggregate(m=Min('invoice_date'))['m']
    if inv_min:
        candidates.append(inv_min)

    manual_qs = DayBookManualEntry.objects.filter(is_deleted=False)
    if on_or_before:
        manual_qs = manual_qs.filter(entry_date__lte=on_or_before)
    manual_min = manual_qs.aggregate(m=Min('entry_date'))['m']
    if manual_min:
        candidates.append(manual_min)

    day_qs = DayBookDay.objects.all()
    if on_or_before:
        day_qs = day_qs.filter(book_date__lte=on_or_before)
    day_min = day_qs.aggregate(m=Min('book_date'))['m']
    if day_min:
        candidates.append(day_min)

    adv_qs = CustomerLedger.objects.filter(reference_type=REF_STORE_ADVANCE, entry_type='CREDIT')
    if on_or_before:
        adv_qs = adv_qs.filter(entry_date__date__lte=on_or_before)
    adv = adv_qs.order_by('entry_date').values_list('entry_date', flat=True).first()
    if adv:
        candidates.append(adv.date() if hasattr(adv, 'date') else adv)

    pay_qs = Payment.objects.filter(instalment__isnull=False, payment_status__code='SUCCESS')
    if on_or_before:
        pay_qs = pay_qs.filter(paid_at__date__lte=on_or_before)
    pay = pay_qs.order_by('paid_at').values_list('paid_at', flat=True).first()
    if pay:
        candidates.append(timezone.localtime(pay).date())

    return min(candidates) if candidates else None


def _day_entry_totals(book_date: date) -> tuple[Decimal, Decimal]:
    """
    Sum money-in and cash-impacting money-out for closing walk.
    Non-cash manual Money Out expenses are excluded from the out total.
    """
    manual = _manual_lines(book_date)
    reconciled = _reconciled_lines(book_date)
    all_lines = reconciled + manual
    total_in = _round_money(sum(
        (ln.amount for ln in all_lines if ln.direction == DayBookManualEntry.DIRECTION_IN),
        Decimal('0'),
    ))
    total_out = _cash_impact_out_total(all_lines)
    return total_in, total_out


def _persist_closing(book_date: date, closing: Decimal) -> None:
    _memo_set(book_date, closing)
    DayBookDay.objects.update_or_create(
        book_date=book_date,
        defaults={'closing_balance': closing},
    )


def _compute_closing_for_date(book_date: date) -> Decimal:
    """
    Closing balance at end of book_date.
    Uses in-request memo + persisted day_book_days.closing_balance to avoid
    walking thousands of days from a fixed epoch.
    """
    if book_date.year < 2000:
        return Decimal('0.00')

    memo = _memo_get(book_date)
    if memo is not None:
        return memo

    cached = (
        DayBookDay.objects.filter(book_date=book_date, closing_balance__isnull=False)
        .values_list('closing_balance', flat=True)
        .first()
    )
    if cached is not None:
        closing = _d(cached)
        _memo_set(book_date, closing)
        return closing

    last_cached_row = (
        DayBookDay.objects.filter(book_date__lt=book_date, closing_balance__isnull=False)
        .order_by('-book_date')
        .first()
    )

    manual_anchor = (
        DayBookDay.objects.filter(
            book_date__lte=book_date,
            opening_balance__isnull=False,
            is_opening_manual=True,
        )
        .order_by('-book_date')
        .first()
    )

    if last_cached_row:
        current = last_cached_row.book_date + timedelta(days=1)
        opening = _d(last_cached_row.closing_balance)
    elif manual_anchor:
        current = manual_anchor.book_date
        opening = _d(manual_anchor.opening_balance)
    else:
        earliest = _earliest_activity_date(on_or_before=book_date)
        if earliest is None:
            return Decimal('0.00')
        current = earliest
        opening = Decimal('0.00')

    if current > book_date:
        return opening

    while current <= book_date:
        day_row = DayBookDay.objects.filter(book_date=current).only(
            'opening_balance', 'is_opening_manual', 'closing_balance',
        ).first()
        if day_row and day_row.closing_balance is not None and current != book_date:
            opening = _d(day_row.closing_balance)
            _memo_set(current, opening)
            if current == book_date:
                return opening
            current += timedelta(days=1)
            continue

        if (
            day_row
            and day_row.opening_balance is not None
            and day_row.is_opening_manual
            and (not last_cached_row or current > last_cached_row.book_date)
        ):
            opening = _d(day_row.opening_balance)

        total_in, total_out = _day_entry_totals(current)
        closing = _round_money(opening + total_in - total_out)
        _memo_set(current, closing)

        if current == book_date:
            return closing

        DayBookDay.objects.update_or_create(
            book_date=current,
            defaults={'closing_balance': closing},
        )
        opening = closing
        current += timedelta(days=1)

    return Decimal('0.00')


def get_previous_day_closing(book_date: date) -> Decimal:
    prev = book_date - timedelta(days=1)
    if prev.year < 2000:
        return Decimal('0.00')
    return _compute_closing_for_date(prev)


def get_opening_balance(book_date: date) -> tuple[Decimal, bool]:
    """
    Returns (opening_balance, is_manual).
    Uses manual DayBookDay row when set; otherwise previous day closing.
    """
    day_row = DayBookDay.objects.filter(book_date=book_date).first()
    if day_row and day_row.opening_balance is not None:
        return _d(day_row.opening_balance), bool(day_row.is_opening_manual)

    prev_closing = get_previous_day_closing(book_date)
    return prev_closing, False


@transaction.atomic
def set_opening_balance(book_date: date, amount, *, user=None) -> DayBookDay:
    amt = _d(amount)
    if amt < 0:
        raise ValueError('Opening balance cannot be negative.')
    day_row, _ = DayBookDay.objects.get_or_create(book_date=book_date)
    day_row.opening_balance = amt
    day_row.is_opening_manual = True
    if user:
        day_row.updated_by = user
        if not day_row.created_by_id:
            day_row.created_by = user
    day_row.save()
    return day_row


def _pos_invoice_lines(
    invoice: SaleInvoice,
    *,
    payments: list[Payment] | None = None,
) -> list[DayBookLine]:
    lines: list[DayBookLine] = []
    book_date = invoice.invoice_date
    ref = invoice.invoice_number
    customer = invoice.bill_to_name or ''
    total = _d(invoice.total_amount)
    inv_at = (
        _as_datetime(getattr(invoice, 'system_created_at', None))
        or _as_datetime(book_date)
    )

    if total > 0:
        lines.append(DayBookLine(
            id=f'pos-in-{invoice.id}',
            source='POS_INVOICE',
            direction=DayBookManualEntry.DIRECTION_IN,
            amount=total,
            transaction_mode='INVOICE',
            narration=f'Invoice — {customer}'.strip(' —'),
            reference=ref,
            entry_date=book_date,
            is_editable=False,
            payment_mode=None,
            source_id=invoice.id,
            occurred_at=inv_at,
        ))

    if payments is None:
        payments = list(
            Payment.objects.filter(reference_type='SALE_INVOICE', reference_id=invoice.id)
            .prefetch_related('collections__payment_mode')
        )
    for payment in payments:
        lines.extend(_payment_collection_out_lines(
            prefix='pos',
            source='POS_INVOICE',
            book_date=book_date,
            reference=ref,
            payment=payment,
            source_id=invoice.id,
            occurred_at=_payment_occurred_at(payment, inv_at),
        ))

    pending = _d(invoice.pending_amount)
    if pending > 0:
        lines.append(DayBookLine(
            id=f'pos-udhar-{invoice.id}',
            source='POS_UDHAR',
            direction=DayBookManualEntry.DIRECTION_OUT,
            amount=pending,
            transaction_mode='UDHAR',
            narration=f'Udhar / credit — {customer}'.strip(' —'),
            reference=ref,
            entry_date=book_date,
            is_editable=False,
            payment_mode='UDHAR',
            source_id=invoice.id,
            occurred_at=inv_at,
        ))

    return lines


def _catalogue_invoice_lines(invoice: SaleInvoice) -> list[DayBookLine]:
    """Catalogue-linked invoices use CATALOGUE_QUOTE payments when SALE_INVOICE link absent."""
    if Payment.objects.filter(reference_type='SALE_INVOICE', reference_id=invoice.id).exists():
        return _pos_invoice_lines(invoice)

    lines: list[DayBookLine] = []
    book_date = invoice.invoice_date
    ref = invoice.invoice_number
    customer = invoice.bill_to_name or ''
    total = _d(invoice.total_amount)
    inv_at = (
        _as_datetime(getattr(invoice, 'system_created_at', None))
        or _as_datetime(book_date)
    )

    if total > 0:
        lines.append(DayBookLine(
            id=f'cat-in-{invoice.id}',
            source='POS_INVOICE',
            direction=DayBookManualEntry.DIRECTION_IN,
            amount=total,
            transaction_mode='INVOICE',
            narration=f'Catalogue invoice — {customer}'.strip(' —'),
            reference=ref,
            entry_date=book_date,
            is_editable=False,
            source_id=invoice.id,
            occurred_at=inv_at,
        ))

    cat_payments = list(
        Payment.objects.filter(
            reference_type='CATALOGUE_QUOTE',
            receipt_no=ref,
        ).prefetch_related('collections__payment_mode')
    )
    for payment in cat_payments:
        pay_at = _payment_occurred_at(payment, inv_at)
        for idx, col in enumerate(payment.collections.all()):
            mode_code = _mode_code(col.payment_mode)
            mode_lbl = _mode_label(col.payment_mode) or mode_code
            amt = _d(col.amount)
            if amt <= 0 or mode_code in CASH_MODES:
                continue
            lines.append(DayBookLine(
                id=f'cat-out-{invoice.id}-{payment.id}-{idx}',
                source='POS_INVOICE',
                direction=DayBookManualEntry.DIRECTION_OUT,
                amount=amt,
                transaction_mode=mode_lbl or 'PAYMENT',
                narration=f'{mode_lbl} — {ref}',
                reference=ref,
                entry_date=book_date,
                is_editable=False,
                payment_mode=mode_code,
                source_id=invoice.id,
                occurred_at=pay_at,
            ))

    pending = _d(invoice.pending_amount)
    if pending > 0:
        lines.append(DayBookLine(
            id=f'cat-udhar-{invoice.id}',
            source='POS_UDHAR',
            direction=DayBookManualEntry.DIRECTION_OUT,
            amount=pending,
            transaction_mode='UDHAR',
            narration=f'Udhar / credit — {customer}'.strip(' —'),
            reference=ref,
            entry_date=book_date,
            is_editable=False,
            payment_mode='UDHAR',
            source_id=invoice.id,
            occurred_at=inv_at,
        ))

    return lines


def _advance_lines(book_date: date) -> list[DayBookLine]:
    lines: list[DayBookLine] = []
    qs = CustomerLedger.objects.filter(
        reference_type=REF_STORE_ADVANCE,
        entry_type='CREDIT',
        entry_date__date=book_date,
    ).select_related('customer').order_by('entry_date', 'id')

    for entry in qs:
        amt = _d(entry.amount)
        if amt <= 0:
            continue
        mode_code = (entry.source or 'CASH').upper()
        mode_lbl = mode_code.replace('_', ' ').title()
        ref = entry.invoice or f'ADV-{entry.id}'
        customer = entry.customer.full_name if entry.customer_id else ''
        at = _as_datetime(entry.entry_date) or _as_datetime(book_date)

        lines.append(DayBookLine(
            id=f'adv-in-{entry.id}',
            source='POS_ADVANCE',
            direction=DayBookManualEntry.DIRECTION_IN,
            amount=amt,
            transaction_mode='ADVANCE',
            narration=entry.description or f'Advance — {customer}',
            reference=ref,
            entry_date=book_date,
            is_editable=False,
            payment_mode=mode_code,
            source_id=entry.id,
            occurred_at=at,
        ))

        if mode_code not in CASH_MODES:
            lines.append(DayBookLine(
                id=f'adv-out-{entry.id}',
                source='POS_ADVANCE',
                direction=DayBookManualEntry.DIRECTION_OUT,
                amount=amt,
                transaction_mode=mode_lbl,
                narration=f'{mode_lbl} — Advance {customer}'.strip(),
                reference=ref,
                entry_date=book_date,
                is_editable=False,
                payment_mode=mode_code,
                source_id=entry.id,
                occurred_at=at,
            ))

    return lines


def _payment_collection_out_lines(
    *,
    prefix: str,
    source: str,
    book_date: date,
    reference: str,
    payment: Payment,
    source_id: int,
    narration_prefix: str = '',
    occurred_at: datetime | None = None,
) -> list[DayBookLine]:
    """Money-out rows for each non-cash payment mode in a payment."""
    lines: list[DayBookLine] = []
    at = occurred_at or _payment_occurred_at(payment, book_date)
    collections = list(payment.collections.all())
    if not collections and payment.payment_mode_id:
        collections = [
            type('Col', (), {
                'payment_mode': payment.payment_mode,
                'amount': payment.amount,
            })()
        ]
    for idx, col in enumerate(collections):
        mode_code = _mode_code(col.payment_mode)
        mode_lbl = _mode_label(col.payment_mode) or mode_code
        amt = _d(col.amount)
        if amt <= 0 or mode_code in CASH_MODES:
            continue
        lines.append(DayBookLine(
            id=f'{prefix}-out-{payment.id}-{idx}',
            source=source,
            direction=DayBookManualEntry.DIRECTION_OUT,
            amount=amt,
            transaction_mode=mode_lbl or 'PAYMENT',
            narration=f'{narration_prefix}{mode_lbl} — {reference}'.strip(' —'),
            reference=reference,
            entry_date=book_date,
            is_editable=False,
            payment_mode=mode_code,
            source_id=source_id,
            occurred_at=at,
        ))
    return lines


def _scheme_payment_lines(book_date: date) -> list[DayBookLine]:
    """
    Scheme instalment receipts at counter (Accounts → Scheme Payments).
    Same rule as POS: full amount Money In; UPI/Card/etc. Money Out.
    """
    lines: list[DayBookLine] = []
    payments = list(
        Payment.objects.filter(
            instalment__isnull=False,
            paid_at__date=book_date,
            payment_status__code='SUCCESS',
        )
        .select_related(
            'instalment__customer_scheme__scheme',
            'instalment__customer_scheme__customer',
        )
        .prefetch_related('collections__payment_mode')
        .order_by('paid_at', 'id')
    )

    for payment in payments:
        amt = _d(payment.amount)
        if amt <= 0:
            continue

        instalment = payment.instalment
        customer = ''
        scheme_name = ''
        instalment_no = None
        if instalment:
            if instalment.customer_scheme_id:
                cs = instalment.customer_scheme
                scheme_name = cs.scheme.scheme_name if cs.scheme_id else ''
                customer = cs.customer.full_name if cs.customer_id else ''
            instalment_no = getattr(instalment, 'instalment_no', None)

        ref = payment.receipt_no or f'RCPT-{payment.id}'
        narr_parts = [p for p in [scheme_name, customer, f'Inst. {instalment_no}' if instalment_no else ''] if p]
        narration = ' — '.join(narr_parts) if narr_parts else 'Scheme payment'
        at = _payment_occurred_at(payment, book_date)

        lines.append(DayBookLine(
            id=f'scheme-in-{payment.id}',
            source='SCHEME_PAYMENT',
            direction=DayBookManualEntry.DIRECTION_IN,
            amount=amt,
            transaction_mode='SCHEME PAYMENT',
            narration=narration,
            reference=ref,
            entry_date=book_date,
            is_editable=False,
            source_id=payment.id,
            occurred_at=at,
        ))

        lines.extend(_payment_collection_out_lines(
            prefix='scheme',
            source='SCHEME_PAYMENT',
            book_date=book_date,
            reference=ref,
            payment=payment,
            source_id=payment.id,
            occurred_at=at,
        ))

    return lines


def _manual_lines(book_date: date) -> list[DayBookLine]:
    lines: list[DayBookLine] = []
    qs = (
        DayBookManualEntry.objects.filter(entry_date=book_date, is_deleted=False)
        .prefetch_related('payments')
        .order_by('system_created_at', 'id')
    )
    for entry in qs:
        amt = _d(entry.amount)
        if amt <= 0:
            continue
        entry_type = normalize_manual_entry_type(entry.transaction_mode)
        type_lbl = day_book_group_label(entry_type)
        narr = (entry.narration or '').strip() or type_lbl
        at = (
            _as_datetime(getattr(entry, 'system_created_at', None))
            or _as_datetime(book_date)
        )
        cols = _entry_payment_cols(entry)
        collections_payload = [
            {'payment_mode': c['payment_mode'], 'amount': str(c['amount'])}
            for c in cols
        ]
        mode_summary = ' + '.join(
            f"{c['payment_mode'].replace('_', ' ').title()} ₹{c['amount']}"
            for c in cols
        )

        # Money Out → one line per payment mode (grouped by mode of payment)
        if entry.direction == DayBookManualEntry.DIRECTION_OUT:
            for idx, col in enumerate(cols):
                pay_code = col['payment_mode']
                pay_amt = col['amount']
                if pay_amt <= 0:
                    continue
                pay_lbl = pay_code.replace('_', ' ').title()
                lines.append(DayBookLine(
                    id=f'manual-{entry.id}' if idx == 0 else f'manual-{entry.id}-{idx}',
                    source='MANUAL',
                    direction=DayBookManualEntry.DIRECTION_OUT,
                    amount=pay_amt,
                    transaction_mode=type_lbl,
                    narration=narr if len(cols) == 1 else f'{pay_lbl} — {narr}',
                    reference=mode_summary if len(cols) > 1 and idx == 0 else '',
                    entry_date=entry.entry_date,
                    is_editable=(idx == 0),
                    payment_mode=pay_code,
                    source_id=entry.id,
                    ledger_group=pay_code,
                    occurred_at=at,
                    payment_collections=collections_payload if idx == 0 else None,
                    # Only cash leaving the drawer reduces closing balance
                    affects_cash=_manual_expense_affects_cash(pay_code),
                ))
            continue

        # Money In → group by Type; non-cash splits appear under Money Out by payment mode
        primary_mode = _primary_payment_mode(cols)
        lines.append(DayBookLine(
            id=f'manual-{entry.id}',
            source='MANUAL',
            direction=DayBookManualEntry.DIRECTION_IN,
            amount=amt,
            transaction_mode=type_lbl,
            narration=narr,
            reference=mode_summary if len(cols) > 1 else (
                cols[0]['payment_mode'].replace('_', ' ').title()
                if cols[0]['payment_mode'] != 'CASH' else ''
            ),
            entry_date=entry.entry_date,
            is_editable=True,
            payment_mode=primary_mode,
            source_id=entry.id,
            ledger_group=entry_type,
            occurred_at=at,
            payment_collections=collections_payload,
        ))

        for idx, col in enumerate(cols):
            pay_code = col['payment_mode']
            if pay_code in CASH_MODES:
                continue
            pay_amt = col['amount']
            if pay_amt <= 0:
                continue
            pay_lbl = pay_code.replace('_', ' ').title()
            lines.append(DayBookLine(
                id=f'manual-out-{entry.id}-{idx}',
                source='MANUAL',
                direction=DayBookManualEntry.DIRECTION_OUT,
                amount=pay_amt,
                transaction_mode=pay_lbl,
                narration=f'{pay_lbl} — {narr}',
                reference='',
                entry_date=entry.entry_date,
                is_editable=False,
                payment_mode=pay_code,
                source_id=entry.id,
                ledger_group=pay_code,
                occurred_at=at,
            ))

    return lines


def _reconciled_lines(book_date: date) -> list[DayBookLine]:
    lines: list[DayBookLine] = []
    invoices = list(
        SaleInvoice.objects.filter(
            invoice_date=book_date,
            is_deleted=False,
        ).order_by('system_created_at', 'id')
    )

    invoice_ids = [inv.id for inv in invoices]
    payments_by_invoice: dict[int, list[Payment]] = {}
    if invoice_ids:
        for payment in (
            Payment.objects.filter(
                reference_type='SALE_INVOICE',
                reference_id__in=invoice_ids,
            )
            .prefetch_related('collections__payment_mode')
            .order_by('paid_at', 'id')
        ):
            payments_by_invoice.setdefault(payment.reference_id, []).append(payment)

    for invoice in invoices:
        invoice_payments = payments_by_invoice.get(invoice.id, [])
        if invoice_payments:
            lines.extend(_pos_invoice_lines(invoice, payments=invoice_payments))
        else:
            lines.extend(_catalogue_invoice_lines(invoice))

    lines.extend(_advance_lines(book_date))
    lines.extend(_scheme_payment_lines(book_date))
    return lines


def build_day_book(book_date: date) -> dict[str, Any]:
    _clear_closing_memo()
    # Always reload DAY_BOOK_GROUP lookup so newly added groupings apply immediately
    clear_day_book_group_cache()

    prev_closing = get_previous_day_closing(book_date)
    opening, is_opening_manual = get_opening_balance(book_date)
    manual = _manual_lines(book_date)
    reconciled = _reconciled_lines(book_date)
    all_lines = _sort_lines_chronological(reconciled + manual)

    money_in = [ln for ln in all_lines if ln.direction == DayBookManualEntry.DIRECTION_IN]
    money_out = [ln for ln in all_lines if ln.direction == DayBookManualEntry.DIRECTION_OUT]

    total_in = _round_money(sum((ln.amount for ln in money_in), Decimal('0')))
    # Display total includes all money-out rows; closing uses cash-impacting only
    total_out = _round_money(sum((ln.amount for ln in money_out), Decimal('0')))
    cash_out = _cash_impact_out_total(all_lines)
    closing = _round_money(opening + total_in - cash_out)

    _persist_closing(book_date, closing)

    mismatch = False
    mismatch_message = ''
    if is_opening_manual and _d(opening) != prev_closing:
        mismatch = True
        mismatch_message = (
            f'Manual opening (₹{opening}) differs from previous day closing (₹{prev_closing}). '
            f'Verify entries or adjust opening balance.'
        )

    return {
        'date': book_date.isoformat(),
        'opening_balance': str(opening),
        'is_opening_manual': is_opening_manual,
        'previous_day_closing': str(prev_closing),
        'money_in': [ln.to_dict() for ln in money_in],
        'money_out': [ln.to_dict() for ln in money_out],
        'transactions': [ln.to_dict() for ln in all_lines],
        'daily_total_in': str(total_in),
        'daily_total_out': str(total_out),
        'closing_balance': str(closing),
        'expected_closing_balance': str(closing),
        'has_mismatch': mismatch,
        'mismatch_message': mismatch_message,
        'count': len(all_lines),
    }


@transaction.atomic
def create_manual_entry(
    *,
    entry_date: date,
    direction: str,
    amount,
    transaction_mode: str,
    narration: str = '',
    payment_mode: str = 'CASH',
    payment_collections: list | None = None,
    user=None,
) -> DayBookManualEntry:
    amt = _d(amount)
    if amt <= 0:
        raise ValueError('Amount must be greater than zero.')
    if direction not in (DayBookManualEntry.DIRECTION_IN, DayBookManualEntry.DIRECTION_OUT):
        raise ValueError('direction must be IN or OUT.')
    entry_type = normalize_manual_entry_type(transaction_mode)
    if not is_allowed_manual_entry_type(entry_type):
        raise ValueError(
            'Invalid entry type. Choose an existing Day Book group or add one from the lookup list.'
        )
    cols = _normalize_payment_collections(
        amount=amt,
        payment_mode=payment_mode,
        payment_collections=payment_collections,
    )
    entry = DayBookManualEntry.objects.create(
        entry_date=entry_date,
        direction=direction,
        amount=amt,
        transaction_mode=entry_type,
        payment_mode=_primary_payment_mode(cols),
        narration=(narration or '').strip(),
        created_by=user,
        updated_by=user,
    )
    _replace_manual_payments(entry, cols, user=user)
    return entry


@transaction.atomic
def update_manual_entry(entry_id: int, *, user=None, **fields) -> DayBookManualEntry:
    entry = DayBookManualEntry.objects.get(pk=entry_id, is_deleted=False)
    if 'amount' in fields:
        amt = _d(fields['amount'])
        if amt <= 0:
            raise ValueError('Amount must be greater than zero.')
        entry.amount = amt
    if 'direction' in fields:
        d = fields['direction']
        if d not in (DayBookManualEntry.DIRECTION_IN, DayBookManualEntry.DIRECTION_OUT):
            raise ValueError('direction must be IN or OUT.')
        entry.direction = d
    if 'transaction_mode' in fields:
        entry_type = normalize_manual_entry_type(fields['transaction_mode'])
        if not is_allowed_manual_entry_type(entry_type):
            raise ValueError('Invalid entry type.')
        entry.transaction_mode = entry_type
    if 'narration' in fields:
        entry.narration = (fields['narration'] or '').strip()
    if 'entry_date' in fields:
        entry.entry_date = fields['entry_date']

    touch_payments = (
        'payment_collections' in fields
        or 'payment_mode' in fields
        or 'amount' in fields
    )
    if touch_payments:
        if 'payment_collections' in fields:
            cols = _normalize_payment_collections(
                amount=_d(entry.amount),
                payment_mode=fields.get('payment_mode', entry.payment_mode),
                payment_collections=fields.get('payment_collections'),
            )
        elif 'payment_mode' in fields:
            cols = _normalize_payment_collections(
                amount=_d(entry.amount),
                payment_mode=fields.get('payment_mode'),
                payment_collections=None,
            )
        else:
            # Amount changed — keep existing splits; must still sum to new amount
            existing = [
                {'payment_mode': c['payment_mode'], 'amount': c['amount']}
                for c in _entry_payment_cols(entry)
            ]
            cols = _normalize_payment_collections(
                amount=_d(entry.amount),
                payment_collections=existing,
            )
        entry.payment_mode = _primary_payment_mode(cols)
        if user:
            entry.updated_by = user
        entry.save()
        _replace_manual_payments(entry, cols, user=user)
    else:
        if user:
            entry.updated_by = user
        entry.save()
    return entry


@transaction.atomic
def delete_manual_entry(entry_id: int, *, user=None) -> None:
    entry = DayBookManualEntry.objects.get(pk=entry_id, is_deleted=False)
    entry.is_deleted = True
    if user:
        entry.updated_by = user
    entry.save(update_fields=['is_deleted', 'updated_by', 'system_updated_at'])


def build_day_book_print_payload(book_date: date) -> dict[str, Any]:
    data = build_day_book(book_date)
    return {
        'date': data['date'],
        'opening_balance': data['opening_balance'],
        'closing_balance': data['closing_balance'],
        'daily_total_in': data['daily_total_in'],
        'daily_total_out': data['daily_total_out'],
        'money_in': data['money_in'],
        'money_out': data['money_out'],
    }


def build_day_book_batch_print(date_from: date, date_to: date) -> list[dict[str, Any]]:
    if date_to < date_from:
        raise ValueError('date_to must be on or after date_from.')
    if (date_to - date_from).days > 31:
        raise ValueError('Batch print supports at most 31 days at a time.')
    _clear_closing_memo()
    pages = []
    current = date_from
    while current <= date_to:
        pages.append(build_day_book_print_payload(current))
        current += timedelta(days=1)
    return pages
