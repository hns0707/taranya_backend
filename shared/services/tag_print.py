"""
Jewellery tag print — single module: TSC TE244 profile (100×13 mm) + HTML/CSS layout.

Tune label size and QR code in TSC_TE244 below. Render via GET /master/tag-templates/render/<id>/.
"""
from __future__ import annotations

import base64
import io
import os
import re
from pathlib import Path
from typing import Dict, TypedDict

import qrcode
from PIL import Image
from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_M
from qrcode.image.svg import SvgPathImage

from shared.models import ProductTag
from shared.services.tag_hallmark import hallmark_mark_html

def tag_label_display(tag: ProductTag) -> str:
    """Human-readable tag code on the label face."""
    value = (tag.tag_value or "").strip()
    return value if value else str(tag.pk)


def tag_qr_payload(tag: ProductTag) -> str:
    """String encoded in the printed QR (tag_value when set, else numeric id)."""
    value = (tag.tag_value or "").strip()
    return value if value else str(tag.pk)


def tag_scan_lookup_q(scan: str):
    """Django Q for admin/catalogue search by scanned or typed code."""
    from django.db.models import Q

    s = (scan or "").strip()
    if not s:
        return Q()
    clauses = Q(tag_value__icontains=s) | Q(display_name__icontains=s) | Q(sku_code__icontains=s)
    if s.isdigit():
        clauses |= Q(pk=int(s))
    return clauses


_QR_LOGO_DEFAULT = Path(__file__).resolve().parent.parent / "assets" / "tag_qr_logo.png"


def _qr_logo_path() -> Path | None:
    custom = (os.getenv("TAG_QR_LOGO_PATH") or "").strip()
    if custom:
        p = Path(custom)
        if p.is_file():
            return p
    return _QR_LOGO_DEFAULT if _QR_LOGO_DEFAULT.is_file() else None


def _embed_logo_on_qr(qr_img: Image.Image, logo_path: Path) -> Image.Image:
    """Center brand logo on QR with white plate so modules stay scannable."""
    logo = Image.open(logo_path).convert("RGBA")
    qr_w, qr_h = qr_img.size
    box = max(10, int(min(qr_w, qr_h) * 0.24))
    logo.thumbnail((box, box), Image.Resampling.LANCZOS)
    lw, lh = logo.size
    pad = max(2, int(box * 0.1))
    plate = Image.new("RGBA", (lw + pad * 2, lh + pad * 2), (255, 255, 255, 255))
    plate.paste(logo, (pad, pad), logo)
    base = qr_img.convert("RGBA")
    pos = ((qr_w - plate.size[0]) // 2, (qr_h - plate.size[1]) // 2)
    base.paste(plate, pos, plate)
    return base


def _tag_qr_svg_html(payload: str) -> str:
    """QR for tag print — PNG with centered logo when asset exists, else inline SVG."""
    data = (payload or "").strip() or "0"
    logo_path = _qr_logo_path()
    safe = data.replace('"', "&quot;")

    if logo_path is not None:
        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_H,
            box_size=3,
            border=1,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#000000", back_color="#FFFFFF")
        img = _embed_logo_on_qr(img, logo_path)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return (
            f'<span class="qr-code qr-with-logo" role="img" aria-label="QR {safe}">'
            f'<img class="qr-img" alt="" src="data:image/png;base64,{b64}" />'
            f"</span>"
        )

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=2,
        border=1,
    )
    qr.add_data(data)
    qr.make(fit=True)
    stream = io.BytesIO()
    qr.make_image(image_factory=SvgPathImage).save(stream)
    svg = stream.getvalue().decode("utf-8")
    if svg.startswith("<?xml"):
        svg = svg.split("?>", 1)[-1].strip()
    return f'<span class="qr-code" role="img" aria-label="QR {safe}">{svg}</span>'


class TagPrintProfile(TypedDict):
    id: str
    dpi: int
    width_mm: float
    height_mm: float
    print_mm: float
    wing_mm: float
    wing_right_mm: float
    half_mm: float
    print_scale: float
    print_safe_padding: str
    qr_size_mm: float
    driver_paper_name: str


