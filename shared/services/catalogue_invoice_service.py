"""
Create POS-style SaleInvoice + Payment records from catalogue quotes for receipts and PDFs.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from shared.models import (
    CatalogueQuote,
    CatalogueQuotePayment,
    Customer,
    LookupValue,
    Payment,
    PaymentCollection,
    SaleInvoice,
    SaleItem,
)
from shared.services.pos_service import (
    _derive_invoice_status,
    _generate_invoice_number,
    _resolve_payment_mode,
    _resolve_payment_status,
    _round_money,
    _to_decimal,
    _validate_pos_payment_mode,
)
from shared.services.sold_stock_service import (
    mark_invoice_pieces_sold,
    resolve_tag_and_item_from_quote_line,
)

PAYMENT_REF_CATALOGUE = 'CATALOGUE_QUOTE'
DEFAULT_HSN = '7113'


def _delivery_address_text(quote: CatalogueQuote) -> str:
    snap = quote.delivery_address_snapshot or {}
    parts = [
        snap.get('addressLine1') or snap.get('address_line1') or '',
        snap.get('addressLine2') or snap.get('address_line2') or '',
        snap.get('city') or '',
        snap.get('state') or '',
        snap.get('pincode') or '',
    ]
    return ', '.join(p for p in parts if p).strip() or '—'


def _purity_from_line(line) -> str:
    b = line.breakdown or {}
    if isinstance(b, dict):
        p = b.get('purity') or b.get('goldPurity') or b.get('gold_purity')
        if p:
            return str(p)
    label = line.variant_label or ''
    if 'k' in label.lower() or 'gold' in label.lower():
        return label.split('·')[0].strip()[:50] if label else 'Gold'
    return 'Gold'


def _line_to_sale_item(invoice: SaleInvoice, line, user) -> SaleItem:
    b = line.breakdown if isinstance(line.breakdown, dict) else {}
    qty = int(line.quantity or 1)
    net_per = _to_decimal(b.get('netGoldWeightGm') or b.get('net_gold_weight_gm') or 0, 'netWt')
    net_total = _round_money(net_per * qty)
    gross = net_total
    making = _round_money(
        _to_decimal(b.get('makingCharges') or 0, 'making')
        + _to_decimal(b.get('operationCharges') or 0, 'operation')
    )
    making_total = _round_money(making * qty)
    final_amount = _round_money(_to_decimal(line.line_total, 'line_total'))
    tag, product_item = resolve_tag_and_item_from_quote_line(line)

    return SaleItem(
        invoice=invoice,
        product_item=product_item,
        tag=tag,
        product_name=line.product_name or 'Item',
        hsn=DEFAULT_HSN,
        qty=Decimal(str(qty)),
        gross_weight=gross,
        net_weight=net_total,
        purity=_purity_from_line(line)[:50],
        making_charge=making_total,
        final_amount=final_amount,
        is_manual_entry=tag is None and product_item is None,
        created_by=user,
        updated_by=user,
    )


@transaction.atomic
def ensure_catalogue_sale_invoice(quote: CatalogueQuote, created_by=None) -> SaleInvoice:
    """
    Idempotent: returns existing linked invoice or creates SaleInvoice + items.
    """
    quote = (
        CatalogueQuote.objects.select_related('customer', 'sale_invoice')
        .prefetch_related('lines', 'payments')
        .get(pk=quote.pk)
    )
    if quote.sale_invoice_id and quote.sale_invoice and not quote.sale_invoice.is_deleted:
        return quote.sale_invoice

    lines = list(quote.lines.order_by('line_no', 'id'))
    if not lines:
        raise ValueError('Quote has no lines for invoice generation.')

    grand_total = _round_money(_to_decimal(quote.grand_total, 'grand_total'))
    paid_amount = _round_money(_to_decimal(quote.paid_amount, 'paid_amount'))
    status, pending_amount = _derive_invoice_status(grand_total, paid_amount)

    customer = quote.customer
    invoice = SaleInvoice.objects.create(
        invoice_number=_generate_invoice_number(for_date=timezone.localdate()),
        invoice_date=timezone.localdate(),
        customer=customer,
        bill_to_name=quote.customer_name_snapshot or (customer.full_name if customer else ''),
        bill_to_phone=quote.contact_mobile or (customer.mobile if customer else ''),
        bill_to_address=_delivery_address_text(quote),
        total_amount=grand_total,
        paid_amount=paid_amount,
        pending_amount=pending_amount,
        status=status,
        created_by=created_by,
        updated_by=created_by,
    )

    SaleItem.objects.bulk_create([_line_to_sale_item(invoice, ln, created_by) for ln in lines])

    quote.sale_invoice = invoice
    quote.save(update_fields=['sale_invoice', 'system_updated_at'])
    mark_invoice_pieces_sold(invoice, created_by)

    return invoice


def _catalogue_payments_for_quote(quote: CatalogueQuote) -> list[CatalogueQuotePayment]:
    return list(quote.payments.order_by('id'))


@transaction.atomic
def ensure_catalogue_quote_payments(quote: CatalogueQuote, invoice: SaleInvoice, created_by=None) -> list[Payment]:
    """
    Create Payment + PaymentCollection rows (receipt_no = invoice_number) for ledger linkage.
    Idempotent per quote payment line.
    """
    existing = list(
        Payment.objects.filter(
            reference_type=PAYMENT_REF_CATALOGUE,
            reference_id=quote.id,
        ).order_by('id')
    )
    if existing:
        return existing

    rows = _catalogue_payments_for_quote(quote)
    paid_total = _round_money(_to_decimal(quote.paid_amount, 'paid_amount'))
    if not rows and paid_total <= 0:
        return []

    created: list[Payment] = []
    payment_status = _resolve_payment_status('SUCCESS' if paid_total > 0 else 'PENDING')

    if rows:
        for qp in rows:
            amt = _round_money(_to_decimal(qp.amount, 'amount'))
            if amt <= 0:
                continue
            mode_code = (qp.mode_code or 'CASH').strip().upper()
            try:
                mode_code = _validate_pos_payment_mode(mode_code, 0)
            except ValueError:
                mode_code = 'CASH'
            payment = Payment.objects.create(
                instalment=None,
                payment_mode=_resolve_payment_mode(mode_code),
                receipt_no=invoice.invoice_number,
                payment_source='POS',
                is_split_payment=False,
                transaction_id=f'CAT-{quote.id}-{qp.id}-{uuid4().hex[:12]}',
                payment_status=payment_status,
                amount=amt,
                paid_at=timezone.now(),
                is_finalized=True,
                reference_type=PAYMENT_REF_CATALOGUE,
                reference_id=quote.id,
                created_by=created_by,
                updated_by=created_by,
            )
            PaymentCollection.objects.create(
                payment=payment,
                payment_mode=_resolve_payment_mode(mode_code),
                amount=amt,
                created_by=created_by,
                updated_by=created_by,
            )
            created.append(payment)
        return created

    payment = Payment.objects.create(
        instalment=None,
        payment_mode=_resolve_payment_mode('CASH'),
        receipt_no=invoice.invoice_number,
        payment_source='POS',
        is_split_payment=False,
        transaction_id=f'CAT-{quote.id}-{uuid4().hex[:12]}',
        payment_status=payment_status,
        amount=paid_total,
        paid_at=timezone.now(),
        is_finalized=True,
        reference_type=PAYMENT_REF_CATALOGUE,
        reference_id=quote.id,
        created_by=created_by,
        updated_by=created_by,
    )
    PaymentCollection.objects.create(
        payment=payment,
        payment_mode=_resolve_payment_mode('CASH'),
        amount=paid_total,
        created_by=created_by,
        updated_by=created_by,
    )
    created.append(payment)
    return created


def get_invoice_receipt_number(quote: CatalogueQuote) -> str:
    """Tax invoice number for ledger + receipts (POS format)."""
    if quote.sale_invoice_id and quote.sale_invoice:
        return quote.sale_invoice.invoice_number
    return quote.quote_number
