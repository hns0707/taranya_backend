"""
Views for customer and payment approvals in the master app.
"""
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from shared.models import Customer, CustomerKYC, AdminUserRole, LookupValue, AuditLog, Payment, CustomerAddress, CustomerScheme, CustomerNominee
from shared.services.approval_service import approve_finance, get_pending_kyc_schemes, get_pending_finance_schemes
from shared.services.kyc_service import approve_kyc, reject_kyc
from shared.services.payment_service import process_successful_payment
from shared.services.payment_processor import finalize_payment
from shared.services.metal_rate_service import get_lock_rate_for_scheme
from master.permissions.permission_checker import admin_auth
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.db import transaction
from rest_framework.decorators import api_view


class CustomerKYCApprovalView(generics.CreateAPIView):
    """
    API endpoint to approve or reject KYC for a customer.
    """
    
    @method_decorator(admin_auth("CRM_CUSTOMER_KYC_PAN_STATUS_APPROVE"))
    def post(self, request, *args, **kwargs):
        """Approve or reject KYC for a customer."""
        customer_id = kwargs.get("customer_id")
        admin_user = request.user
        action = request.data.get("action", "APPROVE")
        remarks = request.data.get("remarks", "")
        try:
            customer = Customer.objects.get(id=customer_id)
            kyc = CustomerKYC.objects.get(customer=customer)
        except Customer.DoesNotExist:
            return Response({"error": "Customer not found"}, status=status.HTTP_404_NOT_FOUND)
        except CustomerKYC.DoesNotExist:
            return Response({"error": "Customer KYC record not found"}, status=status.HTTP_404_NOT_FOUND)



        if action == "APPROVE":
            try:
                # Use centralized KYC approval service
                kyc = approve_kyc(kyc, admin_user)
                
                return Response({
                    "message": "KYC approved successfully",
                    "data": {
                        "customer_id": customer.id,
                        "kyc_status": kyc.status.code,
                    }
                }, status=status.HTTP_200_OK)
            except ValueError as e:
                return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
                
        elif action == "REJECT":
            try:
                # Use centralized KYC rejection service
                kyc = reject_kyc(kyc, admin_user, remarks)
                
                return Response({
                    "message": "KYC rejected successfully",
                    "data": {
                        "customer_id": customer.id,
                        "kyc_status": kyc.status.code,
                        "remarks": remarks
                    }
                }, status=status.HTTP_200_OK)
            except ValueError as e:
                return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

            return Response({
                "message": "KYC rejected successfully",
                "data": {
                    "customer_id": customer.id,
                    "kyc_status": kyc.status.code,
                    "remarks": remarks
                }
            }, status=status.HTTP_200_OK)
        else:
            return Response({"error": "Invalid action. Use APPROVE or REJECT"}, status=status.HTTP_400_BAD_REQUEST)


def _build_pending_kyc_customer_item(customer, kyc=None):
    """Build a single customer payload for pending KYC list (with or without KYC record)."""
    addresses = []
    for addr in customer.addresses.filter(is_active=True):
        addresses.append({
            "id": addr.id,
            "address_line1": addr.address_line1,
            "address_line2": addr.address_line2,
            "city": addr.city,
            "state": addr.state,
            "pincode": addr.pincode,
            "country": addr.country,
            "is_default": addr.is_default
        })

    nominees = []
    for scheme in CustomerScheme.objects.filter(customer=customer).select_related("scheme").prefetch_related("nominees"):
        for nominee in scheme.nominees.all():
            nominees.append({
                "id": nominee.id,
                "full_name": nominee.full_name,
                "relationship": nominee.relationship,
                "mobile": nominee.mobile,
                "share_percentage": str(nominee.share_percentage),
                "scheme_id": scheme.id,
                "scheme_name": scheme.scheme.scheme_name
            })

    kyc_details = None
    if kyc:
        kyc_details = {
            "id": kyc.id,
            "pan_number": kyc.pan_number,
            "pan_document_url": getattr(kyc, 'pan_document_url', None) or "",
            "status": kyc.status.code if kyc.status else None,
            "verified_by": kyc.verified_by.full_name if kyc.verified_by else None,
            "verified_at": kyc.verified_at,
            "created_at": kyc.system_created_at,
            "updated_at": kyc.system_updated_at
        }

    return {
        "customer_id": customer.id,
        "customer_name": customer.full_name,
        "mobile": customer.mobile,
        "email": customer.email or "",
        "date_of_birth": customer.date_of_birth,
        "gender": customer.gender,
        "kyc_details": kyc_details,
        "addresses": addresses,
        "nominees": nominees
    }


