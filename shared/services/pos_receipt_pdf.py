"""
POS tax invoice PDF (ReportLab). Section order and copy match the browser print receipt
(`PosInvoiceReceipt.tsx`) so download and print stay aligned.

Letterhead matches Taranya Jewels printed stationery:
  logo + TARANYA JEWELS / address / GSTIN left + Mob. right / blue rule / watermark.
"""
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path
from typing import List

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Table,
    TableStyle,
)

from shared.models import Payment, SaleInvoice, SaleItem

CGST_RATE = Decimal("0.015")
SGST_RATE = Decimal("0.015")
TOTAL_GST_RATE = CGST_RATE + SGST_RATE
MONEY_Q = Decimal("0.01")

# Taranya Jewels letterhead (matches printed stationery)
SHOP_HEADER = {
    "name": "TARANYA JEWELS",
    "address": "GANDHI CHOWK DURG (C.G.) 491001",
    "gstin": "22BORPJ6242R1ZA",
    "phone": "9111166788",
}
BRAND_BLUE = colors.HexColor("#5B8FB8")
BRAND_BLUE_HEX = "#5B8FB8"
LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "taranya-logo.png"

TERMS_LINES: List[str] = [
    "1. All goods must be returned within 3 working days from the date of purchase exchange.",
    "2. Goods with broken, damaged or missing tags will not be accepted for return or exchange. Includes Hallmarking Charges.",
    "3. Customers are advised to thoroughly inspect all goods for any damage or defects before purchase. Once purchased, goods will not be accepted for return if found broken or damaged. Goods altered out of our supervision/premises will not be entertained.",
    "4. Some jewellery may react with pollutants, sweat, or perfume, causing discoloration. This is a natural occurrence and not considered a defect.",
    "All disputes are subject to the jurisdiction of the competent courts in Durg District.",
    "By making a purchase, you agree to these terms and conditions.",
]

GRID = 0.5
SECTION_PAD = 6


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_money(d: Decimal) -> str:
    return f"{d.quantize(MONEY_Q):,.2f}"


def _fmt_qty(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.001')):,.3f}".rstrip("0").rstrip(".")


def _fmt_wt(d: Decimal) -> str:
    """Weights formatted like print `formatAmount` (2 dp for display on receipt)."""
    return f"{d.quantize(Decimal('0.01')):,.2f}"


def _draw_letterhead_watermark(canvas, doc) -> None:
    """Faint centered logo watermark (matches printed Taranya letterhead)."""
    if not LOGO_PATH.is_file():
        return
    canvas.saveState()
    try:
        page_w, page_h = A4
        size = 95 * mm
        x = (page_w - size) / 2.0
        y = (page_h - size) / 2.0 - 10 * mm
        if hasattr(canvas, "setFillAlpha"):
            canvas.setFillAlpha(0.08)
        canvas.drawImage(
            str(LOGO_PATH),
            x,
            y,
            width=size,
            height=size,
            preserveAspectRatio=True,
            mask="auto",
        )
    except Exception:
        pass
    finally:
        canvas.restoreState()


def _build_letterhead_table(tw: float, styles_map: dict) -> Table:
    """
    Printed stationery layout:
      [logo]  TARANYA JEWELS
              GANDHI CHOWK DURG (C.G.) 491001
      GSTIN : …                         Mob. : …
      ──────────────── blue rule ────────────────
      TAX INVOICE
    """
    shop_nm = styles_map["shop_nm"]
    shop_addr = styles_map["shop_addr"]
    shop_meta = styles_map["shop_meta"]
    tax_inv = styles_map["tax_inv"]

    logo_cell: object
    if LOGO_PATH.is_file():
        try:
            logo_cell = Image(str(LOGO_PATH), width=18 * mm, height=18 * mm, kind="proportional")
        except Exception:
            logo_cell = Paragraph("", shop_addr)
    else:
        logo_cell = Paragraph("", shop_addr)

    title_block = Table(
        [
            [Paragraph(f"<b>{_esc(SHOP_HEADER['name'])}</b>", shop_nm)],
            [Paragraph(_esc(SHOP_HEADER["address"]), shop_addr)],
        ],
        colWidths=[tw - 22 * mm],
    )
    title_block.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )

    brand_row = Table(
        [[logo_cell, title_block]],
        colWidths=[20 * mm, tw - 20 * mm],
    )
    brand_row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, 0), "LEFT"),
                ("ALIGN", (1, 0), (1, 0), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    gst_mob = Table(
        [[
            Paragraph(_esc(f"GSTIN : {SHOP_HEADER['gstin']}"), shop_meta),
            Paragraph(_esc(f"Mob. : {SHOP_HEADER['phone']}"), ParagraphStyle(
                "shop_meta_r", parent=shop_meta, alignment=TA_RIGHT
            )),
        ]],
        colWidths=[tw * 0.55, tw * 0.45],
    )
    gst_mob.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, 0), (-1, -1), 1.2, BRAND_BLUE),
            ]
        )
    )

    hdr = Table(
        [
            [brand_row],
            [gst_mob],
            [Paragraph("<b>TAX INVOICE</b>", tax_inv)],
        ],
        colWidths=[tw],
    )
    hdr.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ("TOPPADDING", (0, -1), (-1, -1), 6),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
                ("LINEBELOW", (0, -1), (-1, -1), GRID, colors.black),
            ]
        )
    )
    return hdr


