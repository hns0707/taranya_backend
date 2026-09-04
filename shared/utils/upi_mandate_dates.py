"""
UPI mandate debit / notification dates aligned with scheme 15-day payment windows.

Business rule:
- anchor_day from first successful payment, else scheme start / applied date
- anchor_day <= 15  -> pay window 1st–15th of due month
- anchor_day > 15   -> pay window 15th–month-end
- autopay debits on anchor day (capped at 28, clamped inside the window)
- notification = debit_date - 1 calendar day
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta
from django.utils import timezone

from shared.models import Payment, SchemeInstalment


def resolve_anchor_day(customer_scheme, *, first_paid_date: date | None = None) -> int:
    """Day-of-month used for 15-day batch and autopay debit."""
    if first_paid_date is not None:
        return first_paid_date.day

    if customer_scheme is None:
        return 1

    first_paid_at = (
        Payment.objects.filter(
            instalment__customer_scheme=customer_scheme,
            payment_status__code='SUCCESS',
        )
        .exclude(paid_at__isnull=True)
        .order_by('paid_at')
        .values_list('paid_at', flat=True)
        .first()
    )
    if first_paid_at:
        return timezone.localtime(first_paid_at).date().day
    if customer_scheme.start_date:
        return customer_scheme.start_date.day
    if customer_scheme.applied_at:
        return timezone.localtime(customer_scheme.applied_at).date().day
    return 1


def resolve_first_paid_date(customer_scheme) -> date | None:
    if customer_scheme is None:
        return None
    first_paid_at = (
        Payment.objects.filter(
            instalment__customer_scheme=customer_scheme,
            payment_status__code='SUCCESS',
        )
        .exclude(paid_at__isnull=True)
        .order_by('paid_at')
        .values_list('paid_at', flat=True)
        .first()
    )
    if first_paid_at:
        return timezone.localtime(first_paid_at).date()
    return None


def compute_debit_day(anchor_day: int) -> int:
    return min(28, max(1, int(anchor_day or 1)))


def _window_bounds(anchor_day: int, year: int, month: int) -> tuple[int, int]:
    month_last = calendar.monthrange(year, month)[1]
    if anchor_day <= 15:
        return 1, min(15, month_last)
    return 15, month_last


def compute_debit_date_for_instalment(
    instalment: SchemeInstalment,
    customer_scheme,
    *,
    debit_day: int | None = None,
    anchor_day: int | None = None,
    first_paid_date: date | None = None,
) -> date:
    """
    Exact calendar date to ExecuteMandate for this instalment.
    Uses the instalment due month with debit_day inside the 15-day window.
    """
    if anchor_day is None:
        anchor_day = resolve_anchor_day(customer_scheme, first_paid_date=first_paid_date)
    if debit_day is None:
        debit_day = compute_debit_day(anchor_day)

    due = instalment.due_date
    win_start, win_end = _window_bounds(anchor_day, due.year, due.month)
    day = max(win_start, min(debit_day, win_end))
    return date(due.year, due.month, day)


def notification_date_for_debit(debit_date: date) -> date:
    return debit_date - timedelta(days=1)


def debit_dates_for_instalment(
    instalment: SchemeInstalment,
    customer_scheme,
    *,
    debit_day: int | None = None,
) -> tuple[date, date]:
    """Return (notification_date, debit_date)."""
    first_paid = resolve_first_paid_date(customer_scheme)
    anchor = resolve_anchor_day(customer_scheme, first_paid_date=first_paid)
    debit_date = compute_debit_date_for_instalment(
        instalment,
        customer_scheme,
        debit_day=debit_day,
        anchor_day=anchor,
        first_paid_date=first_paid,
    )
    return notification_date_for_debit(debit_date), debit_date
