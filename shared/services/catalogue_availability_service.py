"""
Catalogue stock availability vs active quotation reservations.

Physical on-hand qty comes from ProductItem.qty. Reserved qty is the sum of line
quantities on non-removed quote lines for draft / order / booking quotes.
"""
from __future__ import annotations

from django.db.models import Q, Sum

from shared.models import CatalogueQuote, CatalogueQuoteLine

ACTIVE_QUOTE_STATUSES = (
    CatalogueQuote.STATUS_DRAFT,
    CatalogueQuote.STATUS_ORDER,
    CatalogueQuote.STATUS_BOOKING,
)


def _active_quote_lines_qs(*, exclude_quote_id: int | None = None):
    qs = CatalogueQuoteLine.objects.filter(
        is_removed=False,
        quote__status__in=ACTIVE_QUOTE_STATUSES,
    )
    if exclude_quote_id:
        qs = qs.exclude(quote_id=exclude_quote_id)
    return qs


def resolve_exclude_quote_id(raw: str | int | None) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    pk = (
        CatalogueQuote.objects.filter(quote_number=text)
        .values_list("id", flat=True)
        .first()
    )
    return pk


def reserved_qty_by_product_item(
    product_item_ids: list[int],
    *,
    exclude_quote_id: int | None = None,
) -> dict[int, int]:
    """Map ProductItem pk → total reserved quantity across active quotes."""
    if not product_item_ids:
        return {}
    id_strs = [str(i) for i in product_item_ids]
    rows = (
        _active_quote_lines_qs(exclude_quote_id=exclude_quote_id)
        .filter(product_id__in=id_strs)
        .values("product_id")
        .annotate(total=Sum("quantity"))
    )
    out: dict[int, int] = {}
    for row in rows:
        pid = row.get("product_id")
        if pid is None:
            continue
        try:
            out[int(pid)] = int(row.get("total") or 0)
        except (TypeError, ValueError):
            continue
    return out


def reserved_tag_ids(
    tag_ids: list[int],
    *,
    exclude_quote_id: int | None = None,
) -> set[int]:
    """Tag ids already on an active quote line (barcode variant_key suffix)."""
    if not tag_ids:
        return set()
    q = Q()
    for tid in tag_ids:
        q |= Q(variant_key__endswith=f"|tag|{tid}")
    rows = (
        _active_quote_lines_qs(exclude_quote_id=exclude_quote_id)
        .filter(q)
        .values_list("variant_key", flat=True)
    )
    reserved: set[int] = set()
    for key in rows:
        if not key or "|tag|" not in key:
            continue
        tail = key.rsplit("|tag|", 1)[-1]
        if tail.isdigit():
            reserved.add(int(tail))
    return reserved


def availability_from_counts(
    stock_qty: int,
    reserved_qty: int,
    *,
    tag_id: int | None = None,
    tag_reserved: set[int] | None = None,
) -> dict:
    """Availability from pre-fetched stock and reserved counts."""
    stock_qty = max(0, int(stock_qty or 0))
    reserved_qty = max(0, int(reserved_qty or 0))
    available_qty = max(0, stock_qty - reserved_qty) if stock_qty > 0 else 0
    is_barcode = tag_id is not None

    tag_locked = False
    if is_barcode and tag_reserved is not None:
        tag_locked = tag_id in tag_reserved

    if is_barcode:
        # Tagged physical stock: qty 0 = out of stock; qty N all reserved = locked.
        is_locked = tag_locked or stock_qty <= 0 or available_qty <= 0
    else:
        # Virtual designs: only lock when stock is tracked and fully reserved.
        is_locked = stock_qty > 0 and available_qty <= 0

    return {
        "stockQty": stock_qty,
        "stockPcs": stock_qty,
        "reservedQty": reserved_qty,
        "availableQty": available_qty,
        "isLocked": is_locked,
        "tagReserved": tag_locked,
        "outOfStock": is_barcode and stock_qty <= 0 and not tag_locked,
    }


def availability_for_item(
    item,
    reserved_map: dict[int, int],
    *,
    tag_id: int | None = None,
    tag_reserved: set[int] | None = None,
) -> dict:
    """Build storefront availability fields for a ProductItem (and optional barcode tag)."""
    stock_qty = max(0, int(getattr(item, "qty", 0) or 0))
    reserved_qty = max(0, int(reserved_map.get(item.id, 0)))
    return availability_from_counts(
        stock_qty,
        reserved_qty,
        tag_id=tag_id,
        tag_reserved=tag_reserved,
    )


def _item_has_barcode_tag(product_item_id: int) -> bool:
    from shared.models import ProductTag

    return ProductTag.objects.filter(
        product_item_id=product_item_id,
        tag_type="barcode",
        is_active=True,
    ).exists()


def max_addable_qty(
    product_item_id: int,
    *,
    exclude_quote_id: int | None = None,
    already_in_session: int = 0,
) -> int | None:
    """
    Max quantity that can still be added for this product item.
    Returns None when stock is not tracked (virtual master, qty 0).
    Returns 0 when tagged barcode stock is zero.
    """
    from shared.models import ProductItem

    item = ProductItem.objects.filter(pk=product_item_id).only("qty").first()
    if not item:
        return 0
    stock_qty = max(0, int(item.qty or 0))
    if stock_qty <= 0:
        if _item_has_barcode_tag(product_item_id):
            return 0
        return None
    reserved = reserved_qty_by_product_item(
        [product_item_id], exclude_quote_id=exclude_quote_id
    ).get(product_item_id, 0)
    available = max(0, stock_qty - reserved)
    return max(0, available - max(0, already_in_session))