class PendingKYCCustomersListView(generics.ListAPIView):
    """
    API endpoint to list customers with pending or incomplete KYC.
    Includes: (1) customers with KYC status PENDING or NOT_STARTED,
    (2) customers who have enrolled in a scheme but have no KYC record yet.
    """
    
    @method_decorator(admin_auth("CRM_CUSTOMER_KYC_PAN_STATUS_VIEW"))
    def get(self, request, *args, **kwargs):
        """List all customers with pending or incomplete KYC."""
        try:
            pending_status = LookupValue.objects.get(lookup__code='KYC_STATUS', code='PENDING')
        except LookupValue.DoesNotExist:
            return Response({"error": "Required status values not found"}, status=status.HTTP_404_NOT_FOUND)

        not_started_status = None
        try:
            not_started_status = LookupValue.objects.get(lookup__code='KYC_STATUS', code='NOT_STARTED')
        except LookupValue.DoesNotExist:
            pass

        status_filter = [pending_status]
        if not_started_status is not None:
            status_filter.append(not_started_status)

        kyc_records = CustomerKYC.objects.filter(status__in=status_filter).select_related(
            'customer',
            'address',
            'status'
        ).prefetch_related(
            'customer__addresses',
        )

        seen_customer_ids = set()
        data = []

        for kyc in kyc_records:
            customer = kyc.customer
            if customer.id in seen_customer_ids:
                continue
            seen_customer_ids.add(customer.id)
            data.append(_build_pending_kyc_customer_item(customer, kyc=kyc))

        customers_with_scheme_no_kyc = Customer.objects.filter(
            pk__in=CustomerScheme.objects.values_list("customer_id", flat=True).distinct()
        ).exclude(
            id__in=CustomerKYC.objects.values_list('customer_id', flat=True)
        ).distinct().prefetch_related(
            'addresses',
        )

        for customer in customers_with_scheme_no_kyc:
            if customer.id in seen_customer_ids:
                continue
            seen_customer_ids.add(customer.id)
            data.append(_build_pending_kyc_customer_item(customer, kyc=None))

        return Response(data)