# --- Adjust tag printer layout here (TSC TE244, 100×13 mm stock) ---
TSC_TE244: TagPrintProfile = {
    "id": "tsc_te244",
    "dpi": 203,
    "width_mm": 100.0,
    "height_mm": 13.0,
    "wing_mm": 17.5,
    "wing_right_mm": 8.5,
    "print_mm": 74.0,
    "half_mm": 37.0,
    "print_scale": 1.0,
    "print_safe_padding": "0 0.5mm 0 0",
    "qr_size_mm": 8.5,
    "driver_paper_name": "Jewellery Tag 100 x 13 mm",
}

_TAG_PROFILES: dict[str, TagPrintProfile] = {"tsc_te244": TSC_TE244}


def get_tag_print_profile() -> TagPrintProfile:
    key = (os.getenv("TAG_PRINTER_PROFILE") or "tsc_te244").strip().lower()
    return _TAG_PROFILES.get(key, TSC_TE244)


def _profile_dims(p: TagPrintProfile) -> dict[str, float]:
    wing_l = p["wing_mm"]
    wing_r = p.get("wing_right_mm", wing_l)
    print_w = p["print_mm"]
    return {
        "width_mm": p["width_mm"],
        "height_mm": p["height_mm"],
        "wing_mm": wing_l,
        "wing_right_mm": wing_r,
        "print_mm": print_w,
        "half_mm": p["half_mm"],
        "qr_mm": p["qr_size_mm"],
        "print_scale": p["print_scale"],
    }

TAG_HTML = """\
<div class="print-safe">
<div class="sheet {{less_wt_class}}">
  <aside class="wing wing-left" aria-hidden="true">
    <div class="loop-line"></div>
  </aside>
  <main class="printable">
    <section class="cell-content">
      <div class="cell-details">
        <div class="info">
          <div class="name">{{item_type_name}}</div>
          <div class="line gwt">G.wt - {{gross_weight}} GM</div>
          <div class="tag-value-line">{{tag_value}}</div>
          <div class="line">N.wt - {{net_weight}} GM</div>
        </div>
        <div class="hallmark">
          {{hallmark_mark}}
          <div class="purity">{{purity}}</div>
        </div>
      </div>
      <div class="brand-bc {{less_wt_class}}">
        <div class="brand-stack">
          <div class="brand">
            <span class="brand-line">{{brand_line1}}</span>
            <span class="brand-line">{{brand_line2}}</span>
          </div>
          <div class="less-wt-line">{{less_weight_brand}}</div>
        </div>
        <div class="qr-slot">
          {{qr_code}}
        </div>
      </div>
    </section>
  </main>
  <aside class="wing wing-right" aria-hidden="true"></aside>
</div>
</div>
"""

