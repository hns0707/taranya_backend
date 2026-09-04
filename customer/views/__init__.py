"""
Package for customer app views.
"""
from .auth_view import RequestOTPView, VerifyOTPView, LogoutView
from .scheme_view import (
    validate_scheme,
    scheme_list,
    customer_enrolled_schemes,
    apply_for_scheme_view,
    customer_scheme_detail,
    customer_scheme_installments,
    customer_metal_rates,
    customer_faq_list,
    scheme_preview,
    customer_cms_page
)