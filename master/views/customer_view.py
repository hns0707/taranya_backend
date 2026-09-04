from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from datetime import timedelta, datetime, date
import calendar
from decimal import Decimal


from django.db.models import Q, Sum, Count, Max, Subquery, OuterRef, F
from django.core.paginator import Paginator
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from django.contrib.auth.hashers import make_password

from shared.models import (
    Customer, CustomerAddress, CustomerScheme, LookupValue, CustomerNominee, CustomerKYC, SchemeInstalment, GoldLockingRecord, Payment, Redemption,
    SaleInvoice, CatalogueQuote, CrmCustomerVisit, CrmServiceTicket,
)
from shared.helper import get_payment_mode_display, get_payment_mode_label
from master.permissions.permission_checker import admin_auth, ensure_admin_permission
from master.permissions.section_auth import (
    CUSTOMER_READ_AUTH,
    CUSTOMER_LOOKUP_AUTH,
    CUSTOMER_WRITE_AUTH,
    CUSTOMER_DELETE_AUTH,
    SCHEME_LIST_READ_AUTH,
)

import re, uuid, secrets
from datetime import datetime
from dateutil.relativedelta import relativedelta
def generate_customer_code(full_name, date_of_birth):

    if full_name:
        name_clean = re.sub(r'[^A-Za-z]', '', full_name)
        name_part = name_clean[:3].upper().ljust(3, 'X')
    else:
        name_part = "CUS"

    dob_part = "000000"
    if date_of_birth:
        try:
            if isinstance(date_of_birth, str):
                dob_obj = datetime.strptime(date_of_birth, "%Y-%m-%d").date()
            else:
                dob_obj = date_of_birth

            dob_part = dob_obj.strftime("%d%m%y")
        except:
            pass

    prefix = f"{name_part}{dob_part}"

    # ✅ Ensure uniqueness
    while True:
        uuid_part = str(uuid.uuid4().int)[:4]
        code = f"{prefix}-{uuid_part}"

        if not Customer.objects.filter(customer_code=code).exists():
            return code


def generate_customer_password(full_name, date_of_birth=None, mobile=None):
    """
    Password rule (admin-managed):
    1) If DOB exists: first 4 letters of name + DDMMYY
    2) Else if mobile exists: first 6 digits of mobile
    3) Else: random 6 digits
    """
    if date_of_birth:
        name_clean = re.sub(r"[^A-Za-z]", "", str(full_name or "")).upper()
        name_part = name_clean[:4].ljust(4, "X")
        try:
            if isinstance(date_of_birth, str):
                dob_obj = datetime.strptime(date_of_birth, "%Y-%m-%d").date()
            else:
                dob_obj = date_of_birth
            return f"{name_part}{dob_obj.strftime('%d%m%y')}"
        except Exception:
            pass

    mobile_digits = re.sub(r"\D", "", str(mobile or ""))
    if mobile_digits:
        return mobile_digits[:6].ljust(6, "0")

    return "".join(secrets.choice("0123456789") for _ in range(6))




@api_view(["GET", "POST"])
@admin_auth()
def customer_list_create(request):
    """
    List and create customers
    """

    if request.method == "GET":
        denied = ensure_admin_permission(request, *CUSTOMER_READ_AUTH)
        if denied:
            return denied
    else:
        denied = ensure_admin_permission(request, *CUSTOMER_WRITE_AUTH)
        if denied:
            return denied

    if request.method == "GET":
        from django.db.models import Prefetch

        queryset = Customer.objects.all()

        is_active = request.GET.get("is_active")
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)

        search = (request.GET.get("search") or "").strip()
        if search:
            q = (
                Q(full_name__icontains=search)
                | Q(mobile__icontains=search)
                | Q(email__icontains=search)
                | Q(customer_code__icontains=search)
            )
            if search.isdigit():
                q = q | Q(id=int(search))
            queryset = queryset.filter(q)

        last_visit_sq = (
            CrmCustomerVisit.objects.filter(customer_id=OuterRef("pk"))
            .order_by("-visited_at")
            .values("visited_at")[:1]
        )
        last_sale_sq = (
            SaleInvoice.objects.filter(customer_id=OuterRef("pk"), is_deleted=False)
            .order_by("-invoice_date", "-id")
            .values("invoice_date")[:1]
        )
        last_inv_addr_sq = (
            SaleInvoice.objects.filter(customer_id=OuterRef("pk"), is_deleted=False)
            .exclude(bill_to_address="")
            .order_by("-invoice_date", "-id")
            .values("bill_to_address")[:1]
        )

        queryset = queryset.annotate(
            last_visit_at=Subquery(last_visit_sq),
            last_sale_date=Subquery(last_sale_sq),
            last_invoice_address=Subquery(last_inv_addr_sq),
        ).prefetch_related(
            Prefetch(
                "addresses",
                queryset=CustomerAddress.objects.filter(is_active=True).order_by("-is_default", "id"),
                to_attr="_active_addresses",
            )
        )

        ordering = (request.GET.get("ordering") or "last_connected_at").strip()
        allowed = {
            "last_connected_at",
            "-last_connected_at",
            "system_created_at",
            "-system_created_at",
            "full_name",
            "-full_name",
            "id",
            "-id",
        }
        if ordering not in allowed:
            ordering = "last_connected_at"

        def _to_aware(val):
            if val is None:
                return None
            if isinstance(val, datetime):
                if timezone.is_naive(val):
                    return timezone.make_aware(val, timezone.get_current_timezone())
                return val
            if isinstance(val, date):
                return timezone.make_aware(
                    datetime.combine(val, datetime.min.time()),
                    timezone.get_current_timezone(),
                )
            return None

        enriched = []
        for c in queryset:
            visit_at = getattr(c, "last_visit_at", None)
            sale_d = getattr(c, "last_sale_date", None)
            candidates = [
                x for x in (_to_aware(visit_at), _to_aware(c.last_login_at), _to_aware(sale_d)) if x
            ]
            last_connected = max(candidates) if candidates else None
            addrs = getattr(c, "_active_addresses", None) or []
            addr = addrs[0] if addrs else None
            address_text = ""
            if addr:
                address_text = ", ".join(
                    p
                    for p in [
                        addr.address_line1,
                        addr.address_line2,
                        addr.city,
                        addr.state,
                        addr.pincode,
                    ]
                    if p
                )
            if not address_text:
                address_text = (getattr(c, "last_invoice_address", None) or "").strip()
            enriched.append((c, last_connected, address_text, visit_at))

        reverse = ordering.startswith("-")
        key = ordering.lstrip("-")
        if key == "last_connected_at":
            # Oldest → earliest: never-connected first, then oldest contact date
            enriched.sort(key=lambda t: (t[1] is not None, t[1] or timezone.now()), reverse=reverse)
        elif key == "full_name":
            enriched.sort(key=lambda t: (t[0].full_name or "").lower(), reverse=reverse)
        elif key == "system_created_at":
            enriched.sort(key=lambda t: t[0].system_created_at or timezone.now(), reverse=reverse)
        elif key == "id":
            enriched.sort(key=lambda t: t[0].id, reverse=reverse)

        data = [
            {
                "id": c.id,
                "customer_code": c.customer_code,
                "full_name": c.full_name,
                "mobile": c.mobile,
                "email": c.email,
                "date_of_birth": c.date_of_birth,
                "anniversary_date": getattr(c, "anniversary_date", None),
                "wedding_date": getattr(c, "wedding_date", None),
                "family_group": getattr(c, "family_group", None),
                "is_active": c.is_active,
                "address": address_text or None,
                "last_connected_at": last_connected.isoformat() if last_connected else None,
                "last_visit_at": visit_at.isoformat() if visit_at else None,
                "last_login_at": c.last_login_at.isoformat() if c.last_login_at else None,
                "system_created_at": c.system_created_at,
            }
            for c, last_connected, address_text, visit_at in enriched
        ]

        return Response({
            "count": len(data),
            "results": data,
        })

    # ================== POST ==================

    data = request.data

    # ✅ Parse DOB properly
    dob = data.get("date_of_birth")
    if dob:
        try:
            dob = datetime.strptime(dob, "%Y-%m-%d").date()
        except Exception:
            dob = None

    email = data.get("email")
    if email is not None and str(email).strip() == "":
        email = None

    gender_raw = data.get("gender")
    gender = gender_raw if gender_raw in ("M", "F", "O") else None

    generated_password = generate_customer_password(
        data.get("full_name"),
        dob,
        data.get("mobile"),
    )

    # ✅ Create new customer
    customer = Customer.objects.create(
        full_name=data.get("full_name"),
        mobile=data.get("mobile"),
        email=email,
        gender=gender,
        date_of_birth=dob,
        password_hash=make_password(generated_password),
        is_active=data.get("is_active", True),
        gst_number=_normalize_gst_number(data.get("gst_number")),
        aadhaar_number=_normalize_aadhaar_number(data.get("aadhaar_number")),
    )

    # ✅ Generate NEW FORMAT CODE
    customer.customer_code = generate_customer_code(
        customer.full_name,
        customer.date_of_birth
    )
    customer.save(update_fields=["customer_code"])

    # PAN handling
    handle_customer_pan(customer, data.get("pan_number"))

    return Response({
        "message": "Customer created successfully",
        "data": {
            "id": customer.id,
            "customer_code": customer.customer_code,
            "full_name": customer.full_name,
            "mobile": customer.mobile,
            "email": customer.email,
            "generated_password": generated_password,
            "is_active": customer.is_active,
            "system_created_at": customer.system_created_at,
        }
    }, status=status.HTTP_201_CREATED)


