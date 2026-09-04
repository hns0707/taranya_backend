"""
URLs for internal endpoints (cron, scheduler).
Mount at /internal/ in root urlconf.
"""
from django.urls import path
from .views import internal_views

urlpatterns = [
    path("schemes/process-matured/", internal_views.process_matured_schemes_view),
    path("payments/reconcile/", internal_views.reconcile_payments_view),
    path("upi-mandates/process-dues/", internal_views.process_upi_mandate_dues_view),
    path("schemes/process-ready-to-redeem/", internal_views.process_ready_to_redeem_schemes_view),
]
