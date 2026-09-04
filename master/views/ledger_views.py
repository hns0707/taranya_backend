"""
Views for ledger in the master app (CustomerLedger).
Supports CASH + GOLD + SILVER + UDHAR SUMMARY
"""

from decimal import Decimal
from datetime import datetime
from rest_framework import generics
from rest_framework.response import Response
from shared.services.ledger_service import get_ledger_entries, get_opening_balance_before_date

CATALOGUE_LEDGER_REFS = frozenset({
    'CATALOGUE_QUOTE',
    'CATALOGUE_QUOTE_PAYMENT',
    'STORE_JAMA_SETTLEMENT',
    'STORE_ADVANCE',
})
STORE_SCHEME_CODE = 'STORE_CATALOGUE'
from shared.models import Payment, CustomerLedger, SaleInvoice, CatalogueQuotePayment
from shared.helper import get_payment_mode_display
from master.permissions.permission_checker import admin_auth
from django.utils.decorators import method_decorator
from django.utils.dateparse import parse_date


# -------------------------------------------------------
# HELPER FUNCTIONS
# -------------------------------------------------------

def _parse_date_flexible(date_string):
    """
    Parse date string in multiple formats:
    - DD/MM/YYYY (e.g., 26/03/2026)
    - YYYY-MM-DD (e.g., 2026-03-26)
    Returns a date object or None if parsing fails.
    """
    if not date_string:
        return None
    
    # Try DD/MM/YYYY format first
    try:
        return datetime.strptime(date_string, '%d/%m/%Y').date()
    except ValueError:
        pass
    
    # Try YYYY-MM-DD format (ISO format)
    try:
        return datetime.strptime(date_string, '%Y-%m-%d').date()
    except ValueError:
        pass
    
    # Try Django's parse_date as fallback
    return parse_date(date_string)


def _is_store_ledger_entry(entry) -> bool:
    if (entry.reference_type or '') in CATALOGUE_LEDGER_REFS:
        return True
    try:
        return entry.customer_scheme.scheme.scheme_code == STORE_SCHEME_CODE
    except Exception:
        return False


def _cash_balance_meta(amount: Decimal) -> dict:
    """
    Store / catalogue running cash: positive = UDHAR (customer owes), negative = JAMA (advance).
    """
    amount = Decimal(str(amount or 0)).quantize(Decimal('0.01'))
    if amount > 0:
        return {
            'position': 'UDHAR',
            'label': 'Outstanding (UDHAR)',
            'signed_balance': str((-amount).quantize(Decimal('0.01'))),
            'display_amount': str(amount),
        }
    if amount < 0:
        adv = abs(amount)
        return {
            'position': 'JAMA',
            'label': 'Advance (JAMA)',
            'signed_balance': str(adv.quantize(Decimal('0.01'))),
            'display_amount': str(adv),
        }
    return {
        'position': 'CLEAR',
        'label': 'Clear',
        'signed_balance': '0.00',
        'display_amount': '0.00',
    }


def _scheme_cash_balance_meta(amount: Decimal) -> dict:
    """
    Scheme instalment running cash: positive = prepaid / JAMA, negative = shortfall (rare).
    """
    amount = Decimal(str(amount or 0)).quantize(Decimal('0.01'))
    if amount > 0:
        return {
            'position': 'JAMA',
            'label': 'Scheme balance (JAMA)',
            'signed_balance': str(amount),
            'display_amount': str(amount),
        }
    if amount < 0:
        adv = abs(amount)
        return {
            'position': 'UDHAR',
            'label': 'Scheme shortfall (UDHAR)',
            'signed_balance': str((-adv).quantize(Decimal('0.01'))),
            'display_amount': str(adv),
        }
    return {
        'position': 'CLEAR',
        'label': 'Clear',
        'signed_balance': '0.00',
        'display_amount': '0.00',
    }


def _signed_cash_display(running_cash, is_store: bool = True) -> str:
    if is_store:
        return _cash_balance_meta(running_cash)['signed_balance']
    return _scheme_cash_balance_meta(running_cash)['signed_balance']


