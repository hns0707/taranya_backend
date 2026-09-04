"""
Shared service for scheme-related business logic - scheme lifecycle only.
Uses shared.utils.scheme_date_engine as single source of truth for dates and schedule.
"""
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from shared.models import (
    SchemeMaster,
    CustomerScheme,
    LookupValue,
    CustomerKYC,
    AuditLog,
    SchemeInstalment,
    SchemeBenefit,
    CustomerSchemeBenefit,
    SchemeReferenceCounter,
)
from shared.utils.scheme_date_engine import generate_scheme_schedule
from shared.services.ledger_service import insert_bonus_ledger_entries
from shared.services.gold_service import get_lock_rate


def list_active_schemes():
    """
    List all active schemes.
    
    Returns:
        QuerySet: A queryset of all active schemes.
    """
    return SchemeMaster.objects.filter(is_active=True).prefetch_related('benefits')


def validate_scheme_amount(scheme, monthly_amount):
    """
    Validate if the monthly amount is within the scheme's limits.
    
    Args:
        scheme (SchemeMaster): The scheme to validate against.
        monthly_amount (Decimal): The monthly amount to validate.
    
    Returns:
        bool: True if the amount is valid, False otherwise.
    """
    return scheme.min_instalment <= monthly_amount <= scheme.max_instalment


def calculate_bonus_amount(scheme, monthly_amount):
    """
    Calculate the bonus amount for a scheme.
    
    Args:
        scheme (SchemeMaster): The scheme for which to calculate the bonus.
        monthly_amount (Decimal): The monthly amount.
    
    Returns:
        Decimal: The calculated bonus amount.
    """
    total_bonus = Decimal('0.00')
    
    for benefit in scheme.benefits.all():
        if benefit.benefit_type == 'FLAT' and benefit.benefit_value:
            total_bonus += benefit.benefit_value
        elif benefit.benefit_type == 'PERCENTAGE' and benefit.benefit_percentage:
            total_paid = monthly_amount * scheme.tenure_months
            total_bonus += total_paid * (benefit.benefit_percentage / Decimal('100'))
        elif benefit.benefit_type == 'BONUS_MONTHS' and benefit.benefit_months > 0:
            total_bonus += monthly_amount * benefit.benefit_months
        elif benefit.benefit_type == 'FIXED_GRAM' and benefit.benefit_value:
            # For gold bonuses, we need to convert grams to amount using current gold rate
            # For now, we'll treat it as 0 since we don't have real-time gold rate here
            pass
    
    return total_bonus


def calculate_bonus_for_completed_scheme(customer_scheme):
    """
    Calculate bonus for a completed customer scheme (scheduler).
    Uses actual total_paid for PERCENTAGE; monthly_amount for BONUS_MONTHS and FLAT.
    """
    scheme = customer_scheme.scheme
    monthly_amount = customer_scheme.monthly_amount
    total_paid = customer_scheme.total_paid or Decimal("0")
    total_bonus = Decimal("0.00")

    for benefit in scheme.benefits.all():
        if benefit.benefit_type == "FLAT" and benefit.benefit_value:
            total_bonus += benefit.benefit_value
        elif benefit.benefit_type == "PERCENTAGE" and benefit.benefit_percentage:
            total_bonus += total_paid * (benefit.benefit_percentage / Decimal("100"))
        elif benefit.benefit_type == "BONUS_MONTHS" and benefit.benefit_months and benefit.benefit_months > 0:
            total_bonus += monthly_amount * benefit.benefit_months
    return total_bonus


