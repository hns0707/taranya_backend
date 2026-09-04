"""
Views for scheme enrollment in the customer app.
"""
from decimal import Decimal
from datetime import date, timedelta
from django.db.models import Count, Q
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from shared.models import SchemeMaster, CustomerScheme, SchemeInstalment, CustomerSchemeBenefit, GoldLockingRecord, LookupValue, Metal, MetalMasterRate
from shared.serializers import SchemeMasterSerializer
from shared.services.scheme_service import (
    list_active_schemes,
    get_customer_active_schemes,
    validate_scheme_amount,
    calculate_scheme_preview,
)
from shared.services.scheme_enrollment_service import enroll_customer_into_scheme
from shared.services.content_service import get_active_faqs, get_cms_page, get_default_cms_page
from shared.services.metal_rate_service import (
    SILVER_CUSTOMER_DISPLAY_GRAMS,
    SILVER_CUSTOMER_SPOT_PURITY,
    get_24k_gold_rate_for_lock,
    get_default_gold_metal_id,
    get_default_silver_metal_id,
    get_metal_master_rate_simple,
    get_metal_rate_by_date,
)
from customer.auth.customer_auth import CustomerAuthentication


@api_view(['POST'])
def validate_scheme(request):
    """
    API endpoint to validate scheme and monthly amount.
    """
    scheme_id = request.data.get('scheme_id')
    monthly_amount = request.data.get('monthly_amount')

    if not scheme_id or not monthly_amount:
        return Response({"error": "scheme_id and monthly_amount are required"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        scheme = SchemeMaster.objects.get(id=scheme_id, is_active=True)
    except SchemeMaster.DoesNotExist:
        return Response({"error": "Scheme not found or inactive"}, status=status.HTTP_404_NOT_FOUND)

    if not validate_scheme_amount(scheme, float(monthly_amount)):
        return Response({"error": "Monthly amount is not within the scheme's limits"}, status=status.HTTP_400_BAD_REQUEST)

    return Response({"message": "Scheme and amount are valid"}, status=status.HTTP_200_OK)


@api_view(['GET'])
def scheme_list(request):
    """
    API endpoint to list available schemes for enrollment.
    """
    schemes = list_active_schemes()
    serializer = SchemeMasterSerializer(schemes, many=True)
    return Response(serializer.data)


@api_view(['GET'])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def customer_enrolled_schemes(request):
    """
    API endpoint to list enrolled schemes for a customer.
    Includes total_installments, paid_installments, pending_installments, progress_percentage.
    """
    try:
        paid_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
        pending_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
    except LookupValue.DoesNotExist:
        return Response({"error": "Required status values not found"}, status=status.HTTP_404_NOT_FOUND)

    schemes = (
        get_customer_active_schemes(request.user.id)
        .select_related("scheme", "scheme_status")
        .annotate(
            total_installments=Count('schemeinstalment'),
            paid_installments=Count('schemeinstalment', filter=Q(schemeinstalment__status=paid_status)),
            pending_installments=Count('schemeinstalment', filter=Q(schemeinstalment__status=pending_status)),
        )
    )
    data = []
    for scheme in schemes:
        total = scheme.total_installments or 0
        paid = scheme.paid_installments or 0
        pending = scheme.pending_installments or 0
        progress_percentage = round((paid / total * 100), 1) if total else 0
        data.append({
            "id": scheme.id,
            "scheme_name": scheme.scheme.scheme_name,
            "monthly_amount": str(scheme.monthly_amount),
            "scheme_status": scheme.scheme_status.code if scheme.scheme_status else None,
            "start_date": scheme.start_date.isoformat() if scheme.start_date else None,
            "end_date": scheme.end_date.isoformat() if scheme.end_date else None,
            "total_installments": total,
            "paid_installments": paid,
            "pending_installments": pending,
            "progress_percentage": progress_percentage,
        })
    return Response(data)


@api_view(['POST'])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def apply_for_scheme_view(request):
    """
    API endpoint to apply for a scheme.
    """
    scheme_id = request.data.get('scheme_id')
    monthly_amount = request.data.get('monthly_amount')

    if not scheme_id or not monthly_amount:
        return Response({"error": "scheme_id and monthly_amount are required"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        scheme = SchemeMaster.objects.get(id=scheme_id)
    except SchemeMaster.DoesNotExist:
        return Response({"error": "Scheme not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        customer_scheme, first_instalment = enroll_customer_into_scheme(
            customer=request.user,
            scheme=scheme,
            monthly_amount=float(monthly_amount),
            address_data=request.data.get("address_data"),
            nominee_data=request.data.get("nominee_data")
        )
        return Response({
            "message": "Scheme enrollment created successfully",
            "data": {
                "enrollment_id": customer_scheme.id,
                "instalment_id": first_instalment.id,
                "amount": str(first_instalment.amount)
            }
        }, status=status.HTTP_201_CREATED)
    except ValidationError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def customer_scheme_detail(request, pk):
    """
    API endpoint to retrieve details of a specific enrolled scheme.
    Includes total_installments, paid_installments, pending_installments, progress_percentage.
    """
    try:
        paid_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
        pending_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
    except LookupValue.DoesNotExist:
        return Response({"error": "Required status values not found"}, status=status.HTTP_404_NOT_FOUND)
    try:
        scheme = (
            CustomerScheme.objects
            .filter(id=pk, customer=request.user)
            .select_related("scheme", "scheme_status")
            .annotate(
                total_installments=Count('schemeinstalment'),
                paid_installments=Count('schemeinstalment', filter=Q(schemeinstalment__status=paid_status)),
                pending_installments=Count('schemeinstalment', filter=Q(schemeinstalment__status=pending_status)),
            )
            .get()
        )
        total = scheme.total_installments or 0
        paid = scheme.paid_installments or 0
        pending = scheme.pending_installments or 0
        progress_percentage = round((paid / total * 100), 1) if total else 0
        data = {
            "id": scheme.id,
            "scheme_name": scheme.scheme.scheme_name,
            "monthly_amount": str(scheme.monthly_amount),
            "scheme_status": scheme.scheme_status.code if scheme.scheme_status else None,
            "start_date": scheme.start_date.isoformat() if scheme.start_date else None,
            "end_date": scheme.end_date.isoformat() if scheme.end_date else None,
            "total_installments": total,
            "paid_installments": paid,
            "pending_installments": pending,
            "progress_percentage": progress_percentage,
        }
        return Response(data)
    except CustomerScheme.DoesNotExist:
        return Response({"error": "Scheme not found"}, status=status.HTTP_404_NOT_FOUND)


@api_view(['GET'])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def customer_scheme_installments(request):
    """
    API endpoint to list installments for all of a customer's schemes.
    Ordered by due_date ascending so upcoming installments appear first.
    """
    installments = (
        SchemeInstalment.objects
        .select_related("customer_scheme__scheme", "customer_scheme__scheme_status", "status")
        .filter(customer_scheme__customer=request.user)
        .order_by("due_date")
    )

    data = [_installment_to_dict(inst) for inst in installments]
    return Response(data)


@api_view(['GET'])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def customer_scheme_installments_by_scheme(request, pk):
    """
    API endpoint to list installments for a single customer scheme.
    URL: /my-schemes/<customer_scheme_id>/installments/
    Ordered by due_date ascending so upcoming installments appear first.
    """
    try:
        customer_scheme = CustomerScheme.objects.get(id=pk, customer=request.user)
    except CustomerScheme.DoesNotExist:
        return Response({"error": "Scheme not found"}, status=status.HTTP_404_NOT_FOUND)

    installments = (
        SchemeInstalment.objects
        .select_related("customer_scheme__scheme", "customer_scheme__scheme_status", "status")
        .filter(customer_scheme=customer_scheme)
        .order_by("due_date")
    )

    data = [_installment_to_dict(inst) for inst in installments]
    return Response(data)


def _installment_to_dict(installment):
    """Build installment response dict (shared by list and by-scheme). Dates in dd/mm/yyyy."""
    scheme_status = (
        installment.customer_scheme.scheme_status.code
        if installment.customer_scheme.scheme_status
        else None
    )
    return {
        "installment_id": installment.id,
        "customer_scheme_id": installment.customer_scheme_id,
        "scheme_name": installment.customer_scheme.scheme.scheme_name,
        "scheme_status": scheme_status,
        "monthly_amount": str(installment.customer_scheme.monthly_amount),
        "installment_no": installment.instalment_no,
        "due_date": installment.due_date.strftime("%d/%m/%Y") if installment.due_date else None,
        "due_date_iso": installment.due_date.isoformat() if installment.due_date else None,
        "amount": str(installment.amount),
        "status": installment.status.code if installment.status else None,
        "is_bonus": bool(installment.is_bonus),
    }


def _public_master_rate_dict(mr, *, rate_applies_for_date=None):
    """
    Serialize MetalMasterRate for public customer APIs (metal_master_rate table).
    published_at = last save time (when HO updated the rate in admin).
    """
    applies = rate_applies_for_date if rate_applies_for_date is not None else mr.effective_date
    published = None
    if getattr(mr, "system_updated_at", None):
        published = timezone.localtime(mr.system_updated_at)
    return {
        "metal_id": mr.metal_id,
        "metal_name": mr.metal.metal_name if mr.metal else None,
        "purity": mr.purity_name or "24K",
        "rate_value": str(mr.sell_price),
        "rate_date": str(mr.effective_date),
        "rate_applies_for_date": str(applies) if applies is not None else None,
        "sell_price": str(mr.sell_price),
        "buyback_price": str(mr.buyback_price),
        "published_at": published.isoformat() if published else None,
    }


def _serialize_portal_rate_slot(
    rate_obj,
    actual_date,
    *,
    display_purity: str,
    metal_id: int,
    metal_display_name: str,
    publish_fallback_row=None,
):
    """
    Serialize one public rate line. rate_obj is MetalMasterRate or a lightweight object
    with .rate_value only (derived purity from get_metal_rate_by_date).
    """
    if rate_obj is None:
        return None
    if isinstance(rate_obj, MetalMasterRate):
        d = _public_master_rate_dict(rate_obj, rate_applies_for_date=actual_date)
        d["display_purity"] = display_purity
        d["metal_display_name"] = metal_display_name
        return d
    val = getattr(rate_obj, "rate_value", None)
    if val is None:
        return None
    published = None
    src = publish_fallback_row
    if src is not None and getattr(src, "system_updated_at", None):
        published = timezone.localtime(src.system_updated_at)
    return {
        "metal_id": metal_id,
        "metal_name": metal_display_name,
        "display_purity": display_purity,
        "purity": display_purity,
        "rate_value": str(val),
        "sell_price": str(val),
        "buyback_price": str(val),
        "rate_date": str(actual_date) if actual_date is not None else None,
        "rate_applies_for_date": str(actual_date) if actual_date is not None else None,
        "published_at": published.isoformat() if published else None,
        "derived": True,
    }


def _serialize_silver_spot_portal(row, actual_date, silver_id):
    """
    Customer silver board: Silver-100 master rate (₹/g in DB) shown as ₹/10g on portal.
    """
    if row is None or not isinstance(row, MetalMasterRate):
        return None
    multiplier = Decimal(str(SILVER_CUSTOMER_DISPLAY_GRAMS))
    sell_per_gm = Decimal(str(row.sell_price))
    buyback_per_gm = Decimal(str(row.buyback_price))
    sell_per_10g = (sell_per_gm * multiplier).quantize(Decimal("0.01"))
    buyback_per_10g = (buyback_per_gm * multiplier).quantize(Decimal("0.01"))
    published = None
    if getattr(row, "system_updated_at", None):
        published = timezone.localtime(row.system_updated_at)
    return {
        "metal_id": silver_id,
        "metal_name": row.metal.metal_name if row.metal else "Silver",
        "metal_display_name": "Silver",
        "display_purity": SILVER_CUSTOMER_SPOT_PURITY,
        "purity": SILVER_CUSTOMER_SPOT_PURITY,
        "rate_value": str(sell_per_10g),
        "sell_price": str(sell_per_10g),
        "buyback_price": str(buyback_per_10g),
        "sell_price_per_gm": str(sell_per_gm),
        "buyback_price_per_gm": str(buyback_per_gm),
        "rate_value_per_gm": str(sell_per_gm),
        "price_unit": "10g",
        "rate_date": str(row.effective_date),
        "rate_applies_for_date": str(actual_date) if actual_date is not None else None,
        "published_at": published.isoformat() if published else None,
    }


def _gold_publish_fallback_row(gold_metal_id, rate_obj_24k):
    """MetalMasterRate used for published_at when a purity is mathematically derived."""
    if isinstance(rate_obj_24k, MetalMasterRate):
        return rate_obj_24k
    if not gold_metal_id:
        return None
    return (
        MetalMasterRate.objects.filter(metal_id=gold_metal_id, is_active=True)
        .filter(Q(purity_name__iexact="24K") | Q(purity_name="") | Q(purity_name__isnull=True))
        .order_by("-effective_date", "-system_updated_at")
        .first()
    )


@api_view(['GET'])
def customer_metal_rates(request):
    """
    Public master metal rates for the customer portal.

    - latest_gold_24k: same 24K Gold row used for scheme gold locking (today, else nearest
      previous effective_date in metal_master_rate), with published_at from system_updated_at.
    - spot_rates: headline board — Gold 24K / 22K / 18K / 9K and Silver-100 (₹/10g on portal;
      master DB row is ₹/g). Derived gold purities use base row timestamps when applicable.
    - rates: latest 24K gold row first, then other metals with a master row for *today* only
      (24K / blank purity), excluding duplicate PK of the gold row.
    """
    today = timezone.localdate()
    rate_obj, actual_date = get_24k_gold_rate_for_lock(return_date_info=True)

    gold_id = get_default_gold_metal_id()
    gold_publish_src = _gold_publish_fallback_row(gold_id, rate_obj)

    latest_gold_24k = None
    if rate_obj:
        if isinstance(rate_obj, MetalMasterRate):
            latest_gold_24k = _public_master_rate_dict(rate_obj, rate_applies_for_date=actual_date)
        elif gold_id:
            latest_gold_24k = _serialize_portal_rate_slot(
                rate_obj,
                actual_date,
                display_purity="24K",
                metal_id=gold_id,
                metal_display_name="Gold",
                publish_fallback_row=gold_publish_src,
            )

    gold_spot = []
    for purity in ("24K", "22K", "18K", "9K"):
        if not gold_id:
            gold_spot.append(None)
            continue
        r, ad = get_metal_rate_by_date(gold_id, today, purity, branch_id=None, return_date_info=True)
        gold_spot.append(
            _serialize_portal_rate_slot(
                r,
                ad,
                display_purity=purity,
                metal_id=gold_id,
                metal_display_name="Gold",
                publish_fallback_row=gold_publish_src,
            )
        )

    silver_spot = None
    silver_id = get_default_silver_metal_id()
    if silver_id:
        sr, sad = get_metal_master_rate_simple(silver_id, today, return_date_info=True)
        silver_spot = _serialize_silver_spot_portal(sr, sad, silver_id)

    skip_pk = rate_obj.pk if rate_obj and isinstance(rate_obj, MetalMasterRate) else None
    other_today = (
        MetalMasterRate.objects.filter(effective_date=today, is_active=True)
        .filter(purity_name__in=["24K", "", None])
        .select_related("metal")
        .order_by("metal_id")
    )

    rates_data = []
    if latest_gold_24k is not None:
        rates_data.append(latest_gold_24k)
    for mr in other_today:
        if skip_pk and mr.pk == skip_pk:
            continue
        rates_data.append(_public_master_rate_dict(mr, rate_applies_for_date=today))

    return Response(
        {
            "rates": rates_data,
            "latest_gold_24k": latest_gold_24k,
            "spot_rates": {"gold": gold_spot, "silver": silver_spot},
        }
    )


@api_view(['GET'])
def customer_faq_list(request):
    """
    API endpoint to list FAQs for the customer.
    """
    faqs = get_active_faqs()
    data = [{
        "id": faq.id,
        "question": faq.question,
        "answer": faq.answer,
    } for faq in faqs]
    return Response(data)


@api_view(['POST'])
def scheme_preview(request):
    """
    API endpoint to get scheme preview with all financial calculations.
    This replaces the need for separate /scheme/{id} and calculation calls.
    """
    scheme_id = request.data.get('scheme_id')
    monthly_amount = request.data.get('monthly_amount')

    if not scheme_id or not monthly_amount:
        return Response({"message": "scheme_id and monthly_amount are required"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        scheme = SchemeMaster.objects.get(id=scheme_id, is_active=True)
    except SchemeMaster.DoesNotExist:
        return Response({"message": "Scheme not found or inactive"}, status=status.HTTP_404_NOT_FOUND)

    try:
        monthly_amount = Decimal(monthly_amount)
    except (ValueError, TypeError):
        return Response({"message": "Monthly amount must be a valid number"}, status=status.HTTP_400_BAD_REQUEST)

    if not validate_scheme_amount(scheme, monthly_amount):
        return Response({
            "message": f"Monthly amount must be between {scheme.min_instalment} and {scheme.max_instalment}"
        }, status=status.HTTP_400_BAD_REQUEST)

    preview_data = calculate_scheme_preview(scheme, monthly_amount)

    return Response(preview_data, status=status.HTTP_200_OK)


@api_view(['GET'])
def customer_cms_page(request):
    """
    API endpoint to retrieve the default CMS page for the customer.
    """
    cms_page = get_default_cms_page()
    data = [
        {
            "id": page.pk,
            "slug": page.page_key,
            "title": page.title,
            "content": page.content,
        }
        for page in (cms_page or [])
    ]
    return Response(data)