def _opening_row_entry(date_str: str, block: dict, title: str) -> dict:
    """Table row for opening balance (prepended to entries)."""
    return {
        "date": date_str,
        "type": "OPENING",
        "narration": title,
        "credit_gold": "",
        "credit_silver": "",
        "credit_amount": "",
        "debit_gold": "",
        "debit_silver": "",
        "debit_amount": "",
        "balance_gold": block.get("gold", "0.0000"),
        "balance_silver": block.get("silver", "0.0000"),
        "balance_amount": block.get("cash", "0.00"),
        "balance_position": block.get("position", "CLEAR"),
        "ledger_bucket": block.get("ledger_bucket", ""),
        "payment_receipt": "",
        "invoice": "",
        "source": "",
        "remark": "",
    }


def _closing_row_entry(date_str: str, block: dict, title: str) -> dict:
    """Table row for closing balance (appended to entries)."""
    return {
        "date": date_str,
        "type": "CLOSING",
        "narration": title,
        "credit_gold": "",
        "credit_silver": "",
        "credit_amount": "",
        "debit_gold": "",
        "debit_silver": "",
        "debit_amount": "",
        "balance_gold": block.get("gold", "0.0000"),
        "balance_silver": block.get("silver", "0.0000"),
        "balance_amount": block.get("cash", "0.00"),
        "balance_position": block.get("position", "CLEAR"),
        "ledger_bucket": block.get("ledger_bucket", ""),
        "payment_receipt": "",
        "invoice": "",
        "source": "",
        "remark": "",
    }


def _balance_block(date_str: str, cash, gold, silver, is_store: bool = True) -> dict:
    cash_d = Decimal(str(cash or 0))
    meta = _cash_balance_meta(cash_d) if is_store else _scheme_cash_balance_meta(cash_d)
    return {
        'date': date_str,
        'cash': meta['signed_balance'],
        'gold': str(gold),
        'silver': str(silver),
        'ledger_bucket': 'store' if is_store else 'scheme',
        **meta,
    }


def _transaction_type_from_entry(entry):
    if entry.reference_type == 'PAYMENT':
        return 'JAMA'
    return 'JAMA' if entry.entry_type == 'CREDIT' else 'NAAM'


def _payment_map_for_ledger_entries(entries):
    """Build a map of payment IDs to Payment objects for ledger entries."""
    ref_ids = [e.reference_id for e in entries if e.reference_type in ['PAYMENT', 'GOLD_LOCK']]
    payments = Payment.objects.none()
    if ref_ids:
        payments = Payment.objects.filter(id__in=ref_ids).select_related(
            'payment_mode', 'payment_status'
        ).prefetch_related('collections__payment_mode')

    return {p.id: p for p in payments}


def _invoice_lookup_for_ledger_entries(entries):
    """Map tax invoice numbers on store ledger rows to SaleInvoice ids (POS PDF)."""
    numbers = {
        (e.invoice or '').strip()
        for e in entries
        if _is_store_ledger_entry(e) and (e.invoice or '').strip()
    }
    numbers.discard('')
    if not numbers:
        return {}
    return {
        inv.invoice_number: inv.id
        for inv in SaleInvoice.objects.filter(
            invoice_number__in=numbers,
            is_deleted=False,
        ).only('id', 'invoice_number')
    }


def _receipt_for_catalogue_quote_payment_line(catalogue_payment_id: int) -> str:
    """Legacy ledger rows: reference_id = CatalogueQuotePayment.pk."""
    try:
        qp = CatalogueQuotePayment.objects.select_related('quote__sale_invoice').get(
            pk=catalogue_payment_id
        )
    except CatalogueQuotePayment.DoesNotExist:
        return ''
    if qp.quote.sale_invoice_id and qp.quote.sale_invoice:
        return qp.quote.sale_invoice.invoice_number
    return qp.quote.quote_number or ''


