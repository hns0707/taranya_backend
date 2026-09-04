from django.urls import path
from .views import auth_view, profile_view, scheme_view, payment_view, address_view, maturity_view, dashboard_view, s3_service_view
from .views import catalogue_view

urlpatterns = [
    # OTP login & authentication
    path('login/', auth_view.PasswordLoginView.as_view(), name='customer-password-login'),
    path('request-otp/', auth_view.RequestOTPView.as_view(), name='customer-request-otp'),
    path('verify-otp/', auth_view.VerifyOTPView.as_view(), name='customer-verify-otp'),
    path('logout/', auth_view.LogoutView.as_view(), name='customer-logout'),
    
    # Customer profile
    path('profile/', profile_view.customer_profile_complete),
    # Customer addresses
    path('addresses/', address_view.CustomerAddressListCreateView.as_view(), name='customer-addresses'),
    path('addresses/<int:pk>/', address_view.CustomerAddressRetrieveUpdateDestroyView.as_view(), name='customer-address-detail'),

    # Scheme enrollment
    path('schemes/', scheme_view.scheme_list, name='customer-scheme-list'),
    path('schemes/validate/', scheme_view.validate_scheme, name='customer-scheme-validate'),
    path('schemes/enroll/', scheme_view.apply_for_scheme_view, name='customer-scheme-enroll'),
    
    # Enrolled schemes
    path('my-schemes/', scheme_view.customer_enrolled_schemes, name='customer-enrolled-schemes'),
    path('my-schemes/<int:pk>/installments/', scheme_view.customer_scheme_installments_by_scheme, name='customer-scheme-installments-by-scheme'),
    path('my-schemes/<int:pk>/', scheme_view.customer_scheme_detail, name='customer-scheme-detail'),
    path('my-schemes/installments/', scheme_view.customer_scheme_installments, name='customer-scheme-installments'),
    
     # Scheme preview
    path('schemes/preview/', scheme_view.scheme_preview, name='customer-scheme-preview'),
    
    # Dashboard
    path('dashboard/', dashboard_view.customer_dashboard, name='customer-dashboard'),
    
    # Metal rates (24K base; used for locking)
    path('metal-rates/', scheme_view.customer_metal_rates, name='customer-metal-rates'),
    
    # Catalogue (storefront + POS; public read)
    path('catalogue/categories/', catalogue_view.catalogue_categories, name='customer-catalogue-categories'),
    path('catalogue/collections/', catalogue_view.catalogue_collections, name='customer-catalogue-collections'),
    path('catalogue/collections/<str:collection_id>/', catalogue_view.catalogue_collection_detail, name='customer-catalogue-collection-detail'),
    path('catalogue/filters/', catalogue_view.catalogue_filters, name='customer-catalogue-filters'),
    path('catalogue/products/', catalogue_view.catalogue_products, name='customer-catalogue-products'),
    path('catalogue/products/<str:product_id>/', catalogue_view.catalogue_product_detail, name='customer-catalogue-product-detail'),
    path('catalogue/product-items/<str:product_id>/', catalogue_view.catalogue_product_items, name='customer-catalogue-product-items'),
    path('catalogue/product-variants/<str:product_item_id>/', catalogue_view.catalogue_product_variants, name='customer-catalogue-product-variants'),

    # FAQs
    path('faqs/', scheme_view.customer_faq_list, name='customer-faqs'),
    
    # CMS pages
    path('cms-pages/', scheme_view.customer_cms_page, name='customer-cms-page'),
    
    # Payments
    path('payments/', payment_view.payment_list),
    path('payments/<int:pk>/', payment_view.payment_detail),
    path('payments/initiate/', payment_view.initiate_payment),
    path("payments/orange/return/", payment_view.orange_pg_return, name="customer-orange-pg-return"),
    path("payments/orange/advice/", payment_view.orange_pg_advice, name="customer-orange-pg-advice"),
    path("payments/callback/", payment_view.icici_upi_callback, name="customer-icici-callback"),
    path("payments/upi-mandate/create/", payment_view.create_upi_mandate, name="customer-upi-mandate-create"),
    path("payments/upi-qr/config/", payment_view.upi_qr_config, name="customer-upi-qr-config"),
    path(
        "payments/upi-mandate/create-qr/",
        payment_view.create_upi_mandate_qr,
        name="customer-upi-mandate-create-qr",
    ),
    path(
        "payments/upi-mandate/<int:mandate_id>/status/",
        payment_view.upi_mandate_status,
        name="customer-upi-mandate-status",
    ),
    path("payments/<int:payment_id>/status/", payment_view.payment_status),
    path("payments/verify/", payment_view.verify_payment, name="customer-payment-verify"),
    
    # Maturity
    path('my-schemes/maturity/', maturity_view.get_maturity_details, name='customer-scheme-maturity'),
    path('my-schemes/<int:customer_scheme_id>/apply-bonus/', maturity_view.apply_maturity_bonus, name='customer-scheme-apply-bonus'),

    #S3
    path('uploadToS3/', s3_service_view.upload_document, name='upload_document'),
    path('getfromS3/', s3_service_view.admin_list_customer_files, name='admin_list_customer_files'),
    path('generate-download-link/', s3_service_view.generate_download_link, name='generate_download_link'),
]