def amount_to_words_inr(amount: Decimal) -> str:
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
    teens = [
        "Ten",
        "Eleven",
        "Twelve",
        "Thirteen",
        "Fourteen",
        "Fifteen",
        "Sixteen",
        "Seventeen",
        "Eighteen",
        "Nineteen",
    ]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def two_digits(n: int) -> str:
        if n < 10:
            return ones[n]
        if n < 20:
            return teens[n - 10]
        t = n // 10
        o = n % 10
        base = tens[t]
        return f"{base}{(' ' + ones[o]) if o else ''}"

    def three_digits(n: int) -> str:
        h = n // 100
        rem = n % 100
        if not h:
            return two_digits(rem)
        return f"{ones[h]} Hundred{(' ' + two_digits(rem)) if rem else ''}"

    num = int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if num < 0:
        num = 0
    if num == 0:
        return "Zero Only/-"

    crore = num // 10000000
    lakh = (num % 10000000) // 100000
    thousand = (num % 100000) // 1000
    rest = num % 1000

    parts: List[str] = []
    if crore:
        parts.append(f"{three_digits(crore)} Crore")
    if lakh:
        parts.append(f"{three_digits(lakh)} Lakh")
    if thousand:
        parts.append(f"{three_digits(thousand)} Thousand")
    if rest:
        parts.append(three_digits(rest))

    return f"{' '.join(parts)} Only/-"


def _section_line_below() -> TableStyle:
    return TableStyle(
        [
            ("LINEBELOW", (0, -1), (-1, -1), 1, colors.black),
            ("LEFTPADDING", (0, 0), (-1, -1), SECTION_PAD),
            ("RIGHTPADDING", (0, 0), (-1, -1), SECTION_PAD),
            ("TOPPADDING", (0, 0), (-1, -1), SECTION_PAD),
            ("BOTTOMPADDING", (0, 0), (-1, -1), SECTION_PAD),
        ]
    )


