"""
Shared service for approval-related business logic.
"""
from shared.models import CustomerScheme, CustomerKYC, AuditLog, LookupValue
from django.utils import timezone


def approve_finance(customer_scheme, admin_user):
    """
    Approve the finance for a customer scheme.

    Args:
        customer_scheme (CustomerScheme): The customer scheme to approve.
        admin_user (AdminUser): The admin user approving the finance.

    Returns:
        CustomerScheme: The updated customer scheme.
    """
    try:
        kyc_approved_status = LookupValue.objects.get(lookup__code='KYC_STATUS', code='APPROVED')
    except LookupValue.DoesNotExist:
        raise ValueError("Required status values not found")

    kyc = CustomerKYC.objects.filter(customer=customer_scheme.customer).first()
    if not kyc or kyc.status != kyc_approved_status:
        raise ValueError("KYC must be approved before finance")

    # Log the finance approval
    AuditLog.objects.create(
        admin=admin_user,
        action='APPROVE_FINANCE',
        entity_type='CustomerScheme',
        entity_id=customer_scheme.id,
        old_value={'scheme_status': customer_scheme.scheme_status.code},
        new_value={'finance_approval': 'APPROVED'},
        ip_address='127.0.0.1'  # Replace with actual IP address
    )

    return customer_scheme


def get_pending_kyc_schemes():
    """
    Get all customers with pending KYC approval.

    Returns:
        QuerySet: A queryset of CustomerKYC records with pending status,
                  including related customer and customer schemes data.
    """
    try:
        pending_status = LookupValue.objects.get(lookup__code='KYC_STATUS', code='PENDING')
    except LookupValue.DoesNotExist:
        return CustomerKYC.objects.none()
        
    return CustomerKYC.objects.filter(status=pending_status).select_related(
        'customer'
    ).prefetch_related(
        'customer__customerscheme_set'
    )


def get_pending_finance_schemes():
    """
    Get all schemes pending finance approval.

    Returns:
        QuerySet: A queryset of schemes pending finance approval.
    """
    try:
        approved_kyc_status = LookupValue.objects.get(lookup__code='KYC_STATUS', code='PENDING')
        pending_scheme_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='PENDING')
    except LookupValue.DoesNotExist:
        return CustomerScheme.objects.none()

    return CustomerScheme.objects.filter(
        scheme_status=pending_scheme_status,
        customer__customerkyc__status=approved_kyc_status
    )
