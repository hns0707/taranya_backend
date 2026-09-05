from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from typing import Any, Dict, List, Tuple
from uuid import uuid4

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from shared.models import LookupValue, Payment, PaymentCollection, SaleInvoice, SaleInvoiceCounter, SaleItem
from shared.services.sold_stock_service import (
    mark_invoice_pieces_sold,
    restore_invoice_pieces,
    restore_removed_invoice_pieces,
    snapshot_invoice_pieces,
)


INVOICE_PREFIX = "IS1"
CGST_RATE = Decimal("0.015")
SGST_RATE = Decimal("0.015")
TOTAL_GST_RATE = CGST_RATE + SGST_RATE
MONEY_PRECISION = Decimal("0.01")


def _to_decimal(value: Any, field_name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid decimal for {field_name}")
    if d < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return d


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_PRECISION)


def _financial_year_code(for_dt: datetime | date) -> str:
    """
    Returns FY code in format YY-YY (Apr-Mar), e.g. 26-27.
    """
    if isinstance(for_dt, datetime):
        for_dt = timezone.localtime(for_dt).date()
    start_year = for_dt.year if for_dt.month >= 4 else for_dt.year - 1
    end_year = start_year + 1
    return f"{str(start_year)[-2:]}-{str(end_year)[-2:]}"


def _parse_invoice_date(raw) -> date:
    """Bill date from API (YYYY-MM-DD). Defaults to today; cannot be in the future."""
    today = timezone.localdate()
    if raw in (None, ""):
        return today
    if isinstance(raw, date) and not isinstance(raw, datetime):
        parsed = raw
    else:
        parsed = parse_date(str(raw).strip())
    if not parsed:
        raise ValueError("invoice_date must be a valid date (YYYY-MM-DD).")
    if parsed > today:
        raise ValueError("invoice_date cannot be in the future.")
    return parsed


def _paid_at_for_invoice_date(invoice_date: date) -> datetime:
    """Payment timestamp aligned to bill date (local noon) for ledger/reporting."""
    tz = timezone.get_current_timezone()
    return timezone.make_aware(datetime.combine(invoice_date, datetime.min.time().replace(hour=12)), tz)


def _extract_invoice_sequence(invoice_number: str | None) -> int | None:
    """
    Parse sequence from invoice numbers like IS1/400/26-27.
    Soft-deleted reuse markers (…~DEL123) are ignored for the numeric part.
    """
    if not invoice_number:
        return None
    raw = str(invoice_number).split("~", 1)[0].strip()
    parts = raw.split("/")
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None


def _max_active_invoice_sequence() -> int:
    """Highest invoice sequence among non-deleted invoices (0 if none)."""
    max_seq = 0
    for num in (
        SaleInvoice.objects.filter(is_deleted=False)
        .values_list("invoice_number", flat=True)
        .iterator(chunk_size=500)
    ):
        seq = _extract_invoice_sequence(num)
        if seq is not None and seq > max_seq:
            max_seq = seq
    return max_seq


def _sync_invoice_counter_to_max_active() -> int:
    """
    After tip-delete, pull the counter down to max(active sequences)
    so the next allocate reuses that tip number.
    """
    max_active = _max_active_invoice_sequence()
    counter, _ = SaleInvoiceCounter.objects.select_for_update().get_or_create(id=1)
    if int(counter.last_number or 0) != max_active:
        counter.last_number = max_active
        counter.save(update_fields=["last_number", "system_updated_at"])
    return max_active


def _next_invoice_sequence_from_watermark() -> int:
    """
    Next invoice sequence = max(counter.last_number, max active invoice seq) + 1.

    - Seed sale_invoice_counters.last_number = 24 → next is 25 (even with no invoices).
    - Tip-delete syncs last_number down to max active so that number is reused.
    """
    counter, _ = SaleInvoiceCounter.objects.select_for_update().get_or_create(id=1)
    max_active = _max_active_invoice_sequence()
    watermark = int(counter.last_number or 0)
    return max(watermark, max_active) + 1


def _allocate_next_invoice_sequence() -> int:
    """
    Allocate and persist the next invoice sequence.
    Deleting the latest invoice reuses that number; deleting a middle one does not.
    """
    counter, _ = SaleInvoiceCounter.objects.select_for_update().get_or_create(id=1)
    next_number = _next_invoice_sequence_from_watermark()
    counter.last_number = next_number
    counter.save(update_fields=["last_number", "system_updated_at"])
    return next_number