def _ledger_entry_to_dict(entry, payment_map):
    """
    Convert a CustomerLedger entry to a dictionary for API response.
    """
    payment = payment_map.get(entry.reference_id) if entry.reference_type == 'PAYMENT' else None
    
    return {
        'id': entry.id,
        'customer_id': entry.customer_id,
        'customer_scheme_id': entry.customer_scheme_id,
        'entry_type': entry.entry_type,
        'value_type': entry.value_type,
        'amount': str(entry.amount) if entry.amount else '0.00',
        'gold_grams': str(entry.gold_grams) if entry.gold_grams else '0.0000',
        'silver_grams': str(entry.silver_grams) if entry.silver_grams else '0.0000',
        'running_balance': str(entry.running_balance) if entry.running_balance else '0.00',
        'running_gold_balance': str(entry.running_gold_balance) if entry.running_gold_balance else '0.0000',
        'running_silver_balance': str(entry.running_silver_balance) if entry.running_silver_balance else '0.0000',
        'reference_type': entry.reference_type,
        'reference_id': entry.reference_id,
        'entry_date': entry.entry_date.isoformat() if entry.entry_date else None,
        'description': entry.description,
        'admin_remark': entry.admin_remark,
        'payment': {
            'id': payment.id,
            'amount': str(payment.amount),
            'payment_mode': payment.payment_mode.code if payment.payment_mode else None,
            'payment_status': payment.payment_status.code if payment.payment_status else None,
        } if payment else None,
    }


# -------------------------------------------------------
# MAIN FORMAT FUNCTION
# -------------------------------------------------------