def _build_tag_css(p: TagPrintProfile) -> str:
    d = _profile_dims(p)
    w, h = d["width_mm"], d["height_mm"]
    wing, wing_r, print_w = (
        d["wing_mm"],
        d["wing_right_mm"],
        d["print_mm"],
    )
    qr_mm = d["qr_mm"]
    pad = p["print_safe_padding"]
    content_top = 1.15

    return f"""\
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: #fff; }}
.print-safe {{
  width: {w}mm; height: {h}mm; overflow: hidden; box-sizing: border-box;
  padding: 0; display: flex; align-items: stretch;
}}
.sheet {{
  width: {w}mm; height: {h}mm; max-height: {h}mm; display: flex; flex-direction: row;
  align-items: stretch; font-family: Arial, Helvetica, sans-serif;
  color: #000; font-size: 6pt; line-height: 1.12; background: #fff;
  overflow: hidden; flex: 1 1 auto; min-height: 0;
}}

/* Wings — left loop strip; narrower right wing so print area sits further right */
.wing {{ position: relative; background: #fff; }}
.wing-left {{ width: {wing}mm; flex: 0 0 {wing}mm; display: flex; align-items: center; padding: 0 0.4mm 0 0.6mm; }}
.wing-right {{ width: {wing_r}mm; flex: 0 0 {wing_r}mm; }}
.wing-left .loop-line {{
  position: absolute; left: 1.2mm; right: 1.8mm; top: 50%;
  height: 1.2mm; transform: translateY(-50%); background: #bdbdbd; z-index: 1;
}}

.printable {{
  width: {print_w}mm; flex: 0 0 {print_w}mm; display: flex; flex-direction: row;
  align-items: flex-start; justify-content: flex-start;
  overflow: hidden; padding: 0; background: #fff;
}}
.cell-content {{
  flex: 1 1 auto; min-width: 0; width: 100%; max-width: 100%;
  padding: {content_top}mm 0.1mm 0.1mm 0.5mm;
  display: grid;
  grid-template-columns: 1fr auto 7mm auto;
  align-items: start;
  column-gap: 0;
  overflow: hidden; background: #fff;
}}
.cell-details {{
  grid-column: 2;
  display: flex; flex-direction: row; align-items: flex-start;
  gap: 0.3mm; min-width: 0; overflow: hidden;
}}
.info {{
  flex: 0 0 auto; min-width: 0; max-width: 18mm;
  display: flex; flex-direction: column; justify-content: flex-start;
  gap: 0.06mm; overflow: hidden;
}}
.info .name {{
  font-weight: bold; font-size: 6.5pt; line-height: 1.02;
  white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; letter-spacing: 0.02em;
}}
.info .line {{ font-size: 5.2pt; line-height: 1.06; white-space: nowrap; }}
.info .line.gwt {{ font-size: 5.85pt; font-weight: bold; }}
.info .tag-value-line {{
  font-weight: bold; font-size: 5.5pt; line-height: 1.08;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  margin-top: 0.06mm; letter-spacing: 0.02em;
}}
.brand-stack {{
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  gap: 0.08mm;
  min-width: 0;
  max-width: 16mm;
  overflow: visible;
  padding-top: 0.45mm;
}}
.brand-bc .less-wt-line {{
  font-weight: bold;
  font-size: 5.2pt;
  line-height: 1.15;
  white-space: nowrap;
  overflow: visible;
  letter-spacing: 0.02em;
  max-width: none;
  margin-top: 0.1mm;
}}
.brand-bc .less-wt-line:empty {{
  display: none;
}}
.sheet.has-less-wt .wing-right {{
  width: 3.5mm; flex: 0 0 3.5mm;
}}
.sheet.has-less-wt .cell-content {{
  grid-template-columns: 1fr auto 4mm auto;
}}
.sheet.has-less-wt .brand-bc,
.brand-bc.has-less-wt,
.brand-bc:has(.less-wt-line:not(:empty)) {{
  gap: 1.0mm;
  overflow: visible;
}}
.sheet.has-less-wt .brand-stack,
.brand-bc.has-less-wt .brand-stack,
.brand-bc:has(.less-wt-line:not(:empty)) .brand-stack {{
  max-width: none;
}}
.sheet.has-less-wt .qr-slot,
.brand-bc.has-less-wt .qr-slot,
.brand-bc:has(.less-wt-line:not(:empty)) .qr-slot {{
  margin-left: 0;
}}

.hallmark {{
  flex: 0 0 6.2mm; display: flex; flex-direction: column; align-items: center;
  justify-content: flex-start; gap: 0.65mm; align-self: flex-start;
  margin-left: 1.0mm;
  border-right: 0.35pt dashed #aaa;
  padding-right: 0.3mm;
  margin-right: 0.05mm;
}}
.hallmark .hm-img {{
  height: 3.8mm; width: auto; max-width: 5.8mm; display: block;
  object-fit: contain; object-position: center top;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}}
.hallmark .hm-bis {{
  image-rendering: -webkit-optimize-contrast;
  image-rendering: crisp-edges;
}}
.hallmark .tri {{ font-size: 6.5pt; line-height: 1; color: #111; font-weight: bold; }}
.hallmark .purity {{ font-weight: bold; font-size: 6.2pt; line-height: 1; margin-top: 0.25mm; }}

/* Brand + QR — right end; gap column before this is the red-line spacer */
.brand-bc {{
  grid-column: 4;
  display: flex;
  flex-direction: row;
  flex-wrap: nowrap;
  align-items: flex-start;
  justify-content: flex-end;
  gap: 1.0mm;
  width: auto;
  max-width: none;
  margin-left: 0;
  min-height: 0;
  padding: 0.35mm 0 0 0;
  overflow: visible;
  background: #fff;
}}
.brand-bc .qr-slot ~ * {{
  display: none !important;
}}
.brand-bc .brand {{
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  font-size: 7pt;
  font-weight: bold;
  letter-spacing: 0;
  text-transform: none;
  text-align: center;
  line-height: 1.25;
  gap: 0.04mm;
  overflow: visible;
  margin-right: 0.2mm;
  min-width: fit-content;
  max-width: 16mm;
  position: relative;
  z-index: 1;
  padding-top: 0.25mm;
}}
.brand-bc .brand-line {{
  display: block;
  width: 100%;
  white-space: nowrap;
  line-height: 1.3;
  text-align: center;
  overflow: visible;
}}
.brand-bc .qr-slot {{
  flex: 0 0 {qr_mm}mm;
  width: {qr_mm}mm;
  height: {qr_mm}mm;
  display: flex; align-items: flex-start; justify-content: center;
  line-height: 0; overflow: hidden;
  margin: 0 0 0 0;
  flex-shrink: 0;
}}
.brand-bc .qr-code {{
  display: block;
  width: {qr_mm}mm;
  height: {qr_mm}mm;
  max-width: 100%;
  max-height: 100%;
  background: #fff;
}}
.brand-bc .qr-code .qr-img,
.brand-bc .qr-code svg {{
  width: 100%; height: 100%; display: block;
  object-fit: contain;
}}
.brand-bc .qr-code svg path {{
  fill: #000;
}}
.brand-bc .qr-with-logo {{
  line-height: 0;
}}
/* TSC TE244: 100×13 mm page — same as the working June/July print. */
@media print {{
  @page {{
    size: {w}mm {h}mm;
    margin: 0;
  }}
  html, body {{
    width: {w}mm !important;
    height: {h}mm !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }}
  .print-safe {{
    width: {w}mm;
    height: {h}mm;
    overflow: hidden;
    display: flex;
    box-sizing: border-box;
    padding: {pad};
    position: relative;
    align-items: stretch;
  }}
  .sheet {{
    width: {w}mm !important;
    height: {h}mm !important;
    max-height: {h}mm !important;
    overflow: hidden !important;
    position: absolute;
    top: 0;
    left: 0;
    margin: 0;
    transform: none;
    page-break-inside: avoid;
    break-inside: avoid;
  }}
  .printable {{
    justify-content: flex-start !important;
    padding: 0 !important;
  }}
  .cell-content {{
    padding-top: {content_top}mm !important;
    padding-left: 0.5mm !important;
    padding-right: 0.1mm !important;
    display: grid !important;
    grid-template-columns: 1fr auto 7mm auto !important;
  }}
  .cell-details {{
    grid-column: 2 !important;
  }}
  .hallmark {{
    margin-left: 1.0mm !important;
    gap: 0.65mm !important;
  }}
  .hallmark .purity {{
    margin-top: 0.25mm !important;
  }}
  .hallmark .hm-img {{
    height: 3.8mm !important;
    max-width: 5.8mm !important;
    -webkit-print-color-adjust: exact !important;
    print-color-adjust: exact !important;
  }}
  .info .tag-value-line {{
    font-size: 5.5pt !important;
    font-weight: bold !important;
  }}
  .brand-bc .less-wt-line {{
    font-size: 5.2pt !important;
    font-weight: bold !important;
    margin-top: 0.1mm !important;
  }}
  .brand-bc {{
    grid-column: 4 !important;
    align-items: flex-start !important;
    justify-content: flex-end !important;
    margin-left: 0 !important;
    width: auto !important;
    max-width: none !important;
    gap: 1.0mm;
    overflow: visible !important;
    flex-wrap: nowrap !important;
    padding-top: 0.35mm !important;
  }}
  .brand-bc .qr-slot ~ * {{
    display: none !important;
  }}
  .brand-bc .brand {{
    margin-right: 0.2mm !important;
    align-items: center !important;
    text-align: center !important;
    text-transform: none !important;
    font-size: 7pt !important;
    line-height: 1.25 !important;
    overflow: visible !important;
    padding-top: 0.25mm !important;
    max-width: 16mm !important;
  }}
  .brand-bc .brand-line {{
    text-align: center !important;
    line-height: 1.3 !important;
    overflow: visible !important;
  }}
  .brand-stack {{
    align-items: center !important;
    overflow: visible !important;
    padding-top: 0.45mm !important;
  }}
  .brand-bc .qr-slot {{
    align-self: flex-start !important;
    overflow: hidden !important;
    margin: 0 !important;
  }}
  .sheet.has-less-wt .wing-right {{
    width: 3.5mm !important;
    flex: 0 0 3.5mm !important;
  }}
  .sheet.has-less-wt .cell-content {{
    grid-template-columns: 1fr auto 4mm auto !important;
  }}
  .sheet.has-less-wt .brand-bc,
  .brand-bc.has-less-wt,
  .brand-bc:has(.less-wt-line:not(:empty)) {{
    gap: 1.0mm !important;
    overflow: visible !important;
  }}
  .sheet.has-less-wt .qr-slot,
  .brand-bc.has-less-wt .qr-slot,
  .brand-bc:has(.less-wt-line:not(:empty)) .qr-slot {{
    margin: 0 !important;
  }}
  .brand-bc .less-wt-line {{
    overflow: visible !important;
    max-width: none !important;
  }}
  .brand-bc .qr-code {{
    width: {qr_mm}mm !important;
    height: {qr_mm}mm !important;
  }}
}}
"""