def _generate_invoice_number(*, for_date: date | None = None) -> str:
    next_number = _allocate_next_invoice_sequence()
    bill_date = for_date or timezone.localdate()
    fy = _financial_year_code(bill_date)
    return f"{INVOICE_PREFIX}/{next_number}/{fy}"


def peek_next_invoice_number(*, for_date: date | None = None) -> str:
    """Preview the next invoice number without consuming the counter."""
    counter = SaleInvoiceCounter.objects.filter(id=1).first()
    watermark = int(counter.last_number or 0) if counter else 0
    next_number = max(watermark, _max_active_invoice_sequence()) + 1
    bill_date = for_date or timezone.localdate()
    fy = _financial_year_code(bill_date)
    return f"{INVOICE_PREFIX}/{next_number}/{fy}"


def _resolve_payment_status(code: str) -> LookupValue:
    return LookupValue.objects.get(lookup__code="PAYMENT_STATUS", code=code)


def _resolve_payment_mode(code: str) -> LookupValue:
    if code == "NETBANKING":
        return (
            LookupValue.objects.filter(lookup__code="PAYMENT_MODE", code__in=["NETBANKING", "BANK_TRANSFER"])
            .order_by("code")
            .first()
            or LookupValue.objects.get(lookup__code="PAYMENT_MODE", code="NETBANKING")
        )
    return LookupValue.objects.get(lookup__code="PAYMENT_MODE", code=code)


def _validate_pos_payment_mode(mode: str, idx: int) -> str:
    """
    Accept any active PAYMENT_MODE lookup code (same rules as GET /master/payment-modes/).
    NETBANKING still matches either NETBANKING or BANK_TRANSFER, consistent with _resolve_payment_mode.
    """
    base = LookupValue.objects.filter(
        lookup__code="PAYMENT_MODE",
        lookup__is_active=True,
        is_active=True,
    )
    if mode == "NETBANKING":
        ok = base.filter(code__in=["NETBANKING", "BANK_TRANSFER"]).exists()
    else:
        ok = base.filter(code=mode).exists()
    if not ok:
        raise ValueError(
            f"payments[{idx}].mode must be a valid PAYMENT_MODE lookup code (received {mode!r})"
        )
    return mode


def _derive_invoice_status(total_amount: Decimal, paid_amount: Decimal) -> Tuple[str, Decimal]:
    pending = total_amount - paid_amount
    if pending < 0:
        raise ValueError("Payment cannot exceed total amount")
    if pending == 0:
        return SaleInvoice.STATUS_PAID, pending
    if paid_amount > 0:
        return SaleInvoice.STATUS_PARTIAL, pending
    return SaleInvoice.STATUS_PENDING, pending


