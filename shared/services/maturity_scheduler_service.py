"""
Monthly maturity scheduler: processes COMPLETED schemes (bonus + gold lock + MATURED).
Run on 1st of every month. Idempotent per scheme (bonus_processed flag).
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from shared.models import CustomerScheme, LookupValue
from shared.services.scheme_service import calculate_bonus_for_completed_scheme
from shared.services.gold_service import get_lock_rate, calculate_gold_grams
from shared.notification_view import create_admin_notification

logger = logging.getLogger(__name__)


def _bonus_for_scheme(customer_scheme):
    """Centralized bonus for completed scheme (uses total_paid for PERCENTAGE)."""
    return calculate_bonus_for_completed_scheme(customer_scheme)


def lock_gold_at_maturity(customer_scheme, total_amount, gold_rate):
    """
    Add gold equivalent of total_amount (total_paid + bonus) to scheme's locked gold.
    No separate GoldLockingRecord for maturity (payment-linked records only for installment flow).
    """
    if gold_rate is None:
        return
    rate = getattr(gold_rate, "rate_value", gold_rate)
    if rate is None or float(rate) == 0:
        return
    amount = float(total_amount)
    gold_grams = Decimal(str(calculate_gold_grams(amount, gold_rate)))
    if gold_grams <= 0:
        return
    
    # Create GoldLockingRecord for maturity gold instead of updating model fields
    from shared.models import GoldLockingRecord, SchemeInstalment
    
    # Get the final instalment
    final_instalment = SchemeInstalment.objects.filter(
        customer_scheme=customer_scheme,
        is_bonus=False
    ).order_by('-instalment_no').first()
    
    if final_instalment:
        GoldLockingRecord.objects.create(
            customer_scheme=customer_scheme,
            instalment=final_instalment,
            payment=None,  # No payment for maturity gold
            gold_rate=rate,
            gold_grams=gold_grams,
            locked_at=timezone.now(),
            payment_date=timezone.localdate(),
        )
    
    logger.info(f"Scheme {customer_scheme.id}: locked {gold_grams}g gold at maturity.")


@transaction.atomic
def process_one_matured_scheme(customer_scheme):
    """
    Process a single COMPLETED scheme: bonus, gold lock, set MATURED, notification.
    Must be called with customer_scheme already selected (caller holds lock or we lock here).
    Idempotent: if bonus_processed is True, returns without change.
    """
    customer_scheme = CustomerScheme.objects.select_for_update().get(id=customer_scheme.id)
    if customer_scheme.bonus_processed:
        logger.info(f"Scheme {customer_scheme.id} already bonus_processed. Skip.")
        return

    completed_status = LookupValue.objects.get(lookup__code="SCHEME_STATUS", code="COMPLETED")
    if customer_scheme.scheme_status_id != completed_status.id:
        logger.warning(f"Scheme {customer_scheme.id} is not COMPLETED (status id={customer_scheme.scheme_status_id}). Skip.")
        return

    scheme = customer_scheme.scheme
    total_paid = customer_scheme.total_paid or Decimal("0")

    # 1) Calculate bonus
    bonus_amount = _bonus_for_scheme(customer_scheme)
    if bonus_amount is None:
        bonus_amount = Decimal("0")

    # 2) Add bonus to maturity_amount
    customer_scheme.bonus_amount = bonus_amount
    customer_scheme.maturity_amount = total_paid + bonus_amount
    customer_scheme.save(update_fields=["bonus_amount", "maturity_amount", "system_updated_at"])

    # 3) Lock gold based on total_paid + bonus (24K Gold, today else yesterday)
    has_gold = scheme.benefits.filter(benefit_type__in=["DYNAMIC_LOCK", "FIXED_GRAM"]).exists()
    if has_gold:
        rate = get_lock_rate()
        lock_gold_at_maturity(customer_scheme, total_paid + bonus_amount, rate)

    # 4) Update status to MATURED
    matured_status = LookupValue.objects.get(lookup__code="SCHEME_STATUS", code="MATURED")
    customer_scheme.scheme_status = matured_status
    customer_scheme.bonus_processed = True
    customer_scheme.processed_at = timezone.now()
    customer_scheme.save(update_fields=["scheme_status", "bonus_processed", "processed_at", "system_updated_at"])

    # 5) Send maturity notification
    create_admin_notification(
        title="Scheme matured",
        message=f"Scheme for {customer_scheme.customer.full_name} has been matured. Bonus applied, gold locked.",
        section_code="SCHEME_MATURED",
        customer_id=customer_scheme.customer.id,
        notification_type="SCHEME_MATURED",
    )
    logger.info(f"Scheme {customer_scheme.id} processed and set to MATURED. Notification sent.")


def process_matured_schemes():
    """
    Get all COMPLETED schemes with bonus_processed=False and process each in its own transaction.
    Safe for cron: idempotent per scheme; select_for_update prevents double processing.
    """
    completed_status = LookupValue.objects.get(lookup__code="SCHEME_STATUS", code="COMPLETED")
    qs = CustomerScheme.objects.filter(
        scheme_status=completed_status,
        bonus_processed=False,
    ).select_related("scheme", "customer")
    count = 0
    for customer_scheme in qs:
        try:
            process_one_matured_scheme(customer_scheme)
            count += 1
        except Exception as e:
            logger.exception(f"Failed to process scheme {customer_scheme.id}: {e}")
    logger.info(f"process_matured_schemes: processed {count} schemes.")
    return count