@api_view(['POST'])
@admin_auth(
    "CRM_ACCOUNTS_PAYMENT_VERIFICATION_UPDATE",
    "CRM_ACCOUNTS_PAYMENT_VERIFICATION_VIEW",
    "CRM_ACCOUNTS_INSTALMENT_RECORDS_UPDATE",
)
def installment_payment_approval(request, payment_id):
    """
    Approve or revoke a payment.
    """

    admin_user = request.user
    action = request.data.get("action", "APPROVE").upper()

    try:
        payment = Payment.objects.select_related(
            'payment_status', 'payment_mode',
            'instalment', 'instalment__customer_scheme', 'instalment__customer_scheme__customer'
        ).get(id=payment_id)
    except Payment.DoesNotExist:
        return Response({"error": "Payment not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        with transaction.atomic():

            if action == "APPROVE":
                current_code = payment.payment_status.code if payment.payment_status else ""
                # UNDER_REVIEW: set SUCCESS and finalize (ledger, gold, activation)
                if current_code == "UNDER_REVIEW":
                    SUCCESS = LookupValue.objects.get(
                        lookup__code='PAYMENT_STATUS', code='SUCCESS'
                    )
                    payment = Payment.objects.select_for_update().get(id=payment.id)
                    if (payment.payment_status and payment.payment_status.code) != "UNDER_REVIEW":
                        return Response({
                            "error": "Payment is no longer UNDER_REVIEW and cannot be approved"
                        }, status=status.HTTP_400_BAD_REQUEST)
                    previous_status = payment.payment_status.code
                    payment.payment_status = SUCCESS
                    payment.is_finalized = True
                    payment.paid_at = payment.paid_at or timezone.now()
                    payment.save(update_fields=["payment_status", "is_finalized", "paid_at", "system_updated_at"])
                    finalize_payment(payment)
                    AuditLog.objects.create(
                        admin=admin_user,
                        action='APPROVE_PAYMENT',
                        entity_type='Payment',
                        entity_id=payment.id,
                        old_value={'payment_status': previous_status},
                        new_value={'payment_status': 'SUCCESS'},
                        ip_address=request.META.get('REMOTE_ADDR')
                    )
                    payment.refresh_from_db()
                    return Response({
                        "message": "Payment approved successfully",
                        "data": {
                            "payment_id": payment.id,
                            "payment_status": payment.payment_status.code,
                            "amount": str(payment.amount),
                            "transaction_id": payment.transaction_id
                        }
                    }, status=status.HTTP_200_OK)
                # Legacy: INITIATED / PENDING
                pending_status = LookupValue.objects.get(
                    lookup__code='PAYMENT_STATUS', code='PENDING'
                )
                initiated_status = LookupValue.objects.get(
                    lookup__code='PAYMENT_STATUS', code='INITIATED'
                )
                if payment.payment_status not in [pending_status, initiated_status]:
                    return Response({
                        "error": f"Payment is in {payment.payment_status.code} state, which cannot be approved"
                    }, status=status.HTTP_400_BAD_REQUEST)

                previous_status = payment.payment_status.code
                # Lock rate: metal_rate (24K) or gold_rate from body; else metal rate from scheme
                lock_rate = request.data.get('metal_rate') or request.data.get('gold_rate')
                if lock_rate is None:
                    scheme = payment.instalment.customer_scheme.scheme
                    rate_obj = get_lock_rate_for_scheme(scheme)
                    if rate_obj is not None:
                        lock_rate = getattr(rate_obj, 'rate_value', rate_obj)

                process_successful_payment(payment, gold_rate=lock_rate)

                AuditLog.objects.create(
                    admin=admin_user,
                    action='APPROVE_PAYMENT',
                    entity_type='Payment',
                    entity_id=payment.id,
                    old_value={'payment_status': previous_status},
                    new_value={'payment_status': 'PAID'},
                    ip_address=request.META.get('REMOTE_ADDR')
                )

                payment.refresh_from_db()
                return Response({
                    "message": "Payment approved successfully",
                    "data": {
                        "payment_id": payment.id,
                        "payment_status": payment.payment_status.code,
                        "amount": str(payment.amount),
                        "transaction_id": payment.transaction_id
                    }
                }, status=status.HTTP_200_OK)

            elif action == "REJECT":
                # Reject UNDER_REVIEW payment: set REJECTED, is_finalized=True. Do NOT update installment.
                under_review_status = LookupValue.objects.get(
                    lookup__code='PAYMENT_STATUS', code='UNDER_REVIEW'
                )
                rejected_status = LookupValue.objects.get(
                    lookup__code='PAYMENT_STATUS', code='REJECTED'
                )
                payment = Payment.objects.select_for_update().get(id=payment.id)
                if payment.payment_status != under_review_status:
                    return Response({
                        "error": "Only UNDER_REVIEW payments can be rejected"
                    }, status=status.HTTP_400_BAD_REQUEST)
                previous_status = payment.payment_status.code
                payment.payment_status = rejected_status
                payment.is_finalized = True
                payment.save(update_fields=['payment_status', 'is_finalized', 'system_updated_at'])
                AuditLog.objects.create(
                    admin=admin_user,
                    action='REJECT_PAYMENT',
                    entity_type='Payment',
                    entity_id=payment.id,
                    old_value={'payment_status': previous_status},
                    new_value={'payment_status': 'REJECTED'},
                    ip_address=request.META.get('REMOTE_ADDR')
                )
                return Response({
                    "message": "Payment rejected",
                    "data": {
                        "payment_id": payment.id,
                        "payment_status": payment.payment_status.code,
                        "transaction_id": payment.transaction_id
                    }
                }, status=status.HTTP_200_OK)

            elif action == "REVOKE":
                success_status = LookupValue.objects.get(
                    lookup__code='PAYMENT_STATUS', code='SUCCESS'
                )
                SUCCESS_status = LookupValue.objects.get(
                    lookup__code='PAYMENT_STATUS', code='SUCCESS'
                )
                revoked_status = LookupValue.objects.get(
                    lookup__code='PAYMENT_STATUS', code='REVOKED'
                )
                if payment.payment_status not in (success_status, SUCCESS_status):
                    return Response({
                        "error": f"Payment is in {payment.payment_status.code} state, which cannot be revoked"
                    }, status=status.HTTP_400_BAD_REQUEST)

                previous_status = payment.payment_status.code

                payment.payment_status = revoked_status
                payment.is_finalized = False
                payment.save(update_fields=[
                    'payment_status',
                    'is_finalized',
                    'system_updated_at'
                ])

                AuditLog.objects.create(
                    admin=admin_user,
                    action='REVOKE_PAYMENT',
                    entity_type='Payment',
                    entity_id=payment.id,
                    old_value={'payment_status': previous_status},
                    new_value={'payment_status': 'REVOKED'},
                    ip_address=request.META.get('REMOTE_ADDR')
                )

                return Response({
                    "message": "Payment revoked successfully",
                    "data": {
                        "payment_id": payment.id,
                        "payment_status": payment.payment_status.code,
                        "amount": str(payment.amount),
                        "transaction_id": payment.transaction_id
                    }
                }, status=status.HTTP_200_OK)

            else:
                return Response(
                    {"error": "Invalid action. Use APPROVE, REJECT, or REVOKE"},
                    status=status.HTTP_400_BAD_REQUEST
                )

    except LookupValue.DoesNotExist:
        return Response(
            {"error": "Required status values not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    except Exception as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )
