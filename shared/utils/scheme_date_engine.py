"""
Single source of truth for scheme start date, maturity date, and installment schedule.

Business rules:
- If enrollment date <= 15 → start_date = 1st of current month
- If enrollment date > 15 → start_date = 1st of next month
- Each installment: starts on the 1st, due on the last day of that month
- Maturity date = last day of (start_date + total_duration_months - 1)
"""
from datetime import date
import calendar

from dateutil.relativedelta import relativedelta


def _last_day_of_month(d: date) -> date:
    """Return the last day of the month for a given date."""
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def generate_scheme_schedule(today: date, installment_months: int, bonus_months: int):
    """
    Generate start date, maturity date, and installment/bonus due dates.

    Args:
        today: Enrollment or first-payment date (day used for ≤15 vs >15 rule).
        installment_months: Number of installments the customer pays (tenure).
        bonus_months: Number of bonus months (company-funded).

    Returns:
        dict with start_date, maturity_date, installment_dates, bonus_dates.
    """
    if today.day <= 15:
        start_date = today.replace(day=1)
    else:
        start_date = (today.replace(day=1) + relativedelta(months=1))

    total_duration = installment_months + bonus_months

    maturity_date = _last_day_of_month(start_date + relativedelta(months=total_duration - 1))

    installment_dates = []
    for i in range(installment_months):
        month_start = start_date + relativedelta(months=i)
        installment_dates.append(_last_day_of_month(month_start))

    bonus_dates = []
    for i in range(bonus_months):
        month_start = start_date + relativedelta(months=installment_months + i)
        bonus_dates.append(_last_day_of_month(month_start))

    return {
        "start_date": start_date,
        "maturity_date": maturity_date,
        "installment_dates": installment_dates,
        "bonus_dates": bonus_dates,
    }