_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _format_weight(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    try:
        n = float(s)
        return f"{n:.3f}"
    except (TypeError, ValueError):
        return s


def _sku_display_line(product_code: str, tag: ProductTag) -> str:
    """Short SKU line for the tag — mirrors frontend `skuDisplayLine`."""
    pc = (product_code or "").strip()
    if pc:
        return pc if len(pc) <= 22 else pc[:22]
    sc = (tag.sku_code or "").strip()
    if sc:
        return sc if len(sc) <= 22 else f"{sc[:18]}…"
    tv = (tag.tag_value or "").strip()
    if not tv:
        return ""
    if len(tv) <= 22:
        return tv
    tail = tv.split("-")[-1] if "-" in tv else tv
    if re.fullmatch(r"[A-Za-z0-9]+", tail) and 4 <= len(tail) <= 14:
        return tail
    return f"{tv[:18]}…"


def _tag_company_display(brand: str) -> str:
    """Label face brand — always show Hindi store name for Ashish."""
    name = (brand or "").strip() or "ASHISH"
    if name.upper() in (
        "ASHISH",
        "ASHISH JEWELLERS",
        "ASHISH JEWELRY",
        "ASHISH JEWELLY",
    ):
        return "आशीष ज्वैलर्स"
    return name


def _tag_brand_lines(brand: str) -> tuple[str, str]:
    """Two-line Hindi brand, centered on the tag."""
    display = _tag_company_display(brand)
    if display == "आशीष ज्वैलर्स":
        return "आशीष", "ज्वैलर्स"
    if " " in display:
        first, rest = display.split(None, 1)
        return first, rest
    return display, ""


def _item_type_name(item) -> str:
    """Item type (subcategory) name for the tag face."""
    if item is None:
        return ""
    sku = item.sku if getattr(item, "sku_id", None) else None
    if not sku:
        return ""
    pg = sku.product_group if getattr(sku, "product_group_id", None) else None
    if not pg:
        return ""
    sub = pg.subcategory if getattr(pg, "subcategory_id", None) else None
    if sub and (sub.name or "").strip():
        return sub.name.strip()
    return ""


def _build_placeholders(tag: ProductTag, *, show_hallmark: bool = False) -> Dict[str, str]:
    item = tag.product_item
    sku = item.sku if item and item.sku_id else None

    def _val(*candidates):
        for c in candidates:
            if c is None:
                continue
            s = str(c).strip()
            if s:
                return s
        return ""

    brand_raw = _val(getattr(sku, "brand", None) if sku else None, "ASHISH")
    company_name = _tag_company_display(brand_raw)
    brand_line1, brand_line2 = _tag_brand_lines(brand_raw)

    product_code = ""
    if item is not None:
        product_code = _val(getattr(item, "product_code", None))

    purity_value = ""
    metal_info = (tag.metal_info or "").strip()
    if metal_info:
        m = re.search(r"(\d+(?:\.\d+)?\s*K?)", metal_info, flags=re.IGNORECASE)
        if m:
            purity_value = m.group(1).replace(" ", "").upper()
    if not purity_value and item is not None:
        bom = (
            item.bom_items.select_related("purity")
            .filter(material_type="METAL")
            .first()
            if hasattr(item, "bom_items") else None
        )
        if bom is not None and bom.purity_id:
            purity_value = (bom.purity.purity_name or "").strip()

    less_raw = _val(tag.less_weight)
    less_display = ""
    if less_raw and less_raw not in ("0", "0.0", "0.000", "0.00"):
        less_display = _format_weight(less_raw) or less_raw

    item_type = _item_type_name(item)

    qr_payload = tag_qr_payload(tag)
    return {
        "company_name": company_name,
        "brand_line1": brand_line1,
        "brand_line2": brand_line2,
        "hallmark_mark": hallmark_mark_html(show_hallmark=show_hallmark),
        "qr_code": _tag_qr_svg_html(qr_payload),
        "qr_payload": qr_payload,
        "tag_value": tag_label_display(tag),
        "sku": _sku_display_line(product_code, tag) or tag_label_display(tag),
        "item_type_name": item_type,
        # Keep legacy key for any older consumers of placeholders payload
        "store_variant_name": item_type,
        "metal_info": _val(tag.metal_info),
        "purity": purity_value,
        "gross_weight": _format_weight(_val(
            tag.gross_weight,
            getattr(item, "gross_weight", None) if item else None,
        )),
        "net_weight": _format_weight(_val(
            tag.net_weight,
            getattr(item, "net_weight", None) if item else None,
        )),
        "less_weight": less_display,
        "less_weight_brand": (
            f"L wt - {less_display}GM" if less_display else ""
        ),
        "less_wt_class": "has-less-wt" if less_display else "",
        "price": _val(tag.price_info),
        "branch": _val(tag.branch_name),
        "remark": _val(tag.remark),
    }


def _render(template: str, ctx: Dict[str, str]) -> str:
    return _TOKEN_RE.sub(lambda m: ctx.get(m.group(1), ""), template or "")


def render_tag(tag: ProductTag, *, show_hallmark: bool = False) -> dict:
    profile = get_tag_print_profile()
    ctx = _build_placeholders(tag, show_hallmark=show_hallmark)
    d = _profile_dims(profile)
    return {
        "width_mm": d["width_mm"],
        "height_mm": d["height_mm"],
        "html": _render(TAG_HTML, ctx),
        "css": _build_tag_css(profile),
        "qr_payload": ctx["qr_payload"],
        "placeholders": ctx,
        "print_profile": profile["id"],
        "qr_size_mm": profile["qr_size_mm"],
        "printer_hint": (
            f"TSC TE244 — Paper: {profile['driver_paper_name']} ({d['width_mm']}×{d['height_mm']} mm). "
            "Chrome → More settings: that paper size, Margins None, Scale 100% (not Fit to page)."
        ),
    }
