# """
# Shared service for financial ledger logic (CustomerLedger, AccountingLedger, FinancialTransaction).
# Used when a payment is finalized (SUCCESS, is_finalized=True).
# Supports PaymentCollection for split payments: ledger entries per collection.
# """
# import logging
# from decimal import Decimal

# from django.utils import timezone

# from shared.models import (
#     CustomerLedger,
#     AccountingLedger,
#     FinancialTransaction,
#     Payment,
#     PaymentCollection,
# )

# logger = logging.getLogger(__name__)

# # Map payment mode code to accounting account (debit side). Default CASH_ACCOUNT for backward compatibility.
# PAYMENT_MODE_ACCOUNT_MAP = {
#     'CASH': 'CASH_ACCOUNT',
#     'UPI': 'UPI_ACCOUNT',
#     'ONLINE': 'CASH_ACCOUNT',
#     'CARD': 'CARD_ACCOUNT',
#     'CHEQUE': 'CHEQUE_ACCOUNT',
#     'BANK_TRANSFER': 'BANK_ACCOUNT',
# }


# def _get_account_for_payment_mode(payment_mode_code):
#     """Return account code for payment mode. Defaults to CASH_ACCOUNT."""
#     return PAYMENT_MODE_ACCOUNT_MAP.get(
#         (payment_mode_code or '').upper(),
#         'CASH_ACCOUNT'
#     )


# def _get_payment_collections(payment):
#     """
#     Get payment mode/amount pairs for ledger. Uses PaymentCollection if present,
#     else falls back to payment.payment_mode (legacy/single).
#     """
#     collections = list(
#         payment.collections.select_related('payment_mode').all()
#     )
#     if collections:
#         return [(c.payment_mode.code, c.amount) for c in collections]
#     # Legacy: single payment_mode on Payment
#     if payment.payment_mode_id:
#         return [(payment.payment_mode.code, payment.amount)]
#     return []


# def insert_financial_records_for_payment(payment):
#     """
#     Insert FinancialTransaction, CustomerLedger, and AccountingLedger entries for a PAID payment.
#     Uses PaymentCollection when present; otherwise falls back to payment.payment_mode.
#     Idempotent: skips if CustomerLedger entry for this payment already exists.
#     Must be called inside transaction.atomic() and only when payment.is_finalized is True.
#     """
#     if not getattr(payment, 'is_finalized', False):
#         logger.warning(f"insert_financial_records_for_payment: payment {payment.id} not finalized. Skipping.")
#         return

#     if CustomerLedger.objects.filter(
#         reference_type='PAYMENT',
#         reference_id=payment.id
#     ).exists():
#         logger.info(f"Financial records already exist for payment {payment.id}. Skipping.")
#         return

#     customer = payment.instalment.customer_scheme.customer
#     customer_scheme = payment.instalment.customer_scheme
#     entry_date = payment.paid_at or timezone.now()
#     amount = payment.amount
#     mode_amounts = _get_payment_collections(payment)

#     if not mode_amounts:
#         logger.warning(f"Payment {payment.id} has no payment_mode or collections. Skipping ledger.")
#         return

#     # Step 1 — Insert FinancialTransaction (one per collection; legacy: one)
#     for mode_code, coll_amount in mode_amounts:
#         FinancialTransaction.objects.create(
#             customer=customer,
#             customer_scheme=customer_scheme,
#             source_type='PAYMENT',
#             source_id=payment.id,
#             direction='CREDIT',
#             amount=coll_amount,
#             payment_mode=mode_code,
#             status='SUCCESS',
#             transaction_date=entry_date,
#             gateway_transaction_id=payment.gateway_transaction_id,
#         )

#     # Step 2 — Insert CustomerLedger (one per payment, total amount)
#     last_balance = CustomerLedger.objects.filter(
#         customer=customer,
#         customer_scheme=customer_scheme,
#     ).order_by('-id').first()

#     prev_running = Decimal('0.00')
#     prev_gold = Decimal('0.0000')
#     if last_balance:
#         if last_balance.running_balance is not None:
#             prev_running = last_balance.running_balance
#         if last_balance.running_gold_balance is not None:
#             prev_gold = last_balance.running_gold_balance

#     new_balance = prev_running + amount

