"""
Central stock mutations: ledger row (StockTransaction) + atomic ProductItem.qty update.
Never update ProductItem.qty without a matching StockTransaction in the same transaction.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from shared.models import ProductItem, StockTransaction


def adjust_product_item_qty(
    *,
    product_item,
    delta,
    txn_type,
    admin,
    branch=None,
    bag=None,
    reference="",
    notes="",
):
    """
    Apply a stock movement.

    :param product_item: ProductItem instance or pk
    :param delta: signed integer (+in / -out)
    :param txn_type: StockTransaction.TXN_TYPE_CHOICES value
    :raises ValidationError: if resulting qty would be negative
    """
    pk = product_item.pk if isinstance(product_item, ProductItem) else int(product_item)
    delta = int(delta)
    if delta == 0:
        raise ValidationError("delta must be non-zero.")

    with transaction.atomic():
        item = ProductItem.objects.select_for_update().get(pk=pk)
        new_qty = item.qty + delta
        if new_qty < 0:
            raise ValidationError(
                f"Insufficient stock for item {item.id}: have {item.qty}, need {-delta}."
            )
        txn = StockTransaction.objects.create(
            product_item=item,
            branch=branch,
            txn_type=txn_type,
            quantity=delta,
            bag=bag,
            reference=reference or "",
            notes=notes or "",
            performed_by=admin,
            created_by=admin,
            updated_by=admin,
        )
        ProductItem.objects.filter(pk=item.pk).update(
            qty=F("qty") + delta,
            system_updated_at=timezone.now(),
            updated_by=admin,
        )
    item.refresh_from_db()
    return txn