def _normalize_invoice_payload(payload: Dict[str, Any]):
    """Shared input validation/normalization for create + update."""
    items = payload.get("items") or []
    payments = payload.get("payments") or []

    if not items:
        raise ValueError("At least one item is required")

    bill_to_name = str(payload.get("bill_to_name") or "").strip()
    bill_to_phone = str(payload.get("bill_to_phone") or "").strip()
    bill_to_address = str(payload.get("bill_to_address") or "").strip()
    if not bill_to_name or not bill_to_phone or not bill_to_address:
        raise ValueError("bill_to_name, bill_to_phone, bill_to_address are required")

    invoice_date = _parse_invoice_date(payload.get("invoice_date"))

    normalized_items: List[Dict[str, Any]] = []
    gross_amount = Decimal("0")
    for idx, item in enumerate(items, start=1):
        product_name = str(item.get("product_name") or "").strip()
        if not product_name:
            raise ValueError(f"items[{idx}] product_name is required")

        qty = _to_decimal(item.get("qty", 0), f"items[{idx}].qty")
        if qty <= 0:
            raise ValueError(f"items[{idx}].qty must be greater than zero")

        gross_weight = _to_decimal(item.get("gross_weight", 0), f"items[{idx}].gross_weight")
        net_weight = _to_decimal(item.get("net_weight", 0), f"items[{idx}].net_weight")
        if net_weight > gross_weight:
            raise ValueError(f"items[{idx}].net_weight cannot exceed gross_weight")
        making_charge = _to_decimal(item.get("making_charge", 0), f"items[{idx}].making_charge")
        final_amount = _to_decimal(item.get("final_amount", 0), f"items[{idx}].final_amount")
        purity = str(item.get("purity") or "").strip()

        if final_amount <= 0:
            raise ValueError(f"items[{idx}].final_amount must be greater than zero")
        if not purity:
            raise ValueError(f"items[{idx}].purity is required")

        gross_amount += final_amount
        normalized_items.append(
            {
                "product_name": product_name,
                "hsn": str(item.get("hsn") or "").strip(),
                "qty": qty,
                "gross_weight": gross_weight,
                "net_weight": net_weight,
                "purity": purity,
                "making_charge": making_charge,
                "final_amount": final_amount,
                "is_manual_entry": bool(item.get("is_manual_entry", True)),
                "product_item_id": item.get("product_item_id"),
                "tag_id": item.get("tag_id"),
            }
        )

    normalized_payments: List[Dict[str, Any]] = []
    paid_amount = Decimal("0")
    for idx, p in enumerate(payments, start=1):
        mode = str(p.get("mode") or "").strip().upper()
        if not mode:
            continue
        mode = _validate_pos_payment_mode(mode, idx)
        amount = _to_decimal(p.get("amount", 0), f"payments[{idx}].amount")
        if amount <= 0:
            raise ValueError(f"payments[{idx}].amount must be greater than zero")
        paid_amount += amount
        normalized_payments.append({"mode": mode, "amount": amount})

    gross_amount = _round_money(gross_amount)
    taxable_amount = _round_money(gross_amount / (Decimal("1") + TOTAL_GST_RATE))
    cgst_amount = _round_money(taxable_amount * CGST_RATE)
    sgst_amount = _round_money(taxable_amount * SGST_RATE)
    total_amount = gross_amount

    if paid_amount > total_amount:
        raise ValueError("Payment cannot exceed total amount")

    status, pending_amount = _derive_invoice_status(total_amount, paid_amount)

    if normalized_payments and sum(p["amount"] for p in normalized_payments) != paid_amount:
        raise ValueError("Split payment sum must match paid_amount")

    return {
        "bill_to_name": bill_to_name,
        "bill_to_phone": bill_to_phone,
        "bill_to_address": bill_to_address,
        "invoice_date": invoice_date,
        "normalized_items": normalized_items,
        "normalized_payments": normalized_payments,
        "total_amount": total_amount,
        "paid_amount": paid_amount,
        "pending_amount": pending_amount,
        "status": status,
    }


def _write_invoice_items_and_payments(
    invoice: SaleInvoice,
    normalized_items,
    normalized_payments,
    paid_amount,
    user,
    *,
    invoice_date: date,
):
    for item in normalized_items:
        SaleItem.objects.create(
            invoice=invoice,
            product_item_id=item["product_item_id"],
            tag_id=item["tag_id"],
            product_name=item["product_name"],
            hsn=item["hsn"],
            qty=item["qty"],
            gross_weight=item["gross_weight"],
            net_weight=item["net_weight"],
            purity=item["purity"],
            making_charge=item["making_charge"],
            final_amount=item["final_amount"],
            is_manual_entry=item["is_manual_entry"],
            created_by=user,
            updated_by=user,
        )

    if normalized_payments:
        status_code = "SUCCESS" if paid_amount > 0 else "PENDING"
        payment_status = _resolve_payment_status(status_code)

        payment = Payment.objects.create(
            instalment=None,
            payment_mode=_resolve_payment_mode(normalized_payments[0]["mode"]) if len(normalized_payments) == 1 else None,
            receipt_no=invoice.invoice_number,
            payment_source="POS",
            is_split_payment=len(normalized_payments) > 1,
            transaction_id=f"POS-{uuid4().hex[:20]}",
            payment_status=payment_status,
            amount=paid_amount,
            paid_at=_paid_at_for_invoice_date(invoice_date),
            is_finalized=True,
            reference_type="SALE_INVOICE",
            reference_id=invoice.id,
            created_by=user,
            updated_by=user,
        )

        for p in normalized_payments:
            PaymentCollection.objects.create(
                payment=payment,
                payment_mode=_resolve_payment_mode(p["mode"]),
                amount=p["amount"],
                reference_number=None,
                created_by=user,
                updated_by=user,
            )


def _maybe_send_outstanding_payment_sms(invoice: SaleInvoice, paid_amount: Decimal, pending_amount: Decimal) -> None:
    """Send DLT SMS when customer makes a payment against an invoice (non-blocking)."""
    if paid_amount <= 0 or not invoice.bill_to_phone:
        return
    try:
        from shared.services.sms_service import send_outstanding_balance_payment_sms

        send_outstanding_balance_payment_sms(
            mobile=invoice.bill_to_phone,
            payment_amount=paid_amount,
            receipt_number=invoice.invoice_number,
            remaining_balance=pending_amount,
        )
    except Exception:
        pass


