"""
Shared service for payment-related business logic.
Single place for successful payment processing: mark paid, lock gold, update totals, ledger.
Supports CP (Customer Portal) and POS (Store) payments with single or split modes.
"""
from datetime import datetime
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from shared.models import SchemeInstalment, Payment, PaymentCollection, LookupValue, CustomerScheme
from shared.services.gold_service import lock_gold_for_payment
from shared.services.ledger_service import insert_financial_records_for_payment
from shared.services.scheme_service import generate_receipt_no, activate_scheme_on_first_payment

# Non-cash modes require reference_number (ONLINE uses gateway_transaction_id)
NON_CASH_MODES = {'UPI', 'CARD', 'CHEQUE', 'BANK_TRANSFER'}


def _validate_collections(collections_data, total_amount):
    """
    Validate collections: sum must equal total_amount.
    For non-cash modes, reference_number is required.
    """
    if not collections_data:
        raise ValueError("At least one collection is required")
    running = Decimal('0')
    for c in collections_data:
        amt = Decimal(str(c['amount']))
        if amt <= 0:
            raise ValueError(f"Invalid amount {amt} for mode {c.get('payment_mode_code', '?')}")
        mode_code = (c.get('payment_mode_code') or '').upper()
        if mode_code in NON_CASH_MODES and not (c.get('reference_number') or '').strip():
            raise ValueError(f"reference_number required for payment mode {mode_code}")
        running += amt
    if running != total_amount:
        raise ValueError(f"Collections sum {running} does not match total {total_amount}")


@transaction.atomic
def create_payment_with_collections(
    instalment,
    amount,
    transaction_id,
    payment_status_code,
    payment_source='CP',
    payment_mode_code=None,
    gateway_transaction_id=None,
    collections_data=None,
    paid_at=None,
    created_by=None,
    payment_provider='ORANGE_PG',
    upi_execution=None,
):
    """
    Create Payment with receipt_no and PaymentCollection rows.
    - CP single: collections_data=None, payment_mode_code=ONLINE -> one PaymentCollection
    - POS single: collections_data=[{payment_mode_code, amount, reference_number?}]
    - POS split: collections_data=[{...}, {...}]
    """
    receipt_no = generate_receipt_no()
    is_split = False
    payment_mode = None

    if collections_data:
        _validate_collections(collections_data, amount)
        is_split = len(collections_data) > 1
        if not is_split:
            payment_mode = LookupValue.objects.get(
                lookup__code='PAYMENT_MODE',
                code=collections_data[0]['payment_mode_code']
            )
    elif payment_mode_code:
        payment_mode = LookupValue.objects.get(
            lookup__code='PAYMENT_MODE',
            code=payment_mode_code
        )
        collections_data = [{'payment_mode_code': payment_mode_code, 'amount': amount, 'reference_number': None}]
    else:
        raise ValueError("Either payment_mode_code or collections_data is required")

    payment_status = LookupValue.objects.get(
        lookup__code='PAYMENT_STATUS',
        code=payment_status_code
    )

    payment = Payment.objects.create(
        instalment=instalment,
        payment_mode=payment_mode,
        receipt_no=receipt_no,
        payment_source=payment_source,
        is_split_payment=is_split,
        transaction_id=transaction_id,
        gateway_transaction_id=gateway_transaction_id,
        payment_status=payment_status,
        amount=amount,
        paid_at=paid_at or timezone.now(),
        created_by=created_by,
        payment_provider=payment_provider,
        upi_execution=upi_execution,
    )

    for c in collections_data:
        mode = LookupValue.objects.get(
            lookup__code='PAYMENT_MODE',
            code=c['payment_mode_code']
        )
        PaymentCollection.objects.create(
            payment=payment,
            payment_mode=mode,
            amount=Decimal(str(c['amount'])),
            reference_number=(c.get('reference_number') or '').strip() or None,
            created_by=created_by,
        )

    return payment