def format_simple_ledger(
    entries,
    payment_map,
    ledger_type="overall",
    date_from=None,
    date_to=None,
    opening_balance=None,
    ledger_bucket="all",
    customer_id=None,
):
    ledger_bucket = (ledger_bucket or 'all').lower()
    sorted_entries = sorted(entries, key=lambda x: (x.entry_date, x.id))

    # -------------------------------
    # AUTO-POPULATE DATES FROM ENTRIES IF NOT PROVIDED
    # -------------------------------
    if sorted_entries:
        first_entry_date = sorted_entries[0].entry_date.date() if sorted_entries[0].entry_date else None
        last_entry_date = sorted_entries[-1].entry_date.date() if sorted_entries[-1].entry_date else None
        
        if date_from is None and first_entry_date:
            date_from = first_entry_date
        if date_to is None and last_entry_date:
            date_to = last_entry_date

    # -------------------------------
    # OPENING BALANCE
    # -------------------------------
    # Use pre-calculated opening balance if provided (from entries before date_from)
    # Otherwise calculate from first entry in filtered range
    store_cash = Decimal('0.00')
    scheme_cash = Decimal('0.00')
    store_gold = Decimal('0.0000')
    store_silver = Decimal('0.0000')
    scheme_gold = Decimal('0.0000')
    scheme_silver = Decimal('0.0000')
    opening_cash = Decimal('0.00')
    opening_gold = Decimal('0.0000')
    opening_silver = Decimal('0.0000')

    if customer_id is None and sorted_entries:
        customer_id = sorted_entries[0].customer_id
    opening_date_for_calc = date_from
    if opening_date_for_calc is None and sorted_entries and sorted_entries[0].entry_date:
        opening_date_for_calc = sorted_entries[0].entry_date.date()

    if ledger_bucket == 'all' and customer_id and opening_date_for_calc:
        store_ob = get_opening_balance_before_date(
            customer_id, opening_date_for_calc, ledger_bucket='store'
        )
        scheme_ob = get_opening_balance_before_date(
            customer_id, opening_date_for_calc, ledger_bucket='scheme'
        )
        store_cash = Decimal(str(store_ob.get('cash', Decimal('0.00'))))
        scheme_cash = Decimal(str(scheme_ob.get('cash', Decimal('0.00'))))
        store_gold = Decimal(str(store_ob.get('gold', Decimal('0.0000'))))
        store_silver = Decimal(str(store_ob.get('silver', Decimal('0.0000'))))
        scheme_gold = Decimal(str(scheme_ob.get('gold', Decimal('0.0000'))))
        scheme_silver = Decimal(str(scheme_ob.get('silver', Decimal('0.0000'))))
        opening_gold = store_gold + scheme_gold
        opening_silver = store_silver + scheme_silver
        opening_cash = store_cash + scheme_cash
    elif opening_balance:
        opening_cash = Decimal(str(opening_balance.get('cash', Decimal('0.00'))))
        opening_gold = Decimal(str(opening_balance.get('gold', Decimal('0.0000'))))
        opening_silver = Decimal(str(opening_balance.get('silver', Decimal('0.0000'))))
        if ledger_bucket == 'store':
            store_cash = opening_cash
            store_gold = opening_gold
            store_silver = opening_silver
        elif ledger_bucket == 'scheme':
            scheme_cash = opening_cash
            scheme_gold = opening_gold
            scheme_silver = opening_silver
    elif customer_id and opening_date_for_calc:
        ob = get_opening_balance_before_date(
            customer_id, opening_date_for_calc, ledger_bucket=ledger_bucket
        )
        opening_cash = Decimal(str(ob.get('cash', Decimal('0.00'))))
        opening_gold = Decimal(str(ob.get('gold', Decimal('0.0000'))))
        opening_silver = Decimal(str(ob.get('silver', Decimal('0.0000'))))
        if ledger_bucket == 'store':
            store_cash = opening_cash
            store_gold = opening_gold
            store_silver = opening_silver
        elif ledger_bucket == 'scheme':
            scheme_cash = opening_cash
            scheme_gold = opening_gold
            scheme_silver = opening_silver
    
    opening_date = date_from.strftime("%d/%m/%Y") if date_from else ""

    # -------------------------------
    # BUILD CASH ENTRY MAP FOR GOLD ENTRIES
    # -------------------------------
    # Create a map of reference_id -> cash entry for linking
    cash_entry_map = {}
    gold_lock_reference_ids = set()  # Track which payments have gold locked
    
    if sorted_entries:
        for entry in sorted_entries:
            if entry.value_type == "CASH" and entry.reference_type == "PAYMENT":
                cash_entry_map[entry.reference_id] = entry
            # Track GOLD_LOCK entries to find corresponding payments
            if entry.value_type == "GOLD" and entry.reference_type == "GOLD_LOCK":
                gold_lock_reference_ids.add(entry.reference_id)
    
    invoice_lookup = _invoice_lookup_for_ledger_entries(sorted_entries)

    ledger = []

    store_opening_block = _balance_block(
        opening_date, store_cash, store_gold, store_silver, is_store=True
    )
    scheme_opening_block = _balance_block(
        opening_date, scheme_cash, scheme_gold, scheme_silver, is_store=False
    )

    if ledger_bucket == 'all':
        ledger.append(_opening_row_entry(opening_date, store_opening_block, "Opening — Store sales"))
        ledger.append(_opening_row_entry(opening_date, scheme_opening_block, "Opening — Schemes"))
    else:
        ob = scheme_opening_block if ledger_bucket == 'scheme' else store_opening_block
        ledger.append(_opening_row_entry(opening_date, ob, "OPENING BALANCE"))

    # Totals
    total_credit_cash = Decimal('0.00')
    total_debit_cash = Decimal('0.00')

    total_credit_gold = Decimal('0.000')
    total_debit_gold = Decimal('0.000')

    total_credit_silver = Decimal('0.000')
    total_debit_silver = Decimal('0.000')

    # Udhar tracking
    total_udhar = Decimal('0.00')
    total_paid = Decimal('0.00')

    # -------------------------------
    # LOOP
    # -------------------------------
    for entry in sorted_entries:

        credit_gold = debit_gold = ""
        credit_silver = debit_silver = ""
        credit_amount = debit_amount = ""

        # ---------------- CASH ----------------
        # Skip CASH entries for payments that have gold locked
        skip_cash_entry = (
            entry.value_type == "CASH" and 
            entry.reference_type == "PAYMENT" and
            entry.reference_id in gold_lock_reference_ids
        )
        
        if skip_cash_entry:
            # Skip this cash entry - the amount was converted to gold
            # But still track running balance using the gold entry's running balance
            pass
        elif entry.value_type == "CASH" and entry.amount:
            is_store = _is_store_ledger_entry(entry)
            if is_store and entry.entry_type == "DEBIT":
                debit_amount = str(entry.amount)
                store_cash += entry.amount
                total_debit_cash += entry.amount
                total_udhar += entry.amount
            elif is_store and entry.entry_type == "CREDIT":
                credit_amount = str(entry.amount)
                store_cash -= entry.amount
                total_credit_cash += entry.amount
                total_paid += entry.amount
            elif not is_store and entry.entry_type == "CREDIT":
                credit_amount = str(entry.amount)
                scheme_cash += entry.amount
                total_credit_cash += entry.amount
            elif not is_store and entry.entry_type == "DEBIT":
                debit_amount = str(entry.amount)
                scheme_cash -= entry.amount
                total_debit_cash += entry.amount

        # ---------------- GOLD ----------------
        linked_cash_amount = Decimal('0.00')
        is_gold_entry = False
        
        if entry.value_type == "GOLD" and entry.gold_grams:
            is_gold_entry = True
            # Find linked CASH entry using reference_id
            linked_cash_amount = Decimal('0.00')
            if entry.reference_id and entry.reference_id in cash_entry_map:
                linked_cash_entry = cash_entry_map[entry.reference_id]
                linked_cash_amount = linked_cash_entry.amount if linked_cash_entry.amount else Decimal('0.00')
            
            # If no linked cash entry found, use amount from GOLD entry itself (if set)
            if linked_cash_amount == Decimal('0.00') and entry.amount and entry.amount > 0:
                linked_cash_amount = entry.amount
            
            is_store_metal = _is_store_ledger_entry(entry)
            if entry.entry_type == "CREDIT":
                credit_gold = str(entry.gold_grams)
                credit_amount = str(linked_cash_amount)  # Show amount used for gold locking
                debit_amount = str(linked_cash_amount)  # Show amount consumed for gold locking
                if is_store_metal:
                    store_gold += entry.gold_grams
                else:
                    scheme_gold += entry.gold_grams
                total_credit_gold += entry.gold_grams
            else:
                debit_gold = str(entry.gold_grams)
                if is_store_metal:
                    store_gold -= entry.gold_grams
                else:
                    scheme_gold -= entry.gold_grams
                total_debit_gold += entry.gold_grams

        # ---------------- SILVER ✅ ----------------
        elif entry.value_type == "SILVER" and entry.silver_grams:
            is_store_metal = _is_store_ledger_entry(entry)
            if entry.entry_type == "CREDIT":
                credit_silver = str(entry.silver_grams)
                if is_store_metal:
                    store_silver += entry.silver_grams
                else:
                    scheme_silver += entry.silver_grams
                total_credit_silver += entry.silver_grams
            else:
                debit_silver = str(entry.silver_grams)
                if is_store_metal:
                    store_silver -= entry.silver_grams
                else:
                    scheme_silver -= entry.silver_grams
                total_debit_silver += entry.silver_grams

        # FILTER - use entry.value_type for accurate filtering
        if ledger_type == "cash" and entry.value_type != "CASH":
            continue
        if ledger_type == "gold" and entry.value_type != "GOLD":
            continue
        if ledger_type == "silver" and entry.value_type != "SILVER":
            continue

        # Narration
        narration = entry.description or entry.reference_type

        if entry.customer_scheme_id and getattr(entry.customer_scheme, 'scheme', None):
            narration = f"{narration} ({entry.customer_scheme.scheme.scheme_name})"

        # Payment receipt — POS tax invoice number (same as Store POS PDF)
        payment_receipt = ""
        sale_invoice_id = None
        if entry.reference_type in ["PAYMENT", "GOLD_LOCK"] and entry.reference_id in payment_map:
            payment = payment_map[entry.reference_id]
            payment_receipt = payment.receipt_no if payment.receipt_no else ""
        elif entry.reference_type == "CATALOGUE_QUOTE_PAYMENT" and entry.reference_id:
            payment_receipt = _receipt_for_catalogue_quote_payment_line(entry.reference_id)
        elif _is_store_ledger_entry(entry) and (entry.invoice or "").strip():
            payment_receipt = (entry.invoice or "").strip()

        if payment_receipt:
            sale_invoice_id = invoice_lookup.get(payment_receipt)

        is_store_row = _is_store_ledger_entry(entry)
        if ledger_bucket == 'store':
            row_gold, row_silver = store_gold, store_silver
        elif ledger_bucket == 'scheme':
            row_gold, row_silver = scheme_gold, scheme_silver
        else:
            row_gold = store_gold if is_store_row else scheme_gold
            row_silver = store_silver if is_store_row else scheme_silver

        ledger.append({
            "date": entry.entry_date.strftime('%d/%m/%Y') if entry.entry_date else "",
            "type": _transaction_type_from_entry(entry),
            "narration": narration,

            "credit_gold": credit_gold,
            "credit_silver": credit_silver,
            "credit_amount": credit_amount,

            "debit_gold": debit_gold,
            "debit_silver": debit_silver,
            "debit_amount": debit_amount,

            "balance_gold": str(row_gold),
            "balance_silver": str(row_silver),
            "balance_amount": _signed_cash_display(
                store_cash if is_store_row else scheme_cash,
                is_store=is_store_row,
            ),
            "balance_position": (
                _cash_balance_meta(store_cash) if _is_store_ledger_entry(entry)
                else _scheme_cash_balance_meta(scheme_cash)
            )['position'],
            "ledger_bucket": 'store' if _is_store_ledger_entry(entry) else 'scheme',

            "payment_receipt": payment_receipt,
            "sale_invoice_id": sale_invoice_id,
            "invoice": entry.invoice or "",
            "source": entry.source or "",
            "remark": entry.admin_remark or "",
        })

    # -------------------------------
    # TOTAL ROW
    # -------------------------------
    ledger.append({
        "date": "",
        "type": "",
        "narration": "TOTAL",

        "credit_gold": str(total_credit_gold),
        "credit_silver": str(total_credit_silver),
        "credit_amount": str(total_credit_cash),

        "debit_gold": str(total_debit_gold),
        "debit_silver": str(total_debit_silver),
        "debit_amount": str(total_debit_cash),
    })

    # -------------------------------
    # FINAL RESPONSE
    # -------------------------------
    closing_date = date_to.strftime("%d/%m/%Y") if date_to else ""

    if ledger_bucket == 'scheme':
        opening_balance_block = scheme_opening_block
    elif ledger_bucket == 'all':
        opening_balance_block = {
            **scheme_opening_block,
            'gold': str(opening_gold),
            'silver': str(opening_silver),
        }
    else:
        opening_balance_block = store_opening_block

    result = {
        "ledger_bucket": ledger_bucket,
        "opening_balance": opening_balance_block,
        "udhar_summary": {
            "total_udhar": str(total_udhar),
            "paid": str(total_paid),
            "remaining": str(total_udhar - total_paid),
        },
        "entries": ledger,
    }

    if ledger_bucket == 'all':
        result["store_opening"] = store_opening_block
        result["scheme_opening"] = scheme_opening_block
        store_cl = _balance_block(
            closing_date, store_cash, store_gold, store_silver, is_store=True
        )
        scheme_cl = _balance_block(
            closing_date, scheme_cash, scheme_gold, scheme_silver, is_store=False
        )
        result["store_closing"] = store_cl
        result["scheme_closing"] = scheme_cl
        ledger.append(_closing_row_entry(closing_date, store_cl, "Closing — Store sales"))
        ledger.append(_closing_row_entry(closing_date, scheme_cl, "Closing — Schemes"))
        result["entries"] = ledger
    elif ledger_bucket == 'scheme':
        scheme_cl = _balance_block(
            closing_date, scheme_cash, scheme_gold, scheme_silver, is_store=False
        )
        result["closing_balance"] = scheme_cl
        ledger.append(_closing_row_entry(closing_date, scheme_cl, "Closing — Schemes"))
        result["entries"] = ledger
        result["udhar_summary"] = {
            "total_udhar": "0.00",
            "paid": str(total_credit_cash),
            "remaining": "0.00",
        }
    else:
        store_cl = _balance_block(
            closing_date, store_cash, store_gold, store_silver, is_store=True
        )
        result["closing_balance"] = store_cl
        ledger.append(_closing_row_entry(closing_date, store_cl, "Closing — Store sales"))
        result["entries"] = ledger

    return result