#     CustomerLedger.objects.create(
#         customer=customer,
#         customer_scheme=customer_scheme,
#         entry_type='CREDIT',
#         amount=amount,
#         gold_grams=Decimal('0.0000'),
#         reference_type='PAYMENT',
#         reference_id=payment.id,
#         running_balance=new_balance,
#         running_gold_balance=prev_gold,
#         entry_date=entry_date,
#         value_type='CASH',
#         description=f"Installment Payment #{payment.instalment.instalment_no}",
#         admin_remark=None,
#     )

#     # Step 3 — Insert AccountingLedger (debit per mode, one credit for total)
#     for mode_code, coll_amount in mode_amounts:
#         account_code = _get_account_for_payment_mode(mode_code)
#         AccountingLedger.objects.create(
#             account_code=account_code,
#             debit=coll_amount,
#             credit=Decimal('0.00'),
#             reference_type='PAYMENT',
#             reference_id=payment.id,
#             description=f'Payment {payment.id} ({mode_code})',
#             entry_date=entry_date,
#         )
#     AccountingLedger.objects.create(
#         account_code='SCHEME_LIABILITY',
#         debit=Decimal('0.00'),
#         credit=amount,
#         reference_type='PAYMENT',
#         reference_id=payment.id,
#         description=f'Payment {payment.id}',
#         entry_date=entry_date,
#     )

#     logger.info(f"Financial records created for payment {payment.id}.")


# def insert_bonus_ledger_entries(customer_scheme, instalment, amount):
#     """
#     Insert CustomerLedger (BONUS) and AccountingLedger (COMPANY_EXPENSE debit, SCHEME_LIABILITY credit)
#     for a company-funded bonus installment. Call when creating the bonus installment; idempotent by
#     reference_type/reference_id. Must be called inside transaction.atomic().
#     """
#     if CustomerLedger.objects.filter(
#         reference_type='BONUS_INSTALMENT',
#         reference_id=instalment.id
#     ).exists():
#         logger.info(f"Bonus ledger already exists for instalment {instalment.id}. Skipping.")
#         return

#     customer = customer_scheme.customer
#     entry_dt = timezone.now()

#     last_balance = CustomerLedger.objects.filter(
#         customer=customer,
#         customer_scheme=customer_scheme,
#     ).order_by('-id').first()

#     prev_running = Decimal('0.00')
#     prev_gold = Decimal('0.0000')
#     if last_balance:
#         if last_balance.running_balance is not None:
#             prev_running = last_balance.running_balance
#         if last_balance.running_gold_balance is not None:
#             prev_gold = last_balance.running_gold_balance

#     new_balance = prev_running + amount

#     CustomerLedger.objects.create(
#         customer=customer,
#         customer_scheme=customer_scheme,
#         entry_type='BONUS',
#         amount=amount,
#         gold_grams=Decimal('0.0000'),
#         reference_type='BONUS_INSTALMENT',
#         reference_id=instalment.id,
#         running_balance=new_balance,
#         running_gold_balance=prev_gold,
#         entry_date=entry_dt,
#         value_type='CASH',
#         description=f"Bonus Installment #{instalment.instalment_no}",
#         admin_remark=None,
#     )

#     AccountingLedger.objects.create(
#         account_code='COMPANY_EXPENSE',
#         debit=amount,
#         credit=Decimal('0.00'),
#         reference_type='BONUS_INSTALMENT',
#         reference_id=instalment.id,
#         description=f'Bonus instalment {instalment.instalment_no}',
#         entry_date=entry_dt,
#     )
#     AccountingLedger.objects.create(
#         account_code='SCHEME_LIABILITY',
#         debit=Decimal('0.00'),
#         credit=amount,
#         reference_type='BONUS_INSTALMENT',
#         reference_id=instalment.id,
#         description=f'Bonus instalment {instalment.instalment_no}',
#         entry_date=entry_dt,
#     )

#     logger.info(f"Bonus ledger entries created for instalment {instalment.id}.")