def build_pos_invoice_pdf_bytes(invoice: SaleInvoice) -> bytes:
    items: List[SaleItem] = list(invoice.items.all())
    if not items:
        raise ValueError("Invoice has no line items")

    gross = invoice.total_amount.quantize(MONEY_Q)
    taxable = (gross / (Decimal("1") + TOTAL_GST_RATE)).quantize(MONEY_Q)
    cgst = (taxable * CGST_RATE).quantize(MONEY_Q)
    sgst = (taxable * SGST_RATE).quantize(MONEY_Q)
    round_off = (gross - (taxable + cgst + sgst)).quantize(MONEY_Q)

    total_gw = sum((it.gross_weight for it in items), start=Decimal("0")).quantize(Decimal("0.01"))
    total_nw = sum(
        ((it.net_weight if it.net_weight and it.net_weight > 0 else it.gross_weight) for it in items),
        start=Decimal("0"),
    ).quantize(Decimal("0.01"))
    total_hall = sum((it.making_charge for it in items), start=Decimal("0")).quantize(MONEY_Q)
    line_final_ex_gst = taxable

    paid = invoice.paid_amount.quantize(MONEY_Q)
    pending = invoice.pending_amount.quantize(MONEY_Q)
    discount = Decimal("0")

    dt = invoice.invoice_date
    if dt:
        date_str = dt.strftime("%d %b %Y").upper()
    else:
        date_str = timezone.localtime(invoice.system_created_at).strftime("%d %b %Y").upper()

    buf = BytesIO()
    margin = 8 * mm
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title=f"Invoice {invoice.invoice_number}",
    )
    tw = doc.width

    styles = getSampleStyleSheet()
    base = styles["Normal"]
    base.fontName = "Helvetica"

    shop_nm = ParagraphStyle(
        "sn",
        parent=base,
        fontName="Times-Bold",
        fontSize=16,
        leading=19,
        alignment=TA_CENTER,
        textColor=BRAND_BLUE,
    )
    shop_addr = ParagraphStyle(
        "sa",
        parent=base,
        fontName="Helvetica",
        fontSize=9,
        leading=11,
        alignment=TA_CENTER,
        textColor=BRAND_BLUE,
    )
    shop_meta = ParagraphStyle(
        "sm",
        parent=base,
        fontName="Helvetica",
        fontSize=9,
        leading=11,
        alignment=TA_LEFT,
        textColor=BRAND_BLUE,
    )
    tax_inv = ParagraphStyle(
        "ti",
        parent=base,
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=14,
        textColor=BRAND_BLUE,
        alignment=TA_CENTER,
    )
    body = ParagraphStyle("b", parent=base, fontSize=9, leading=11.5, alignment=TA_LEFT)
    body_r = ParagraphStyle("br", parent=body, alignment=TA_RIGHT)
    body_b = ParagraphStyle("bb", parent=body, fontName="Helvetica-Bold")
    sales_hd = ParagraphStyle("sh", parent=body, fontName="Helvetica-Bold", fontSize=9.5, leading=12, alignment=TA_CENTER)
    sales_sub = ParagraphStyle("ssu", parent=body, fontSize=9, leading=11, alignment=TA_CENTER)
    th = ParagraphStyle("th", parent=base, fontName="Helvetica-Bold", fontSize=7.6, leading=9, alignment=TA_CENTER)
    tc = ParagraphStyle("tc", parent=base, fontSize=8.5, leading=10.5, alignment=TA_LEFT)
    tc_c = ParagraphStyle("tcc", parent=tc, alignment=TA_CENTER)
    tc_r = ParagraphStyle("tcr", parent=tc, alignment=TA_RIGHT)
    gold_ln = ParagraphStyle("gl", parent=body, fontName="Helvetica-Bold", fontSize=9, leading=11)
    sum_lbl = ParagraphStyle("suml", parent=body, fontSize=9, leading=11.5)
    sum_num = ParagraphStyle("sumn", parent=body, fontSize=9, leading=11.5, alignment=TA_RIGHT, fontName="Helvetica")
    sum_b = ParagraphStyle("sumb", parent=sum_lbl, fontName="Helvetica-Bold")
    sum_bn = ParagraphStyle("sumbn", parent=sum_num, fontName="Helvetica-Bold")
    bal_l = ParagraphStyle("bl", parent=sum_b, textColor=colors.HexColor("#dc2626"))
    bal_n = ParagraphStyle("bn", parent=sum_bn, textColor=colors.HexColor("#dc2626"))
    terms_t = ParagraphStyle("tm", parent=body, fontName="Helvetica-Bold", fontSize=8.5, leading=11)
    terms_l = ParagraphStyle("tl", parent=body, fontSize=8, leading=10)
    sign = ParagraphStyle("sg", parent=body, fontSize=9.5, leading=12)
    thanks = ParagraphStyle("ty", parent=body, fontSize=9.5, leading=12, alignment=TA_CENTER)

    sections: List = []

    # ----- 1) Taranya Jewels letterhead -----
    h1 = _build_letterhead_table(
        tw,
        {
            "shop_nm": shop_nm,
            "shop_addr": shop_addr,
            "shop_meta": shop_meta,
            "tax_inv": tax_inv,
        },
    )
    sections.append([h1])

    # ----- 2) Customer | Invoice (print: grid 2 cols, border-b) -----
    cust = (
        f"<b>NAME</b> : {_esc(invoice.bill_to_name or '-') }<br/>"
        f"<b>ADDRESS</b> : {_esc(invoice.bill_to_address or '-') }<br/>"
        f"<b>PHONE</b> : {_esc(invoice.bill_to_phone or '-') }"
    )
    inv = (
        f"<b>INVOICE NO:</b> {_esc(invoice.invoice_number)}<br/>"
        f"<b>DATE:</b> {_esc(date_str)}"
    )
    h2 = Table([[Paragraph(cust, body), Paragraph(inv, body_r)]], colWidths=[tw * 0.55, tw * 0.45])
    h2.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, -1), (-1, -1), GRID, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    sections.append([h2])

    # ----- 3) SALES INVOICE (single-row banner; "Gold SELL" subtitle removed) -----
    h3 = Table(
        [[Paragraph("<b>SALES INVOICE</b>", sales_hd)]],
        colWidths=[tw],
    )
    h3.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, -1), (-1, -1), GRID, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    sections.append([h3])

    # ----- 4) Line items (same headers as print; HSN / Add. Details not stored on SaleItem → "-") -----
    hdr = [
        "SR NO",
        "PRODUCT NAME",
        "QTY",
        "HSN",
        "Add. Details",
        "GS WT",
        "LESS WT",
        "NT WT",
        "PURITY",
        "OTHER CHARGES",
        "FINAL AMT (Before Tax)",
    ]
    data: List[List] = [[Paragraph(f"<b>{_esc(h)}</b>", th) for h in hdr]]

    def _classify(product_name: str, purity: str) -> str:
        p = (purity or "").strip().lower()
        n = (product_name or "").strip().lower()
        if "silver" in n or "silver" in p or p.startswith("sl") or p in {"925", "999", "92.5", "800"}:
            return "SILVER"
        if "diamond" in n or "diamond" in p:
            return "DIAMOND"
        if "platinum" in n or "platinum" in p or p.startswith("pt"):
            return "PLATINUM"
        return "GOLD"

    # First: list ALL items in their original order (no per-metal grouping).
    # Then: a single divider row, followed by one subtotal row per metal.
    metal_buckets: dict = {}  # metal -> list of items (preserves first-seen order via list of metals below)
    metal_seq: List[str] = []
    valid_items = []
    row_num = 0
    cur_row = 1  # row 0 is header
    for it in items:
        pname = (it.product_name or "").strip()
        pur = (it.purity or "").strip()
        fa = it.final_amount
        if not pname and fa <= 0 and not pur:
            continue
        valid_items.append(it)
        row_num += 1
        gw = it.gross_weight
        nw = it.net_weight if it.net_weight and it.net_weight > 0 else gw
        less = (gw - nw) if (gw is not None and nw is not None and gw > nw) else Decimal("0")
        qty_disp = _fmt_qty(it.qty) if it.qty and it.qty != 0 else "-"
        data.append(
            [
                Paragraph(str(row_num), tc_c),
                Paragraph(_esc(pname or "-"), tc),
                Paragraph(qty_disp, tc_c),
                Paragraph(_esc((it.hsn or "").strip() or "-"), tc_c),
                Paragraph("-", tc_c),
                Paragraph(_fmt_wt(gw), tc_c),
                Paragraph(_fmt_wt(less) if less > 0 else "-", tc_c),
                Paragraph(_fmt_wt(nw), tc_c),
                Paragraph(_esc(pur or "-"), tc_c),
                Paragraph(_fmt_money(it.making_charge), tc_c),
                Paragraph(_fmt_money((it.final_amount / (Decimal("1") + TOTAL_GST_RATE)).quantize(MONEY_Q)), tc_r),
            ]
        )
        cur_row += 1
        m = _classify(pname, pur)
        if m not in metal_buckets:
            metal_buckets[m] = []
            metal_seq.append(m)
        metal_buckets[m].append(it)

    span_rows: List[int] = []  # rows that need SPAN(0..3) for the metal label
    first_subtotal_row: int | None = None  # first metal-subtotal row (gets a heavier top border)
    if valid_items and metal_seq:
        first_subtotal_row = cur_row
        for metal in metal_seq:
            group_items = metal_buckets[metal]
            sub_gw = sum((g.gross_weight for g in group_items), start=Decimal("0")).quantize(Decimal("0.01"))
            sub_nw = sum(
                ((g.net_weight if g.net_weight and g.net_weight > 0 else g.gross_weight) for g in group_items),
                start=Decimal("0"),
            ).quantize(Decimal("0.01"))
            sub_less = (sub_gw - sub_nw) if sub_gw > sub_nw else Decimal("0")
            sub_hall = sum((g.making_charge for g in group_items), start=Decimal("0")).quantize(MONEY_Q)
            sub_final = sum(
                ((g.final_amount / (Decimal("1") + TOTAL_GST_RATE)) for g in group_items),
                start=Decimal("0"),
            ).quantize(MONEY_Q)
            data.append(
                [
                    Paragraph(f"<b>TOTAL {metal} :</b>", gold_ln),
                    Paragraph("", tc),
                    Paragraph("", tc_c),
                    Paragraph("", tc_c),
                    Paragraph("-", tc_c),
                    Paragraph(_fmt_wt(sub_gw), tc_c),
                    Paragraph(_fmt_wt(sub_less) if sub_less > 0 else "-", tc_c),
                    Paragraph(_fmt_wt(sub_nw), tc_c),
                    Paragraph("", tc_c),
                    Paragraph(_fmt_money(sub_hall), tc_c),
                    Paragraph(_fmt_money(sub_final), tc_r),
                ]
            )
            span_rows.append(cur_row)
            cur_row += 1

    col_w = [0.045, 0.22, 0.05, 0.06, 0.10, 0.065, 0.065, 0.065, 0.10, 0.09, 0.14]
    w_abs = [tw * c for c in col_w]
    tbl_style_cmds = [
        ("INNERGRID", (0, 0), (-1, -1), GRID, colors.black),
        ("BOX", (0, 0), (-1, -1), 0, colors.white),
        ("LINEBELOW", (0, 0), (-1, 0), GRID, colors.black),
        ("LINEABOVE", (0, 0), (-1, 0), 0, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    # Subtotal rows: span first 4 cols for the metal label, drop inner vertical grid for that row.
    for r in span_rows:
        tbl_style_cmds.append(("SPAN", (0, r), (3, r)))
        tbl_style_cmds.append(("ALIGN", (0, r), (0, r), "LEFT"))
        tbl_style_cmds.append(("LINEBELOW", (0, r), (-1, r), GRID, colors.black))
    # Divider line above the first metal subtotal row (separates items from subtotals).
    if first_subtotal_row is not None:
        tbl_style_cmds.append(("LINEABOVE", (0, first_subtotal_row), (-1, first_subtotal_row), GRID, colors.black))

    tbl = Table(data, colWidths=w_abs, repeatRows=1)
    tbl.setStyle(TableStyle(tbl_style_cmds))
    sections.append([tbl])
    _ = line_final_ex_gst

    # ----- 6) Cheque + words | totals (print: grid 2 cols, border-t) -----
    # Print uses formatAmount(discount) inside parentheses with "%" (same as value column when discount is 0)
    disc_pct_label = _fmt_money(discount)
    ro_sign = "+" if round_off >= 0 else "-"

    sum_rows = [
        [
            Paragraph(f"DISC WITHOUT TAX ({disc_pct_label}%)", sum_lbl),
            Paragraph(_fmt_money(discount), sum_num),
        ],
        [Paragraph("TAXABLE AMT", sum_lbl), Paragraph(_fmt_money(taxable), sum_num)],
        [Paragraph("CGST (1.5%)", sum_lbl), Paragraph(_fmt_money(cgst), sum_num)],
        [Paragraph("SGST (1.5%)", sum_lbl), Paragraph(_fmt_money(sgst), sum_num)],
        [
            Paragraph("ROUND OFF", sum_lbl),
            Paragraph(f"{ro_sign} {_fmt_money(abs(round_off))}", sum_num),
        ],
        [Paragraph("<b>TOTAL AMOUNT</b>", sum_b), Paragraph(_fmt_money(gross), sum_bn)],
        [Paragraph("NET RECEIVABLE AMT", sum_lbl), Paragraph(_fmt_money(gross), sum_num)],
        [Paragraph("<b><u>FINAL REC AMT</u></b>", sum_b), Paragraph(f"<b><u>{_fmt_money(gross)}</u></b>", sum_bn)],
        [Paragraph("<b>AMT BALANCE</b>", bal_l), Paragraph(f"<b>{_fmt_money(pending)} DR</b>", bal_n)],
    ]
    # Inner usable width inside each half-column after the bottom table's cell padding (4 + 4).
    half_w = tw * 0.50
    inner_avail = half_w - 8

    # Summary block (totals/taxes) — now shown on the LEFT side of the bottom row.
    summary_inner = Table(
        sum_rows,
        colWidths=[inner_avail * 0.62, inner_avail * 0.38],
    )
    summary_inner.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("LINEABOVE", (0, 7), (-1, 7), GRID, colors.black),
                ("TOPPADDING", (0, 7), (-1, 7), 5),
            ]
        )
    )

    # Mode of Payment (right side) — pulled from Payment + PaymentCollection.
    mop_rows: List[List] = [
        [
            Paragraph("<b>MODE</b>", th),
            Paragraph("<b>AMOUNT</b>", ParagraphStyle("th_r", parent=th, alignment=TA_RIGHT)),
        ]
    ]
    payments_qs = (
        Payment.objects
        .filter(reference_type="SALE_INVOICE", reference_id=invoice.id)
        .select_related("payment_mode")
        .prefetch_related("collections__payment_mode")
    )
    mop_total = Decimal("0")
    for pay in payments_qs:
        if pay.is_split_payment:
            for col in pay.collections.all():
                label = (col.payment_mode.label or col.payment_mode.code or "").strip() or "-"
                amt = Decimal(col.amount or 0)
                mop_total += amt
                mop_rows.append([Paragraph(_esc(label), tc), Paragraph(_fmt_money(amt), tc_r)])
        else:
            label = (
                (pay.payment_mode.label or pay.payment_mode.code or "").strip()
                if pay.payment_mode_id else ""
            ) or "-"
            amt = Decimal(pay.amount or 0)
            mop_total += amt
            mop_rows.append([Paragraph(_esc(label), tc), Paragraph(_fmt_money(amt), tc_r)])
    if len(mop_rows) == 1:
        mop_rows.append([Paragraph("-", tc_c), Paragraph(_fmt_money(Decimal("0")), tc_r)])
    mop_rows.append([
        Paragraph("<b>TOTAL PAID</b>", sum_b),
        Paragraph(f"<b>{_fmt_money(mop_total)}</b>", sum_bn),
    ])

    mop_inner = Table(mop_rows, colWidths=[inner_avail * 0.55, inner_avail * 0.45])
    mop_total_row_idx = len(mop_rows) - 1
    mop_inner.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("LINEBELOW", (0, 0), (-1, 0), GRID, colors.black),
                ("LINEABOVE", (0, mop_total_row_idx), (-1, mop_total_row_idx), GRID, colors.black),
                ("TOPPADDING", (0, mop_total_row_idx), (-1, mop_total_row_idx), 5),
            ]
        )
    )

    bottom = Table(
        [[summary_inner, mop_inner]],
        colWidths=[half_w, half_w],
    )
    bottom.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                # Inner vertical divider between the two columns
                ("LINEAFTER", (0, 0), (0, 0), GRID, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    sections.append([bottom])

    # Title row above the two columns: "SUMMARY" | "MODE OF PAYMENT"
    titles = Table(
        [[
            Paragraph("<b>SUMMARY</b>", sales_hd),
            Paragraph("<b>MODE OF PAYMENT</b>", sales_hd),
        ]],
        colWidths=[half_w, half_w],
    )
    titles.setStyle(
        TableStyle(
            [
                ("LINEAFTER", (0, 0), (0, 0), GRID, colors.black),
                ("LINEBELOW", (0, 0), (-1, 0), GRID, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    # Insert the title row just before the bottom block
    sections.insert(-1, [titles])

    # ----- 6b) AMOUNT IN WORDS (full width) -----
    words_tbl = Table(
        [[Paragraph(f"<b>AMOUNT IN WORDS :</b> {_esc(amount_to_words_inr(gross))}", body)]],
        colWidths=[tw],
    )
    words_tbl.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, -1), GRID, colors.black),
                ("LINEBELOW", (0, 0), (-1, -1), GRID, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    sections.append([words_tbl])

    # ----- 7) Terms (print: border-t, 9px) -----
    terms_block: List[List] = [[Paragraph("<b>Terms and Conditions :</b>", terms_t)]]
    for ln in TERMS_LINES:
        terms_block.append([Paragraph(_esc(ln), terms_l)])
    tsec = Table(terms_block, colWidths=[tw])
    tsec.setStyle(
        TableStyle(
            [
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (0, 0), 6),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
            ]
        )
    )
    sections.append([tsec])

    # ----- 8) Signatories + thank you -----
    sig = Table(
        [
            [
                Paragraph("Customer Signatory", sign),
                Paragraph("Authorized Signatory", ParagraphStyle("as", parent=sign, alignment=TA_RIGHT)),
            ]
        ],
        colWidths=[tw * 0.5, tw * 0.5],
    )
    sig.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, -1), GRID, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 14),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    sections.append([sig])

    thanks_tbl = Table([[Paragraph("Thank You For Your Business!", thanks)]], colWidths=[tw])
    thanks_tbl.setStyle(
        TableStyle(
            [
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    sections.append([thanks_tbl])

    # Outer wrapper with continuous border around the whole receipt
    outer = Table(sections, colWidths=[tw])
    outer.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 1, colors.black),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    doc.build(
        [outer],
        onFirstPage=_draw_letterhead_watermark,
        onLaterPages=_draw_letterhead_watermark,
    )
    pdf = buf.getvalue()
    buf.close()
    return pdf
