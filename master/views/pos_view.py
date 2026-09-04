from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
import csv

from master.permissions.permission_checker import admin_auth
from shared.models import Payment, PaymentCollection, SaleInvoice
from shared.services.pos_receipt_pdf import build_pos_invoice_pdf_bytes
from shared.services.pos_service import (
    create_pos_invoice,
    iter_sales_invoice_export_rows,
    payment_mode_map_for_invoices,
    peek_next_invoice_number,
    soft_delete_pos_invoice,
    update_pos_invoice,
    _parse_invoice_date,
)


@api_view(["GET"])
@admin_auth("CRM_STORES_POS_VIEW", "CRM_STORES_POS_CREATE")
def next_invoice_number(request):
    """
    GET /master/pos/next-invoice-number/?invoice_date=YYYY-MM-DD
    Preview the next sequential invoice number (does not consume the counter).
    """
    try:
        bill_date = _parse_invoice_date(request.GET.get("invoice_date"))
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"invoice_number": peek_next_invoice_number(for_date=bill_date)})


@api_view(["POST"])
@admin_auth("CRM_STORES_POS_CREATE")
def create_invoice(request):
    """
    Temporary POS invoice creation endpoint.
    Creates SaleInvoice + SaleItems + Payment + PaymentCollections.
    """
    try:
        invoice = create_pos_invoice(request.data, created_by=request.user)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response(
        {
            "message": "Invoice created successfully",
            "data": {
                "id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "status": invoice.status,
                "total_amount": str(invoice.total_amount),
                "paid_amount": str(invoice.paid_amount),
                "pending_amount": str(invoice.pending_amount),
                "invoice_date": invoice.invoice_date.isoformat(),
                "created_at": invoice.system_created_at,
            },
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@admin_auth(
    "CRM_STORES_POS_VIEW",
    "CRM_STORES_POS_CREATE",
    "CRM_STORES_POS_UPDATE",
    "CRM_ACCOUNTS_INVOICE_VIEW",
)
def invoice_pdf(request, pk: int):
    """Download tax invoice as PDF (ReportLab); matches server-side totals and line items."""
    invoice = get_object_or_404(
        SaleInvoice.objects.prefetch_related("items").filter(is_deleted=False), pk=pk
    )
    try:
        pdf_bytes = build_pos_invoice_pdf_bytes(invoice)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in invoice.invoice_number)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="Receipt-{safe}.pdf"'
    return response


def _extract_city(address: str) -> str:
    """
    Best-effort city extraction from free-form bill_to_address.
    Expected format commonly: Street, City, State, PIN.
    """
    parts = [p.strip() for p in (address or "").split(",") if p and p.strip()]
    if not parts:
        return ""
    if len(parts) >= 3:
        # Street, City, State, PIN -> City
        return parts[-3]
    if len(parts) == 2:
        return parts[1]
    return parts[0]


@api_view(["GET"])
@admin_auth("CRM_ACCOUNTS_INVOICE_VIEW")
def list_invoices(request):
    """
    Accounts listing view for Sales Invoices.
    Returns latest invoices with payment mode summary.
    """
    invoices = list(
        SaleInvoice.objects.filter(is_deleted=False).order_by("-system_created_at")[:500]
    )
    invoice_ids = [inv.id for inv in invoices]

    payment_mode_by_invoice_id = payment_mode_map_for_invoices(invoice_ids)

    data = [
        {
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "date": inv.invoice_date.isoformat() if inv.invoice_date else inv.system_created_at,
            "customer_name": inv.bill_to_name,
            "mobile": inv.bill_to_phone,
            "city": _extract_city(inv.bill_to_address),
            "total_amount": str(inv.total_amount),  # GST-inclusive snapshot
            "payment_mode": payment_mode_by_invoice_id.get(inv.id, ""),
        }
        for inv in invoices
    ]
    return Response({"results": data}, status=status.HTTP_200_OK)


@api_view(["GET"])
@admin_auth("CRM_ACCOUNTS_INVOICE_VIEW")
def export_invoices_csv(request):
    """
    Export sales invoices to CSV for a date range (invoice_date).
    GET /master/pos/invoices/export/?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
    """
    try:
        date_from = _parse_invoice_date(request.GET.get("date_from")) if request.GET.get("date_from") else None
        date_to = _parse_invoice_date(request.GET.get("date_to")) if request.GET.get("date_to") else None
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    if date_from and date_to and date_from > date_to:
        return Response({"error": "date_from cannot be after date_to"}, status=status.HTTP_400_BAD_REQUEST)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    filename = "sales_invoices"
    if date_from:
        filename += f"_{date_from.strftime('%Y%m%d')}"
    if date_to:
        filename += f"_to_{date_to.strftime('%Y%m%d')}"
    filename += ".csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")  # UTF-8 BOM for Excel

    writer = csv.writer(response)
    for row in iter_sales_invoice_export_rows(date_from=date_from, date_to=date_to):
        writer.writerow(row)
    return response