# def get_ledger_entries(
#     customer_scheme_id=None,
#     customer_id=None,
#     scheme_id=None,
#     entry_type=None,
#     date_from=None,
#     date_to=None,
#     ordering=None,
#     gold_lock=None,
# ):
#     """
#     Get CustomerLedger entries by filters.
#     ordering: default '-entry_date' (latest first). Use 'entry_date' for oldest first.
#     gold_lock: if True, filter to only gold-locked entries (gold_grams > 0 or has GoldLockingRecord)
#     """
#     queryset = CustomerLedger.objects.select_related(
#         'customer', 'customer_scheme', 'customer_scheme__scheme'
#     ).all()
#     if customer_scheme_id:
#         queryset = queryset.filter(customer_scheme_id=customer_scheme_id)
#     if customer_id:
#         queryset = queryset.filter(customer_id=customer_id)
#     if scheme_id:
#         queryset = queryset.filter(customer_scheme__scheme_id=scheme_id)
#     if entry_type:
#         queryset = queryset.filter(entry_type=entry_type)
#     if date_from:
#         queryset = queryset.filter(entry_date__date__gte=date_from)
#     if date_to:
#         queryset = queryset.filter(entry_date__date__lte=date_to)
#     if gold_lock is not None:
#         if gold_lock:
#             # Filter to entries with gold_grams > 0
#             queryset = queryset.filter(gold_grams__gt=0)
#         else:
#             # Filter to entries without gold (gold_grams is 0 or null)
#             queryset = queryset.filter(gold_grams__isnull=True) | queryset.filter(gold_grams=0)
#             queryset = queryset.distinct()
#     order_by = ordering if ordering else '-entry_date'
#     return queryset.order_by(order_by, '-id')


# def get_current_balance(customer_scheme_id):
#     """Get current running balance for a customer scheme from CustomerLedger."""
#     last_entry = CustomerLedger.objects.filter(
#         customer_scheme_id=customer_scheme_id
#     ).order_by('-id').first()
#     return last_entry.running_balance if last_entry and last_entry.running_balance is not None else Decimal('0.00')


# def get_payment_mode_collection_summary(date_from=None, date_to=None):
#     """
#     Collection summary by payment mode using payment_collections.
#     Returns list of {payment_mode_code, payment_mode_label, total_amount}.
#     Powers reports: Mode | Amount (Cash 20000, UPI 30000, etc.)
#     """
#     from django.db.models import Sum
#     from shared.models import PaymentCollection

#     qs = PaymentCollection.objects.filter(
#         payment__payment_status__code='SUCCESS',
#         payment__is_finalized=True,
#     ).values('payment_mode_id', 'payment_mode__code', 'payment_mode__label').annotate(
#         total_amount=Sum('amount')
#     ).order_by('-total_amount')

#     if date_from:
#         qs = qs.filter(payment__paid_at__date__gte=date_from)
#     if date_to:
#         qs = qs.filter(payment__paid_at__date__lte=date_to)

#     return [
#         {
#             'payment_mode_id': r['payment_mode_id'],
#             'payment_mode_code': r['payment_mode__code'],
#             'payment_mode_label': r['payment_mode__label'] or r['payment_mode__code'],
#             'total_amount': str(r['total_amount']) if r['total_amount'] is not None else '0.00',
#         }
#         for r in qs
#     ]

"""
Shared service for financial ledger logic (CustomerLedger, AccountingLedger, FinancialTransaction).
Handles CASH payments + GOLD locking (derived from instalment.gold_grams).
"""

import logging
from datetime import datetime
from decimal import Decimal
from django.utils import timezone

from shared.models import (
    CustomerLedger,
    AccountingLedger,
    FinancialTransaction,
    PaymentCollection,
    SchemeMaster,
)
from shared.utils.ledger_utils import create_ledger_entry

logger = logging.getLogger(__name__)

# -------------------------------------------------------
# PAYMENT MODE MAP
# -------------------------------------------------------

PAYMENT_MODE_ACCOUNT_MAP = {
    'CASH': 'CASH_ACCOUNT',
    'UPI': 'UPI_ACCOUNT',
    'ONLINE': 'CASH_ACCOUNT',
    'CARD': 'CARD_ACCOUNT',
    'CHEQUE': 'CHEQUE_ACCOUNT',
    'BANK_TRANSFER': 'BANK_ACCOUNT',
}


def _get_account_for_payment_mode(payment_mode_code):
    return PAYMENT_MODE_ACCOUNT_MAP.get(
        (payment_mode_code or '').upper(),
        'CASH_ACCOUNT'
    )


def _get_payment_collections(payment):
    collections = list(
        payment.collections.select_related('payment_mode').all()
    )
    if collections:
        return [(c.payment_mode.code, c.amount) for c in collections]

    if payment.payment_mode_id:
        return [(payment.payment_mode.code, payment.amount)]

    return []


