"""
URL configuration for the master app (Admin/Backoffice).
"""
from django.urls import path
from .views import scheme_view, cms_view, faq_view, admin_user_view, customer_view, customer_policy_acceptance_view, approval_views, ledger_views, permission_views, customer_address_view, lookup_views, department_views, accounts_views, hsn_view, pos_view, catalogue_quote_view, store_account_view, product_code_prefix_view, pattern_code_registry_view
from .views import crm_communication_view, crm_visit_view, crm_customer_360_view, crm_insights_view, crm_prospect_view, crm_service_ticket_view, crm_store_contact_view
from shared import notification_view
from .views.scheme_variation_view import (
    list_schemes,
    create_scheme,
    get_scheme,
    update_scheme,
    delete_scheme,
    get_customer_scheme_maturity,
    apply_maturity_bonus
)
from .views import metal_view, metal_rule_view, metal_rate_view, vendors_view, stone_view, category_view, model_views, tag_template_view
from .views.purchase_order_view import (
    purchase_order_list_create,
    purchase_order_detail,
    purchase_order_product_prefill,
)
from .views.grn_batch_view import (
    grn_batch_list_create,
    grn_batch_detail,
)
from .views.grn_lot_view import (
    grn_lot_detail,
    grn_lot_list_create,
    grn_lot_vendor_terms,
)
from .views.reverse_grn_view import (
    reverse_grn_bag,
    reverse_grn_bag_lookup,
    reverse_grn_lot,
    reverse_grn_lot_lookup,
)
from .views.make_bag_view import (
    make_bag_lots_sidebar,
    make_bag_next_bag_no,
    make_bag_search_items,
    make_bag_catalog_items,
    make_bag_catalog_facets,
    make_bag_save,
)
from .views.stock_view import (
    stock_in,
    stock_out,
    stock_list,
    stock_transactions,
    stock_item_provenance,
)
from .views.barcode_view import (
    barcode_bag_list,
    barcode_bag_detail,
    barcode_generate,
    barcode_list,
    barcode_fg_dashboard,
    barcode_mark_printed,
    barcode_deactivate,
    barcode_update_tag,
    barcode_tag_photos,
    barcode_tag_photo_delete,
)
from .views.tag_attribute_view import (
    tag_attribute_definitions_list,
    tag_mapping_tag_list,
    tag_mapping_detail,
    tag_mapping_save,
)
from .views.bulk_image_import_view import (
    bulk_image_match,
    bulk_image_import,
)
from shared.services.scale import scale_machines_list
from .views.product_views import (
    create_draft,
    save_step_data,
    upload_media,
    publish_draft,
    list_drafts,
    get_draft,
    list_products_hierarchy,
    search_product_items,

    get_product_item,
    upload_item_media,
    update_product_item,
    vendor_previous_bom,
)

from .views.customer_nominee_view import AdminSchemeNomineeListCreateView, AdminNomineeRetrieveUpdateDestroyView
from .views.department_views import (
    DepartmentCreateView,
    DepartmentUpdateView,
    DepartmentListView,
    DepartmentDetailView
)

