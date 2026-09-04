"""
Views for scheme configuration in the master app.
"""
import logging
import uuid
from datetime import datetime, date, timedelta
import calendar
from decimal import Decimal
from django.db.models import Count, Q, Sum
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db import transaction
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status
from shared.services.pos_service import peek_next_invoice_number, _parse_invoice_date
from master.permissions.permission_checker import admin_auth
from master.permissions.section_auth import (
    ACCOUNTS_INSTALMENT_READ_AUTH,
    ACCOUNTS_SCHEME_READ_AUTH,
    ACCOUNTS_SCHEME_WRITE_AUTH,
    SCHEME_DASHBOARD_READ_AUTH,
    SCHEME_LIST_READ_AUTH,
    SCHEME_ENROLLMENT_WRITE_AUTH,
)
from shared.models import SchemeMaster, Customer, SchemeInstalment, LookupValue, Payment, CustomerScheme, CustomerNominee, CustomerAddress, SchemeBenefit, CustomerSchemeBenefit
from shared.services.scheme_service import apply_for_scheme
from shared.services.payment_service import process_successful_payment, create_payment_with_collections
from shared.helper import get_payment_mode_display, get_payment_mode_label
from shared.services.metal_rate_service import get_lock_rate_for_scheme
from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)


def _build_installment_window(anchor_day, due_date):
    """Return batch window for a due month based on enrollment/apply day."""
    month_last_day = calendar.monthrange(due_date.year, due_date.month)[1]
    if anchor_day <= 15:
        return date(due_date.year, due_date.month, 1), date(due_date.year, due_date.month, 15)
    return date(due_date.year, due_date.month, 15), date(due_date.year, due_date.month, month_last_day)


def _resolve_anchor_day(customer_scheme, cache=None):
    """
    Resolve batch anchor day for a scheme.
    Priority: first successful payment date > start_date > applied_at > 1.
    """
    cs_id = customer_scheme.id if customer_scheme else None
    if cache is not None and cs_id in cache:
        return cache[cs_id]

    day = 1
    if customer_scheme:
        first_paid_at = (
            Payment.objects.filter(
                instalment__customer_scheme=customer_scheme,
                payment_status__code="SUCCESS",
            )
            .exclude(paid_at__isnull=True)
            .order_by("paid_at")
            .values_list("paid_at", flat=True)
            .first()
        )
        if first_paid_at:
            day = timezone.localtime(first_paid_at).date().day
        elif customer_scheme.start_date:
            day = customer_scheme.start_date.day
        elif customer_scheme.applied_at:
            day = timezone.localtime(customer_scheme.applied_at).date().day

    if cache is not None and cs_id is not None:
        cache[cs_id] = day
    return day


def _resolve_first_paid_date(customer_scheme, cache=None):
    """Return first successful payment date (local date) for a scheme, else None."""
    cs_id = customer_scheme.id if customer_scheme else None
    if cache is not None and cs_id in cache:
        return cache[cs_id]

    first_paid_date = None
    if customer_scheme:
        first_paid_at = (
            Payment.objects.filter(
                instalment__customer_scheme=customer_scheme,
                payment_status__code="SUCCESS",
            )
            .exclude(paid_at__isnull=True)
            .order_by("paid_at")
            .values_list("paid_at", flat=True)
            .first()
        )
        if first_paid_at:
            first_paid_date = timezone.localtime(first_paid_at).date()

    if cache is not None and cs_id is not None:
        cache[cs_id] = first_paid_date
    return first_paid_date


@api_view(['GET'])
@admin_auth("CRM_MASTERS_SCHEME_MASTER_VIEW")
def list_schemes(request):
    """List all schemes with pagination, filtering and ordering."""
    # Get query parameters
    is_active = request.GET.get('is_active')
    scheme_name = request.GET.get('scheme_name')
    
    queryset = SchemeMaster.objects.all()
    
    # Apply filters
    if is_active is not None:
        queryset = queryset.filter(is_active=is_active.lower() == 'true')
    
    if scheme_name:
        queryset = queryset.filter(scheme_name__icontains=scheme_name)
    
    # Apply ordering
    ordering = request.GET.get('ordering', '-system_created_at')
    queryset = queryset.order_by(ordering)
    
    # Apply pagination
    page = int(request.GET.get('page', 1))
    page_size = int(request.GET.get('page_size', 10))
    start = (page - 1) * page_size
    end = start + page_size
    
    paginated_queryset = queryset[start:end]
    
    data = []
    for scheme in paginated_queryset:
        benefits = SchemeBenefit.objects.filter(scheme=scheme)
        benefits_list = []
        for benefit in benefits:
            benefits_list.append({
                "id": benefit.id,
                "benefit_type": benefit.benefit_type,
                "benefit_value": str(benefit.benefit_value) if benefit.benefit_value else None,
                "benefit_percentage": str(benefit.benefit_percentage) if benefit.benefit_percentage else None,
                "benefit_months": benefit.benefit_months
            })
        
        data.append({
            "id": scheme.id,
            "scheme_code": scheme.scheme_code,
            "scheme_name": scheme.scheme_name,
            "tenure_months": scheme.tenure_months,
            "gold_purity": scheme.gold_purity,
            "min_instalment": str(scheme.min_instalment),
            "max_instalment": str(scheme.max_instalment),
            "is_active": scheme.is_active,
            "system_created_at": scheme.system_created_at,
            "benefits": benefits_list
        })
    
    return Response({
        "count": queryset.count(),
        "page": page,
        "page_size": page_size,
        "total_pages": (queryset.count() + page_size - 1) // page_size,
        "results": data
    })