def calculate_scheme_preview(scheme, monthly_amount):
    """
    Calculate the complete scheme preview including all financial values.
    
    Args:
        scheme (SchemeMaster): The scheme to calculate preview for.
        monthly_amount (Decimal): The monthly amount.
    
    Returns:
        dict: Complete preview object including scheme details, financial calculations, and UI hints.
    """
    # Calculate core financial values
    paid_months = scheme.tenure_months
    
    total_paid = monthly_amount * paid_months
    
    # Calculate bonus amount
    bonus_amount = calculate_bonus_amount(scheme, monthly_amount)
    
    total_redeemable = total_paid + bonus_amount
    
    # Gold rate for preview: 24K Gold, today else yesterday (no scheme metal_id/purity)
    gold_rate = None
    gold_weight = None
    has_gold_benefits = scheme.benefits.filter(benefit_type__in=['FIXED_GRAM', 'DYNAMIC_LOCK']).exists()
    if has_gold_benefits:
        rate_obj = get_lock_rate()
        if rate_obj:
            gold_rate = float(getattr(rate_obj, 'rate_value', rate_obj))
            gold_weight = float(total_redeemable) / gold_rate if gold_rate else None
    
    # Build scheme details
    scheme_details = {
        "id": scheme.id,
        "scheme_code": scheme.scheme_code,
        "name": scheme.scheme_name,
        "tenure_months": scheme.tenure_months,
        "gold_purity": scheme.gold_purity,
        "min_instalment": float(scheme.min_instalment),
        "max_instalment": float(scheme.max_instalment),
        "benefits": [
            {
                "benefit_type": benefit.benefit_type,
                "benefit_value": float(benefit.benefit_value) if benefit.benefit_value else 0.0,
                "benefit_percentage": float(benefit.benefit_percentage) if benefit.benefit_percentage else 0.0,
                "benefit_months": benefit.benefit_months
            } for benefit in scheme.benefits.all()
        ]
    }
    
    # Build preview details
    preview_details = {
        "monthly_amount": float(monthly_amount),
        "total_paid": float(total_paid),
        "bonus_amount": float(bonus_amount),
        "total_redeemable": float(total_redeemable),
        "gold_rate": gold_rate,
        "gold_weight": round(gold_weight, 4) if gold_weight else None
    }
    
    # Build UI hints
    ui_hints = {
        "currency": "INR",
        "gold_unit": "grams",
        "warnings": []
    }
    
    return {
        "scheme": scheme_details,
        "preview": preview_details,
        "ui_hints": ui_hints
    }


def calculate_total_scheme_value(scheme, monthly_amount):
    """
    Calculate the total value of a scheme.
    
    Args:
        scheme (SchemeMaster): The scheme for which to calculate the total value.
        monthly_amount (Decimal): The monthly amount.
    
    Returns:
        Decimal: The calculated total scheme value.
    """
    total_value = monthly_amount * scheme.tenure_months
    bonus_amount = calculate_bonus_amount(scheme, monthly_amount)
    return total_value + bonus_amount


def can_customer_enroll(customer, scheme, max_active_schemes=5):
    """
    Check if a customer can enroll in a scheme.
    Customer can have up to `max_active_schemes` active schemes.
    """
    try:
        active_status = LookupValue.objects.get(
            lookup__code='SCHEME_STATUS',
            code='ACTIVE'
        )
    except LookupValue.DoesNotExist:
        return False

    active_count = CustomerScheme.objects.filter(
        customer=customer,
        scheme_status=active_status
    ).count()
    return active_count < max_active_schemes


def get_customer_active_schemes(customer_id):
    """
    Get ongoing schemes for a customer (enrolled, not abandoned or completed).
    Includes ACTIVE, PENDING, and any other non-terminal status so lists match payments/ledger.
    """
    return (
        CustomerScheme.objects.filter(customer_id=customer_id)
        .exclude(
            scheme_status__code__in=[
                "ABANDONED",
                "COMPLETED",
                "MATURED",
                "REDEEMED",
                "CANCELLED",
            ]
        )
        .select_related("scheme", "scheme_status")
    )


def _get_bonus_months(scheme):
    """Total bonus months from scheme BONUS_MONTHS benefits."""
    return sum(
        b.benefit_months or 0
        for b in scheme.benefits.filter(benefit_type='BONUS_MONTHS')
    )