@transaction.atomic
def process_successful_payment(payment, gold_rate=None, payment_date=None):
    """
    Single unified path for processing a successful payment. Call from both customer
    and admin flows. Idempotent: if payment is already finalized, returns without change.
    Updates payment/instalment status, locks gold (if gold_rate given),
    updates customer_scheme.total_paid, inserts financial records. All in one atomic block.
    
    payment_date: Optional datetime for back-dated payments. If provided, this date will be
    used for ledger entries instead of the current time.
    """
    payment = Payment.objects.select_for_update().get(id=payment.id)
    if getattr(payment, 'is_finalized', False):
        return

    instalment = SchemeInstalment.objects.select_for_update().get(id=payment.instalment_id)
    customer_scheme = instalment.customer_scheme

    SUCCESS = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code='SUCCESS')
    installment_paid = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')

    from datetime import date as datetime_date, time
    payment.payment_status = SUCCESS

    # Handle payment_date - can be date or datetime object
    if payment_date:
        # Check if it's specifically a date object (not datetime)
        if isinstance(payment_date, datetime_date):
            # It's a date object, convert to datetime at noon and make timezone-aware
            dt = datetime.combine(payment_date, time(hour=12))
            payment.paid_at = timezone.make_aware(dt)
        else:
            # It's a datetime, ensure it's timezone-aware
            if timezone.is_naive(payment_date):
                payment.paid_at = timezone.make_aware(payment_date)
            else:
                payment.paid_at = payment_date
    else:
        payment.paid_at = payment.paid_at or timezone.now()

    payment.is_finalized = True
    payment.save(update_fields=['payment_status', 'paid_at', 'is_finalized', 'system_updated_at'])

    instalment.status = installment_paid
    instalment.save(update_fields=['status', 'system_updated_at'])

    # Gold lock: only for schemes with DYNAMIC_LOCK or FIXED_GRAM benefit type
    customer_scheme = instalment.customer_scheme
    scheme = customer_scheme.scheme
    has_gold_lock = customer_scheme.benefits.filter(
        benefit_type__in=["DYNAMIC_LOCK", "FIXED_GRAM"]
    ).exists() or scheme.benefits.filter(
        benefit_type__in=["DYNAMIC_LOCK", "FIXED_GRAM"]
    ).exists()
    
    if gold_rate is not None and has_gold_lock:
        lock_gold_for_payment(payment, instalment, gold_rate)

    customer_scheme.total_paid = (customer_scheme.total_paid or Decimal('0')) + payment.amount
    customer_scheme.save(update_fields=['total_paid', 'system_updated_at'])

    insert_financial_records_for_payment(payment, payment_date=payment_date)

    # Same rule as payment_processor.finalize_payment (customer gateway path): first paid installment
    # while scheme is PENDING -> ACTIVE. Admin/POS uses this function only, so without this hook the
    # scheme stayed PENDING forever despite paid installments.
    cs = CustomerScheme.objects.select_for_update().get(id=instalment.customer_scheme_id)
    scheme_status_code = (cs.scheme_status and cs.scheme_status.code) or ""
    if scheme_status_code == "PENDING" and instalment.instalment_no == 1:
        activate_scheme_on_first_payment(cs, payment.paid_at)

    # Parity with finalize_payment: all tenure installments paid -> COMPLETED
    cs = CustomerScheme.objects.select_for_update().get(id=instalment.customer_scheme_id)
    try:
        scheme = cs.scheme
        paid_status_lv = LookupValue.objects.get(lookup__code="INSTALLMENT_STATUS", code="PAID")
        completed_status_lv = LookupValue.objects.get(lookup__code="SCHEME_STATUS", code="COMPLETED")
        tenure_count = scheme.tenure_months or 0
        paid_count = SchemeInstalment.objects.filter(
            customer_scheme=cs,
            is_bonus=False,
            status=paid_status_lv,
        ).count()
        if tenure_count and paid_count >= tenure_count and cs.scheme_status_id != completed_status_lv.id:
            cs.scheme_status = completed_status_lv
            cs.completed_at = timezone.now()
            cs.save(update_fields=["scheme_status", "completed_at", "system_updated_at"])
            from shared.notification_view import create_admin_notification

            create_admin_notification(
                message=(
                    f"Customer scheme for {cs.customer.full_name} has been completed (all installments paid). "
                    "Bonus will be processed by monthly scheduler."
                ),
                section_code="CRM_SCHEME_COMPLETED",
                title="Scheme completed",
                notification_type="SCHEME_COMPLETED",
                customer_id=cs.customer.id,
            )
            from shared.services.icici_upi_service import maybe_revoke_mandate_when_scheme_completed

            maybe_revoke_mandate_when_scheme_completed(cs.id)
    except LookupValue.DoesNotExist:
        pass


def get_pending_instalments(customer_scheme_id):
    """
    Get all pending instalments for a customer scheme.
    
    Args:
        customer_scheme_id (int): The ID of the customer scheme.
    
    Returns:
        QuerySet: A queryset of all pending instalments for the customer scheme.
    """
    try:
        pending_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
    except LookupValue.DoesNotExist:
        return SchemeInstalment.objects.none()
        
    return SchemeInstalment.objects.filter(customer_scheme_id=customer_scheme_id, status=pending_status)


def calculate_total_paid_amount(customer_scheme_id):
    """
    Calculate the total paid amount for a customer scheme.
    
    Args:
        customer_scheme_id (int): The ID of the customer scheme.
    
    Returns:
        float: The total paid amount.
    """
    # Count payments in SUCCESS or PAID (legacy)
    payments = Payment.objects.filter(
        instalment__customer_scheme_id=customer_scheme_id,
        payment_status__code__in=('SUCCESS', 'PAID', 'SUCCESS'),
    )
    return sum(payment.instalment.amount for payment in payments)


def is_scheme_payment_complete(customer_scheme_id):
    """
    Check if the payment for a customer scheme is complete.
    
    Args:
        customer_scheme_id (int): The ID of the customer scheme.
    
    Returns:
        bool: True if the payment is complete, False otherwise.
    """
    pending_instalments = get_pending_instalments(customer_scheme_id)
    return not pending_instalments.exists()