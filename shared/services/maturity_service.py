"""
Scheme maturity and bonus calculation service.
Handles maturity value calculation and bonus application.
"""
from django.utils import timezone
from shared.models import CustomerScheme, CustomerSchemeBenefit
from shared.services.gold_service import get_lock_rate, calculate_maturity_gold
from decimal import Decimal


def calculate_maturity_value(customer_scheme):
    """
    Calculate maturity value for a customer scheme.
    
    Args:
        customer_scheme: CustomerScheme instance
        
    Returns:
        dict with total value, cash amount, gold grams, and gold rate
    """
    # Check if scheme has gold benefits
    has_gold_benefits = customer_scheme.benefits.filter(benefit_type__in=['FIXED_GRAM', 'DYNAMIC_LOCK']).exists()
    
    if has_gold_benefits:
        return calculate_gold_maturity(customer_scheme)
    else:
        return calculate_cash_maturity(customer_scheme)


def calculate_cash_maturity(customer_scheme):
    """
    Calculate maturity for cash-based schemes.
    """
    maturity_amount = customer_scheme.total_payable_amount
    
    # Apply cash bonuses from CustomerSchemeBenefit
    for benefit in customer_scheme.benefits.all():
        if benefit.benefit_type == 'FLAT' and benefit.benefit_value:
            maturity_amount += benefit.benefit_value
        elif benefit.benefit_type == 'PERCENTAGE' and benefit.benefit_percentage:
            bonus_amount = customer_scheme.total_payable_amount * (benefit.benefit_percentage / Decimal('100'))
            maturity_amount += bonus_amount
        elif benefit.benefit_type == 'BONUS_MONTHS' and benefit.benefit_months > 0:
            bonus_amount = customer_scheme.monthly_amount * benefit.benefit_months
            maturity_amount += bonus_amount
        
    return {
        'total_value': round(maturity_amount, 2),
        'cash_amount': round(maturity_amount, 2),
        'gold_grams': 0.0000,
        'gold_rate': None,
        'gold_rate_value': None
    }


def calculate_gold_maturity(customer_scheme):
    """
    Calculate maturity for gold-linked schemes. Rate: 24K Gold, today else yesterday.
    """
    current_gold_rate = get_lock_rate()
    total_gold_grams = calculate_maturity_gold(customer_scheme, current_gold_rate)

    for benefit in customer_scheme.benefits.all():
        if benefit.benefit_type == 'FIXED_GRAM' and benefit.benefit_value:
            total_gold_grams += benefit.benefit_value
        elif benefit.benefit_type == 'PERCENTAGE' and benefit.benefit_percentage:
            bonus_grams = total_gold_grams * (benefit.benefit_percentage / Decimal('100'))
            total_gold_grams += bonus_grams

    rate_val = getattr(current_gold_rate, 'rate_value', current_gold_rate) if current_gold_rate else None
    gold_value = (total_gold_grams * float(rate_val)) if rate_val else Decimal('0')

    return {
        'total_value': round(gold_value, 2),
        'cash_amount': 0.00,
        'gold_grams': round(total_gold_grams, 4),
        'gold_rate': current_gold_rate,
        'gold_rate_value': round(float(rate_val), 2) if rate_val else None
    }


def calculate_hybrid_maturity(customer_scheme):
    """
    Calculate maturity for hybrid schemes (cash + gold). Rate: 24K Gold, today else yesterday.
    """
    current_gold_rate = get_lock_rate()
    
    # Calculate cash amount
    cash_amount = customer_scheme.total_payable_amount
    
    # Apply cash bonuses
    for benefit in customer_scheme.benefits.all():
        if benefit.benefit_type == 'FLAT' and benefit.benefit_value:
            cash_amount += benefit.benefit_value
        elif benefit.benefit_type == 'PERCENTAGE' and benefit.benefit_percentage:
            bonus_amount = customer_scheme.total_payable_amount * (benefit.benefit_percentage / Decimal('100'))
            cash_amount += bonus_amount
        elif benefit.benefit_type == 'BONUS_MONTHS' and benefit.benefit_months > 0:
            bonus_amount = customer_scheme.monthly_amount * benefit.benefit_months
            cash_amount += bonus_amount
        
    # Calculate gold amount
    gold_grams = calculate_maturity_gold(customer_scheme, current_gold_rate)
    
    # Apply gold bonuses
    for benefit in customer_scheme.benefits.all():
        if benefit.benefit_type == 'FIXED_GRAM' and benefit.benefit_value:
            gold_grams += benefit.benefit_value
        elif benefit.benefit_type == 'PERCENTAGE' and benefit.benefit_percentage:
            bonus_grams = gold_grams * (benefit.benefit_percentage / Decimal('100'))
            gold_grams += bonus_grams
    
    rate_val = getattr(current_gold_rate, 'rate_value', current_gold_rate) if current_gold_rate else None
    gold_value = (gold_grams * float(rate_val)) if rate_val else Decimal('0')

    return {
        'total_value': round(cash_amount + gold_value, 2),
        'cash_amount': round(cash_amount, 2),
        'gold_grams': round(gold_grams, 4),
        'gold_rate': current_gold_rate,
        'gold_rate_value': round(float(rate_val), 2) if rate_val else None
    }


def apply_bonus_at_maturity(customer_scheme):
    """
    Apply bonus at scheme maturity.
    """
    maturity_value = calculate_maturity_value(customer_scheme)

    # Update customer scheme with maturity values (maturity_gold_rate is DecimalField)
    customer_scheme.maturity_amount = maturity_value['total_value']
    customer_scheme.maturity_gold_grams = maturity_value['gold_grams']
    customer_scheme.maturity_gold_rate = maturity_value.get('gold_rate_value')
    customer_scheme.save()