"""
Package for master app views.
"""
# Scheme views
from .scheme_view import (
    list_schemes,
    create_scheme,
    create_customer_scheme,
    customer_scheme_instalments,
    process_instalment_payment,
    admin_payment_initiation,
    payment_status
)

# CMS views
from .cms_view import CMSPageListCreateView, CMSPageRetrieveUpdateDestroyView

# FAQ views
from .faq_view import FAQListCreateView, FAQRetrieveUpdateDestroyView

# Admin user views
from .admin_user_view import AdminUserListCreateView, AdminUserRetrieveUpdateDestroyView

# CRM view modules (re-exported so `from .views import crm_*` works in urls.py)
from . import crm_communication_view
from . import crm_visit_view
from . import crm_customer_360_view
from . import crm_insights_view
from . import crm_prospect_view
from . import crm_service_ticket_view
from . import crm_store_contact_view