def _serialize_invoice_detail(invoice: SaleInvoice):
    items = [
        {
            "id": it.id,
            "product_name": it.product_name,
            "hsn": getattr(it, "hsn", "") or "",
            "qty": str(it.qty),
            "gross_weight": str(it.gross_weight),
            "net_weight": str(it.net_weight),
            "purity": it.purity,
            "making_charge": str(it.making_charge),
            "final_amount": str(it.final_amount),
            "is_manual_entry": bool(it.is_manual_entry),
        }
        for it in invoice.items.all()
    ]
    payments = []
    for p in Payment.objects.filter(reference_type="SALE_INVOICE", reference_id=invoice.id).prefetch_related(
        "collections__payment_mode"
    ):
        if p.is_split_payment:
            for col in p.collections.all():
                payments.append({"mode": col.payment_mode.code, "amount": str(col.amount)})
        else:
            mode_code = p.payment_mode.code if p.payment_mode_id else ""
            payments.append({"mode": mode_code, "amount": str(p.amount)})
    return {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "bill_to_name": invoice.bill_to_name,
        "bill_to_phone": invoice.bill_to_phone,
        "bill_to_address": invoice.bill_to_address,
        "total_amount": str(invoice.total_amount),
        "paid_amount": str(invoice.paid_amount),
        "pending_amount": str(invoice.pending_amount),
        "status": invoice.status,
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else "",
        "items": items,
        "payments": payments,
    }


@api_view(["GET"])
@admin_auth(
    "CRM_STORES_POS_VIEW",
    "CRM_STORES_POS_UPDATE",
    "CRM_ACCOUNTS_INVOICE_VIEW",
    "CRM_ACCOUNTS_INVOICE_UPDATE",
)
def get_invoice(request, pk: int):
    invoice = get_object_or_404(
        SaleInvoice.objects.prefetch_related("items").filter(is_deleted=False), pk=pk
    )
    return Response({"data": _serialize_invoice_detail(invoice)}, status=status.HTTP_200_OK)


@api_view(["PUT"])
@admin_auth("CRM_ACCOUNTS_INVOICE_UPDATE")
def update_invoice(request, pk: int):
    try:
        invoice = update_pos_invoice(pk, request.data, updated_by=request.user)
    except SaleInvoice.DoesNotExist:
        return Response({"error": "Invoice not found"}, status=status.HTTP_404_NOT_FOUND)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return Response(
        {"message": "Invoice updated successfully", "data": _serialize_invoice_detail(invoice)},
        status=status.HTTP_200_OK,
    )


@api_view(["DELETE"])
@admin_auth("CRM_ACCOUNTS_INVOICE_UPDATE")
def delete_invoice(request, pk: int):
    try:
        soft_delete_pos_invoice(pk, deleted_by=request.user)
    except SaleInvoice.DoesNotExist:
        return Response({"error": "Invoice not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return Response({"message": "Invoice deleted"}, status=status.HTTP_200_OK)