# -------------------------------------------------------
# MAIN FUNCTION (UPDATED)
# -------------------------------------------------------

def insert_financial_records_for_payment(payment, payment_date=None):

    if not getattr(payment, 'is_finalized', False):
        logger.warning(f"Payment {payment.id} not finalized. Skipping.")
        return

    if CustomerLedger.objects.filter(
        reference_type='PAYMENT',
        reference_id=payment.id
    ).exists():
        logger.info(f"Ledger already exists for payment {payment.id}")
        return

    instalment = payment.instalment
    customer_scheme = instalment.customer_scheme
    customer = customer_scheme.customer
    scheme = customer_scheme.scheme

    # -------------------------------------------------------
    # CHECK BENEFIT TYPE FOR DIFFERENTIATION
    # -------------------------------------------------------
    # Get benefit types from both customer_scheme benefits and scheme benefits
    customer_benefit_types = set(
        customer_scheme.benefits.values_list('benefit_type', flat=True)
    )
    scheme_benefit_types = set(
        scheme.benefits.values_list('benefit_type', flat=True)
    )
    all_benefit_types = customer_benefit_types | scheme_benefit_types
    
    has_gold_benefits = all_benefit_types & {'DYNAMIC_LOCK', 'FIXED_GRAM'}
    has_cash_benefits = all_benefit_types & {'BONUS_MONTHS', 'FLAT', 'PERCENTAGE'}
    
    is_gold_scheme = bool(has_gold_benefits)
    is_cash_scheme = bool(has_cash_benefits) or not is_gold_scheme

    from datetime import date as datetime_date, time
    # Use payment_date if provided, otherwise fall back to paid_at or current time
    # payment_date can be date or datetime object
    if payment_date:
        # Convert to timezone-aware datetime if needed
        if isinstance(payment_date, datetime_date):
            # It's a date object, convert to datetime at noon and make timezone-aware
            dt = datetime.combine(payment_date, time(hour=12))
            entry_date = timezone.make_aware(dt)
        elif timezone.is_naive(payment_date):
            # It's a naive datetime, make it timezone-aware
            entry_date = timezone.make_aware(payment_date)
        else:
            # It's already timezone-aware
            entry_date = payment_date
    else:
        entry_date = payment.paid_at if payment.paid_at else timezone.now()

    amount = payment.amount

    mode_amounts = _get_payment_collections(payment)

    if not mode_amounts:
        logger.warning(f"No payment mode for payment {payment.id}")
        return

    # -------------------------------------------------------
    # STEP 1 — FINANCIAL TRANSACTIONS
    # -------------------------------------------------------

    for mode_code, coll_amount in mode_amounts:
        FinancialTransaction.objects.create(
            customer=customer,
            customer_scheme=customer_scheme,
            source_type='PAYMENT',
            source_id=payment.id,
            direction='CREDIT',
            amount=coll_amount,
            payment_mode=mode_code,
            status='SUCCESS',
            transaction_date=entry_date,
            gateway_transaction_id=payment.gateway_transaction_id,
        )

    # -------------------------------------------------------
    # STEP 2 — CUSTOMER LEDGER - Based on benefit_type
    # -------------------------------------------------------

    last_balance = CustomerLedger.objects.filter(
        customer=customer,
        customer_scheme=customer_scheme,
    ).order_by('-id').first()

    prev_running = Decimal('0.00')
    prev_gold = Decimal('0.0000')

    if last_balance:
        prev_running = last_balance.running_balance or Decimal('0.00')
        prev_gold = last_balance.running_gold_balance or Decimal('0.0000')

    if is_gold_scheme:
        # ---------------------------------------------------------
        # GOLD SCHEME (DYNAMIC_LOCK/FIXED_GRAM): Create GOLD entry only
        # With credit_amount and debit_amount to show amount used for gold lock
        # ---------------------------------------------------------
        gold_grams = instalment.gold_grams or Decimal('0.0000')
        gold_rate = instalment.gold_rate or Decimal('0.00')
        
        if gold_grams > 0:
            new_gold_balance = prev_gold + gold_grams
            
            # Get payment source/mode (use first mode from collections)
            payment_source = mode_amounts[0][0] if mode_amounts else 'CASH'
            
            CustomerLedger.objects.create(
                customer=customer,
                customer_scheme=customer_scheme,
                entry_type='CREDIT',
                amount=amount,  # Show amount as credit and debit for transparency
                gold_grams=gold_grams,
                reference_type='GOLD_LOCK',
                reference_id=payment.id,
                invoice=payment.receipt_no,  # Store receipt number
                source=payment_source,  # Store payment mode (CASH, UPI, CARD, NETBANKING, etc.)
                running_balance=prev_running,  # Cash balance unchanged - cash is locked for gold
                running_gold_balance=new_gold_balance,
                entry_date=entry_date,
                value_type='GOLD',
                description=f"Gold locked for instalment #{instalment.instalment_no} - {scheme.scheme_name} | Gold Rate: ₹{gold_rate}/gm",
                admin_remark=None,
            )
            
            logger.info(f"GOLD ledger entry created: {gold_grams}g for payment {payment.id}, scheme: {scheme.scheme_name}, gold_rate: {gold_rate}")
    else:
        # ---------------------------------------------------------
        # CASH SCHEME (BONUS_MONTHS/FLAT/PERCENTAGE): Create CASH entry only
        # ---------------------------------------------------------
        new_balance = prev_running + amount
        
        # Get payment source/mode (use first mode from collections)
        payment_source = mode_amounts[0][0] if mode_amounts else 'CASH'
        
        CustomerLedger.objects.create(
            customer=customer,
            customer_scheme=customer_scheme,
            entry_type='CREDIT',
            amount=amount,
            gold_grams=Decimal('0.0000'),
            reference_type='PAYMENT',
            reference_id=payment.id,
            invoice=payment.receipt_no,  # Store receipt number
            source=payment_source,  # Store payment mode (CASH, UPI, CARD, NETBANKING, etc.)
            running_balance=new_balance,
            running_gold_balance=prev_gold,
            entry_date=entry_date,
            value_type='CASH',
            description=f"Installment Payment #{instalment.instalment_no} - {scheme.scheme_name}",
            admin_remark=None,
        )
        
        logger.info(f"CASH ledger entry created for payment {payment.id}, scheme: {scheme.scheme_name}, benefit_types: {all_benefit_types}")

    #     CustomerLedger.objects.create(
    #         customer=customer,
    #         customer_scheme=customer_scheme,
    #         entry_type='CREDIT',
    #         amount=Decimal('0.00'),  # no cash impact
    #         gold_grams=gold_grams,
    #         reference_type='GOLD_LOCK',
    #         reference_id=instalment.id,
    #         running_balance=new_balance,  # keep same cash balance
    #         running_gold_balance=new_gold_balance,
    #         entry_date=entry_date,
    #         value_type='GOLD',
    #         description=f"Gold locked for instalment #{instalment.instalment_no}",
    #         admin_remark=None,
    #     )

    #     logger.info(f"Gold entry created: {gold_grams}g for instalment {instalment.id}")

    # -------------------------------------------------------
    # STEP 4 — ACCOUNTING LEDGER
    # -------------------------------------------------------

    for mode_code, coll_amount in mode_amounts:
        account_code = _get_account_for_payment_mode(mode_code)

        AccountingLedger.objects.create(
            account_code=account_code,
            debit=coll_amount,
            credit=Decimal('0.00'),
            reference_type='PAYMENT',
            reference_id=payment.id,
            description=f'Payment {payment.id} ({mode_code})',
            entry_date=entry_date,
        )

    AccountingLedger.objects.create(
        account_code='SCHEME_LIABILITY',
        debit=Decimal('0.00'),
        credit=amount,
        reference_type='PAYMENT',
        reference_id=payment.id,
        description=f'Payment {payment.id}',
        entry_date=entry_date,
    )

    logger.info(f"✅ Financial records created for payment {payment.id}")