@api_view(['POST'])
@admin_auth("CRM_MASTERS_SCHEME_MASTER_CREATE")
def create_scheme(request):
    """Create a new scheme with benefits."""
    data = request.data
    
    # Validate required fields
    required_fields = ['scheme_code', 'scheme_name', 'tenure_months', 'min_instalment', 'max_instalment']
    for field in required_fields:
        if field not in data:
            return Response({"error": f"{field.replace('_', ' ').title()} is required"}, status=status.HTTP_400_BAD_REQUEST)
    
    # Check if scheme code already exists
    if SchemeMaster.objects.filter(scheme_code=data['scheme_code']).exists():
        return Response({"error": "Scheme code already exists"}, status=status.HTTP_400_BAD_REQUEST)
    
    # Create scheme
    scheme = SchemeMaster.objects.create(
        scheme_code=data['scheme_code'],
        scheme_name=data['scheme_name'],
        tenure_months=data['tenure_months'],
        gold_purity=data.get('gold_purity'),
        min_instalment=Decimal(str(data['min_instalment'])),
        max_instalment=Decimal(str(data['max_instalment'])),
        scheme_description=data.get('scheme_description'),
        marketing_banner_url=data.get('marketing_banner_url'),
        highlight_tags=data.get('highlight_tags'),
        is_active=data.get('is_active', True)
    )
    
    # Create benefits
    benefits = data.get("benefits", [])
    for benefit in benefits:
        SchemeBenefit.objects.create(
            scheme=scheme,
            benefit_type=benefit.get("benefit_type"),
            benefit_value=Decimal(str(benefit.get("benefit_value"))) if benefit.get("benefit_value") else None,
            benefit_percentage=Decimal(str(benefit.get("benefit_percentage"))) if benefit.get("benefit_percentage") else None,
            benefit_months=benefit.get("benefit_months", 0)
        )
    
    return Response({
        "message": "Scheme created successfully",
        "data": {
            "id": scheme.id,
            "scheme_code": scheme.scheme_code,
            "scheme_name": scheme.scheme_name,
            "tenure_months": scheme.tenure_months,
            "gold_purity": scheme.gold_purity,
            "min_instalment": str(scheme.min_instalment),
            "max_instalment": str(scheme.max_instalment),
            "is_active": scheme.is_active,
            "system_created_at": scheme.system_created_at
        }
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@admin_auth("CRM_SCHEME_ENROLLMENT_CREATE")
def create_customer_scheme(request):
    """Enroll customer into a scheme via admin interface."""
    data = request.data
    
    try:
        customer = Customer.objects.get(id=data.get("customer_id"))
        scheme = SchemeMaster.objects.get(id=data.get("scheme_id"))
        monthly_amount = Decimal(str(data["monthly_amount"]))
    except Customer.DoesNotExist:
        return Response({"error": "Customer not found"}, status=status.HTTP_404_NOT_FOUND)
    except SchemeMaster.DoesNotExist:
        return Response({"error": "Scheme not found"}, status=status.HTTP_404_NOT_FOUND)
    except KeyError as e:
        return Response({"error": f"Missing field: {e}"}, status=status.HTTP_400_BAD_REQUEST)

    expected_code = data.get("customer_code")
    if expected_code is not None and str(expected_code).strip():
        cc = (customer.customer_code or "").strip()
        if cc and cc != str(expected_code).strip():
            return Response(
                {"error": "customer_code does not match customer_id"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    try:
        from shared.services.scheme_service import apply_for_scheme
        
        # Get start_date from request (for backdated enrollment)
        start_date_str = data.get("start_date")
        
        customer_scheme, first_instalment = apply_for_scheme(
            customer=customer,
            scheme=scheme,
            monthly_amount=monthly_amount,
            start_date=start_date_str,
        )

        # Optional address snapshot for this enrollment
        address_payload = data.get("address") if isinstance(data.get("address"), dict) else None
        if address_payload:
            address_line1 = str(address_payload.get("address_line_1") or "").strip()
            city = str(address_payload.get("city") or "").strip()
            state = str(address_payload.get("state") or "").strip()
            pincode = str(address_payload.get("pincode") or "").strip()
            if address_line1 and city and state and pincode:
                address_obj = CustomerAddress.objects.filter(
                    customer=customer,
                    address_line1=address_line1,
                    city=city,
                    state=state,
                    pincode=pincode,
                    is_active=True,
                ).first()
                if not address_obj:
                    address_obj = CustomerAddress.objects.create(
                        customer=customer,
                        address_line1=address_line1,
                        address_line2=str(address_payload.get("address_line_2") or "").strip() or None,
                        city=city,
                        state=state,
                        pincode=pincode,
                        country=str(address_payload.get("country") or "India").strip() or "India",
                        is_default=bool(address_payload.get("is_default", False)),
                        is_active=True,
                    )
                customer_scheme.address = address_obj
                customer_scheme.save(update_fields=["address"])

        # Optional nominee details for this newly enrolled scheme
        nominee_payload = data.get("nominee_details") if isinstance(data.get("nominee_details"), dict) else None
        if nominee_payload:
            nominee_name = str(nominee_payload.get("nominee_name") or "").strip()
            nominee_relationship = str(nominee_payload.get("nominee_relationship") or "").strip()
            nominee_mobile = str(nominee_payload.get("mobile") or "").strip() or None
            share_percentage = nominee_payload.get("share_percentage", 100)
            try:
                share_percentage = Decimal(str(share_percentage))
            except Exception:
                share_percentage = Decimal("100")
            if nominee_name and nominee_relationship:
                CustomerNominee.objects.create(
                    customer_scheme=customer_scheme,
                    full_name=nominee_name,
                    relationship=nominee_relationship,
                    mobile=nominee_mobile,
                    share_percentage=share_percentage,
                )

        return Response({
            "message": "Customer enrolled successfully",
            "data": {
                "customer_scheme_id": customer_scheme.id,
                "first_instalment_id": first_instalment.id,
                "scheme_name": customer_scheme.scheme.scheme_name,
                "monthly_amount": str(customer_scheme.monthly_amount),
                "scheme_status": customer_scheme.scheme_status.code,
                "start_date": customer_scheme.start_date.isoformat() if customer_scheme.start_date else None,
                "end_date": customer_scheme.end_date.isoformat() if customer_scheme.end_date else None,
            }
        }, status=status.HTTP_201_CREATED)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@admin_auth(*ACCOUNTS_SCHEME_WRITE_AUTH)
def process_instalment_payment(request):
    """Process instalment payment: single path via process_successful_payment (mark paid, lock metal/gold, total_paid, ledger).
    Lock rate: use metal_rate or gold_rate from body; else 24K Gold rate (today/yesterday)."""
    instalment_id = request.data.get("instalment_id")
    payment_id = request.data.get("payment_id")
    metal_rate = request.data.get("metal_rate")
    gold_rate = request.data.get("gold_rate")

    if not instalment_id or not payment_id:
        return Response(
            {"error": "instalment_id and payment_id are required"},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        payment = Payment.objects.select_related("instalment__customer_scheme__scheme").get(id=payment_id)
        if payment.instalment_id != int(instalment_id):
            return Response({"error": "Payment does not belong to this instalment"}, status=status.HTTP_400_BAD_REQUEST)

        # Rate for locking: metal_rate (24K) preferred, else gold_rate from body, else metal rate from scheme
        lock_rate = None
        if metal_rate is not None:
            lock_rate = float(metal_rate)
        elif gold_rate is not None:
            lock_rate = float(gold_rate)
        else:
            scheme = payment.instalment.customer_scheme.scheme
            rate_obj = get_lock_rate_for_scheme(scheme)
            if rate_obj is not None:
                lock_rate = getattr(rate_obj, "rate_value", rate_obj)
                if lock_rate is not None:
                    lock_rate = float(lock_rate)

        process_successful_payment(payment, gold_rate=lock_rate)
        return Response({"message": "Payment processed successfully"}, status=status.HTTP_200_OK)

    except Payment.DoesNotExist:
        return Response({"error": "Payment not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@admin_auth(*ACCOUNTS_SCHEME_READ_AUTH, *ACCOUNTS_INSTALMENT_READ_AUTH)
def customer_scheme_instalments(request, scheme_id):
    """List all instalments for a customer scheme."""
    instalments = SchemeInstalment.objects.filter(
        customer_scheme_id=scheme_id
    ).order_by("instalment_no")
    
    customer_scheme = instalments.first().customer_scheme if instalments.exists() else None
    anchor_day = _resolve_anchor_day(customer_scheme)
    first_paid_date = _resolve_first_paid_date(customer_scheme)
    data = []
    for instalment in instalments:
        due_window_start, due_window_end = _build_installment_window(anchor_day, instalment.due_date)
        if instalment.instalment_no == 1 and first_paid_date:
            due_window_start = first_paid_date
            due_window_end = instalment.due_date
        data.append({
            "id": instalment.id,
            "instalment_no": instalment.instalment_no,
            "due_date": str(instalment.due_date),
            "due_window_start": str(due_window_start),
            "due_window_end": str(due_window_end),
            "amount": str(instalment.amount),
            "status": instalment.status.code if instalment and instalment.status else None,
            "is_bonus": instalment.is_bonus
        })
    
    return Response(data)


@api_view(['GET'])
@admin_auth(*ACCOUNTS_SCHEME_READ_AUTH, "CRM_SCHEME_ENROLLMENT_CREATE", "CRM_STORES_POS_VIEW")
def payment_modes_list(request):
    """
    GET /master/payment-modes/
    Returns active payment modes for dropdown: [{mode_code, mode_name}]
    Read-only lookup shared by Scheme Payments, Scheme Enrollment, and the Store POS form.
    """
    values = LookupValue.objects.filter(
        lookup__code='PAYMENT_MODE',
        lookup__is_active=True,
        is_active=True
    ).order_by('sort_order', 'label').values('code', 'label')
    data = [{"mode_code": v['code'], "mode_name": v['label']} for v in values]
    if str(request.GET.get('include_next_invoice', '')).lower() in ('1', 'true', 'yes'):
        try:
            bill_date = _parse_invoice_date(request.GET.get('invoice_date'))
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'modes': data,
            'next_invoice_number': peek_next_invoice_number(for_date=bill_date),
        })
    return Response(data)


@api_view(['POST'])
@admin_auth(*ACCOUNTS_SCHEME_WRITE_AUTH)
def admin_payment_initiation(request, instalment_id):
    """
    Initiate POS payment for a specific instalment.
    Supports single or split payment modes.
    Single: {"payment_mode": "CASH", "amount": 5000}
    Split: {"collections": [{"payment_mode_code": "UPI", "amount": 2000, "reference_number": "UPI123"}, {"payment_mode_code": "CASH", "amount": 8000}]}
    """
    try:
        print('instalment_id', instalment_id)
        instalment = SchemeInstalment.objects.get(id=instalment_id)

        try:
            paid_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
            active_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='ACTIVE')
            pending_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='PENDING')
        except LookupValue.DoesNotExist:
            return Response({"error": "Required status values not found"}, status=status.HTTP_404_NOT_FOUND)

        body_customer_id = request.data.get("customer_id")
        if body_customer_id is None:
            return Response(
                {"error": "customer_id is required to confirm instalment ownership"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            body_customer_id = int(body_customer_id)
        except (TypeError, ValueError):
            return Response({"error": "Invalid customer_id"}, status=status.HTTP_400_BAD_REQUEST)
        if body_customer_id != instalment.customer_scheme.customer_id:
            return Response(
                {"error": "Instalment does not belong to the selected customer"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        body_customer_code = request.data.get("customer_code")
        if body_customer_code is not None and str(body_customer_code).strip():
            owner = instalment.customer_scheme.customer
            occ = (owner.customer_code or "").strip()
            if occ and occ != str(body_customer_code).strip():
                return Response(
                    {"error": "customer_code does not match instalment owner"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if instalment.status == paid_status:
            return Response({"error": "Instalment already paid"}, status=status.HTTP_400_BAD_REQUEST)

        print(instalment)
        # Allow payment for both ACTIVE and PENDING schemes (first installment can be paid when scheme is PENDING)
        if instalment.customer_scheme.scheme_status not in [active_status, pending_status]:
            return Response({"error": "Scheme is not active or pending"}, status=status.HTTP_400_BAD_REQUEST)
        print(request.data.get("collections"))

        collections_data = request.data.get("collections")
        if collections_data:
            # Split or single via collections
            total = sum(Decimal(str(c.get("amount", 0))) for c in collections_data)
            amount = total
            if amount <= 0 or amount > instalment.amount:
                return Response(
                    {"error": "Collections sum must be between 1 and instalment amount"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            # Normalize collections for create_payment_with_collections
            collections_data = [
                {
                    "payment_mode_code": c.get("payment_mode_code") or c.get("payment_mode"),
                    "amount": str(c.get("amount")),
                    "reference_number": c.get("reference_number"),
                }
                for c in collections_data
            ]
            payment_mode_code = None
        else:
            # Single payment (legacy)
            payment_mode_code = request.data.get("payment_mode", "ONLINE")
            raw_amount = request.data.get("amount")
            if raw_amount is not None:
                amount = Decimal(str(raw_amount))
                if amount <= 0 or amount > instalment.amount:
                    return Response(
                        {"error": "Invalid payment amount. Must be between 1 and instalment amount"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                amount = Decimal(str(instalment.amount))
            collections_data = None

        # Get payment_date from payload for back-dated payments
        payment_date_str = request.data.get("payment_date")
        payment_date = None
        if payment_date_str:
            try:
                # Parse date directly without timezone to avoid conversion issues
                # Use date only to avoid time zone problems
                payment_date = datetime.strptime(payment_date_str, "%Y-%m-%d").date()

            except ValueError:
                return Response(
                    {"error": "Invalid payment_date format. Use YYYY-MM-DD"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        txnid = str(uuid.uuid4()).replace("-", "")[:20]

        try:
            payment = create_payment_with_collections(
                instalment=instalment,
                amount=Decimal(str(amount)),
                transaction_id=txnid,
                payment_status_code='SUCCESS',
                payment_source='POS',
                payment_mode_code=payment_mode_code if not collections_data else None,
                collections_data=collections_data,
                paid_at=payment_date if payment_date else timezone.now(),
                created_by=request.user,
            )
        except LookupValue.DoesNotExist:
            return Response({"error": "Required lookup values not found"}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # POS payments: finalize immediately (ledger, gold lock, total_paid)
        # For back-dated payments, get the gold rate for that specific date
        # payment_date can be date or datetime, pass it directly to handle both
        process_successful_payment(
            payment,

            gold_rate=get_lock_rate_for_scheme(instalment.customer_scheme.scheme, target_date=payment_date),

            payment_date=payment_date,
        )

        # Primary mode for response (single or first collection)
        primary_mode = payment.payment_mode.code if payment.payment_mode else (
            payment.collections.first().payment_mode.code if payment.collections.exists() else None
        )

        return Response({
            "message": "Payment successfully",
            "data": {
                "payment_id": payment.id,
                "receipt_no": payment.receipt_no,
                "transaction_id": txnid,
                "instalment_id": instalment.id,
                "amount": str(amount),
                "payment_mode": primary_mode,
                "is_split_payment": payment.is_split_payment,
                "status": "SUCCESS"
            }
        }, status=status.HTTP_201_CREATED)

    except SchemeInstalment.DoesNotExist:
        return Response({"error": "Instalment not found"}, status=status.HTTP_404_NOT_FOUND)


@api_view(['GET'])
@admin_auth("CRM_ACCOUNTS_PAYMENT_VERIFICATION_VIEW")
def payment_status(request, payment_id):
    """Get payment status."""
    try:
        payment = Payment.objects.prefetch_related('collections__payment_mode').get(id=payment_id)
        data = {
            "id": payment.id,
            "instalment_id": payment.instalment.id,
            "payment_mode": get_payment_mode_display(payment),
            "payment_status": payment.payment_status.code if payment.payment_status else None,
            "amount": str(payment.amount),
            "paid_at": payment.paid_at,
            "transaction_id": payment.transaction_id,
            "receipt_no": payment.receipt_no,
            "gateway_transaction_id": payment.gateway_transaction_id
        }
        return Response(data)
    except Payment.DoesNotExist:
        return Response({"error": "Payment not found"}, status=status.HTTP_404_NOT_FOUND)


@api_view(['GET'])
@admin_auth("CRM_ACCOUNTS_PAYMENT_VERIFICATION_VIEW")
def payment_verification_details(request, payment_id):
    """
    Get all details for the payment verification screen (Customer, Scheme, Payment tabs).
    Returns customer_details, scheme_details, and payment_details keyed by payment_id.
    """
    try:
        payment = Payment.objects.select_related(
            "instalment__customer_scheme__customer",
            "instalment__customer_scheme__scheme",
            "payment_mode",
            "payment_status",
        ).prefetch_related(
            "collections__payment_mode",
        ).get(id=payment_id)
    except Payment.DoesNotExist:
        return Response({"error": "Payment not found"}, status=status.HTTP_404_NOT_FOUND)

    instalment = payment.instalment
    customer_scheme = instalment.customer_scheme
    customer = customer_scheme.customer
    scheme = customer_scheme.scheme

    customer_details = {
        "customer_code": f"CUST-{customer.id}",
        "customer_name": customer.full_name,
        "mobile": customer.mobile or "",
    }

    scheme_details = {
        "customer_scheme_id": customer_scheme.id,
        "scheme_name": scheme.scheme_name,
        "scheme_code": scheme.scheme_code,
        "instalment_no": instalment.instalment_no,
        "expected_amount": str(instalment.amount),
        "tenure_months": customer_scheme.tenure_months or scheme.tenure_months,
    }

    collections_data = [
        {
            "payment_mode_code": c.payment_mode.code,
            "payment_mode_label": c.payment_mode.label or c.payment_mode.code,
            "amount": str(c.amount),
            "reference_number": c.reference_number,
        }
        for c in payment.collections.select_related("payment_mode").all()
    ] if payment.collections.exists() else None

    payment_details = {
        "payment_id": payment.id,
        "amount": str(payment.amount),
        "transaction_reference": payment.transaction_id,
        "receipt_no": payment.receipt_no,
        "gateway_transaction_id": payment.gateway_transaction_id or None,
        "payment_mode": get_payment_mode_display(payment),
        "payment_mode_label": get_payment_mode_label(payment),
        "payment_date": payment.paid_at.isoformat() if payment.paid_at else None,
        "payment_status": payment.payment_status.code if payment.payment_status else None,
        "uploaded_proof_url": None,
        "is_split_payment": payment.is_split_payment,
        "collections": collections_data,
    }

    return Response({
        "payment_id": payment.id,
        "customer_details": customer_details,
        "scheme_details": scheme_details,
        "payment_details": payment_details,
    })


@api_view(['GET'])
@admin_auth(
    "CRM_SCHEME_ENROLLMENT_VIEW",
    "CRM_CUSTOMER_UPCOMING_REMINDERS",
    "CRM_CUSTOMER_PAST_DUE_REMINDERS",
)
def upcoming_installment_reminders(request):
    """
    Get upcoming instalments due in next N days (default 7 days).
    """
    try:
        from shared.models import CommunicationLog

        days = int(request.GET.get("days", 7))
        today = timezone.localdate()
        end_date = today + timedelta(days=days)

        instalments = SchemeInstalment.objects.filter(
            due_date__range=[today, end_date],
            status__code="PENDING"
        ).select_related("customer_scheme", "customer_scheme__customer", "customer_scheme__scheme")

        inst_ids = [inst.id for inst in instalments]
        reminded_ids = set(
            CommunicationLog.objects.filter(
                ref_instalment_id__in=inst_ids,
                status=CommunicationLog.STATUS_SENT,
                message_type=CommunicationLog.TYPE_SCHEME_REMINDER,
            ).values_list("ref_instalment_id", flat=True)
        ) if inst_ids else set()

        data = []
        anchor_day_cache = {}
        first_paid_cache = {}
        for inst in instalments:
            anchor_day = _resolve_anchor_day(inst.customer_scheme, anchor_day_cache)
            first_paid_date = _resolve_first_paid_date(inst.customer_scheme, first_paid_cache)
            due_window_start, due_window_end = _build_installment_window(anchor_day, inst.due_date)
            if inst.instalment_no == 1 and first_paid_date:
                due_window_start = first_paid_date
                due_window_end = inst.due_date
            customer = inst.customer_scheme.customer
            data.append({
                "instalment_id": inst.id,
                "customer_id": customer.id,
                "customer_name": customer.full_name,
                "mobile": customer.mobile or "",
                "scheme_name": inst.customer_scheme.scheme.scheme_name,
                "due_date": str(inst.due_date),
                "due_window_start": str(due_window_start),
                "due_window_end": str(due_window_end),
                "amount": str(inst.amount),
                "instalment_no": inst.instalment_no,
                "days_until": (inst.due_date - today).days,
                "reminder_sent": inst.id in reminded_ids,
            })

        return Response(data)

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    

@api_view(['GET'])
@admin_auth(
    "CRM_SCHEME_ENROLLMENT_VIEW",
    "CRM_CUSTOMER_UPCOMING_REMINDERS",
    "CRM_CUSTOMER_PAST_DUE_REMINDERS",
)
def past_due_installments(request):
    """
    Get instalments that are past due and not paid.
    """
    try:
        from shared.models import CommunicationLog

        today = timezone.localdate()

        instalments = SchemeInstalment.objects.filter(
            due_date__lt=today,
            status__code="PENDING"
        ).select_related("customer_scheme", "customer_scheme__customer", "customer_scheme__scheme")

        inst_ids = [inst.id for inst in instalments]
        reminded_ids = set(
            CommunicationLog.objects.filter(
                ref_instalment_id__in=inst_ids,
                status=CommunicationLog.STATUS_SENT,
                message_type=CommunicationLog.TYPE_SCHEME_REMINDER,
            ).values_list("ref_instalment_id", flat=True)
        ) if inst_ids else set()

        data = []
        anchor_day_cache = {}
        first_paid_cache = {}
        for inst in instalments:
            anchor_day = _resolve_anchor_day(inst.customer_scheme, anchor_day_cache)
            first_paid_date = _resolve_first_paid_date(inst.customer_scheme, first_paid_cache)
            due_window_start, due_window_end = _build_installment_window(anchor_day, inst.due_date)
            if inst.instalment_no == 1 and first_paid_date:
                due_window_start = first_paid_date
                due_window_end = inst.due_date
            customer = inst.customer_scheme.customer
            data.append({
                "instalment_id": inst.id,
                "customer_id": customer.id,
                "customer_name": customer.full_name,
                "mobile": customer.mobile or "",
                "scheme_name": inst.customer_scheme.scheme.scheme_name,
                "due_date": str(inst.due_date),
                "due_window_start": str(due_window_start),
                "due_window_end": str(due_window_end),
                "amount": str(inst.amount),
                "instalment_no": inst.instalment_no,
                "days_overdue": (today - inst.due_date).days,
                "reminder_sent": inst.id in reminded_ids,
                "penalty_applied": False,
            })

        return Response(data)

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


def build_scheme_response(queryset):
    """
    Reusable utility method to build standardized scheme response structure.
    """
    data = []
    for cs in queryset:
        # Calculate maturity date using relativedelta for accurate month calculation
        start_date = timezone.localtime(cs.applied_at).date() if cs.applied_at else (cs.start_date if cs.start_date else None)
        maturity_date = (start_date + relativedelta(months=cs.scheme.tenure_months)) if start_date else (cs.end_date if cs.end_date else None)
        
        # Get benefit type from CustomerSchemeBenefit (return first benefit type if multiple)
        benefit_type = cs.benefits.first().benefit_type if cs.benefits.exists() else None
        
        data.append({
            "customer_scheme_id": cs.id,
            "customer_name": cs.customer.full_name,
            "scheme_code": cs.scheme.scheme_code,
            "scheme_name": cs.scheme.scheme_name,
            "total_tenure_months": cs.scheme.tenure_months,
            "start_date": str(start_date),
            "maturity_date": str(maturity_date),
            "monthly_installment_amount": str(cs.monthly_amount),
            "status": cs.scheme_status.code,
            "benefit_type": benefit_type
        })
    return data


@api_view(['GET'])
@admin_auth(*SCHEME_LIST_READ_AUTH)
def active_schemes(request):
    """
    List all active customer schemes.
    """
    try:
        schemes = CustomerScheme.objects.filter(
            scheme_status__code="ACTIVE"
        ).select_related("customer", "scheme").prefetch_related("benefits")

        data = build_scheme_response(schemes)
        return Response(data)

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@admin_auth(*SCHEME_LIST_READ_AUTH)
def missed_schemes(request):
    """
    Schemes with at least one overdue instalment.
    """
    try:
        today = timezone.localdate()

        overdue_scheme_ids = SchemeInstalment.objects.filter(
            due_date__lt=today,
            status__code="PENDING"
        ).values_list("customer_scheme_id", flat=True).distinct()

        schemes = CustomerScheme.objects.filter(
            id__in=overdue_scheme_ids
        ).select_related("customer", "scheme").prefetch_related("benefits")

        data = build_scheme_response(schemes)
        return Response(data)

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@admin_auth(*SCHEME_LIST_READ_AUTH)
def completed_schemes(request):
    """
    List completed schemes.
    """
    try:
        schemes = CustomerScheme.objects.filter(
            scheme_status__code="COMPLETED"
        ).select_related("customer", "scheme").prefetch_related("benefits")

        data = build_scheme_response(schemes)
        return Response(data)

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@admin_auth(*SCHEME_DASHBOARD_READ_AUTH)
def scheme_dashboard_summary(request):
    """
    Dashboard summary for schemes.
    """
    try:
        total_schemes = CustomerScheme.objects.count()
        active_schemes = CustomerScheme.objects.filter(scheme_status__code="ACTIVE").count()
        completed_schemes = CustomerScheme.objects.filter(scheme_status__code="COMPLETED").count()

        total_collection = Payment.objects.filter(
            payment_status__code="SUCCESS"
        ).aggregate(total=Sum("amount"))["total"] or 0

        return Response({
            "total_schemes": total_schemes,
            "active_schemes": active_schemes,
            "completed_schemes": completed_schemes,
            "total_collection": str(total_collection)
        })

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@admin_auth(*SCHEME_DASHBOARD_READ_AUTH, "CRM_ACCOUNTS_COLLECTION_SUMMARY_VIEW")
def collection_summary(request):
    """
    Collection summary grouped by date.
    """
    try:
        collections = Payment.objects.filter(
            payment_status__code="SUCCESS"
        ).values("paid_at__date").annotate(
            total_amount=Sum("amount"),
            total_payments=Count("id")
        ).order_by("-paid_at__date")

        data = [{
            "date": str(item["paid_at__date"]),
            "total_amount": str(item["total_amount"]),
            "total_payments": item["total_payments"]
        } for item in collections]

        return Response(data)

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@admin_auth("CRM_ACCOUNTS_COLLECTION_SUMMARY_VIEW")
def payment_mode_collection_summary(request):
    """
    Collection summary by payment mode using payment_collections.
    Returns: Mode | Amount (e.g. Cash 20000, UPI 30000, Cheque 50000).
    Query params: date_from, date_to (YYYY-MM-DD).
    """
    from shared.services.ledger_service import get_payment_mode_collection_summary
    from django.utils.dateparse import parse_date

    try:
        date_from = parse_date(request.GET.get('date_from')) if request.GET.get('date_from') else None
        date_to = parse_date(request.GET.get('date_to')) if request.GET.get('date_to') else None
        data = get_payment_mode_collection_summary(date_from=date_from, date_to=date_to)
        return Response(data)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@admin_auth(*SCHEME_DASHBOARD_READ_AUTH)
def scheme_recent_activity(request):
    """
    Get recent payments and enrollments.
    """
    try:
        recent_payments = Payment.objects.select_related(
            "instalment__customer_scheme__customer"
        ).order_by("-paid_at")[:10]

        data = []

        for payment in recent_payments:
            data.append({
                "type": "PAYMENT",
                "customer_name": payment.instalment.customer_scheme.customer.full_name,
                "amount": str(payment.amount),
                "transaction_id": payment.transaction_id,
                "date": payment.paid_at
            })

        return Response(data)

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@admin_auth(*SCHEME_DASHBOARD_READ_AUTH)
def scheme_recent_enrollments(request):
    """
    Get latest 5 customer scheme enrollments for dashboard (customer + scheme name, link to scheme details).
    """
    try:
        recent = (
            CustomerScheme.objects.select_related("customer", "scheme")
            .order_by("-applied_at")[:5]
        )
        data = [
            {
                "id": cs.id,
                "customer_name": cs.customer.full_name,
                "scheme_name": cs.scheme.scheme_name,
                "applied_at": cs.applied_at.isoformat() if cs.applied_at else None,
            }
            for cs in recent
        ]
        return Response(data)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)



@api_view(['GET'])
@admin_auth(*ACCOUNTS_INSTALMENT_READ_AUTH)
def installment_records(request):
    """
    List all installment records with filters.
    """
    try:
        queryset = SchemeInstalment.objects.select_related(
            "customer_scheme__customer",
            "customer_scheme__scheme"
        )

        status_filter = request.GET.get("status")
        if status_filter:
            queryset = queryset.filter(status__code=status_filter)

        data = []
        anchor_day_cache = {}
        first_paid_cache = {}
        for inst in queryset.order_by("-due_date"):
            anchor_day = _resolve_anchor_day(inst.customer_scheme, anchor_day_cache)
            first_paid_date = _resolve_first_paid_date(inst.customer_scheme, first_paid_cache)
            due_window_start, due_window_end = _build_installment_window(anchor_day, inst.due_date)
            if inst.instalment_no == 1 and first_paid_date:
                due_window_start = first_paid_date
                due_window_end = inst.due_date
            data.append({
                "instalment_id": inst.id,
                "customer_name": inst.customer_scheme.customer.full_name,
                "scheme_name": inst.customer_scheme.scheme.scheme_name,
                "instalment_no": inst.instalment_no,
                "amount": str(inst.amount),
                "due_date": str(inst.due_date),
                "due_window_start": str(due_window_start),
                "due_window_end": str(due_window_end),
                "status": inst.status.code if inst.status else None
            })

        return Response(data)

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@admin_auth(
    "CRM_ACCOUNTS_PAYMENT_VERIFICATION_VIEW",
    "CRM_ACCOUNTS_PAYMENT_VERIFICATION",
)
def pending_finance(request):
    """
    List payments pending for finance processing.
    Returns all details used in the pending-finance table: Customer Name, Scheme ID,
    Amount, Payment Mode, Transaction Reference, Submitted Date, Status, and id for Actions.
    """
    try:
        payments = Payment.objects.filter(
            payment_status__code="SUCCESS",
            instalment__isnull=False,
        ).select_related(
            "instalment__customer_scheme__customer",
            "instalment__customer_scheme__scheme",
            "payment_mode",
            "payment_status",
        ).prefetch_related(
            "collections__payment_mode",
        ).order_by("-paid_at")

        data = []

        for payment in payments:
            instalment = payment.instalment
            if not instalment:
                continue
            customer_scheme = instalment.customer_scheme
            if not customer_scheme or not customer_scheme.customer or not customer_scheme.scheme:
                continue

            data.append({
                "id": payment.id,
                "customer_name": customer_scheme.customer.full_name,
                "scheme_id": customer_scheme.id,
                "scheme_code": customer_scheme.scheme.scheme_code,
                "amount": str(payment.amount),
                "payment_mode": get_payment_mode_display(payment),
                "receipt_no": payment.receipt_no,
                "transaction_reference": payment.transaction_id,
                "gateway_transaction_id": payment.gateway_transaction_id or None,
                "submitted_date": payment.paid_at.isoformat() if payment.paid_at else None,
                "payment_date": payment.paid_at.isoformat() if payment.paid_at else None,
                "status": payment.payment_status.code if payment.payment_status else None,
                "instalment_no": instalment.instalment_no,
            })

        return Response(data)

    except Exception as e:
        logger.exception("pending_finance failed")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@admin_auth(*SCHEME_ENROLLMENT_WRITE_AUTH)
@transaction.atomic
def abandon_customer_scheme(request, customer_scheme_id):
    """
    Force abandon a customer scheme (admin only).
    Uses customer_scheme_id (not master scheme ID).
    All status values from LookupValue – no hardcoded status codes/ids.
    No bonus; maturity_amount = total_paid_amount (sum of PAID installments).
    Locks further installments (PENDING -> CANCELLED via lookup).
    """
    # Fetch required statuses from lookup table (no hardcoded values)
    try:
        active_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='ACTIVE')
        abandoned_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='ABANDONED')
        paid_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
        pending_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
        cancelled_inst = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='CANCELLED')
    except LookupValue.DoesNotExist:
        return Response(
            {"error": "Required lookup value not found. Ensure SCHEME_STATUS (ACTIVE, ABANDONED) and INSTALLMENT_STATUS (PAID, PENDING, CANCELLED) exist."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    # 1) Fetch CustomerScheme; if not found → 404
    try:
        customer_scheme = CustomerScheme.objects.select_related('scheme_status').get(id=customer_scheme_id)
    except CustomerScheme.DoesNotExist:
        return Response({"error": "Customer scheme not found."}, status=status.HTTP_404_NOT_FOUND)

    # Validate scheme status using lookup object (do not compare with numbers)
    if customer_scheme.scheme_status != active_status:
        return Response(
            {"error": "Scheme must be ACTIVE to abandon. It is already COMPLETED, ABANDONED, or CANCELLED."},
            status=status.HTTP_400_BAD_REQUEST
        )

    # 2) Financial calculation: only installments with status = PAID (from lookup)
    paid_aggregate = SchemeInstalment.objects.filter(
        customer_scheme=customer_scheme,
        status__code='PAID'
    ).aggregate(total=Sum('amount'))
    total_paid_amount = (paid_aggregate.get('total') or Decimal('0.00'))
    bonus_amount = Decimal('0.00')
    maturity_amount = total_paid_amount

    now = timezone.now()
    abandoned_reason = request.data.get('abandoned_reason', '')

    # 3) Update CustomerScheme using lookup object (status alone defines lifecycle; no is_active)
    customer_scheme.scheme_status = abandoned_status
    customer_scheme.closed_at = now
    customer_scheme.maturity_amount = maturity_amount
    customer_scheme.bonus_amount = bonus_amount
    customer_scheme.abandoned_by = request.user
    customer_scheme.abandoned_reason = abandoned_reason
    customer_scheme.abandoned_at = now
    customer_scheme.save(update_fields=[
        'scheme_status', 'closed_at', 'maturity_amount', 'bonus_amount',
        'abandoned_by', 'abandoned_reason', 'abandoned_at', 'system_updated_at'
    ])

    # 4) Lock installments: PENDING -> CANCELLED (using lookup)
    SchemeInstalment.objects.filter(
        customer_scheme=customer_scheme,
        status__code='PENDING'
    ).update(status=cancelled_inst)

    return Response({
        "status": "success",
        "message": "Customer scheme abandoned successfully",
        "customer_scheme_id": customer_scheme_id,
        "total_paid_amount": float(total_paid_amount),
        "bonus_amount": 0,
        "maturity_amount": float(maturity_amount),
        "scheme_status": "ABANDONED"
    }, status=status.HTTP_200_OK)
