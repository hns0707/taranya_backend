"""
Timezone utilities for India (Asia/Kolkata) business logic.
Use these when business logic depends on "today" or "date of a datetime" in IST.
DB continues to store UTC; convert only when reading for business rules.
"""
from django.utils import timezone


def get_ist_date(dt):
    """
    Return the date in Asia/Kolkata for a given datetime (e.g. from DB).
    Use for gold rate lookup, payment_date, and any business logic that needs India date.
    """
    if dt is None:
        return None
    return timezone.localtime(dt).date()