# -------------------------------------------------------
# API VIEWS
# -------------------------------------------------------

class CustomerLedgerListView(generics.ListAPIView):

    @method_decorator(admin_auth("CRM_CUSTOMER_LIST_VIEW"))
    def get(self, request, *args, **kwargs):
        customer_id = kwargs.get("pk")
        ledger_type = request.GET.get('type', 'overall')
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')

        date_from_parsed = _parse_date_flexible(date_from)
        date_to_parsed = _parse_date_flexible(date_to)

        if ledger_type in ('store', 'scheme'):
            ledger_bucket = ledger_type
            column_type = 'overall'
        else:
            ledger_bucket = 'all'
            column_type = ledger_type

        opening_balance = None
        if date_from_parsed:
            opening_balance = get_opening_balance_before_date(
                customer_id, date_from_parsed, ledger_bucket=ledger_bucket
            )

        ledger_entries = list(get_ledger_entries(
            customer_id=customer_id,
            date_from=date_from_parsed,
            date_to=date_to_parsed,
            ledger_bucket=ledger_bucket,
        ))
        payment_map = _payment_map_for_ledger_entries(ledger_entries)

        data = format_simple_ledger(
            ledger_entries,
            payment_map,
            column_type,
            date_from=date_from_parsed,
            date_to=date_to_parsed,
            opening_balance=opening_balance,
            ledger_bucket=ledger_bucket,
            customer_id=customer_id,
        )
        try:
            from shared.services.customer_store_account_service import get_customer_store_balance
            data['store_account'] = get_customer_store_balance(customer_id)
        except Exception:
            pass
        return Response(data)