@transaction.atomic
def create_pos_invoice(payload: Dict[str, Any], created_by=None) -> SaleInvoice:
    n = _normalize_invoice_payload(payload)

    invoice = SaleInvoice.objects.create(
        invoice_number=_generate_invoice_number(for_date=n["invoice_date"]),
        invoice_date=n["invoice_date"],
        bill_to_name=n["bill_to_name"],
        bill_to_phone=n["bill_to_phone"],
        bill_to_address=n["bill_to_address"],
        total_amount=n["total_amount"],
        paid_amount=n["paid_amount"],
        pending_amount=n["pending_amount"],
        status=n["status"],
        created_by=created_by,
        updated_by=created_by,
    )

    _write_invoice_items_and_payments(
        invoice,
        n["normalized_items"],
        n["normalized_payments"],
        n["paid_amount"],
        created_by,
        invoice_date=n["invoice_date"],
    )
    mark_invoice_pieces_sold(invoice, created_by)

    _maybe_send_outstanding_payment_sms(invoice, n["paid_amount"], n["pending_amount"])

    return invoice


@transaction.atomic
def update_pos_invoice(invoice_id: int, payload: Dict[str, Any], updated_by=None) -> SaleInvoice:
    """
    Update an existing POS invoice. Replaces items + payments transactionally
    (delete-then-recreate). Invoice number is preserved.
    """
    invoice = SaleInvoice.objects.select_for_update().get(pk=invoice_id, is_deleted=False)
    n = _normalize_invoice_payload(payload)

    invoice.bill_to_name = n["bill_to_name"]
    invoice.bill_to_phone = n["bill_to_phone"]
    invoice.bill_to_address = n["bill_to_address"]
    invoice.invoice_date = n["invoice_date"]
    invoice.total_amount = n["total_amount"]
    invoice.paid_amount = n["paid_amount"]
    invoice.pending_amount = n["pending_amount"]
    invoice.status = n["status"]
    invoice.updated_by = updated_by
    invoice.save()

    previous_pieces = snapshot_invoice_pieces(invoice)
    invoice.items.all().delete()

    # Drop existing Payment + PaymentCollections tied to this invoice
    old_payments = list(Payment.objects.filter(reference_type="SALE_INVOICE", reference_id=invoice.id))
    for op in old_payments:
        PaymentCollection.objects.filter(payment=op).delete()
        op.delete()

    _write_invoice_items_and_payments(
        invoice,
        n["normalized_items"],
        n["normalized_payments"],
        n["paid_amount"],
        updated_by,
        invoice_date=n["invoice_date"],
    )
    mark_invoice_pieces_sold(invoice, updated_by)
    restore_removed_invoice_pieces(previous_pieces, invoice, updated_by)

    _maybe_send_outstanding_payment_sms(invoice, n["paid_amount"], n["pending_amount"])

    return invoice


@transaction.atomic
def soft_delete_pos_invoice(invoice_id: int, deleted_by=None) -> SaleInvoice:
    """
    Soft-delete an invoice (is_deleted=True).

    Invoice sequence rules:
    - If the deleted invoice is the latest number (e.g. 400), free that number so the
      next generate reuses 400.
    - If a middle invoice is deleted (e.g. 395 while 400 still exists), keep the gap;
      next generate remains 401.
    """
    invoice = SaleInvoice.objects.select_for_update().get(pk=invoice_id, is_deleted=False)
    restore_invoice_pieces(invoice, deleted_by)
    deleted_seq = _extract_invoice_sequence(invoice.invoice_number)
    max_before = _max_active_invoice_sequence()

    invoice.is_deleted = True
    invoice.updated_by = deleted_by
    update_fields = ["is_deleted", "updated_by", "system_updated_at"]

    # Free unique invoice_number only when deleting the tip, so it can be reused.
    if deleted_seq is not None and deleted_seq == max_before:
        freed = invoice.invoice_number
        invoice.invoice_number = f"{freed}~DEL{invoice.id}"
        update_fields.append("invoice_number")

    invoice.save(update_fields=update_fields)
    _sync_invoice_counter_to_max_active()
    return invoice


WEIGHT_PRECISION = Decimal("0.001")
RATE_PRECISION = Decimal("0.01")

