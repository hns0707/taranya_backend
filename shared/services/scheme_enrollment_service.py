"""
Shared service for customer scheme enrollment with address and nominee handling.
This service provides a single reusable function that handles all aspects of scheme enrollment,
ensuring consistency across both customer and admin API endpoints.
"""
from django.db import transaction
from django.core.exceptions import ValidationError
from decimal import Decimal

from shared.models import CustomerAddress, CustomerNominee
from shared.services.scheme_service import apply_for_scheme


def enroll_customer_into_scheme(
    *,
    customer,
    scheme,
    monthly_amount,
    address_data=None,
    nominee_data=None
):
    """
    Enroll a customer into a scheme with optional address and nominee details.
    
    This function wraps all operations in a single atomic transaction to ensure
    consistency. It handles address creation, default address management, and
    nominee creation with share percentage validation.
    
    Args:
        customer (Customer): The customer to enroll
        scheme (SchemeMaster): The scheme to enroll into
        monthly_amount (Decimal): The monthly installment amount
        address_data (dict, optional): Address details (default: None)
            Expected keys: address_line_1, address_line_2, city, state, pincode, country
        nominee_data (dict, optional): Nominee details (default: None)
            Expected keys: nominee_name, relationship, mobile, share_percentage
            
    Returns:
        tuple: (CustomerScheme, SchemeInstalment) - The created customer scheme and first installment
        
    Raises:
        ValidationError: For business validation failures
        ValueError: For scheme amount or eligibility validation failures
        Exception: For other unexpected errors
    """
    with transaction.atomic():
        # Step 1: Enroll customer into scheme
        customer_scheme, first_instalment = apply_for_scheme(
            customer=customer,
            scheme=scheme,
            monthly_amount=monthly_amount
        )
        
        # Step 2: Handle address creation if provided
        if address_data:
            # Check if customer has any existing active addresses
            has_existing_address = CustomerAddress.objects.filter(
                customer=customer,
                is_active=True
            ).exists()
            
            # First address must be default if no existing addresses
            is_default = not has_existing_address
            
            # Create address
            address = CustomerAddress.objects.create(
                customer=customer,
                address_line1=address_data.get("address_line_1"),
                address_line2=address_data.get("address_line_2"),
                city=address_data.get("city"),
                state=address_data.get("state"),
                pincode=address_data.get("pincode"),
                country=address_data.get("country", "India"),
                is_default=is_default,
                is_active=True
            )
            
            # Unset other defaults if this is set as default
            if is_default:
                CustomerAddress.objects.filter(
                    customer=customer,
                    is_active=True
                ).exclude(id=address.id).update(is_default=False)
                
            # Assign address to customer scheme
            customer_scheme.address = address
            customer_scheme.save(update_fields=['address', 'system_updated_at'])
        
        # Step 3: Handle nominee creation if provided
        if nominee_data:
            CustomerNominee.objects.create(
                customer_scheme=customer_scheme,
                full_name=nominee_data.get("full_name"),
                relationship=nominee_data.get("relationship"),
                mobile=nominee_data.get("mobile"),
            )
            
    return customer_scheme, first_instalment