class LedgerListView(generics.ListAPIView):

    @method_decorator(admin_auth("CRM_ACCOUNTS_DAILY_LEDGER_VIEW", "ledger.view"))
    def get(self, request, *args, **kwargs):

        customer_id = request.GET.get('customer_id')
        scheme_id = request.GET.get('scheme_id')
        entry_type = request.GET.get('entry_type')
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        ordering = request.GET.get('ordering', '-entry_date')

        customer_id = int(customer_id) if customer_id and customer_id.isdigit() else None
        scheme_id = int(scheme_id) if scheme_id and scheme_id.isdigit() else None

        date_from_parsed = _parse_date_flexible(date_from)
        date_to_parsed = _parse_date_flexible(date_to)

        # Get opening balance from entries before date_from
        opening_balance = None
        if date_from_parsed and customer_id:
            opening_balance = get_opening_balance_before_date(customer_id, date_from_parsed)

        ledger_entries = list(get_ledger_entries(
            customer_id=customer_id,
            scheme_id=scheme_id,
            entry_type=entry_type or None,
            date_from=date_from_parsed,
            date_to=date_to_parsed,
            ordering=ordering,
        ))

        payment_map = _payment_map_for_ledger_entries(ledger_entries)
        
        data = format_simple_ledger(
            ledger_entries, 
            payment_map,
            date_from=date_from_parsed,
            date_to=date_to_parsed,
            opening_balance=opening_balance
        )
        return Response(data)


