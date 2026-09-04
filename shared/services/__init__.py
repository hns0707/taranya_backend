"""
Package for shared services.
"""
from .gold_service import get_lock_rate
from .scheme_service import (
    list_active_schemes,
    validate_scheme_amount,
    calculate_bonus_amount,
    calculate_total_scheme_value,
    can_customer_enroll,
    get_customer_active_schemes,
)
from .content_service import (
    get_active_faqs,
    get_cms_page,
)
from .payment_service import (
    get_pending_instalments,
    calculate_total_paid_amount,
    is_scheme_payment_complete,
)
from .kyc_service import (
    approve_kyc,
    reject_kyc,
)

__all__ = [
    "get_lock_rate",
    "list_active_schemes",
    "validate_scheme_amount",
    "calculate_bonus_amount",
    "calculate_total_scheme_value",
    "can_customer_enroll",
    "get_customer_active_schemes",
    "get_active_faqs",
    "get_cms_page",
    "get_pending_instalments",
    "calculate_total_paid_amount",
    "is_scheme_payment_complete",
    "approve_kyc",
    "reject_kyc",
]