# Soft sort order for PAYMENT_MODE codes that exist in lookup (does not invent missing modes).
_EXPORT_PAYMENT_MODE_ORDER = (
    "CASH",
    "UPI",
    "CARD",
    "CHEQUE",
    "RTGS",
    "NEFT",
    "IMPS",
    "NETBANKING",
    "BANK_TRANSFER",
)

SALES_INVOICE_EXPORT_BASE_HEADER = [
    "Date",
    "INV Number",
    "Customer Name",
    "Mobile",
    "Gold weight",
    "Selling rate",
    "Silver weight",
    "Selling rate",
    "Total Without GST",
    "Cgst",
    "SGST",
    "total invoice",
]

SALES_INVOICE_EXPORT_TRAILING_HEADER = [
    "Amount balance",
]

# Kept for callers that still import the old constant name.
SALES_INVOICE_EXPORT_HEADER = (
    SALES_INVOICE_EXPORT_BASE_HEADER
    + ["Mode of Payment"]
    + SALES_INVOICE_EXPORT_TRAILING_HEADER
)


def _payment_mode_export_label(code: str, fallback_label: str = "") -> str:
    code_u = (code or "").strip().upper()
    if code_u == "ADVANCE":
        return "Advance amount"
    label = (fallback_label or "").strip()
    if label:
        return label
    return code_u.title() if code_u else "Other"


def _export_payment_mode_columns() -> List[Tuple[str, str]]:
    """
    Export amount columns:
      1) Advance amount — always first (managed outside PAYMENT_MODE)
      2) Every active PAYMENT_MODE lookup value (new modes appear automatically)
    ADVANCE from the lookup is skipped to avoid a duplicate column.
    """
    rows = list(
        LookupValue.objects.filter(
            lookup__code="PAYMENT_MODE",
            lookup__is_active=True,
            is_active=True,
        )
        .order_by("id")
        .values_list("code", "label")
    )

    by_code: Dict[str, str] = {}
    for code, label in rows:
        c = (code or "").strip().upper()
        if not c or c == "ADVANCE":
            continue
        by_code[c] = _payment_mode_export_label(c, label or "")

    ordered: List[Tuple[str, str]] = [
        ("ADVANCE", "Advance amount"),
    ]
    seen = {"ADVANCE"}

    for code in _EXPORT_PAYMENT_MODE_ORDER:
        if code in by_code and code not in seen:
            ordered.append((code, by_code[code]))
            seen.add(code)

    for code in sorted(by_code.keys()):
        if code not in seen:
            ordered.append((code, by_code[code]))
            seen.add(code)

    return ordered


def payment_amounts_by_mode_for_invoices(
    invoice_ids: List[int],
) -> Dict[int, Dict[str, Decimal]]:
    """
    Per invoice: map of payment-mode code -> amount paid via that mode.
    Split payments use PaymentCollection rows; single payments use Payment.payment_mode.
    """
    if not invoice_ids:
        return {}
    out: Dict[int, Dict[str, Decimal]] = {iid: {} for iid in invoice_ids}
    payments = (
        Payment.objects.filter(reference_type="SALE_INVOICE", reference_id__in=invoice_ids)
        .select_related("payment_mode")
        .prefetch_related("collections__payment_mode")
    )
    for payment in payments:
        ref_id = payment.reference_id
        if ref_id is None or ref_id not in out:
            continue
        bucket = out[ref_id]
        collections = list(payment.collections.all())
        if payment.is_split_payment or collections:
            for col in collections:
                code = (
                    (col.payment_mode.code if col.payment_mode_id else "") or ""
                ).strip().upper()
                if not code:
                    continue
                bucket[code] = bucket.get(code, Decimal("0")) + Decimal(str(col.amount or 0))
        elif payment.payment_mode_id:
            code = (payment.payment_mode.code or "").strip().upper()
            if code:
                bucket[code] = bucket.get(code, Decimal("0")) + Decimal(str(payment.amount or 0))
    return out


def _classify_sale_item_metal(product_name: str, purity: str) -> str:
    p = (purity or "").strip().lower()
    n = (product_name or "").strip().lower()
    if "silver" in n or "silver" in p or p.startswith("sl") or p in {"925", "999", "92.5", "800"}:
        return "SILVER"
    return "GOLD"


def _item_net_weight(item: SaleItem) -> Decimal:
    nw = item.net_weight if item.net_weight and item.net_weight > 0 else item.gross_weight
    return Decimal(str(nw or 0))


