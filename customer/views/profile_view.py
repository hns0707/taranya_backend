"""
Customer Profile API (Fetch + Update)

Rules:
- Always return customer data
- Optional: KYC, Address, Scheme, Nominee
- If optional data does not exist → return null / empty list
- Nominee is NOT mandatory
- Safe create / update logic
"""

import re
from datetime import datetime, date

from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response
from rest_framework import status

from customer.auth.customer_auth import CustomerAuthentication
from shared.models import (
    LookupValue,
    CustomerKYC,
    CustomerAddress,
    CustomerScheme,
    CustomerNominee,
    Customer
)

PAN_REGEX = re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]{1}$')


# ---------------- HELPERS ---------------- #

def calculate_age(dob):
    today = timezone.localdate()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def is_profile_complete(customer):
    """Check if customer profile is complete based on new requirements."""
    # Check customer basic details
    if not (customer.full_name and customer.date_of_birth):
        return False
    
    # Check if address exists
    if not CustomerAddress.objects.filter(customer=customer, is_active=True).exists():
        return False
    
    # Check if nominee exists
    if not CustomerNominee.objects.filter(customer_scheme__customer=customer).exists():
        return False
    
    # Check KYC details are complete
    kyc = CustomerKYC.objects.filter(customer=customer).first()
    if not kyc:
        return False
    
    if not (kyc.pan_number and kyc.pan_document_url):
        return False
    
    return True


# ---------------- API ---------------- #

@api_view(['GET', 'POST'])
@authentication_classes([CustomerAuthentication])
def customer_profile_complete(request):
    customer = request.user

    # ======================================================
    # GET : FETCH PROFILE
    # ======================================================
    if request.method == 'GET':

        # -------- Customer --------
        customer_data = {
            "id": customer.id,
            "full_name": customer.full_name,
            "mobile": customer.mobile,
            "email": customer.email or "",
            "gender": customer.gender,
            "date_of_birth": customer.date_of_birth.isoformat() if customer.date_of_birth else None,
        }

        # -------- KYC (optional) --------
        kyc = CustomerKYC.objects.filter(customer=customer).first()
        kyc_data = {}
        if kyc:
            kyc_data = {
                "id": kyc.id,
                "pan_number": kyc.pan_number,
                "pan_document_url": kyc.pan_document_url,
                "status": kyc.status.code if kyc.status else "NOT_STARTED",
                "verified_by": kyc.verified_by.id if kyc.verified_by else None,
                "verified_at": kyc.verified_at.isoformat() if kyc.verified_at else None,
                "system_created_at": kyc.system_created_at.isoformat(),
                "system_updated_at": kyc.system_updated_at.isoformat()
            }

        # -------- Addresses (optional) --------
        addresses = list(
            CustomerAddress.objects.filter(
                customer=customer,
                is_active=True
            ).values(
                'id', 'address_line1', 'address_line2',
                'city', 'state', 'pincode',
                'country', 'is_default'
            )
        )

        # -------- Schemes (optional) --------
        schemes = list(
            CustomerScheme.objects.filter(customer=customer).values(
                'id', 'monthly_amount',
                'scheme_status', 'applied_at'
            )
        )

        # -------- Nominees (SPREAD, optional) --------
        nominees = list(
            CustomerNominee.objects.filter(
                customer_scheme__customer=customer
            ).values(
                'id',
                'full_name',
                'relationship',
                'mobile',
                'share_percentage',
            )
        )

        return Response({
            "customer": customer_data,
            "kyc": kyc_data,
            "addresses": addresses,
            "active_schemes": schemes,
            "nominees": nominees,
            "is_profile_complete": is_profile_complete(customer)
        }, status=status.HTTP_200_OK)

    # ======================================================
    # POST : UPDATE / CREATE PROFILE DATA
    # ======================================================
    if request.method == 'POST':
        data = request.data
        mobile = data.get("mobile")
        if not mobile:
            return Response(
                {"error": "Mobile number is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        customer, created = Customer.objects.get_or_create(
            mobile=mobile,
            defaults={
                "full_name": data.get("full_name", ""),
                "email": data.get("email", ""),
                "gender": data.get("gender"),
            }
        )

        # If customer already exists → update basic fields
        if not created:
            if "full_name" in data:
                customer.full_name = data.get("full_name")

            if "email" in data:
                customer.email = data.get("email")

            if "gender" in data:
                customer.gender = data.get("gender")

            if "date_of_birth" in data:
                customer.date_of_birth = data.get("date_of_birth")

            customer.save()

        # KYC: one record per customer (any status). Create only if none exists; do not update if exists.
        pan = data.get("pan")
        if pan:
            existing_kyc = CustomerKYC.objects.filter(customer=customer).first()
            if not existing_kyc:
                pending_status = LookupValue.objects.get(
                    lookup__code="KYC_STATUS",
                    code="PENDING"
                )
                kyc = CustomerKYC.objects.create(
                    customer=customer,
                    status=pending_status,
                    pan_number=pan.upper(),
                    pan_document_url=data.get("pan_document_url") or ""
                )
            # If KYC already exists (pending/approved/rejected), do not update — one KYC per customer

        return Response({
            "message": "Customer profile processed successfully",
            "customer_id": customer.id,
            "created": created
        }, status=status.HTTP_200_OK)