# -------------------------------------------------------
# BONUS LEDGER
# -------------------------------------------------------

def insert_bonus_ledger_entries(customer_scheme, instalment, amount):

    if CustomerLedger.objects.filter(
        reference_type='BONUS_INSTALMENT',
        reference_id=instalment.id
    ).exists():
        logger.info(f"Bonus already exists for instalment {instalment.id}")
        return

    customer = customer_scheme.customer

    # Customer Ledger (UPDATED)
    create_ledger_entry(
        customer=customer,
        customer_scheme=customer_scheme,
        entry_type='CREDIT',
        value_type='CASH',
        amount=amount,
        reference_type='BONUS_INSTALMENT',
        reference_id=instalment.id,
        description=f"Bonus Installment #{instalment.instalment_no}",
    )

    # Accounting Ledger
    AccountingLedger.objects.create(
        account_code='COMPANY_EXPENSE',
        debit=amount,
        credit=Decimal('0.00'),
        reference_type='BONUS_INSTALMENT',
        reference_id=instalment.id,
        description=f'Bonus instalment {instalment.instalment_no}',
        entry_date=timezone.now(),
    )

    AccountingLedger.objects.create(
        account_code='SCHEME_LIABILITY',
        debit=Decimal('0.00'),
        credit=amount,
        reference_type='BONUS_INSTALMENT',
        reference_id=instalment.id,
        description=f'Bonus instalment {instalment.instalment_no}',
        entry_date=timezone.now(),
    )

    logger.info(f"Bonus ledger created for instalment {instalment.id}")


