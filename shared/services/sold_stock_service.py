"""
Mark barcoded pieces as sold when a product invoice is created.

- Deactivate the ProductTag so it leaves catalogue / existing stock.
- Rename tag_value (…~SOLD{id}) so the original barcode number can be reused.
- Stock-out ProductItem.qty with txn_type=sale.
- SaleItem keeps the sold-tag FK (new sold-out identity).
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from shared.models import ProductItem, ProductTag, SaleInvoice, StockTransaction
from shared.services.stock_service import adjust_product_item_qty

SOLD_TAG_MARKER = "~SOLD"


def _as_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _line_qty(sale_item: SaleItem) -> int:
    try:
        qty = int(sale_item.qty or 1)
    except (TypeError, ValueError):
        qty = 1
    return max(1, qty)


def _sold_tag_value(original: str, tag_id: int) -> str:
    base = (original or "").strip()
    marker = f"{SOLD_TAG_MARKER}{tag_id}"
    if marker in base:
        return base[:128]
    if SOLD_TAG_MARKER in base:
        return base[:128]
    if not base:
        return f"SOLD{tag_id}"[:128]
    return f"{base}{marker}"[:128]


def original_barcode_from_tag_value(tag_value: str | None) -> str:
    raw = (tag_value or "").strip()
    idx = raw.rfind(SOLD_TAG_MARKER)
    if idx > 0:
        return raw[:idx]
    return raw


def resolve_tag_and_item_from_quote_line(line) -> tuple[ProductTag | None, ProductItem | None]:
    """Map a catalogue quote line to ProductTag / ProductItem."""
    tag_id = None
    item_id = None
    vk = (getattr(line, "variant_key", None) or "").strip()
    lower = vk.lower()
    if lower.startswith("barcode|"):
        parts = vk.split("|")
        if len(parts) >= 2:
            item_id = _as_int(parts[1])
        if len(parts) >= 4 and parts[2].lower() == "tag":
            tag_id = _as_int(parts[3])
    elif "|tag|" in lower:
        try:
            tag_id = int(vk.rsplit("|tag|", 1)[-1].strip())
        except ValueError:
            tag_id = None

    sources = []
    breakdown = getattr(line, "breakdown", None)
    if isinstance(breakdown, dict):
        sources.append(breakdown)
    pricing_meta = getattr(line, "pricing_meta", None)
    if isinstance(pricing_meta, dict):
        sources.append(pricing_meta)

    for src in sources:
        if tag_id is not None:
            break
        for key in ("productTagId", "product_tag_id", "tagId", "tag_id"):
            tag_id = _as_int(src.get(key))
            if tag_id is not None:
                break

    if item_id is None:
        item_id = _as_int(getattr(line, "product_id", None))
    for src in sources:
        if item_id is not None:
            break
        for key in ("productItemId", "product_item_id"):
            item_id = _as_int(src.get(key))
            if item_id is not None:
                break

    tag = None
    if tag_id is not None:
        tag = (
            ProductTag.objects.select_related("product_item", "grn_bag")
            .filter(pk=tag_id)
            .first()
        )
        if tag is not None:
            return tag, tag.product_item

    if item_id is not None:
        item = ProductItem.objects.filter(pk=item_id).first()
        return None, item
    return None, None


def _sale_txn_exists(*, product_item_id: int, reference: str, notes_needle: str) -> bool:
    return StockTransaction.objects.filter(
        product_item_id=product_item_id,
        txn_type="sale",
        reference=reference,
        notes__contains=notes_needle,
    ).exists()


def _return_txn_exists(*, product_item_id: int, reference: str, notes_needle: str) -> bool:
    return StockTransaction.objects.filter(
        product_item_id=product_item_id,
        txn_type="return",
        reference=reference,
        notes__contains=notes_needle,
    ).exists()


def _deactivate_sold_tag(tag: ProductTag, invoice: SaleInvoice, admin) -> None:
    original = original_barcode_from_tag_value(tag.tag_value)
    tag.is_active = False
    tag.tag_value = _sold_tag_value(original, tag.id)
    note = f"Sold as {original} on {invoice.invoice_number}"
    existing = (tag.remark or "").strip()
    if note not in existing:
        tag.remark = f"{existing} | {note}".strip(" |")[:255] if existing else note[:255]
    tag.updated_by = admin
    try:
        tag.save(update_fields=["is_active", "tag_value", "remark", "updated_by", "system_updated_at"])
    except IntegrityError:
        tag.tag_value = f"SOLD{tag.id}"[:128]
        tag.save(update_fields=["is_active", "tag_value", "remark", "updated_by", "system_updated_at"])


def _stock_out_sale_item(sale_item: SaleItem, invoice: SaleInvoice, admin) -> None:
    product_item = sale_item.product_item
    tag = sale_item.tag
    if product_item is None and tag is not None:
        product_item = tag.product_item
        sale_item.product_item = product_item
        sale_item.save(update_fields=["product_item", "system_updated_at"])
    if product_item is None:
        return

    qty = _line_qty(sale_item)
    notes_needle = f"tag:{tag.id}" if tag is not None else f"item:{product_item.id}"
    if _sale_txn_exists(
        product_item_id=product_item.id,
        reference=invoice.invoice_number,
        notes_needle=notes_needle,
    ):
        return
    try:
        adjust_product_item_qty(
            product_item=product_item,
            delta=-qty,
            txn_type="sale",
            admin=admin,
            bag=tag.grn_bag if tag is not None else None,
            reference=invoice.invoice_number,
            notes=f"Sold {notes_needle} invoice {invoice.invoice_number}",
        )
    except ValidationError:
        pass


@transaction.atomic
def mark_invoice_pieces_sold(invoice: SaleInvoice, admin=None) -> None:
    """Deactivate tagged pieces and stock-out. Idempotent per invoice + tag/item."""
    items = list(
        invoice.items.select_related("tag", "tag__grn_bag", "product_item").all()
    )
    for sale_item in items:
        tag = sale_item.tag
        if tag is not None:
            if tag.is_active or SOLD_TAG_MARKER not in (tag.tag_value or ""):
                _deactivate_sold_tag(tag, invoice, admin)
        _stock_out_sale_item(sale_item, invoice, admin)


def snapshot_invoice_pieces(invoice: SaleInvoice) -> list[dict]:
    rows = []
    for sale_item in invoice.items.select_related("tag", "tag__grn_bag", "product_item").all():
        rows.append(
            {
                "tag_id": sale_item.tag_id,
                "product_item_id": sale_item.product_item_id
                or (sale_item.tag.product_item_id if sale_item.tag_id else None),
                "qty": _line_qty(sale_item),
                "bag_id": sale_item.tag.grn_bag_id if sale_item.tag_id else None,
            }
        )
    return rows


def _restore_tag(tag: ProductTag, admin) -> None:
    original = original_barcode_from_tag_value(tag.tag_value)
    tag.is_active = True
    tag.updated_by = admin
    update_fields = ["is_active", "updated_by", "system_updated_at"]
    if original and original != tag.tag_value:
        taken = (
            ProductTag.objects.filter(tag_value=original)
            .exclude(pk=tag.pk)
            .exists()
        )
        if not taken:
            tag.tag_value = original
            update_fields.append("tag_value")
    tag.save(update_fields=update_fields)


def _stock_in_return(*, product_item_id: int, qty: int, bag_id, reference: str, notes_needle: str, admin) -> None:
    if _return_txn_exists(
        product_item_id=product_item_id,
        reference=reference,
        notes_needle=notes_needle,
    ):
        return
    if not _sale_txn_exists(
        product_item_id=product_item_id,
        reference=reference,
        notes_needle=notes_needle,
    ):
        return
    try:
        adjust_product_item_qty(
            product_item=product_item_id,
            delta=qty,
            txn_type="return",
            admin=admin,
            bag=bag_id,
            reference=reference,
            notes=f"Unsold {notes_needle} invoice {reference}",
        )
    except ValidationError:
        pass


def _restore_piece_row(row: dict, *, reference: str, admin=None) -> None:
    tag_id = row.get("tag_id")
    product_item_id = row.get("product_item_id")
    notes_needle = f"tag:{tag_id}" if tag_id else f"item:{product_item_id}"
    if tag_id:
        tag = ProductTag.objects.filter(pk=tag_id).first()
        if tag is not None:
            _restore_tag(tag, admin)
            if product_item_id is None:
                product_item_id = tag.product_item_id
    if product_item_id:
        _stock_in_return(
            product_item_id=product_item_id,
            qty=int(row.get("qty") or 1),
            bag_id=row.get("bag_id"),
            reference=reference,
            notes_needle=notes_needle,
            admin=admin,
        )


@transaction.atomic
def restore_removed_invoice_pieces(
    previous: list[dict],
    invoice: SaleInvoice,
    admin=None,
) -> None:
    """Reactivate tags / reverse stock for lines dropped on invoice update."""
    current_tag_ids = set(
        invoice.items.exclude(tag_id=None).values_list("tag_id", flat=True)
    )
    current_untagged_item_ids = set(
        invoice.items.filter(tag_id=None)
        .exclude(product_item_id=None)
        .values_list("product_item_id", flat=True)
    )
    for row in previous:
        tag_id = row.get("tag_id")
        product_item_id = row.get("product_item_id")
        if tag_id and tag_id in current_tag_ids:
            continue
        if not tag_id and product_item_id in current_untagged_item_ids:
            continue
        _restore_piece_row(row, reference=invoice.invoice_number, admin=admin)


@transaction.atomic
def restore_invoice_pieces(invoice: SaleInvoice, admin=None) -> None:
    """Undo sold-out for every line (invoice delete / void)."""
    previous = snapshot_invoice_pieces(invoice)
    for row in previous:
        _restore_piece_row(row, reference=invoice.invoice_number, admin=admin)
