"""
Service for KYC state transitions.
"""
from django.db import transaction
from django.utils import timezone
from shared.models import CustomerKYC, LookupValue, AuditLog


@transaction.atomic
def approve_kyc(kyc_instance, approved_by):
    """
    Approve a KYC record with atomic transaction and concurrency safety.
    
    Args:
        kyc_instance (CustomerKYC): The KYC record to approve
        approved_by: The admin user approving the KYC
        
    Returns:
        CustomerKYC: The updated KYC record
        
    Raises:
        ValueError: If KYC is not in pending state or other validation fails
    """
    # Lock the KYC record for update to prevent race conditions
    kyc = CustomerKYC.objects.select_for_update().get(id=kyc_instance.id)
    
    try:
        pending_status = LookupValue.objects.get(lookup__code='KYC_STATUS', code='PENDING')
        approved_status = LookupValue.objects.get(lookup__code='KYC_STATUS', code='APPROVED')
    except LookupValue.DoesNotExist:
        raise ValueError("Required KYC status values not found")
    
    if kyc.status != pending_status:
        raise ValueError(f"KYC is already {kyc.status.code}, cannot approve")
    
    previous_status = kyc.status.code
    kyc.status = approved_status
    kyc.verified_by = approved_by
    kyc.verified_at = timezone.now()
    kyc.save(update_fields=['status', 'verified_by', 'verified_at', 'system_updated_at'])
    
    # Log the KYC approval
    AuditLog.objects.create(
        admin=approved_by,
        action='APPROVE_KYC',
        entity_type='Customer',
        entity_id=kyc.customer.id,
        old_value={'kyc_status': previous_status},
        new_value={'kyc_status': 'APPROVED'},
        ip_address='127.0.0.1'  # Replace with actual IP address
    )
    
    return kyc


@transaction.atomic
def reject_kyc(kyc_instance, rejected_by, remarks=""):
    """
    Reject a KYC record with atomic transaction and concurrency safety.
    
    Args:
        kyc_instance (CustomerKYC): The KYC record to reject
        rejected_by: The admin user rejecting the KYC
        remarks (str): Optional remarks for rejection
        
    Returns:
        CustomerKYC: The updated KYC record
        
    Raises:
        ValueError: If KYC is not in pending state or other validation fails
    """
    # Lock the KYC record for update to prevent race conditions
    kyc = CustomerKYC.objects.select_for_update().get(id=kyc_instance.id)
    
    try:
        pending_status = LookupValue.objects.get(lookup__code='KYC_STATUS', code='PENDING')
        rejected_status = LookupValue.objects.get(lookup__code='KYC_STATUS', code='REJECTED')
    except LookupValue.DoesNotExist:
        raise ValueError("Required KYC status values not found")
    
    if kyc.status != pending_status:
        raise ValueError(f"KYC is already {kyc.status.code}, cannot reject")
    
    previous_status = kyc.status.code
    kyc.status = rejected_status
    kyc.verified_by = rejected_by
    kyc.verified_at = timezone.now()
    kyc.save(update_fields=['status', 'verified_by', 'verified_at', 'system_updated_at'])
    
    # Log the KYC rejection
    AuditLog.objects.create(
        admin=rejected_by,
        action='REJECT_KYC',
        entity_type='Customer',
        entity_id=kyc.customer.id,
        old_value={'kyc_status': previous_status},
        new_value={'kyc_status': 'REJECTED', 'remarks': remarks},
        ip_address='127.0.0.1'  # Replace with actual IP address
    )
    
    return kyc
