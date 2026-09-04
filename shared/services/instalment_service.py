"""
Instalment helpers. Installment creation is centralized in scheme_service using scheme_date_engine.
"""
def get_total_bonus_months(customer_scheme):
    """
    Get total bonus months from scheme benefits (BONUS_MONTHS).
    """
    if hasattr(customer_scheme, 'benefits') and customer_scheme.benefits.exists():
        return sum(benefit.benefit_months or 0 for benefit in customer_scheme.benefits.filter(benefit_type='BONUS_MONTHS'))
    return sum(benefit.benefit_months or 0 for benefit in customer_scheme.scheme.benefits.filter(benefit_type='BONUS_MONTHS'))