def _customer_admin_lookup_payload(customer):
    """Shared shape for by-mobile / by-code admin lookups (scheme payment, enrollment)."""
    addresses = CustomerAddress.objects.filter(
        customer=customer, is_active=True
    ).values(
        "id", "address_line1", "address_line2",
        "city", "state", "pincode", "country", "is_default"
    )

    schemes = CustomerScheme.objects.filter(customer=customer).select_related('scheme_status')
    schemes_data = []
    nominees = {}
    for cs in schemes:
        scheme_data = {
            "id": cs.id,
            "monthly_amount": cs.monthly_amount,
            "scheme_status": cs.scheme_status.code,
            "applied_at": cs.applied_at,
            "activated_at": cs.activated_at,
            "closed_at": cs.closed_at
        }
        schemes_data.append(scheme_data)

        nominees[cs.id] = list(
            CustomerNominee.objects.filter(
                customer_scheme_id=cs.id
            ).values(
                "id", "full_name", "relationship",
                "mobile", "share_percentage"
            )
        )

    kyc = CustomerKYC.objects.filter(customer=customer).first()

    return {
        "id": customer.id,
        "customer_code": customer.customer_code,
        "full_name": customer.full_name,
        "mobile": customer.mobile,
        "email": customer.email,
        "gender": customer.gender,
        "date_of_birth": customer.date_of_birth,
        "is_active": customer.is_active,
        "last_login_at": customer.last_login_at,
        "system_created_at": customer.system_created_at,
        "addresses": list(addresses),
        "schemes": schemes_data,
        "kyc_status": kyc.status.code if kyc else None,
        "nominees": nominees
    }


@api_view(["GET"])
@admin_auth(*CUSTOMER_LOOKUP_AUTH)
def customer_by_mobile(request, mobile):
    """
    Lookup customer by mobile for admin flows (scheme enrollment, POS payment).
    Returns 409 with candidates when multiple rows share the same mobile (non-unique mobile).
    """
    qs = Customer.objects.filter(mobile=mobile).order_by("id")
    count = qs.count()
    if count == 0:
        return Response(
            {"error": "Customer not found with this mobile number"},
            status=status.HTTP_404_NOT_FOUND,
        )
    if count > 1:
        candidates = []
        for c in qs:
            addr = (
                CustomerAddress.objects.filter(customer=c, is_active=True)
                .order_by("-is_default", "id")
                .first()
            )
            candidates.append({
                "id": c.id,
                "customer_code": c.customer_code,
                "full_name": c.full_name,
                "mobile": c.mobile,
                "email": c.email,
                "city": addr.city if addr else None,
                "pincode": addr.pincode if addr else None,
            })
        return Response(
            {
                "error": "Multiple customers share this mobile number. Select one by id or customer_code.",
                "candidates": candidates,
            },
            status=status.HTTP_409_CONFLICT,
        )
    customer = qs.first()
    return Response(_customer_admin_lookup_payload(customer))