@transaction.atomic
def apply_for_scheme(customer, scheme, monthly_amount, address='null', start_date=None):
    """
    Apply for a scheme: create CustomerScheme and all installments (customer + bonus).
    Scheme status = PENDING until first payment; first installment = PENDING; total_paid = 0.
    After first payment success, activation sets scheme to ACTIVE and activated_at.
    
    Args:
        start_date: Optional custom start date for backdated enrollment (YYYY-MM-DD string or date object).
                    If provided, installments will be scheduled from this date.
    """
    if not isinstance(monthly_amount, Decimal):
        monthly_amount = Decimal(str(monthly_amount))

    if not validate_scheme_amount(scheme, monthly_amount):
        raise ValueError("Monthly amount is not within the scheme's limits")
    if not can_customer_enroll(customer, scheme):
        raise ValueError("Customer already has an active scheme")

    try:
        pending_scheme_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='PENDING')
        pending_inst_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
        paid_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
    except LookupValue.DoesNotExist:
        raise ValueError("Required status values not found")

    installment_months = scheme.tenure_months
    
    # Handle start_date for backdated enrollment
    if start_date:
        # Parse start_date if it's a string
        if isinstance(start_date, str):
            from datetime import datetime
            start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        # Use the custom start_date for schedule generation
        schedule = generate_scheme_schedule(start_date, installment_months, bonus_months=0)
    else:
        today = timezone.localdate()
        # Only tenure installments at enrollment; no bonus installments (handled by monthly scheduler)
        schedule = generate_scheme_schedule(today, installment_months, bonus_months=0)

    total_payable = monthly_amount * installment_months
    bonus_amount = calculate_bonus_amount(scheme, monthly_amount)
    total_redeemable = total_payable + bonus_amount

    customer_scheme = CustomerScheme.objects.create(
        customer=customer,
        scheme=scheme,
        monthly_amount=monthly_amount,
        scheme_status=pending_scheme_status,
        applied_at=timezone.now(),
        total_paid=Decimal('0'),
        bonus_amount=bonus_amount,
        total_redeemable=total_redeemable,
        tenure_months=scheme.tenure_months,
        total_instalments=installment_months,
        total_payable_amount=total_payable,
        start_date=schedule['start_date'],
        end_date=schedule['maturity_date'],
    )

    for benefit in scheme.benefits.all():
        CustomerSchemeBenefit.objects.create(
            customer_scheme=customer_scheme,
            benefit_type=benefit.benefit_type,
            benefit_value=benefit.benefit_value,
            benefit_percentage=benefit.benefit_percentage,
            benefit_months=benefit.benefit_months,
        )

    first_instalment = _create_installments_from_schedule(
        customer_scheme, schedule, monthly_amount,
        pending_inst_status, paid_status, skip_first=False,
    )

    return customer_scheme, first_instalment


def _create_installments_from_schedule(
    customer_scheme,
    schedule,
    monthly_amount,
    pending_status,
    paid_status,
    *,
    skip_first=False,
):
    """
    Create SchemeInstalment records from schedule. If skip_first is True, only create
    instalment_no 2..tenure and bonus (for legacy path where first already exists).
    """
    installment_months = len(schedule['installment_dates'])
    first_instalment = None

    for i, due_date in enumerate(schedule['installment_dates']):
        if skip_first and i == 0:
            continue
        inst = SchemeInstalment.objects.create(
            customer_scheme=customer_scheme,
            instalment_no=i + 1,
            due_date=due_date,
            amount=monthly_amount,
            is_bonus=False,
            status=pending_status,
            created_by_company=False,
        )
        if first_instalment is None and not skip_first:
            first_instalment = inst

    for i, due_date in enumerate(schedule['bonus_dates']):
        inst = SchemeInstalment.objects.create(
            customer_scheme=customer_scheme,
            instalment_no=installment_months + i + 1,
            due_date=due_date,
            amount=monthly_amount,
            is_bonus=True,
            status=paid_status,
            created_by_company=True,
        )
        insert_bonus_ledger_entries(customer_scheme, inst, monthly_amount)

    return first_instalment