urlpatterns = [
    # Admin authentication
    path('login/', admin_user_view.AdminLoginView.as_view(), name='admin-login'),
    path('get-user-notifications/', notification_view.get_user_notifications, name='get-user-notifications'),
        
    # Scheme configuration with variations
    path('schemes/', list_schemes, name='scheme-variation-list'),
    path('schemes/create/', create_scheme, name='scheme-variation-create'),
    # Specific scheme paths must precede schemes/<int:pk>/ (avoids resolver edge cases).
    path('schemes/pending-finance/', scheme_view.pending_finance, name='pending-finance'),
    path('schemes/dashboard/summary/', scheme_view.scheme_dashboard_summary, name='scheme-dashboard-summary'),
    path('schemes/recent-activity/', scheme_view.scheme_recent_activity, name='scheme-recent-activity'),
    path('schemes/recent-enrollments/', scheme_view.scheme_recent_enrollments, name='scheme-recent-enrollments'),
    path('schemes/active/', scheme_view.active_schemes, name='active-schemes'),
    path('schemes/missed/', scheme_view.missed_schemes, name='missed-schemes'),
    path('schemes/completed/', scheme_view.completed_schemes, name='completed-schemes'),
    path('schemes/<int:pk>/', get_scheme, name='scheme-variation-detail'),
    path('schemes/<int:pk>/update/', update_scheme, name='scheme-variation-update'),
    path('schemes/<int:pk>/delete/', delete_scheme, name='scheme-variation-delete'),
    
    # Customer scheme maturity
    path('customer-schemes/<int:pk>/maturity/', get_customer_scheme_maturity, name='customer-scheme-maturity'),
    path('customer-schemes/<int:pk>/apply-bonus/', apply_maturity_bonus, name='apply-maturity-bonus'),
    
    # Metal rates (24K base priority; used for locking amount)
    path('metal-rates/', metal_rate_view.metal_rate_list),
    path('metal-rates/<int:pk>/', metal_rate_view.update_metal_rate),
    path('metal-rates/create-or-update/', metal_rate_view.create_or_update_metal_rate),
    path('metal-rates/branch-rates/', metal_rate_view.branch_rates_list),
    path('metal-rates/branch-rates/<int:branch_id>/', metal_rate_view.branch_rates_detail),
    # Store metal rates: master + branch override merged (GET ?branch_id=&metal_id=&date=)
    path('store/metal-rates/', metal_rate_view.store_metal_rates),

    # CMS pages
    path('cms-pages/', cms_view.CMSPageListCreateView.as_view(), name='cms-page-list'),
    path('cms-pages/<int:pk>/', cms_view.CMSPageRetrieveUpdateDestroyView.as_view(), name='cms-page-detail'),
    
    # FAQs
    path('faqs/', faq_view.FAQListCreateView.as_view(), name='faq-list'),
    path('faqs/<int:pk>/', faq_view.FAQRetrieveUpdateDestroyView.as_view(), name='faq-detail'),
    
    # Admin users, roles & permissions
    path('admin-users/', admin_user_view.AdminUserListCreateView.as_view(), name='admin-user-list'),
    path('admin-users/<int:pk>/', admin_user_view.AdminUserRetrieveUpdateDestroyView.as_view(), name='admin-user-detail'),
    
    # Customers
    path('customers/', customer_view.customer_list_create, name='customer-list'),
    path('customers/segments/', customer_view.customer_segment_list, name='customer-segment-list'),
    path('customers/<int:pk>/',customer_view.customer_detail,name='customer-detail'),
    path('customers/<int:pk>/crm-360/', crm_customer_360_view.customer_crm_360, name='customer-crm-360'),
    path('customers/by-mobile/<str:mobile>/',customer_view.customer_by_mobile,name='customer-by-mobile'),
    path('customers/by-code/<str:customer_code>/',customer_view.customer_by_code,name='customer-by-code'),
    path('customers/<int:pk>/active-schemes/', customer_view.customer_active_schemes, name='customer-active-schemes'),
    path('customers/<int:pk>/payments/', customer_view.customer_payments, name='customer-payments'),
    path('customer-schemes/<int:customer_scheme_id>/details/', customer_view.customer_scheme_details, name='customer-scheme-details'),
    path('customer-scheme/<int:customer_scheme_id>/abandon/', scheme_view.abandon_customer_scheme, name='customer-scheme-abandon'),

    # Customer addresses (admin)
    path('customer-address/<int:customer_id>/', customer_address_view.admin_customer_address, name='admin_customer_address'),

    # Catalogue quotations (store assisted selling) — specific paths before detail
    path('catalogue/quotes/active-visit/', catalogue_quote_view.catalogue_quote_active_visit, name='catalogue-quote-active-visit'),
    path('catalogue/quotes/removal-requests/', catalogue_quote_view.catalogue_quote_removal_requests_list, name='catalogue-quote-removal-requests'),
    path('catalogue/quotes/removal-requests/<int:request_id>/approve/', catalogue_quote_view.catalogue_quote_removal_request_approve, name='catalogue-quote-removal-approve'),
    path('catalogue/quotes/removal-requests/<int:request_id>/reject/', catalogue_quote_view.catalogue_quote_removal_request_reject, name='catalogue-quote-removal-reject'),
    path('catalogue/quotes/', catalogue_quote_view.catalogue_quote_list_create, name='catalogue-quote-list'),
    path('catalogue/quotes/<str:quote_id>/status/', catalogue_quote_view.catalogue_quote_status, name='catalogue-quote-status'),
    path('catalogue/quotes/<str:quote_id>/pdf/', catalogue_quote_view.catalogue_quote_pdf, name='catalogue-quote-pdf'),
    path('catalogue/quotes/<str:quote_id>/duplicate/', catalogue_quote_view.catalogue_quote_duplicate, name='catalogue-quote-duplicate'),
    path('catalogue/quotes/<str:quote_id>/join/', catalogue_quote_view.catalogue_quote_join, name='catalogue-quote-join'),
    path('catalogue/quotes/<str:quote_id>/contributors/', catalogue_quote_view.catalogue_quote_contributors, name='catalogue-quote-contributors'),
    path('catalogue/quotes/<str:quote_id>/changes/', catalogue_quote_view.catalogue_quote_changes, name='catalogue-quote-changes'),
    path('catalogue/quotes/<str:quote_id>/discount-approvals/', catalogue_quote_view.catalogue_quote_discount_approvals_list, name='catalogue-quote-discount-approvals'),
    path('catalogue/quotes/<str:quote_id>/discount-approvals/<int:approval_id>/approve/', catalogue_quote_view.catalogue_quote_discount_approval_approve, name='catalogue-quote-discount-approve'),
    path('catalogue/quotes/<str:quote_id>/discount-approvals/<int:approval_id>/reject/', catalogue_quote_view.catalogue_quote_discount_approval_reject, name='catalogue-quote-discount-reject'),
    path('catalogue/quotes/<str:quote_id>/', catalogue_quote_view.catalogue_quote_detail, name='catalogue-quote-detail'),

    # Customer nominees (admin)
    path('customers/<int:customer_id>/schemes/<int:scheme_id>/nominees/', AdminSchemeNomineeListCreateView.as_view(), name='admin-scheme-nominees'),
    path('customer-nominees/<int:pk>/', AdminNomineeRetrieveUpdateDestroyView.as_view(), name='admin-nominee-detail'),
    

    # Customer policy acceptances
    path('customer-policy-acceptances/', customer_policy_acceptance_view.CustomerPolicyAcceptanceListCreateView.as_view(), name='customer-policy-acceptance-list'),
    path('customer-policy-acceptances/<int:pk>/', customer_policy_acceptance_view.CustomerPolicyAcceptanceRetrieveUpdateDestroyView.as_view(), name='customer-policy-acceptance-detail'),

    
    # Customer KYC Approval (new endpoints)
    path('customers/pending-kyc/', approval_views.PendingKYCCustomersListView.as_view(), name='pending-kyc-customers'),
    path('customers/<int:customer_id>/kyc/approve/', approval_views.CustomerKYCApprovalView.as_view(), name='customer-kyc-approve'),
    
    # Payment Approval (new endpoints)
    path('payments/<int:payment_id>/approve/', approval_views.installment_payment_approval, name='payment-approval'),

    # Scheme enrollment
    path('scheme-enrollments/', scheme_view.create_customer_scheme, name='admin-customer-enroll'),
    path('customer-schemes/<int:scheme_id>/instalments/', scheme_view.customer_scheme_instalments, name='customer-scheme-instalments'),
    
    # Payments
    path('payment-modes/', scheme_view.payment_modes_list, name='payment-modes-list'),
    path('instalments/<int:instalment_id>/pay/', scheme_view.admin_payment_initiation, name='admin-payment-initiate'),
    path('payments/<int:payment_id>/status/', scheme_view.payment_status, name='payment-status'),
    path('payments/<int:payment_id>/verification-details/', scheme_view.payment_verification_details, name='payment-verification-details'),
    path('collections/summary/', scheme_view.collection_summary, name='collection-summary'),
    path('collections/payment-mode-summary/', scheme_view.payment_mode_collection_summary, name='payment-mode-collection-summary'),
    path('installments/upcoming-reminders/', scheme_view.upcoming_installment_reminders, name='upcoming-reminders'),
    path('installments/past-due/', scheme_view.past_due_installments, name='past-due-installments'),
    path('installments/', scheme_view.installment_records, name='installment-records'),
    path('instalments/<int:instalment_id>/pay/', scheme_view.admin_payment_initiation, name='admin-payment-initiate'),
    path('payments/<int:payment_id>/status/', scheme_view.payment_status, name='payment-status'),
    path('instalments/<int:instalment_id>/process-payment/', scheme_view.process_instalment_payment, name='process-instalment-payment'),
    
    # Scheme management
    path('schemes/', scheme_view.list_schemes, name='scheme-list'),
    path('schemes/create/', scheme_view.create_scheme, name='scheme-create'),

    # Ledger
    path('ledger/', ledger_views.LedgerListView.as_view(), name='ledger-list'),
    path('ledger/customer/<int:pk>/', ledger_views.CustomerLedgerListView.as_view(), name='customer-ledger'),
    path('ledger/customer/<int:pk>/export/', ledger_views.CustomerLedgerExportView.as_view(), name='customer-ledger-export'),
    path('ledger/scheme/<int:pk>/', ledger_views.SchemeLedgerListView.as_view(), name='scheme-ledger'),
    path('customer/<int:pk>/ledger/', ledger_views.CustomerLedgerListView.as_view(), name='customer-ledger-by-id'),
    path('customer/<int:pk>/store-balance/', store_account_view.customer_store_balance, name='customer-store-balance'),
    path('customer/<int:pk>/store-advance/', store_account_view.customer_store_advance, name='customer-store-advance'),
    path('customer/<int:pk>/scheme-redeem-options/', store_account_view.customer_scheme_redeem_options, name='customer-scheme-redeem-options'),
    path('customers/<int:pk>/store-balance/', store_account_view.customer_store_balance, name='customers-store-balance'),
    path('customers/<int:pk>/store-advance/', store_account_view.customer_store_advance, name='customers-store-advance'),
    path('customers/<int:pk>/scheme-redeem-options/', store_account_view.customer_scheme_redeem_options, name='customers-scheme-redeem-options'),

    # Accounts (Collections & Ledger)
    path('accounts/transactions/', accounts_views.AccountsTransactionListView.as_view(), name='accounts-transactions'),
    path('accounts/ledger/', accounts_views.AccountsLedgerListView.as_view(), name='accounts-ledger'),
    path('accounts/daily-book/', accounts_views.accounts_daily_book, name='accounts-daily-book'),
    path('accounts/daily-book/opening/', accounts_views.accounts_daily_book_opening, name='accounts-daily-book-opening'),
    path('accounts/daily-book/entries/', accounts_views.accounts_daily_book_entry_create, name='accounts-daily-book-entry-create'),
    path('accounts/daily-book/entries/<int:pk>/', accounts_views.accounts_daily_book_entry_detail, name='accounts-daily-book-entry-detail'),
    path('accounts/daily-book/print/', accounts_views.accounts_daily_book_print, name='accounts-daily-book-print'),
    # Jewellery tag print (hardcoded layout in shared.services.tag_print)
    path('tag-templates/render/<int:tag_id>/', tag_template_view.render_tag_view, name='tag-template-render'),

    path('pos/create-invoice/', pos_view.create_invoice, name='pos-create-invoice'),
    # Preview next POS invoice number (mPOS header)
    path('pos/next-invoice-number/', pos_view.next_invoice_number, name='pos-next-invoice-number'),
    path('pos/invoices/', pos_view.list_invoices, name='pos-invoice-list'),
    path('pos/invoices/export/', pos_view.export_invoices_csv, name='pos-invoice-export'),
    path('pos/invoice/<int:pk>/', pos_view.get_invoice, name='pos-invoice-detail'),
    path('pos/invoice/<int:pk>/update/', pos_view.update_invoice, name='pos-invoice-update'),
    path('pos/invoice/<int:pk>/delete/', pos_view.delete_invoice, name='pos-invoice-delete'),
    path('pos/invoice/<int:pk>/pdf/', pos_view.invoice_pdf, name='pos-invoice-pdf'),
    path('api/pos/create-invoice/', pos_view.create_invoice, name='api-pos-create-invoice'),
    path('api/pos/next-invoice-number/', pos_view.next_invoice_number, name='api-pos-next-invoice-number'),
    path('api/pos/invoices/', pos_view.list_invoices, name='api-pos-invoice-list'),
    path('api/pos/invoices/export/', pos_view.export_invoices_csv, name='api-pos-invoice-export'),
    path('api/pos/invoice/<int:pk>/', pos_view.get_invoice, name='api-pos-invoice-detail'),
    path('api/pos/invoice/<int:pk>/update/', pos_view.update_invoice, name='api-pos-invoice-update'),
    path('api/pos/invoice/<int:pk>/delete/', pos_view.delete_invoice, name='api-pos-invoice-delete'),
    path('api/pos/invoice/<int:pk>/pdf/', pos_view.invoice_pdf, name='api-pos-invoice-pdf'),
    path('pos/invoice/<int:pk>/send-whatsapp/', crm_communication_view.send_invoice_whatsapp, name='pos-invoice-send-whatsapp'),
    path('api/pos/invoice/<int:pk>/send-whatsapp/', crm_communication_view.send_invoice_whatsapp, name='api-pos-invoice-send-whatsapp'),

    # CRM Communication / Reminders & Marketing
    path('crm/reminders/send-whatsapp/', crm_communication_view.send_whatsapp_reminder, name='crm-send-whatsapp-reminder'),
    path('crm/reminders/send-sms/', crm_communication_view.send_sms_reminder, name='crm-send-sms-reminder'),
    path('crm/reminders/send-udhar-sms/', crm_communication_view.send_udhar_sms_reminder, name='crm-send-udhar-sms'),
    path('crm/reminders/send-gold-rate-sms/', crm_communication_view.send_gold_rate_sms, name='crm-send-gold-rate-sms'),
    path('crm/reminders/send-offer-whatsapp/', crm_communication_view.send_offer_whatsapp, name='crm-send-offer-whatsapp'),
    path('crm/reminders/log-call/', crm_communication_view.log_call_reminder, name='crm-log-call-reminder'),
    path('crm/reminders/scheduled/', crm_communication_view.scheduled_reminders, name='crm-scheduled-reminders'),
    path('crm/reminders/scheduled/<int:pk>/cancel/', crm_communication_view.cancel_scheduled_reminder, name='crm-cancel-scheduled-reminder'),
    path('crm/reminders/process-scheduled/', crm_communication_view.process_scheduled_reminders, name='crm-process-scheduled-reminders'),
    path('crm/communication-logs/', crm_communication_view.communication_log_list, name='crm-communication-log-list'),
    path('crm/communication-logs/analytics/', crm_communication_view.communication_analytics, name='crm-communication-analytics'),

    # CRM Visit Tracking / Dashboard
    path('crm/visits/dashboard/', crm_visit_view.crm_visit_dashboard, name='crm-visit-dashboard'),
    path('crm/visits/', crm_visit_view.crm_visit_list, name='crm-visit-list'),
    path('crm/visits/<int:pk>/', crm_visit_view.crm_visit_update, name='crm-visit-update'),
    path('crm/insights/wishlist-trends/', crm_insights_view.crm_wishlist_trends, name='crm-wishlist-trends'),
    path('crm/insights/customer-demographics/', crm_insights_view.crm_customer_demographics, name='crm-customer-demographics'),
    path('crm/service-tickets/', crm_service_ticket_view.service_ticket_list_create, name='crm-service-tickets'),
    path('crm/service-tickets/<int:pk>/', crm_service_ticket_view.service_ticket_update, name='crm-service-ticket-update'),
    path('crm/store-contacts/', crm_store_contact_view.store_contact_list_create, name='crm-store-contacts'),
    path('crm/store-contacts/reasons/', crm_store_contact_view.store_contact_options, name='crm-store-contact-options'),
    path('crm/prospects/check/', crm_prospect_view.crm_prospect_check, name='crm-prospect-check'),
    path('crm/prospects/summary/', crm_prospect_view.crm_prospect_summary, name='crm-prospect-summary'),
    path('crm/prospects/', crm_prospect_view.crm_prospect_list_create, name='crm-prospect-list'),

    # Roles & Permissions
    path('roles/', permission_views.RoleListCreateView.as_view(), name='role-list'),
    path('roles/<int:pk>/', permission_views.RoleRetrieveUpdateDestroyView.as_view(), name='role-detail'),
    path('departments/<str:department_ids>/roles/', permission_views.RolesByDepartmentView.as_view(), name='roles-by-department'),
    path('permissions/', permission_views.PermissionListCreateView.as_view(), name='permission-list'),

    
    # Lookup Management
    path('lookups/bulk/', lookup_views.ApiBulkLookupValueView.as_view(), name='api-lookup-bulk'),
    path('lookups/', lookup_views.LookupListView.as_view(), name='lookup-list'),
    path('lookups/<str:lookup_code>/', lookup_views.LookupDetailView.as_view(), name='lookup-detail'),
    path('lookups/<str:lookup_code>/values/', lookup_views.LookupValueListView.as_view(), name='lookup-values-list'),
    path('lookups/<str:lookup_code>/values/create/', lookup_views.LookupValueCreateView.as_view(), name='lookup-value-create'),
    path('lookups/<str:lookup_code>/values/<str:value_code>/', lookup_views.LookupValueUpdateView.as_view(), name='lookup-value-update'),
    
    # Department Management
    path('departments/', DepartmentListView.as_view(), name='department-list'),
    path('departments/create/', DepartmentCreateView.as_view(), name='department-create'),
    path('departments/<int:pk>/', DepartmentDetailView.as_view(), name='department-detail'),
    path('departments/<int:pk>/update/', DepartmentUpdateView.as_view(), name='department-update'),

    # METAL CRUD
    path("metals/", metal_view.list_metals),
    path("metal/create/", metal_view.create_metal),
    path("metal/<int:metal_id>/", metal_view.get_metal),
    path("metal/<int:metal_id>/update/", metal_view.update_metal),
    path("metal/<int:metal_id>/delete/", metal_view.delete_metal),

    # Branches (Store section)
    path("branches/", metal_view.list_branches),

    # Branch–metal mapping (Store: metal availability per branch)
    path("branch-metals/", metal_view.list_branch_metals),
    path("branch-metal/toggle/", metal_view.create_or_toggle_branch_metal),
    path("branch-metal/<int:branch_metal_id>/update/", metal_view.update_branch_metal),

    # METAL RULE CRUD (master rules)
    path("rules/", metal_rule_view.list_rules),
    path("metals/<int:metal_id>/rules/", metal_rule_view.list_rules),
    path("rule/create/", metal_rule_view.create_rule),
    path("rule/<int:rule_id>/update/", metal_rule_view.update_rule),
    path("rule/<int:rule_id>/delete/", metal_rule_view.delete_rule),

    # Branch rule CRUD (Store section: branch-wise rules)
    path("branch-rule/create/", metal_rule_view.create_branch_rule),
    path("branch-rule/<int:rule_id>/update/", metal_rule_view.update_branch_rule),
    path("branch-rule/<int:rule_id>/delete/", metal_rule_view.delete_branch_rule),
    path("vendor/create/", vendors_view.create_vendor),
    path("vendor/create-with-address/", vendors_view.create_vendor_with_address),
    path("vendor/list/", vendors_view.get_vendors),
    path("vendor/update/<int:vendor_id>/", vendors_view.update_vendor),
    path("vendor/delete/<int:vendor_id>/", vendors_view.delete_vendor),

    path("vendor/bank/add/", vendors_view.add_vendor_bank),
    path("vendor/bank/<int:vendor_id>/", vendors_view.get_vendor_banks),
    path('vendor/bank/update/<int:bank_id>/', vendors_view.update_vendor_bank),
    path('vendor/bank/delete/<int:bank_id>/', vendors_view.delete_vendor_bank),

    path("vendor/address/add/", vendors_view.add_vendor_address),
    path("vendor/address/<int:vendor_id>/", vendors_view.get_vendor_addresses),
    path('vendor/address/update/<int:address_id>/', vendors_view.update_vendor_address),
    path('vendor/address/delete/<int:address_id>/', vendors_view.delete_vendor_address),
    path('vendor/profile/<int:vendor_id>/', vendors_view.get_vendor_profile),

    # Stone APIs
    path("stones/preview-identifiers/", stone_view.preview_stone_identifiers, name="preview_stone_identifiers"),
    path("stones/create/", stone_view.create_stone, name="create_stone"),
    path("stones/", stone_view.get_stones, name="get_stones"),
    path("stones/<int:stone_id>/", stone_view.get_stone, name="get_stone"),
    path("stones/update/<int:stone_id>/", stone_view.update_stone, name="update_stone"),
    path("stones/delete/<int:stone_id>/", stone_view.delete_stone, name="delete_stone"),

    # Product code prefix counters (barcode tag sequences per prefix)
    path("product-code-prefixes/", product_code_prefix_view.list_product_code_prefixes),
    path("product-code-prefixes/create/", product_code_prefix_view.create_product_code_prefix),
    path("product-code-prefixes/sync/", product_code_prefix_view.sync_product_code_prefixes),
    path("product-code-prefixes/ensure/", product_code_prefix_view.ensure_product_code_prefix),
    path("product-code-prefixes/validate/", product_code_prefix_view.validate_product_code_prefix_mapping),
    path("product-code-prefixes/<int:prefix_id>/", product_code_prefix_view.get_product_code_prefix),
    path("product-code-prefixes/<int:prefix_id>/update/", product_code_prefix_view.update_product_code_prefix),

    # Pattern code ↔ store variant name registry
    path("pattern-codes/", pattern_code_registry_view.list_pattern_codes),
    path("pattern-codes/create/", pattern_code_registry_view.create_pattern_code),
    path("pattern-codes/ensure/", pattern_code_registry_view.ensure_pattern_code),
    path("pattern-codes/bind/", pattern_code_registry_view.bind_pattern_code),
    path("pattern-codes/validate/", pattern_code_registry_view.validate_pattern_mapping),
    path("pattern-codes/sync/", pattern_code_registry_view.sync_pattern_codes),

    # HSN Master
    path("hsn/create/", hsn_view.create_hsn),
    path("hsn/list/", hsn_view.list_hsn),
    path("hsn/<int:hsn_id>/", hsn_view.get_hsn),
    path("hsn/update/<int:hsn_id>/", hsn_view.update_hsn),
    path("hsn/delete/<int:hsn_id>/", hsn_view.delete_hsn),

    # Category & Subcategory management
    path("categories/", category_view.category_list_create, name="category-list-create"),
    path("categories/<int:category_id>/", category_view.category_detail, name="category-detail"),
    path("categories/<int:category_id>/subcategories/", category_view.subcategory_list_create, name="subcategory-list-create"),
    path("categories/<int:category_id>/subcategories/<int:subcategory_id>/", category_view.subcategory_detail, name="subcategory-detail"),

    # Models
    # path("model-items/create/", model_views.create_model_item),
    # path("model-items/", model_views.get_model_items),
    # path("model-items/<int:id>/", model_views.get_model_item),
    # path("model-items/update/<int:id>/", model_views.update_model_item),

    # Product APIs
    path("products/draft/", list_drafts, name="list-drafts"),  # GET - List all drafts
    path("products/draft/create/", create_draft, name="create-draft"),  # POST - Create new draft
    path("products/draft/<int:draft_id>/", get_draft, name="get-draft"),  # GET - Get draft detail
    path("products/draft/<int:draft_id>/save/", save_step_data, name="save-step-data"),  # PATCH - Update draft
    path("products/draft/<int:draft_id>/media/", upload_media, name="upload-media"),  # POST - Upload media
    path("products/publish/<int:draft_id>/", publish_draft, name="publish-draft"),  # POST - Publish draft

    # Published Product APIs
    path("products/hierarchy/", list_products_hierarchy, name="products-hierarchy"),  # GET - ProductGroup -> SKU -> Product
    path("products/search/", search_product_items, name="products-search"),  # GET - Search published items by code/style

    path("products/item/<int:product_item_id>/detail/", get_product_item, name="get-product-item"),      # GET  - Full product detail (draft-compatible format)
    path("products/item/<int:product_item_id>/media/", upload_item_media, name="upload-item-media"),     # POST - Upload images for published product
    path("products/item/<int:product_item_id>/update/", update_product_item, name="update-product-item"), # PATCH - Update published product (no draft)
    path("products/vendor-previous-bom/", vendor_previous_bom, name="vendor-previous-bom"),

    # Purchase orders (procurement before GRN)
    path("purchase-orders/", purchase_order_list_create, name="purchase-order-list-create"),
    path(
        "purchase-orders/product-prefill/<int:product_item_id>/",
        purchase_order_product_prefill,
        name="purchase-order-product-prefill",
    ),
    path("purchase-orders/<int:pk>/", purchase_order_detail, name="purchase-order-detail"),

    # GRN batches (BatchRow — GRN batch management UI)
    path(
        "grn-batches/",
        grn_batch_list_create,
        name="grn-batch-list-create",
    ),
    path(
        "grn-batches/<int:pk>/",
        grn_batch_detail,
        name="grn-batch-detail",
    ),
    path(
        "grn-lots/",
        grn_lot_list_create,
        name="grn-lot-list-create",
    ),
    path(
        "grn-lots/vendor-terms/",
        grn_lot_vendor_terms,
        name="grn-lot-vendor-terms",
    ),
    path(
        "grn-lots/<int:pk>/",
        grn_lot_detail,
        name="grn-lot-detail",
    ),
    path(
        "make-bag/lots-sidebar/",
        make_bag_lots_sidebar,
        name="make-bag-lots-sidebar",
    ),
    path(
        "make-bag/next-bag-no/",
        make_bag_next_bag_no,
        name="make-bag-next-bag-no",
    ),
    path(
        "make-bag/search-items/",
        make_bag_search_items,
        name="make-bag-search-items",
    ),
    path(
        "make-bag/catalog-items/",
        make_bag_catalog_items,
        name="make-bag-catalog-items",
    ),
    path(
        "make-bag/catalog-facets/",
        make_bag_catalog_facets,
        name="make-bag-catalog-facets",
    ),
    path(
        "make-bag/save/",
        make_bag_save,
        name="make-bag-save",
    ),
    path(
        "reverse-grn/bag/",
        reverse_grn_bag_lookup,
        name="reverse-grn-bag-lookup",
    ),
    path(
        "reverse-grn/bag/reverse/",
        reverse_grn_bag,
        name="reverse-grn-bag",
    ),
    path(
        "reverse-grn/lot/<int:lot_id>/",
        reverse_grn_lot_lookup,
        name="reverse-grn-lot-lookup",
    ),
    path(
        "reverse-grn/lot/<int:lot_id>/reverse/",
        reverse_grn_lot,
        name="reverse-grn-lot",
    ),

    # Stock & Inventory
    path("stock/in/", stock_in, name="stock-in"),
    path("stock/out/", stock_out, name="stock-out"),
    path("stock/list/", stock_list, name="stock-list"),
    path("stock/transactions/", stock_transactions, name="stock-transactions"),
    path("stock/provenance/<int:product_item_id>/", stock_item_provenance, name="stock-provenance"),

    # Barcode / tag system (single namespace — barcode === tag)
    path("barcode/bags/", barcode_bag_list, name="barcode-bag-list"),
    path("barcode/bag/<int:bag_id>/", barcode_bag_detail, name="barcode-bag-detail"),
    path("barcode/generate/", barcode_generate, name="barcode-generate"),
    path("barcode/tags/", barcode_list, name="barcode-list"),
    path("barcode/fg-dashboard/", barcode_fg_dashboard, name="barcode-fg-dashboard"),
    path("barcode/mark-printed/", barcode_mark_printed, name="barcode-mark-printed"),
    path("barcode/photos/<int:photo_id>/", barcode_tag_photo_delete, name="barcode-tag-photo-delete"),
    path("barcode/<int:tag_id>/photos/", barcode_tag_photos, name="barcode-tag-photos"),
    path("barcode/<int:tag_id>/update/", barcode_update_tag, name="barcode-update-tag"),
    path("barcode/<int:tag_id>/deactivate/", barcode_deactivate, name="barcode-deactivate"),

    # Post-tag attribute mapping
    path("tag-attributes/definitions/", tag_attribute_definitions_list, name="tag-attribute-definitions"),
    path("tag-attributes/tags/", tag_mapping_tag_list, name="tag-mapping-tag-list"),
    path("tag-attributes/tags/<int:tag_id>/mapping/", tag_mapping_detail, name="tag-mapping-detail"),
    path("tag-attributes/tags/<int:tag_id>/mapping/save/", tag_mapping_save, name="tag-mapping-save"),

    # GRN bulk product image import (filename → product code matching)
    path("grn/bulk-images/match/", bulk_image_match, name="grn-bulk-images-match"),
    path("grn/bulk-images/import/", bulk_image_import, name="grn-bulk-images-import"),

    # Weighing scale (barcode generator — live gross weight)
    path("scale/machines/", scale_machines_list, name="scale-machines-list"),

    ]

