"""
Stock & inventory — function-based views, no serializers.
Ledger: product_stock_transactions. Current balance: product_items.qty
"""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from master.views.product_views import get_admin_user_from_request
from shared.models import Branch, ProductItem, GrnBag, StockTransaction
from shared.product_item_size import product_item_search_q, serialize_product_item_size_for_api
from shared.services.stock_service import adjust_product_item_qty


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_branch(data):
    """Return Branch or None. branch_id is fully optional for single-location setups."""
    branch_id = data.get("branch_id")
    if branch_id in (None, "", 0):
        return None
    return Branch.objects.filter(pk=int(branch_id)).first()


def _item_display_code(item):
    if not item:
        return ""
    if item.sku_id and item.sku.product_code:
        return item.sku.product_code
    return f"ITEM-{item.pk}"


def _txn_to_dict(txn):
    row = {
        "id": txn.id,
        "product_item_id": txn.product_item_id,
        "product_code": _item_display_code(txn.product_item),
        "branch_id": txn.branch_id,
        "branch_name": txn.branch.name if txn.branch_id else "",
        "txn_type": txn.txn_type,
        "quantity": txn.quantity,
        "bag_id": txn.bag_id,
        "reference": txn.reference or "",
        "notes": txn.notes or "",
        "txn_date": txn.txn_date.isoformat() if txn.txn_date else "",
        "performed_by": txn.performed_by_id,
    }
    if txn.product_item_id and getattr(txn, "product_item", None):
        row.update(serialize_product_item_size_for_api(txn.product_item))
    return row


def _stock_row_from_item(item):
    row = {
        "id": item.id,
        "product_item_id": item.id,
        "product_code": _item_display_code(item),
        "branch_id": None,
        "branch_name": "",
        "quantity": item.qty,
        "last_txn_date": item.system_updated_at.isoformat() if item.system_updated_at else "",
    }
    row.update(serialize_product_item_size_for_api(item))
    return row


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@api_view(["POST"])
@admin_auth()
def stock_in(request):
    """
    Receive a product item into stock at a branch.
    POST { product_item_id, branch_id, quantity?, bag_id?, reference?, notes? }
    """
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Auth required."}, status=status.HTTP_401_UNAUTHORIZED)

    data = request.data or {}
    errors = {}

    try:
        item = ProductItem.objects.get(pk=int(data.get("product_item_id", 0)))
    except (ProductItem.DoesNotExist, TypeError, ValueError):
        errors["product_item_id"] = ["Invalid or missing."]

    branch = _resolve_branch(data)

    qty = int(data.get("quantity", 1))
    if qty < 1:
        errors["quantity"] = ["Must be >= 1."]

    bag = None
    bag_id = data.get("bag_id")
    if bag_id not in (None, "", 0):
        try:
            bag = GrnBag.objects.get(pk=int(bag_id))
        except (GrnBag.DoesNotExist, TypeError, ValueError):
            errors["bag_id"] = ["Invalid."]

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    try:
        txn = adjust_product_item_qty(
            product_item=item,
            delta=qty,
            txn_type="bag_in",
            admin=admin,
            branch=branch,
            bag=bag,
            reference=(data.get("reference") or "").strip(),
            notes=(data.get("notes") or "").strip(),
        )
    except ValidationError as e:
        return Response({"errors": {"quantity": [str(e)]}}, status=status.HTTP_400_BAD_REQUEST)

    return Response(_txn_to_dict(txn), status=status.HTTP_201_CREATED)