def _item_taxable_amount(item: SaleItem) -> Decimal:
    return _round_money(Decimal(str(item.final_amount or 0)) / (Decimal("1") + TOTAL_GST_RATE))


def _metal_weight_and_rate(items: List[SaleItem], metal: str) -> Tuple[Decimal, Decimal]:
    group = [
        it
        for it in items
        if _classify_sale_item_metal(it.product_name, it.purity) == metal
    ]
    if not group:
        return Decimal("0"), Decimal("0")
    net_weight = sum((_item_net_weight(it) for it in group), start=Decimal("0"))
    taxable = sum((_item_taxable_amount(it) for it in group), start=Decimal("0"))
    rate = _round_money(taxable / net_weight) if net_weight > 0 else Decimal("0")
    return net_weight.quantize(WEIGHT_PRECISION), rate.quantize(RATE_PRECISION)


def payment_mode_map_for_invoices(invoice_ids: List[int]) -> Dict[int, str]:
    if not invoice_ids:
        return {}
    payments = (
        Payment.objects.filter(reference_type="SALE_INVOICE", reference_id__in=invoice_ids)
        .select_related("payment_mode")
        .prefetch_related("collections__payment_mode")
    )
    out: Dict[int, str] = {}
    for payment in payments:
        ref_id = payment.reference_id
        if ref_id is None or ref_id in out:
            continue
        if payment.is_split_payment:
            labels = []
            for col in payment.collections.all():
                label = (col.payment_mode.label or col.payment_mode.code or "").strip()
                if label:
                    labels.append(label)
            out[ref_id] = ", ".join(dict.fromkeys(labels)) if labels else "Split"
        elif payment.payment_mode_id:
            out[ref_id] = (payment.payment_mode.label or payment.payment_mode.code or "").strip()
        else:
            out[ref_id] = ""
    return out


def iter_sales_invoice_export_rows(
    *,
    date_from: date | None = None,
    date_to: date | None = None,
):
    """Yield CSV rows (header first) for sales invoice export.

    Payment modes are separate amount columns (Advance amount, Cash, UPI, …)
    instead of one combined \"Mode of Payment\" text column.
    """
    mode_columns = _export_payment_mode_columns()
    yield (
        SALES_INVOICE_EXPORT_BASE_HEADER
        + [label for _, label in mode_columns]
        + SALES_INVOICE_EXPORT_TRAILING_HEADER
    )

    qs = (
        SaleInvoice.objects.filter(is_deleted=False)
        .prefetch_related("items")
        .order_by("invoice_date", "id")
    )
    if date_from:
        qs = qs.filter(invoice_date__gte=date_from)
    if date_to:
        qs = qs.filter(invoice_date__lte=date_to)

    invoices = list(qs)
    amounts_by_invoice = payment_amounts_by_mode_for_invoices([inv.id for inv in invoices])

    def _fmt_amt(value: Decimal) -> str:
        if not value:
            return "0"
        return f"{_round_money(value):f}"

    for inv in invoices:
        items = list(inv.items.all())
        gold_wt, gold_rate = _metal_weight_and_rate(items, "GOLD")
        silver_wt, silver_rate = _metal_weight_and_rate(items, "SILVER")

        gross = _round_money(Decimal(str(inv.total_amount or 0)))
        taxable = _round_money(gross / (Decimal("1") + TOTAL_GST_RATE))
        cgst = _round_money(taxable * CGST_RATE)
        sgst = _round_money(taxable * SGST_RATE)
        pending = _round_money(Decimal(str(inv.pending_amount or 0)))
        mode_amounts = amounts_by_invoice.get(inv.id, {})

        inv_dt = inv.invoice_date
        date_str = inv_dt.strftime("%d/%m/%Y") if inv_dt else ""

        row = [
            date_str,
            inv.invoice_number,
            inv.bill_to_name or "",
            inv.bill_to_phone or "",
            f"{gold_wt:f}".rstrip("0").rstrip(".") if gold_wt else "0",
            f"{gold_rate:f}".rstrip("0").rstrip(".") if gold_rate else "0",
            f"{silver_wt:f}".rstrip("0").rstrip(".") if silver_wt else "0",
            f"{silver_rate:f}".rstrip("0").rstrip(".") if silver_rate else "0",
            f"{taxable:f}",
            f"{cgst:f}",
            f"{sgst:f}",
            f"{gross:f}",
        ]
        for code, _label in mode_columns:
            row.append(_fmt_amt(mode_amounts.get(code, Decimal("0"))))
        row.append(f"{pending:f}")
        yield row