# -------------------------------------------------------
# GOLD LOCK (NEW)
# -------------------------------------------------------

def insert_gold_lock_entry(customer_scheme, gold_grams):

    customer = customer_scheme.customer

    create_ledger_entry(
        customer=customer,
        customer_scheme=customer_scheme,
        entry_type='CREDIT',
        value_type='GOLD',
        gold_grams=gold_grams,
        reference_type='GOLD_LOCK',
        reference_id=customer_scheme.id,
        description="Gold Locking",
    )

    logger.info(f"Gold ledger entry created for {customer_scheme.id}")


# -------------------------------------------------------
# SILVER LOCK (NEW)
# -------------------------------------------------------

def insert_silver_lock_entry(customer_scheme, silver_grams):

    customer = customer_scheme.customer

    create_ledger_entry(
        customer=customer,
        customer_scheme=customer_scheme,
        entry_type='CREDIT',
        value_type='SILVER',
        silver_grams=silver_grams,
        reference_type='SILVER_LOCK',
        reference_id=customer_scheme.id,
        description="Silver Locking",
    )

    logger.info(f"Silver ledger entry created for {customer_scheme.id}")


# -------------------------------------------------------
# FETCH LEDGER
# -------------------------------------------------------

STORE_SCHEME_CODE = 'STORE_CATALOGUE'

CATALOGUE_LEDGER_REF_TYPES = frozenset({
    'CATALOGUE_QUOTE',
    'CATALOGUE_QUOTE_PAYMENT',
    'STORE_JAMA_SETTLEMENT',
    'STORE_ADVANCE',
})


def _ledger_entry_is_store(entry) -> bool:
    if (entry.reference_type or '') in CATALOGUE_LEDGER_REF_TYPES:
        return True
    try:
        return entry.customer_scheme.scheme.scheme_code == STORE_SCHEME_CODE
    except Exception:
        return False


def get_ledger_entries(
    customer_scheme_id=None,
    customer_id=None,
    scheme_id=None,
    entry_type=None,
    date_from=None,
    date_to=None,
    ordering=None,
    gold_lock=None,
    ledger_bucket=None,
):
    """
    ledger_bucket: None | 'all' = everything; 'store' = catalogue POS only; 'scheme' = savings schemes only.
    """
    queryset = CustomerLedger.objects.select_related(
        'customer', 'customer_scheme', 'customer_scheme__scheme'
    ).all()

    if customer_scheme_id:
        queryset = queryset.filter(customer_scheme_id=customer_scheme_id)

    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)

    if scheme_id:
        queryset = queryset.filter(customer_scheme__scheme_id=scheme_id)

    if entry_type:
        queryset = queryset.filter(entry_type=entry_type)

    if date_from:
        from datetime import datetime, time
        # Convert date_from to a datetime at start of day in the server's timezone (Asia/Kolkata)
        # This ensures proper comparison with timezone-aware entry_date values
        date_from_str = date_from.strftime('%Y-%m-%d')
        queryset = queryset.extra(
            where=["DATE(entry_date) >= %s"],
            params=[date_from_str]
        )
    else:
        # If no date_from, get earliest entry
        pass

    if date_to:
        from datetime import datetime, time
        # Convert date_to to a string for SQL comparison
        date_to_str = date_to.strftime('%Y-%m-%d')
        queryset = queryset.extra(
            where=["DATE(entry_date) <= %s"],
            params=[date_to_str]
        )

    if gold_lock is not None:
        if gold_lock:
            queryset = queryset.filter(gold_grams__gt=0)
        else:
            queryset = queryset.filter(gold_grams=0)

    bucket = (ledger_bucket or 'all').lower()
    if bucket == 'store':
        from django.db.models import Q
        queryset = queryset.filter(
            Q(customer_scheme__scheme__scheme_code=STORE_SCHEME_CODE)
            | Q(reference_type__in=CATALOGUE_LEDGER_REF_TYPES)
        )
    elif bucket == 'scheme':
        queryset = queryset.exclude(customer_scheme__scheme__scheme_code=STORE_SCHEME_CODE)

    order_by = ordering if ordering else '-entry_date'

    return queryset.order_by(order_by, '-id')