@api_view(["POST"])
@admin_auth()
def stock_out(request):
    """
    Remove a product item from stock (sale, transfer_out, adjustment).
    POST { product_item_id, branch_id, txn_type, quantity?, reference?, notes? }
    """
    admin = get_admin_user_from_request(request)
    if not admin:
        return Response({"detail": "Auth required."}, status=status.HTTP_401_UNAUTHORIZED)

    data = request.data or {}
    errors = {}

    try:
        item = ProductItem.objects.get(pk=int(data.get("product_item_id", 0)))
    except (ProductItem.DoesNotExist, TypeError, ValueError):
        errors["product_item_id"] = ["Invalid or missing."]

    branch = _resolve_branch(data)

    txn_type = (data.get("txn_type") or "").strip()
    allowed_out = ("sale", "transfer_out", "adjustment")
    if txn_type not in allowed_out:
        errors["txn_type"] = [f"Must be one of: {', '.join(allowed_out)}."]

    qty = int(data.get("quantity", 1))
    if qty < 1:
        errors["quantity"] = ["Must be >= 1."]

    if errors:
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

    if item.qty < qty:
        return Response(
            {"errors": {"quantity": [f"Only {item.qty} in stock."]}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        txn = adjust_product_item_qty(
            product_item=item,
            delta=-qty,
            txn_type=txn_type,
            admin=admin,
            branch=branch,
            reference=(data.get("reference") or "").strip(),
            notes=(data.get("notes") or "").strip(),
        )
    except ValidationError as e:
        return Response({"errors": {"quantity": [str(e)]}}, status=status.HTTP_400_BAD_REQUEST)

    return Response(_txn_to_dict(txn), status=status.HTTP_201_CREATED)


@api_view(["GET"])
@admin_auth()
def stock_list(request):
    """
    Current stock (ProductItem rows with qty > 0).
    GET ?branch_id=&product_item_id=&page=1&page_size=50&q=
    """
    from django.db.models import Q as _Q

    qs = ProductItem.objects.select_related("sku", "sku__product_group").filter(qty__gt=0)

    item_id = request.GET.get("product_item_id")
    if item_id:
        qs = qs.filter(pk=int(item_id))

    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            _Q(sku__product_code__icontains=q)
            | _Q(store_variant_name__icontains=q)
            | _Q(sku__sku_code__icontains=q)
            | _Q(sku__product_group__style_name__icontains=q)
            | product_item_search_q(q)
        )

    sn = request.GET.get("size_number")
    if sn not in (None, ""):
        try:
            qs = qs.filter(size_number=int(sn))
        except (TypeError, ValueError):
            pass
    sm = request.GET.get("size_mm")
    if sm not in (None, ""):
        try:
            qs = qs.filter(size_mm=Decimal(str(sm)))
        except (InvalidOperation, ValueError, TypeError):
            pass
    hm = request.GET.get("height_mm")
    wm = request.GET.get("width_mm")
    if hm not in (None, "") and wm not in (None, ""):
        try:
            qs = qs.filter(height_mm=Decimal(str(hm)), width_mm=Decimal(str(wm)))
        except (InvalidOperation, ValueError, TypeError):
            pass

    page = max(int(request.GET.get("page", 1)), 1)
    page_size = min(int(request.GET.get("page_size", 50)), 200)
    start = (page - 1) * page_size
    total = qs.count()
    items = list(qs.order_by("-system_updated_at")[start : start + page_size])

    return Response(
        {
            "results": [_stock_row_from_item(s) for s in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@api_view(["GET"])
@admin_auth()
def stock_transactions(request):
    """
    Transaction history.
    GET ?product_item_id=&branch_id=&txn_type=&page=1&page_size=50
    """
    qs = StockTransaction.objects.select_related(
        "product_item",
        "product_item__sku",
        "branch",
    ).order_by("-txn_date")

    item_id = request.GET.get("product_item_id")
    if item_id:
        qs = qs.filter(product_item_id=int(item_id))

    branch_id = request.GET.get("branch_id")
    if branch_id:
        qs = qs.filter(branch_id=int(branch_id))

    txn_type = request.GET.get("txn_type")
    if txn_type:
        qs = qs.filter(txn_type=txn_type)

    page = max(int(request.GET.get("page", 1)), 1)
    page_size = min(int(request.GET.get("page_size", 50)), 200)
    start = (page - 1) * page_size
    total = qs.count()
    items = qs[start : start + page_size]

    return Response(
        {
            "results": [_txn_to_dict(t) for t in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@api_view(["GET"])
@admin_auth()
def stock_item_provenance(request, product_item_id):
    """
    Full traceability for a single item: item → bag → lot → batch.
    GET /master/stock/provenance/<product_item_id>/
    """
    try:
        item = ProductItem.objects.select_related("sku", "sku__product_group").get(pk=product_item_id)
    except ProductItem.DoesNotExist:
        return Response({"detail": "Item not found."}, status=status.HTTP_404_NOT_FOUND)

    bag = (
        GrnBag.objects.filter(product_item=item)
        .select_related("lot", "lot__batch")
        .first()
    )

    result = {
        "item": {
            "id": item.id,
            "product_code": _item_display_code(item),
            "qty": item.qty,
            "sku_code": item.sku.sku_code if item.sku else "",
            **serialize_product_item_size_for_api(item),
        },
        "bag": None,
        "lot": None,
        "batch": None,
    }
    if bag:
        result["bag"] = {"id": bag.id, "bag_no": bag.bag_no}
        if bag.lot:
            result["lot"] = {"id": bag.lot.id, "lot_no": bag.lot.lot_no}
            if bag.lot.batch:
                result["batch"] = {
                    "id": bag.lot.batch.id,
                    "doc_no": bag.lot.batch.doc_no or "",
                }

    result["qty_on_hand"] = item.qty
    return Response(result)