class SchemeLedgerListView(generics.ListAPIView):

    @method_decorator(admin_auth("CRM_ACCOUNTS_DAILY_LEDGER_VIEW", "ledger.view"))
    def get(self, request, *args, **kwargs):
        scheme_id = kwargs.get("pk")
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')

        date_from_parsed = _parse_date_flexible(date_from)
        date_to_parsed = _parse_date_flexible(date_to)

        # Get opening balance from entries before date_from
        # Note: For scheme-level ledger, we need customer_id to get opening balance
        # Since scheme_id is provided, we'll skip opening balance calculation for now
        opening_balance = None

        ledger_entries = list(get_ledger_entries(
            scheme_id=scheme_id,
            ordering='-entry_date'
        ))

        payment_map = _payment_map_for_ledger_entries(ledger_entries)
        
        data = format_simple_ledger(
            ledger_entries, 
            payment_map,
            date_from=date_from_parsed,
            date_to=date_to_parsed,
            opening_balance=opening_balance
        )
        return Response(data)


class CustomerLedgerExportView(generics.ListAPIView):
    """
    Export customer ledger to CSV format.
    """
    
    @method_decorator(admin_auth("CRM_CUSTOMER_LIST_VIEW"))
    def get(self, request, *args, **kwargs):
        import csv
        from django.http import HttpResponse
        from shared.models import Customer
        
        customer_id = kwargs.get("pk")
        ledger_type = request.GET.get('type', 'overall')
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        
        date_from_parsed = _parse_date_flexible(date_from)
        date_to_parsed = _parse_date_flexible(date_to)
        
        # Get customer name for filename
        customer_name = 'customer'
        try:
            customer = Customer.objects.get(id=customer_id)
            print(f"DEBUG: Customer found - id={customer.id}, full_name={customer.full_name}")
            customer_name = customer.full_name.replace(' ', '_') if customer.full_name else f'customer_{customer_id}'
            print(f"DEBUG: customer_name={customer_name}")
        except Customer.DoesNotExist:
            print(f"DEBUG: Customer with id={customer_id} does not exist")
            customer_name = f'customer_{customer_id}'
        except Exception as e:
            print(f"DEBUG: Error getting customer: {e}")
            customer_name = f'customer_{customer_id}'
        
        # Get opening balance from entries before date_from
        opening_balance = None
        if date_from_parsed:
            opening_balance = get_opening_balance_before_date(customer_id, date_from_parsed)
        
        ledger_entries = list(get_ledger_entries(
            customer_id=customer_id,
            date_from=date_from_parsed,
            date_to=date_to_parsed
        ))
        payment_map = _payment_map_for_ledger_entries(ledger_entries)
        
        data = format_simple_ledger(
            ledger_entries, 
            payment_map, 
            ledger_type,
            date_from=date_from_parsed,
            date_to=date_to_parsed,
            opening_balance=opening_balance
        )
        
        # Create CSV response
        response = HttpResponse(content_type='text/csv')
        filename = f'ledger_{customer_name}'
        print("filename", filename)
        if date_from_parsed:
            filename += f'_{date_from_parsed.strftime("%Y%m%d")}'
        if date_to_parsed:
            filename += f'_to_{date_to_parsed.strftime("%Y%m%d")}'
        filename += '.csv'
        print("file_name", filename)
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        
        writer = csv.writer(response)
        
        # Write header row
        writer.writerow([
            'Date',
            'Type',
            'Narration',
            'Credit Gold',
            'Credit Silver',
            'Credit Amount',
            'Debit Gold',
            'Debit Silver',
            'Debit Amount',
            'Balance Gold',
            'Balance Silver',
            'Balance Amount',
            'Payment Mode',
            'Remark'
        ])
        
        # Write data rows
        for entry in data.get('entries', []):
            writer.writerow([
                entry.get('date', ''),
                entry.get('type', ''),
                entry.get('narration', ''),
                entry.get('credit_gold', ''),
                entry.get('credit_silver', ''),
                entry.get('credit_amount', ''),
                entry.get('debit_gold', ''),
                entry.get('debit_silver', ''),
                entry.get('debit_amount', ''),
                entry.get('balance_gold', ''),
                entry.get('balance_silver', ''),
                entry.get('balance_amount', ''),
                entry.get('source', ''),
                entry.get('remark', '')
            ])
        
        # Write closing balance row
        closing_balance = data.get('closing_balance', {})
        writer.writerow([])  # Empty row for separation
        writer.writerow([
            closing_balance.get('date', ''),
            'CLOSING',
            'CLOSING BALANCE',
            '',
            '',
            '',
            '',
            '',
            '',
            closing_balance.get('gold', ''),
            closing_balance.get('silver', ''),
            closing_balance.get('cash', ''),
            '',
            ''
        ])
        
        return response