# -------------------------------------------------------
# BALANCE
# -------------------------------------------------------

def get_current_balance(customer_scheme_id):

    last_entry = CustomerLedger.objects.filter(
        customer_scheme_id=customer_scheme_id
    ).order_by('-id').first()

    return last_entry.running_balance if last_entry else Decimal('0.00')


def get_opening_balance_before_date(customer_id, date_from, ledger_bucket=None):
    """
    Get the opening balance (cash, gold, silver) for a customer before a given date.
    Calculates the balance by summing all entries before date_from.
    Excludes GOLD_LOCK entries from cash balance calculation to avoid double-counting.
    """
    if not date_from:
        return {
            'cash': Decimal('0.00'),
            'gold': Decimal('0.0000'),
            'silver': Decimal('0.0000'),
        }
    
    # Use date-based comparison to get entries strictly before date_from
    # This ensures entries on date_from are NOT included in opening balance
    # Use raw SQL with DATE() function for consistent date extraction
    date_from_str = date_from.strftime('%Y-%m-%d')
    entries_before_date = CustomerLedger.objects.extra(
        where=["DATE(entry_date) < %s"],
        params=[date_from_str]
    ).filter(customer_id=customer_id)

    bucket = (ledger_bucket or 'all').lower()
    if bucket == 'store':
        from django.db.models import Q
        entries_before_date = entries_before_date.filter(
            Q(customer_scheme__scheme__scheme_code=STORE_SCHEME_CODE)
            | Q(reference_type__in=CATALOGUE_LEDGER_REF_TYPES)
        )
    elif bucket == 'scheme':
        entries_before_date = entries_before_date.exclude(
            customer_scheme__scheme__scheme_code=STORE_SCHEME_CODE
        )

    if not entries_before_date.exists():
        return {
            'cash': Decimal('0.00'),
            'gold': Decimal('0.0000'),
            'silver': Decimal('0.0000'),
        }
    
    cash_entries = entries_before_date.filter(value_type='CASH').exclude(reference_type='GOLD_LOCK')
    cash_balance = Decimal('0.00')
    scheme_ids = cash_entries.values_list('customer_scheme_id', flat=True).distinct()
    for scheme_id in scheme_ids:
        scheme_cash = Decimal('0.00')
        for entry in cash_entries.filter(customer_scheme_id=scheme_id).order_by('entry_date', 'id'):
            if _ledger_entry_is_store(entry):
                if entry.entry_type == 'DEBIT':
                    scheme_cash += entry.amount or Decimal('0')
                elif entry.entry_type == 'CREDIT':
                    scheme_cash -= entry.amount or Decimal('0')
            else:
                if entry.entry_type == 'CREDIT':
                    scheme_cash += entry.amount or Decimal('0')
                elif entry.entry_type == 'DEBIT':
                    scheme_cash -= entry.amount or Decimal('0')
        cash_balance += scheme_cash
    
    # Calculate gold balance by summing all GOLD entries
    gold_entries = entries_before_date.filter(value_type='GOLD')
    gold_balance = Decimal('0.0000')
    for entry in gold_entries:
        if entry.entry_type == 'CREDIT':
            gold_balance += entry.gold_grams
        elif entry.entry_type == 'DEBIT':
            gold_balance -= entry.gold_grams
    
    # Calculate silver balance by summing all SILVER entries
    silver_entries = entries_before_date.filter(value_type='SILVER')
    silver_balance = Decimal('0.0000')
    for entry in silver_entries:
        if entry.entry_type == 'CREDIT':
            silver_balance += entry.silver_grams
        elif entry.entry_type == 'DEBIT':
            silver_balance -= entry.silver_grams
    
    return {
        'cash': cash_balance,
        'gold': gold_balance,
        'silver': silver_balance,
    }