@transaction.atomic
def activate_scheme(customer_scheme, admin_user):
    """
    Activate a PENDING customer scheme (KYC must be approved). Uses scheme_date_engine
    for start/end dates and creates all installments.
    """
    try:
        pending_scheme_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='PENDING')
        active_scheme_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='ACTIVE')
        approved_kyc_status = LookupValue.objects.get(lookup__code='KYC_STATUS', code='APPROVED')
        pending_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
        paid_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
    except LookupValue.DoesNotExist:
        raise ValueError("Required status values not found")

    if customer_scheme.scheme_status != pending_scheme_status:
        raise ValueError("Scheme is not in a pending state")

    kyc = CustomerKYC.objects.filter(customer=customer_scheme.customer).first()
    if not kyc or kyc.status != approved_kyc_status:
        raise ValueError("KYC must be approved to activate the scheme")

    existing_count = SchemeInstalment.objects.filter(customer_scheme=customer_scheme).count()
    if existing_count > 0:
        raise ValueError("Scheme already has installments")

    scheme = customer_scheme.scheme
    installment_months = scheme.tenure_months
    today = timezone.localdate()
    schedule = generate_scheme_schedule(today, installment_months, bonus_months=0)

    customer_scheme.scheme_status = active_scheme_status
    customer_scheme.start_date = schedule['start_date']
    customer_scheme.end_date = schedule['maturity_date']
    customer_scheme.total_instalments = installment_months
    customer_scheme.save(update_fields=['scheme_status', 'start_date', 'end_date', 'total_instalments', 'system_updated_at'])

    AuditLog.objects.create(
        admin=admin_user,
        action='ACTIVATE_SCHEME',
        entity_type='CustomerScheme',
        entity_id=customer_scheme.id,
        old_value={'scheme_status': 'PENDING'},
        new_value={'scheme_status': 'ACTIVE'},
        ip_address='127.0.0.1',
    )

    _create_installments_from_schedule(
        customer_scheme, schedule, customer_scheme.monthly_amount,
        pending_inst, paid_inst, skip_first=False,
    )

    return customer_scheme


def generate_scheme_reference():
    """
    Generate unique scheme reference number in format TS0001, TS0002, etc.
    Uses SchemeReferenceCounter to ensure atomicity.
    """
    next_number = SchemeReferenceCounter.get_next_number()
    return f"TS{next_number:04d}"


def generate_receipt_no():
    """
    Generate unique receipt number in format RCPT000001, RCPT000002, etc.
    Uses ReceiptCounter to ensure atomicity.
    """
    from shared.models import ReceiptCounter
    next_number = ReceiptCounter.get_next_number()
    return f"RCPT{next_number:06d}"


@transaction.atomic
def activate_scheme_on_first_payment(customer_scheme, payment_date=None):
    """
    On first payment success: set scheme_status to ACTIVE and activated_at (runs only once).
    Optionally set start_date/end_date and create installments if not already set (legacy).
    Generates scheme reference number if not already present.
    """
    customer_scheme = CustomerScheme.objects.select_for_update().get(id=customer_scheme.id)
    active_scheme_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='ACTIVE')

    if customer_scheme.scheme_status == active_scheme_status:
        return customer_scheme
    
    # Generate scheme reference if not already present
    if not customer_scheme.scheme_reference:
        customer_scheme.scheme_reference = generate_scheme_reference()

    if customer_scheme.start_date is not None:
        customer_scheme.scheme_status = active_scheme_status
        customer_scheme.activated_at = timezone.now()
        customer_scheme.save(update_fields=['scheme_reference', 'scheme_status', 'activated_at', 'system_updated_at'])
        return customer_scheme

    if payment_date is None:
        payment_date = timezone.localdate()
    elif hasattr(payment_date, 'date'):
        # Handle both naive and timezone-aware datetimes
        if timezone.is_naive(payment_date):
            payment_date = payment_date.date()
        else:
            payment_date = timezone.localtime(payment_date).date()

    scheme = customer_scheme.scheme
    installment_months = scheme.tenure_months
    schedule = generate_scheme_schedule(payment_date, installment_months, bonus_months=0)

    customer_scheme.start_date = schedule['start_date']
    customer_scheme.end_date = schedule['maturity_date']
    customer_scheme.total_instalments = installment_months
    customer_scheme.scheme_status = active_scheme_status
    customer_scheme.activated_at = timezone.now()
    customer_scheme.save(update_fields=['scheme_reference', 'start_date', 'end_date', 'total_instalments', 'scheme_status', 'activated_at', 'system_updated_at'])

    existing_count = SchemeInstalment.objects.filter(customer_scheme=customer_scheme).count()
    if existing_count == 0:
        try:
            pending_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
            paid_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
        except LookupValue.DoesNotExist:
            raise ValueError("Required status values not found")
        _create_installments_from_schedule(
            customer_scheme, schedule, customer_scheme.monthly_amount,
            pending_inst, paid_inst, skip_first=False,
        )
    elif existing_count == 1:
        try:
            pending_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
            paid_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
        except LookupValue.DoesNotExist:
            raise ValueError("Required status values not found")
        _create_installments_from_schedule(
            customer_scheme, schedule, customer_scheme.monthly_amount,
            pending_inst, paid_inst, skip_first=True,
        )

    return customer_scheme