@api_view(["GET"])
@admin_auth(*CUSTOMER_LOOKUP_AUTH)
def customer_by_code(request, customer_code):
    """
    Lookup customer by unique customer_code (admin POS / enrollment).
    """
    raw = (customer_code or "").strip()
    if not raw:
        return Response(
            {"error": "customer_code is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        customer = Customer.objects.get(customer_code__iexact=raw)
    except Customer.DoesNotExist:
        return Response(
            {"error": "Customer not found with this customer_code"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(_customer_admin_lookup_payload(customer))

@api_view(["GET"])
@admin_auth(*CUSTOMER_READ_AUTH, *SCHEME_LIST_READ_AUTH)
def customer_active_schemes(request, pk):
    """
    API endpoint to get active schemes for a customer with detailed payment information.
    """
    try:
        customer = Customer.objects.get(id=pk)
    except Customer.DoesNotExist:
        return Response(
            {"error": "Customer not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    # Get active schemes
    try:
        active_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='ACTIVE')
        paid_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
        pending_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
    except LookupValue.DoesNotExist:
        return Response(
            {"error": "Required status values not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    active_schemes = CustomerScheme.objects.filter(
        customer=customer,
        scheme_status=active_status
    ).select_related("scheme")

    data = []
    for cs in active_schemes:
        # Get all installments for the scheme with aggregates

        # Calculate payment statistics using ORM aggregations
        stats = SchemeInstalment.objects.filter(customer_scheme=cs).aggregate(
            total_installments=Count('id'),
            paid_installments=Count('id', filter=Q(status=paid_status)),
            pending_installments=Count('id', filter=Q(status=pending_status)),
            total_paid_amount=Sum('amount', filter=Q(status=paid_status)),
            total_pending_amount=Sum('amount', filter=Q(status=pending_status))
        )

        data.append({
            "scheme_id": cs.id,
            "scheme_name": cs.scheme.scheme_name,
            "start_date": str(cs.start_date) if cs.start_date else None,
            "end_date": str(cs.end_date) if cs.end_date else None,
            "total_installments": stats['total_installments'],
            "paid_installments": stats['paid_installments'],
            "pending_installments": stats['pending_installments'],
            "total_paid_amount": str(stats['total_paid_amount'] or 0),
            "total_pending_amount": str(stats['total_pending_amount'] or 0),
            "monthly_amount": str(cs.monthly_amount)
        })

    return Response({
        "customer_id": customer.id,
        "customer_name": customer.full_name,
        "active_schemes": data
    })


@api_view(["GET"])
@admin_auth(*CUSTOMER_READ_AUTH, "CRM_ACCOUNTS_COLLECTION_SUMMARY_VIEW")
def customer_payments(request, pk):
    """
    Return all paid payment details for a customer by customer id.
    Includes payments with payment_status in (PAID, SUCCESS, SUCCESS).
    """
    try:
        customer = Customer.objects.get(id=pk)
    except Customer.DoesNotExist:
        return Response(
            {"error": "Customer not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    paid_status_codes = ('PAID', 'SUCCESS', 'SUCCESS')
    paid_status_ids = list(
        LookupValue.objects.filter(
            lookup__code='PAYMENT_STATUS',
            code__in=paid_status_codes
        ).values_list('id', flat=True)
    )
    if not paid_status_ids:
        return Response(
            {"error": "Required status values not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    payments = Payment.objects.filter(
        instalment__customer_scheme__customer=customer,
        payment_status_id__in=paid_status_ids
    ).select_related(
        'instalment__customer_scheme__scheme',
        'payment_mode',
        'payment_status'
    ).prefetch_related(
        'collections__payment_mode',
    ).order_by('-paid_at')

    data = []
    for payment in payments:
        instalment = payment.instalment
        customer_scheme = instalment.customer_scheme
        data.append({
            "payment_id": payment.id,
            "instalment_id": instalment.id,
            "instalment_number": instalment.instalment_no,
            "scheme_name": customer_scheme.scheme.scheme_name,
            "scheme_id": customer_scheme.scheme.id,
            "customer_scheme_id": customer_scheme.id,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
            "payment_mode": get_payment_mode_label(payment),
            "payment_mode_code": get_payment_mode_display(payment),
            "receipt_no": payment.receipt_no,
            "transaction_id": payment.transaction_id,
            "gateway_transaction_id": payment.gateway_transaction_id,
            "payment_status": payment.payment_status.code if payment.payment_status else None,
        })

    return Response({
        "customer_id": customer.id,
        "customer_name": customer.full_name,
        "mobile": customer.mobile,
        "payments": data,
        "total_count": len(data),
    })


@api_view(["GET"])
@admin_auth(*CUSTOMER_READ_AUTH, *SCHEME_LIST_READ_AUTH)
def customer_scheme_details(request, customer_scheme_id):
    """
    API endpoint to fetch complete Scheme Details using customer_scheme_id.
    Returns summary, progress, installment history, and benefit details.
    """
    try:
        # Fetch customer scheme with related data (optimized query)
        customer_scheme = CustomerScheme.objects.select_related(
            'customer', 'scheme', 'scheme_status'
        ).prefetch_related(
            'benefits',
            'schemeinstalment_set'
        ).get(id=customer_scheme_id)
        
        # Get installment status lookup values
        paid_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
        pending_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
        overdue_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='OVERDUE')
        
        # Calculate progress statistics
        installments = SchemeInstalment.objects.filter(customer_scheme=customer_scheme)
        total_installments = installments.count()
        installments_paid_count = installments.filter(status=paid_status).count()
        total_paid_amount = installments.filter(status=paid_status).aggregate(total=Sum('amount'))['total'] or 0
        # accumulated_gold_grams = customer_scheme.accumulated_gold_grams
        
        # Prefer first successful payment date as scheme start date for report card display.
        first_payment = (
            Payment.objects.filter(
                instalment__customer_scheme=customer_scheme,
                payment_status__code='SUCCESS'
            )
            .exclude(paid_at__isnull=True)
            .order_by('paid_at')
            .first()
        )
        first_paid_date = timezone.localtime(first_payment.paid_at).date() if first_payment and first_payment.paid_at else None

        tenure_months = customer_scheme.tenure_months or customer_scheme.scheme.tenure_months
        maturity_date = customer_scheme.end_date
        if first_paid_date and tenure_months:
            maturity_month = first_paid_date + relativedelta(months=tenure_months - 1)
            maturity_date = date(
                maturity_month.year,
                maturity_month.month,
                calendar.monthrange(maturity_month.year, maturity_month.month)[1],
            )

        # Get next due date
        next_due_date = None
        next_pending_installment = None
        pending_installments = installments.filter(status=pending_status).order_by('due_date')
        if pending_installments.exists():
            next_pending_installment = pending_installments.first()
            next_due_date = next_pending_installment.due_date
        
        # Get overdue installments count
        overdue_installments_count = installments.filter(status=overdue_status).count()
        
        anchor_day = (
            first_paid_date.day
            if first_paid_date
            else timezone.localtime(customer_scheme.applied_at).date().day
            if customer_scheme.applied_at
            else customer_scheme.start_date.day
            if customer_scheme.start_date
            else 1
        )

        # Prepare installment history
        installment_history = []
        for installment in installments.order_by('instalment_no'):
            due_window_start, due_window_end = _build_installment_window_for_sequence(
                anchor_day,
                installment.instalment_no,
                first_paid_date,
                installment.due_date,
            )
            # Get payment date from Payment record or gold locking record
            payment_date = None
            
            # First check Payment table (works for all scheme types)
            payment = Payment.objects.filter(
                instalment=installment,
                payment_status__code='SUCCESS'
            ).first()
            if payment and payment.paid_at:
                payment_date = payment.paid_at
            
            # Also check gold locking record if exists (for gold schemes)
            if not payment_date:
                gold_locking_record = GoldLockingRecord.objects.filter(instalment=installment).first()
                if gold_locking_record:
                    payment_date = gold_locking_record.payment_date
            
            installment_history.append({
                "installment_no": installment.instalment_no,
                "due_date": str(installment.due_date),
                "due_window_start": str(due_window_start),
                "due_window_end": str(due_window_end),
                "amount": str(installment.amount),
                "status": installment.status.code,
                "gold_grams": str(installment.gold_grams or 0),
                "payment_date": str(payment_date) if payment_date else None
            })
        
        # Prepare benefit details
        benefit_details = None
        if customer_scheme.benefits.exists():
            benefit = customer_scheme.benefits.first()
            benefit_details = {
                "benefit_type": benefit.benefit_type,
                "benefit_value": str(benefit.benefit_value) if benefit.benefit_value else None,
                "benefit_percentage": str(benefit.benefit_percentage) if benefit.benefit_percentage else None,
                "bonus_month": benefit.benefit_months
            }
        
        # Prepare response data
        response_data = {
            "summary": {
                "customer_scheme_id": customer_scheme.id,
                "scheme_code": customer_scheme.scheme.scheme_code,
                "scheme_name": customer_scheme.scheme.scheme_name,
                "customer_name": customer_scheme.customer.full_name,
                "status": customer_scheme.scheme_status.code,
                "start_date": str(first_paid_date) if first_paid_date else str(customer_scheme.start_date) if customer_scheme.start_date else str(timezone.localtime(customer_scheme.applied_at).date()) if customer_scheme.applied_at else None,
                "maturity_date": str(maturity_date) if maturity_date else None,
                "total_tenure_months": tenure_months,
                "monthly_installment_amount": str(customer_scheme.monthly_amount)
            },
            "progress": {
                "total_installments": total_installments,
                "installments_paid": installments_paid_count,
                "total_paid_amount": str(total_paid_amount),
                # "accumulated_gold_grams": str(accumulated_gold_grams),
                "next_due_date": str(next_due_date) if next_due_date else None,
                "next_due_window_start": str(
                    _build_installment_window_for_sequence(
                        anchor_day,
                        next_pending_installment.instalment_no,
                        first_paid_date,
                        next_due_date,
                    )[0]
                ) if next_due_date and next_pending_installment else None,
                "next_due_window_end": str(
                    _build_installment_window_for_sequence(
                        anchor_day,
                        next_pending_installment.instalment_no,
                        first_paid_date,
                        next_due_date,
                    )[1]
                ) if next_due_date and next_pending_installment else None,
                "overdue_installments": overdue_installments_count
            },
            "installments": installment_history,
            "benefit": benefit_details
        }
        
        return Response(response_data)
        
    except CustomerScheme.DoesNotExist:
        return Response({"error": "Scheme not found"}, status=status.HTTP_404_NOT_FOUND)
    except LookupValue.DoesNotExist:
        return Response({"error": "Required status values not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["GET", "PUT", "DELETE"])
@admin_auth()
def customer_detail(request, pk):
    try:
        customer = Customer.objects.select_related("referred_by").get(id=pk)
    except Customer.DoesNotExist:
        return Response(
            {"error": "Customer not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    if request.method == "GET":
        denied = ensure_admin_permission(request, *CUSTOMER_READ_AUTH)
        if denied:
            return denied
        addresses = CustomerAddress.objects.filter(
            customer=customer, is_active=True
        ).values(
            "id", "address_line1", "address_line2",
            "city", "state", "pincode", "country", "is_default"
        )

        # All customer_scheme records for this customer (multiple per scheme allowed)
        # Using values() so we get one dict per DB row - no ORM deduplication
        schemes = list(
            CustomerScheme.objects.filter(customer=customer)
            .order_by("-system_created_at", "-system_updated_at")
            .values(
                "id",
                "monthly_amount",
                "scheme_status_id",
                "applied_at",
                "activated_at",
                "closed_at",
            )
            .annotate(
                scheme_name=F("scheme__scheme_name"),
                scheme_code=F("scheme__scheme_code"),
                scheme_status_name=F("scheme_status__code"),
            )
        )
        # Normalize for response (Decimal/dates serialized; None for missing FKs)
        for s in schemes:
            s["scheme_status"] = s.pop("scheme_status_id")
            if s.get("scheme_name") is None:
                s["scheme_name"] = None
            if s.get("scheme_code") is None:
                s["scheme_code"] = None
            if s.get("scheme_status_name") is None:
                s["scheme_status_name"] = None

        nominees = {}
        for scheme in schemes:
            nominees[scheme["id"]] = list(
                CustomerNominee.objects.filter(
                    customer_scheme_id=scheme["id"]
                ).values(
                    "id", "full_name", "relationship",
                    "mobile", "share_percentage"
                )
            )

        kyc = CustomerKYC.objects.filter(customer=customer).first()
        referral_count = Customer.objects.filter(referred_by_id=customer.id).count()
        referred_by = customer.referred_by

        return Response({
            "id": customer.id,
            "customer_code": customer.customer_code,
            "full_name": customer.full_name,
            "mobile": customer.mobile,
            "email": customer.email,
            "gender": customer.gender,
            "date_of_birth": customer.date_of_birth,
            "anniversary_date": getattr(customer, "anniversary_date", None),
            "wedding_date": getattr(customer, "wedding_date", None),
            "family_group": getattr(customer, "family_group", None),
            "referred_by_id": customer.referred_by_id,
            "referred_by_name": referred_by.full_name if referred_by else None,
            "referred_by_code": referred_by.customer_code if referred_by else None,
            "referral_code": getattr(customer, "referral_code", None),
            "referral_count": referral_count,
            "is_active": customer.is_active,
            "last_login_at": customer.last_login_at,
            "system_created_at": customer.system_created_at,
            "system_updated_at": customer.system_updated_at,
            "addresses": list(addresses),
            "schemes": list(schemes),
            "kyc_status": kyc.status.code if kyc and kyc.status else None,
            "pan_number": kyc.pan_number if kyc else None,
            "pan_image": kyc.pan_document_url if kyc and kyc.pan_document_url else None,
            "gst_number": customer.gst_number,
            "aadhaar_number": customer.aadhaar_number,
            "nominees": nominees
        })

    if request.method == "PUT":
        denied = ensure_admin_permission(request, *CUSTOMER_WRITE_AUTH)
        if denied:
            return denied
        data = request.data
        new_password = str(data.get("password", "")).strip() if "password" in data else ""
        generate_password = bool(data.get("generate_password"))
        generated_password = None
        for field in [
            "full_name", "mobile", "email",
            "gender", "date_of_birth",
            "anniversary_date", "wedding_date", "family_group",
            "referral_code",
            "is_active"
        ]:
            if field in data:
                setattr(customer, field, data[field] if data[field] != "" else None)

        if "referred_by_id" in data or "referred_by" in data:
            raw_ref = data.get("referred_by_id", data.get("referred_by"))
            if raw_ref in (None, "", 0, "0"):
                customer.referred_by_id = None
            else:
                try:
                    ref_id = int(raw_ref)
                except (TypeError, ValueError):
                    return Response({"error": "Invalid referred_by_id"}, status=status.HTTP_400_BAD_REQUEST)
                if ref_id == customer.id:
                    return Response({"error": "Customer cannot refer themselves"}, status=status.HTTP_400_BAD_REQUEST)
                if not Customer.objects.filter(id=ref_id).exists():
                    return Response({"error": "Referring customer not found"}, status=status.HTTP_400_BAD_REQUEST)
                customer.referred_by_id = ref_id

        if "gst_number" in data:
            customer.gst_number = _normalize_gst_number(data.get("gst_number"))
        if "aadhaar_number" in data:
            customer.aadhaar_number = _normalize_aadhaar_number(data.get("aadhaar_number"))

        if generate_password:
            generated_password = generate_customer_password(
                customer.full_name,
                customer.date_of_birth,
                customer.mobile,
            )
            customer.password_hash = make_password(generated_password)
        elif new_password:
            customer.password_hash = make_password(new_password)

        customer.save()

        if "pan_number" in data:
            handle_customer_pan(customer, data.get("pan_number"))

        response_body = {"message": "Customer updated successfully"}
        if generated_password:
            response_body["generated_password"] = generated_password
        return Response(response_body)

    # DELETE
    denied = ensure_admin_permission(request, *CUSTOMER_DELETE_AUTH)
    if denied:
        return denied
    customer.delete()
    return Response({
        "message": "Customer deleted successfully"
    }, status=status.HTTP_200_OK)



def _normalize_gst_number(value):
    if not value:
        return None
    gst = re.sub(r'\s', '', str(value)).strip().upper()
    return gst or None


def _normalize_aadhaar_number(value):
    if not value:
        return None
    digits = re.sub(r'\D', '', str(value))
    return digits if len(digits) == 12 else None


def handle_customer_pan(customer, pan_number):
    if not pan_number:
        return

    pan_number = pan_number.strip().upper()

    customer_kyc = CustomerKYC.objects.filter(customer=customer).first()

    if not customer_kyc:
        pending_status = LookupValue.objects.get(
            lookup__code="KYC_STATUS",
            code="PENDING"
        )
        CustomerKYC.objects.create(
            customer=customer,
            pan_number=pan_number,
            pan_document_url="",
            status=pending_status
        )
    else:
        if customer_kyc.pan_number != pan_number:
            customer_kyc.pan_number = pan_number
            customer_kyc.save(update_fields=["pan_number"])


def _build_installment_window(anchor_day, due_date):
    """
    Build 15-day batch window in due_date month using enrollment/apply anchor day.
    anchor_day <= 15 -> 1st to 15th
    anchor_day > 15  -> 15th to end of month
    """
    month_last_day = calendar.monthrange(due_date.year, due_date.month)[1]
    if anchor_day <= 15:
        window_start = date(due_date.year, due_date.month, 1)
        window_end = date(due_date.year, due_date.month, 15)
    else:
        window_start = date(due_date.year, due_date.month, 15)
        window_end = date(due_date.year, due_date.month, month_last_day)
    return window_start, window_end


def _build_installment_window_for_sequence(anchor_day, installment_no, first_paid_date, due_date):
    """
    Build installment window by sequence month from incorporation(first_paid_date).
    Falls back to due_date month when first_paid_date is unavailable.
    """
    if not first_paid_date:
        return _build_installment_window(anchor_day, due_date)

    month_ref = first_paid_date + relativedelta(months=installment_no - 1)
    month_last_day = calendar.monthrange(month_ref.year, month_ref.month)[1]

    if installment_no == 1:
        return first_paid_date, date(month_ref.year, month_ref.month, month_last_day)

    if anchor_day <= 15:
        return date(month_ref.year, month_ref.month, 1), date(month_ref.year, month_ref.month, 15)

    return date(month_ref.year, month_ref.month, 15), date(month_ref.year, month_ref.month, month_last_day)


@api_view(["GET"])
@admin_auth(*CUSTOMER_READ_AUTH)
def customer_segment_list(request):
    """
    List customers by segment for Customer List tabs.
    Query param: segment = top_customers | recent_visitors | long_time_no_visit | udhar | active_orders | pending_deliveries | advance_balance | scheme_participants | wishlist | referrals | repair
    Returns same shape as customer list: { count, total_pages, current_page, results } with results having id, full_name, mobile, email, is_active (+ segment-specific fields in extra).
    """
    segment = (request.GET.get("segment") or "").strip().lower()
    page_number = max(1, int(request.GET.get("page", 1)))
    page_size = min(50, max(1, int(request.GET.get("page_size", 10))))
    search = (request.GET.get("search") or "").strip()

    def search_qs(qs):
        if search:
            return qs.filter(
                Q(full_name__icontains=search)
                | Q(mobile__icontains=search)
                | Q(email__icontains=search)
                | Q(customer_code__icontains=search)
            )
        return qs

    def to_row(c, **extra):
        row = {
            "id": c.id,
            "customer_code": c.customer_code,
            "full_name": c.full_name,
            "mobile": c.mobile,
            "email": c.email or "",
            "date_of_birth": c.date_of_birth,
            "anniversary_date": getattr(c, "anniversary_date", None),
            "wedding_date": getattr(c, "wedding_date", None),
            "family_group": getattr(c, "family_group", None),
            "is_active": c.is_active,
        }
        row.update(extra)
        return row

    # Segments that return customer IDs then we fetch Customer and paginate
    customer_ids = []
    extra_by_id = {}

    if segment == "top_customers":
        # Top 75 by jewellery SaleInvoice purchase value only (not scheme payments).
        agg = (
            SaleInvoice.objects.filter(customer_id__isnull=False, is_deleted=False)
            .values("customer_id")
            .annotate(total=Sum("total_amount"), visits=Count("id"))
            .order_by("-total")[:75]
        )
        top_ids = []
        for r in agg:
            cid = r.get("customer_id")
            if cid:
                top_ids.append(cid)
                total = r.get("total") or Decimal("0")
                visits = r.get("visits") or 0
                extra_by_id[cid] = {
                    "total_purchase": str(total),
                    "visit_count": visits,
                    "average_bill": str((total / visits).quantize(Decimal("0.01"))) if visits else "0",
                    "referral_count": 0,
                }
        if top_ids:
            ref_counts = {
                row["referred_by_id"]: row["c"]
                for row in (
                    Customer.objects.filter(referred_by_id__in=top_ids)
                    .values("referred_by_id")
                    .annotate(c=Count("id"))
                )
            }
            for cid in top_ids:
                extra_by_id[cid]["referral_count"] = ref_counts.get(cid, 0)
        customer_ids = top_ids

    elif segment == "recent_visitors":
        last_visit_sq = (
            CrmCustomerVisit.objects.filter(customer_id=OuterRef("pk"))
            .order_by("-visited_at")
            .values("visited_at")[:1]
        )
        qs = (
            Customer.objects.annotate(last_visit=Subquery(last_visit_sq))
            .filter(last_visit__isnull=False)
            .order_by("-last_visit")
        )
        qs = search_qs(qs)
        for c in qs[:500]:
            customer_ids.append(c.id)
            lv = c.last_visit
            extra_by_id[c.id] = {
                "last_visit_date": lv.strftime("%Y-%m-%d") if lv else None,
            }

    elif segment == "long_time_no_visit":
        # Inactive = no CRM visit and no SaleInvoice for 1 year.
        cutoff = timezone.now() - timedelta(days=365)
        last_visit_sq = (
            CrmCustomerVisit.objects.filter(customer_id=OuterRef("pk"))
            .order_by("-visited_at")
            .values("visited_at")[:1]
        )
        last_sale_sq = (
            SaleInvoice.objects.filter(customer_id=OuterRef("pk"), is_deleted=False)
            .order_by("-invoice_date")
            .values("invoice_date")[:1]
        )
        qs = (
            Customer.objects.annotate(
                last_visit=Subquery(last_visit_sq),
                last_sale=Subquery(last_sale_sq),
            )
            .filter(
                Q(last_visit__lt=cutoff) | Q(last_visit__isnull=True),
            )
            .filter(
                Q(last_sale__lt=cutoff.date()) | Q(last_sale__isnull=True),
            )
            .filter(
                Q(last_visit__isnull=False) | Q(last_sale__isnull=False) | Q(system_created_at__lt=cutoff),
            )
            .order_by("last_visit")
        )
        qs = search_qs(qs)
        for c in qs[:500]:
            customer_ids.append(c.id)
            ref = None
            if c.last_visit:
                ref = c.last_visit.date() if hasattr(c.last_visit, "date") else c.last_visit
            elif c.last_sale:
                ref = c.last_sale
            days = (timezone.localdate() - ref).days if ref else None
            extra_by_id[c.id] = {
                "last_visit_date": ref.strftime("%Y-%m-%d") if ref else None,
                "days_since_last_visit": days,
            }

    elif segment == "udhar":
        agg = (
            SchemeInstalment.objects.filter(status__code="PENDING", is_bonus=False)
            .values("customer_scheme__customer_id")
            .annotate(pending_amount=Sum("amount"))
            .filter(pending_amount__gt=0)
            .order_by("-pending_amount")[:500]
        )
        for r in agg:
            cid = r.get("customer_scheme__customer_id")
            if cid:
                customer_ids.append(cid)
                extra_by_id[cid] = {"pending_amount": str(r["pending_amount"])}

    elif segment == "active_orders":
        for q in (
            CatalogueQuote.objects.filter(
                status__in=[CatalogueQuote.STATUS_ORDER, CatalogueQuote.STATUS_BOOKING],
            )
            .select_related("customer")
            .order_by("-system_created_at")[:500]
        ):
            customer_ids.append(q.customer_id)
            delivery = q.expected_delivery_date
            if not delivery and q.valid_until:
                delivery = q.valid_until.date()
            extra_by_id[q.customer_id] = {
                "order_id": q.quote_number,
                "expected_delivery": delivery.isoformat() if delivery else "—",
                "quote_status": q.status,
            }

    elif segment == "pending_deliveries":
        # Orders/bookings with expected delivery in the next 7 days (inclusive).
        today = timezone.localdate()
        week_end = today + timedelta(days=7)
        for q in (
            CatalogueQuote.objects.filter(
                status__in=[CatalogueQuote.STATUS_ORDER, CatalogueQuote.STATUS_BOOKING],
                expected_delivery_date__gte=today,
                expected_delivery_date__lte=week_end,
            )
            .select_related("customer")
            .order_by("expected_delivery_date", "quote_number")[:500]
        ):
            customer_ids.append(q.customer_id)
            extra_by_id[q.customer_id] = {
                "order_id": q.quote_number,
                "expected_delivery": q.expected_delivery_date.isoformat(),
                "quote_status": q.status,
                "days_until_delivery": (q.expected_delivery_date - today).days,
            }

    elif segment == "advance_balance":
        # No advance model: return empty
        pass

    elif segment == "scheme_participants":
        qs = (
            CustomerScheme.objects.filter(customer__is_active=True, scheme_status__code__in=["ACTIVE", "PENDING"])
            .select_related("customer", "scheme")
            .order_by("-system_updated_at")[:500]
        )
        seen = set()
        for cs in qs:
            if cs.customer_id not in seen:
                seen.add(cs.customer_id)
                customer_ids.append(cs.customer_id)
                paid = SchemeInstalment.objects.filter(customer_scheme=cs, status__code="PAID", is_bonus=False).count()
                total = cs.scheme.tenure_months or 0
                extra_by_id[cs.customer_id] = {"scheme_name": cs.scheme.scheme_name, "installments_paid": paid, "remaining": max(0, total - paid)}

    elif segment == "wishlist":
        # Buy-next-time visits + customers with unconverted (draft) quotations.
        buy_next = (
            CrmCustomerVisit.objects.filter(buy_next_time=True)
            .values("customer_id")
            .annotate(wish_count=Count("id"))
            .order_by("-wish_count")[:500]
        )
        for r in buy_next:
            cid = r.get("customer_id")
            if cid:
                customer_ids.append(cid)
                extra_by_id[cid] = {
                    "wishlist_items": r.get("wish_count") or 0,
                    "source": "buy_next_time",
                }
        drafts = (
            CatalogueQuote.objects.filter(status=CatalogueQuote.STATUS_DRAFT)
            .values("customer_id")
            .annotate(draft_count=Count("id"))
            .order_by("-draft_count")[:500]
        )
        for r in drafts:
            cid = r.get("customer_id")
            if not cid:
                continue
            if cid in extra_by_id:
                extra_by_id[cid]["draft_quotes"] = r.get("draft_count") or 0
            else:
                customer_ids.append(cid)
                extra_by_id[cid] = {
                    "wishlist_items": 0,
                    "draft_quotes": r.get("draft_count") or 0,
                    "source": "unconverted_quote",
                }

    elif segment == "referrals":
        # Customers who referred others, ranked by referral count.
        qs = (
            Customer.objects.annotate(referral_count=Count("referrals"))
            .filter(referral_count__gt=0)
            .order_by("-referral_count", "full_name")
        )
        qs = search_qs(qs)
        for c in qs[:500]:
            customer_ids.append(c.id)
            # Show one recent referred customer name for context
            latest = (
                Customer.objects.filter(referred_by_id=c.id)
                .order_by("-system_created_at")
                .values_list("full_name", flat=True)
                .first()
            )
            extra_by_id[c.id] = {
                "referral_count": c.referral_count,
                "latest_referral_name": latest or "—",
                "referral_code": c.referral_code or "",
            }

    elif segment == "repair":
        open_statuses = [
            CrmServiceTicket.STATUS_OPEN,
            CrmServiceTicket.STATUS_IN_PROGRESS,
            CrmServiceTicket.STATUS_READY,
        ]
        qs = (
            Customer.objects.filter(
                crm_service_tickets__ticket_type=CrmServiceTicket.TYPE_REPAIR,
                crm_service_tickets__status__in=open_statuses,
            )
            .distinct()
            .order_by("-system_updated_at")
        )
        qs = search_qs(qs)
        customer_ids = list(qs.values_list("id", flat=True)[:500])
        for c in Customer.objects.filter(id__in=customer_ids).annotate(
            open_repairs=Count(
                "crm_service_tickets",
                filter=Q(
                    crm_service_tickets__ticket_type=CrmServiceTicket.TYPE_REPAIR,
                    crm_service_tickets__status__in=open_statuses,
                ),
            )
        ):
            extra_by_id[c.id] = {"open_repairs": c.open_repairs}

    elif segment == "exchange":
        open_statuses = [
            CrmServiceTicket.STATUS_OPEN,
            CrmServiceTicket.STATUS_IN_PROGRESS,
            CrmServiceTicket.STATUS_READY,
        ]
        qs = (
            Customer.objects.filter(
                crm_service_tickets__ticket_type=CrmServiceTicket.TYPE_EXCHANGE,
                crm_service_tickets__status__in=open_statuses,
            )
            .distinct()
            .order_by("-system_updated_at")
        )
        qs = search_qs(qs)
        customer_ids = list(qs.values_list("id", flat=True)[:500])
        for c in Customer.objects.filter(id__in=customer_ids).annotate(
            open_exchanges=Count(
                "crm_service_tickets",
                filter=Q(
                    crm_service_tickets__ticket_type=CrmServiceTicket.TYPE_EXCHANGE,
                    crm_service_tickets__status__in=open_statuses,
                ),
            )
        ):
            extra_by_id[c.id] = {"open_exchanges": c.open_exchanges}

    else:
        # Default: all customers (same as main list but with segment param)
        qs = Customer.objects.all().order_by("-system_created_at")
        qs = search_qs(qs)
        customer_ids = list(qs.values_list("id", flat=True)[:500])

    # Dedupe preserving order
    seen = set()
    unique_ids = []
    for cid in customer_ids:
        if cid not in seen:
            seen.add(cid)
            unique_ids.append(cid)

    demo_flag = (request.GET.get("demo") or "").strip().lower()
    force_demo = demo_flag in ("1", "true", "yes")
    force_real = demo_flag in ("0", "false", "no")
    crm_demo_segments = {
        "top_customers",
        "recent_visitors",
        "long_time_no_visit",
        "wishlist",
        "active_orders",
        "pending_deliveries",
        "referrals",
    }
    use_demo = (
        segment in crm_demo_segments
        and (force_demo or (not unique_ids and not force_real))
    )

    if use_demo:
        demo_rows = _crm_segment_demo_rows(segment)
        if search:
            q = search.lower()
            demo_rows = [
                r for r in demo_rows
                if q in (r.get("full_name") or "").lower()
                or q in (r.get("mobile") or "")
                or q in (r.get("email") or "").lower()
            ]
        paginator = Paginator(demo_rows, page_size)
        page = paginator.get_page(page_number)
        return Response({
            "count": paginator.count,
            "total_pages": paginator.num_pages,
            "current_page": page.number,
            "is_demo": True,
            "results": list(page.object_list),
        })

    paginator = Paginator(unique_ids, page_size)
    page = paginator.get_page(page_number)
    ids_page = list(page.object_list)

    if not ids_page:
        return Response({"count": 0, "total_pages": 0, "current_page": page_number, "is_demo": False, "results": []})

    customers = {c.id: c for c in Customer.objects.filter(id__in=ids_page)}
    # Preserve order
    results = []
    for cid in ids_page:
        c = customers.get(cid)
        if not c:
            continue
        extra = extra_by_id.get(cid, {})
        results.append(to_row(c, **extra))

    return Response({
        "count": paginator.count,
        "total_pages": paginator.num_pages,
        "current_page": page.number,
        "is_demo": False,
        "results": results,
    })


def _crm_segment_demo_rows(segment: str):
    """Sample rows for CRM list segments when live data is empty."""
    today = timezone.localdate()
    real = list(
        Customer.objects.filter(is_active=True).order_by("-system_created_at")[:6]
    )
    if real:
        base = [
            {
                "id": c.id,
                "customer_code": c.customer_code,
                "full_name": c.full_name,
                "mobile": c.mobile,
                "email": c.email or "",
                "is_active": c.is_active,
            }
            for c in real
        ]
    else:
        base = [
            {"id": 9001, "customer_code": "CUS-DEMO-01", "full_name": "Anjana", "mobile": "9876500001", "email": "anjana@demo.test", "is_active": True},
            {"id": 9002, "customer_code": "CUS-DEMO-02", "full_name": "Hari Sahu", "mobile": "9876500002", "email": "hari@demo.test", "is_active": True},
            {"id": 9003, "customer_code": "CUS-DEMO-03", "full_name": "Prasoon Shukla", "mobile": "9876500003", "email": "prasoon@demo.test", "is_active": True},
            {"id": 9004, "customer_code": "CUS-DEMO-04", "full_name": "Priya Sharma", "mobile": "9876500004", "email": "priya@demo.test", "is_active": True},
            {"id": 9005, "customer_code": "CUS-DEMO-05", "full_name": "Rahul Verma", "mobile": "9876500005", "email": "rahul@demo.test", "is_active": True},
            {"id": 9006, "customer_code": "CUS-DEMO-06", "full_name": "Anita Patel", "mobile": "9876500006", "email": "anita@demo.test", "is_active": True},
        ]
    if segment == "top_customers":
        extras = [
            {"total_purchase": "230000.00", "visit_count": 28, "average_bill": "8214.29", "referral_count": 8},
            {"total_purchase": "79300.00", "visit_count": 15, "average_bill": "5286.67", "referral_count": 5},
            {"total_purchase": "65000.00", "visit_count": 9, "average_bill": "7222.22", "referral_count": 2},
            {"total_purchase": "54000.00", "visit_count": 7, "average_bill": "7714.29", "referral_count": 1},
            {"total_purchase": "41000.00", "visit_count": 6, "average_bill": "6833.33", "referral_count": 0},
            {"total_purchase": "28500.00", "visit_count": 4, "average_bill": "7125.00", "referral_count": 0},
        ]
    elif segment == "recent_visitors":
        extras = [
            {"last_visit_date": today.isoformat()},
            {"last_visit_date": (today - timedelta(days=1)).isoformat()},
            {"last_visit_date": (today - timedelta(days=2)).isoformat()},
            {"last_visit_date": (today - timedelta(days=3)).isoformat()},
            {"last_visit_date": (today - timedelta(days=5)).isoformat()},
            {"last_visit_date": (today - timedelta(days=6)).isoformat()},
        ]
    elif segment == "long_time_no_visit":
        extras = [
            {"last_visit_date": (today - timedelta(days=400)).isoformat(), "days_since_last_visit": 400},
            {"last_visit_date": (today - timedelta(days=420)).isoformat(), "days_since_last_visit": 420},
            {"last_visit_date": (today - timedelta(days=450)).isoformat(), "days_since_last_visit": 450},
            {"last_visit_date": (today - timedelta(days=480)).isoformat(), "days_since_last_visit": 480},
            {"last_visit_date": (today - timedelta(days=520)).isoformat(), "days_since_last_visit": 520},
            {"last_visit_date": (today - timedelta(days=600)).isoformat(), "days_since_last_visit": 600},
        ]
    elif segment == "wishlist":
        extras = [
            {"wishlist_items": 3, "draft_quotes": 1, "source": "buy_next_time"},
            {"wishlist_items": 2, "draft_quotes": 0, "source": "buy_next_time"},
            {"wishlist_items": 0, "draft_quotes": 2, "source": "unconverted_quote"},
            {"wishlist_items": 1, "draft_quotes": 1, "source": "buy_next_time"},
            {"wishlist_items": 4, "draft_quotes": 0, "source": "buy_next_time"},
            {"wishlist_items": 0, "draft_quotes": 3, "source": "unconverted_quote"},
        ]
    elif segment == "active_orders":
        extras = [
            {"order_id": "DEMO-BK-101", "expected_delivery": (today + timedelta(days=5)).isoformat(), "quote_status": "booking"},
            {"order_id": "DEMO-OR-088", "expected_delivery": (today + timedelta(days=10)).isoformat(), "quote_status": "order"},
            {"order_id": "DEMO-BK-077", "expected_delivery": (today + timedelta(days=3)).isoformat(), "quote_status": "booking"},
            {"order_id": "DEMO-OR-066", "expected_delivery": (today + timedelta(days=14)).isoformat(), "quote_status": "order"},
            {"order_id": "DEMO-BK-055", "expected_delivery": (today + timedelta(days=7)).isoformat(), "quote_status": "booking"},
            {"order_id": "DEMO-OR-044", "expected_delivery": (today + timedelta(days=21)).isoformat(), "quote_status": "order"},
        ]
    elif segment == "pending_deliveries":
        extras = [
            {"order_id": "DEMO-BK-201", "expected_delivery": today.isoformat(), "quote_status": "booking", "days_until_delivery": 0},
            {"order_id": "DEMO-OR-202", "expected_delivery": (today + timedelta(days=1)).isoformat(), "quote_status": "order", "days_until_delivery": 1},
            {"order_id": "DEMO-BK-203", "expected_delivery": (today + timedelta(days=2)).isoformat(), "quote_status": "booking", "days_until_delivery": 2},
            {"order_id": "DEMO-OR-204", "expected_delivery": (today + timedelta(days=4)).isoformat(), "quote_status": "order", "days_until_delivery": 4},
            {"order_id": "DEMO-BK-205", "expected_delivery": (today + timedelta(days=5)).isoformat(), "quote_status": "booking", "days_until_delivery": 5},
            {"order_id": "DEMO-OR-206", "expected_delivery": (today + timedelta(days=7)).isoformat(), "quote_status": "order", "days_until_delivery": 7},
        ]
    elif segment == "referrals":
        extras = [
            {"referral_count": 8, "latest_referral_name": "Neha Joshi", "referral_code": "REF-ANJ"},
            {"referral_count": 5, "latest_referral_name": "Amit Shah", "referral_code": "REF-HAR"},
            {"referral_count": 4, "latest_referral_name": "Kavita Rao", "referral_code": "REF-PRA"},
            {"referral_count": 3, "latest_referral_name": "Rohan Mehta", "referral_code": "REF-PRI"},
            {"referral_count": 2, "latest_referral_name": "Sonal Jain", "referral_code": "REF-RAH"},
            {"referral_count": 1, "latest_referral_name": "Deepak Iyer", "referral_code": "REF-ANI"},
        ]
    else:
        extras = [{} for _ in base]

    rows = []
    for i, row in enumerate(base):
        item = dict(row)
        item.update(extras[i] if i < len(extras) else {})
        rows.append(item)
    return rows

