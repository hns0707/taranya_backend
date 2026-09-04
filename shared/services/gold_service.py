"""
Gold locking: single flow. Rate is always 24K Gold (today else yesterday) from metal rate service.
No metal_id or purity on scheme; no separate gold rate model.
"""
from decimal import Decimal
from django.db.models import Sum
from django.utils import timezone

from shared.models import GoldLockingRecord, CustomerLedger
from shared.services.metal_rate_service import get_24k_gold_rate_for_lock


def get_lock_rate():
    """
    Rate for locking gold: 24K Gold with flexible date handling (today else nearest previous).
    Returns object with .rate_value or None. For date info, use get_24k_gold_rate_for_lock(return_date_info=True).
    """
    return get_24k_gold_rate_for_lock()


def calculate_gold_grams(amount, gold_rate):
    """
    Gold grams = amount / rate. gold_rate: numeric or object with .rate_value.
    """
    if gold_rate is None:
        return 0.0
    rate = getattr(gold_rate, "rate_value", gold_rate)
    if rate is None or rate == 0:
        return 0.0
    return float(amount) / float(rate)


def lock_gold_for_payment(payment, instalment, gold_rate):
    """
    Lock gold for one payment.
    ALSO creates ledger entry (FIX ADDED).
    """
    try:
        rate = getattr(gold_rate, "rate_value", gold_rate)
        if rate is None or float(rate) <= 0:
            return None

        rate_decimal = Decimal(str(rate))
        amount = payment.amount

        if amount is None or float(amount) <= 0:
            return None

        gold_grams = Decimal(str(calculate_gold_grams(amount, gold_rate)))

        if gold_grams <= 0:
            return None

        # -------------------------------------------------------
        # ✅ PREVENT DUPLICATE GOLD LOCK
        # -------------------------------------------------------
        existing_record = GoldLockingRecord.objects.filter(payment=payment).first()

        if not existing_record:
            # Handle both timezone-aware and naive datetimes
            if payment.paid_at:
                if timezone.is_naive(payment.paid_at):
                    # Already naive, convert to timezone-aware first
                    aware_paid_at = timezone.make_aware(payment.paid_at)
                    payment_date = timezone.localtime(aware_paid_at).date()
                else:
                    payment_date = timezone.localtime(payment.paid_at).date()
            else:
                payment_date = timezone.localdate()

            # -------------------------------------------------------
            # ✅ CREATE GOLD LOCK RECORD
            # -------------------------------------------------------
            GoldLockingRecord.objects.create(
                customer_scheme=instalment.customer_scheme,
                instalment=instalment,
                payment=payment,
                gold_rate=rate_decimal,
                gold_grams=gold_grams,
                locked_at=timezone.now(),
                payment_date=payment_date,
            )

            # -------------------------------------------------------
            # ✅ UPDATE INSTALMENT
            # -------------------------------------------------------
            instalment.gold_grams = gold_grams
            instalment.gold_rate = rate_decimal
            instalment.save(update_fields=["gold_grams", "gold_rate", "system_updated_at"])

            # -------------------------------------------------------
            # ✅ UPDATE CUSTOMER SCHEME - Removed total_locked_gold and accumulated_gold_grams
            # Gold tracking is now done via GoldLockingRecord table
            # -------------------------------------------------------
            customer_scheme = instalment.customer_scheme

            # Save is no longer needed as fields are removed from model
            # Gold tracking is now done via GoldLockingRecord table

        # -------------------------------------------------------
        # NOTE: Ledger entry creation moved to insert_financial_records_for_payment()
        # to properly check benefit_type and avoid duplicate entries
        # -------------------------------------------------------

        print(f"GOLD LOCKED: {gold_grams}g for payment {payment.id}, instalment #{instalment.instalment_no}")

        return gold_grams

    except Exception as e:
        print("Gold locking error:", str(e))
        return None



def calculate_maturity_gold(customer_scheme, gold_rate):
    """
    Total gold at maturity (locked + bonus from benefits). gold_rate: object with .rate_value.
    Now calculates total from GoldLockingRecord table instead of model fields.
    """
    # Get total gold from GoldLockingRecord table
    from shared.models import GoldLockingRecord
    total_gold = GoldLockingRecord.objects.filter(
        customer_scheme=customer_scheme
    ).aggregate(total=Sum('gold_grams'))['total'] or Decimal('0')

    for benefit in customer_scheme.benefits.all():
        if benefit.benefit_type == "FIXED_GRAM" and benefit.benefit_value:
            total_gold += benefit.benefit_value
        elif benefit.benefit_type == "DYNAMIC_LOCK" and benefit.benefit_percentage:
            bonus_grams = total_gold * (benefit.benefit_percentage / 100)
            total_gold += bonus_grams
        elif benefit.benefit_type == "BONUS_MONTHS" and benefit.benefit_months > 0:
            rate_val = getattr(gold_rate, "rate_value", gold_rate)
            if rate_val and float(rate_val) != 0:
                monthly_gold = customer_scheme.monthly_amount / float(rate_val)
                bonus_gold = monthly_gold * benefit.benefit_months
                total_gold += bonus_gold

    return round(total_gold, 4)
