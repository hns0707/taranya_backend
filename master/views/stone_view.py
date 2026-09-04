import re

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from decimal import Decimal, InvalidOperation

from shared.models import HSNMaster, Stone, LookupValue


def _stone_code_max_length() -> int:
    return Stone._meta.get_field("stone_code").max_length


def _clamp_stone_code_length(s: str, max_len: int) -> str:
    """
    Fit generated SKU in DB. Prefer trimming at the last hyphen so we do not store a
    dangling partial segment (e.g. ...CUTPRI- with nothing after).
    """
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    pref = s[:max_len]
    if pref.endswith("-"):
        pref = pref[:-1]
    hy = pref.rfind("-")
    if hy >= max(8, max_len // 5):
        return pref[:hy]
    return pref[:max_len].rstrip("-")


def _optional_lookup_id(raw):
    if raw in (None, "", []):
        return None
    try:
        return LookupValue.objects.get(id=int(raw))
    except (LookupValue.DoesNotExist, TypeError, ValueError):
        return None


def _optional_decimal(raw):
    if raw in (None, "", []):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _optional_hsn_fk(raw):
    if raw in (None, "", []):
        return None
    try:
        return HSNMaster.objects.get(id=int(raw))
    except (HSNMaster.DoesNotExist, TypeError, ValueError):
        return None


def _truthy_auto_generate(val):
    if val is True:
        return True
    if isinstance(val, str) and val.strip().lower() in ("1", "true", "yes"):
        return True
    return False


def _sanitize_sku_token(code):
    """Uppercase alphanumeric token from a LookupValue.code (or fallback)."""
    if code is None:
        return ""
    s = str(code).strip().upper()
    s = re.sub(r"[^A-Z0-9]", "", s)
    return s


def _label_primary_segment(label: str) -> str:
    """First meaningful segment when masters use 'A / B / C' labels."""
    if not label:
        return ""
    parts = [p.strip() for p in re.split(r"\s*/\s*", label) if p.strip()]
    return parts[0] if parts else label.strip()


def _letters_only_upper(s: str) -> str:
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def _shape_style_code(raw: str) -> str:
    """
    Short industry-style shape token (Round→RND, Oval→OVL).
    Used for both standalone Shape and as the suffix after CUT for Cut.
    """
    if not raw:
        return ""
    seg = _label_primary_segment(raw)
    letters = _letters_only_upper(seg)
    if not letters:
        letters = _letters_only_upper(raw)
    lm = letters[:24]
    if lm.startswith("ROUND"):
        return "RND"
    if lm.startswith("OVAL"):
        return "OVL"
    if lm.startswith("PEAR"):
        return "PEA"
    if lm.startswith("PRINCESS"):
        return "PRI"
    if lm.startswith("CUSHION"):
        return "CUS"
    if lm.startswith("EMERALD"):
        return "EME"
    if lm.startswith("MARQUISE"):
        return "MRQ"
    if lm.startswith("SQUARE"):
        return "SQU"
    if lm.startswith("HEART"):
        return "HRT"
    if len(letters) >= 4:
        return (letters[0] + letters[-2:])[:8]
    return letters[:8]


def _stone_code_token_from_lookup(role: str, lv) -> str:
    """
    Readable SKU fragment from LookupValue.label (master Code is ignored when label exists,
    so long codes like LAB / DIAMONDGRP do not override sensible abbreviations).

    Examples:
      Lab Grown → LG, Diamond (group) → DIA, Precious → PR, Purple → PURPLE,
      VVS → CLRVVS, Round (cut) → CUTRND, Rank 2 → RNK2, Round (shape) → RND.
    """
    if lv is None:
        return ""
    code_t = _sanitize_sku_token(getattr(lv, "code", None))
    raw = (lv.label or "").strip()
    seg = _label_primary_segment(raw)
    letters = _letters_only_upper(seg)
    if not letters and raw:
        letters = _letters_only_upper(raw)

    if not raw:
        return code_t[:12] if code_t else ""

    role = (role or "").lower()

    if role == "stone_type":
        rl = raw.lower()
        if "lab" in rl and "grown" in rl:
            return "LG"
        if "glass" in rl and "fill" in rl:
            return "GF"
        if "gem" in rl and "stone" in rl:
            return "GS"
        words = re.split(r"[\s/|,-]+", raw.strip())
        words = [w for w in words if w]
        if len(words) >= 2:
            initials = "".join(_letters_only_upper(w[:1]) for w in words[:5])
            if len(initials) >= 2:
                return initials[:8]
        if len(letters) >= 3:
            return letters[:3]
        return letters[:8] if letters else (code_t[:12] if code_t else "")

    if role == "stone_group":
        if letters.startswith("DIAMOND"):
            return "DIA"
        if len(letters) >= 3:
            return letters[:3]
        return letters[:1] if letters else (code_t[:12] if code_t else "")

    if role == "stone_category":
        return letters[:2] if len(letters) >= 2 else letters[:1]

    if role == "color":
        # Full colour name in letters (readable at a glance), capped for DB segment size.
        full = _letters_only_upper(seg) or letters
        return full[:20] if full else (code_t[:12] if code_t else "")

    if role == "clarity":
        m = re.search(
            r"(?i)\b(VVS1|VVS2|VVS|VS1|VS2|VS|SI1|SI2|SI|I\s*[-]?\s*\d+|[IVX]{1,4}[-+]?\d*)\b",
            raw,
        ) or re.search(
            r"(?i)\b(VVS1|VVS2|VVS|VS1|VS2|VS|SI1|SI2|SI|I\s*[-]?\s*\d+|[IVX]{1,4}[-+]?\d*)\b",
            seg,
        )
        body = _sanitize_sku_token(m.group(0))[:10] if m else ""
        if not body and (re.fullmatch(r"\d+", letters) or re.fullmatch(r"\d+", seg.strip())):
            n = re.sub(r"\D", "", seg or raw)
            body = n[:6] if n else ""
        if not body:
            body = (letters[:6] if letters else "")[:10]
        if not body:
            return code_t[:12] if code_t else ""
        return f"CLR{body}"[:16]

    if role == "cut":
        sh = _shape_style_code(raw)
        if sh:
            return f"CUT{sh}"[:16]
        return (f"CUT{letters[:4]}"[:16]) if letters else (code_t[:12] if code_t else "")

    if role == "rank":
        m = re.search(r"(?i)rank\s*(\d+)", raw) or re.search(r"(?i)rank\s*(\d+)", seg)
        if m:
            return f"RNK{m.group(1)}"
        m2 = re.search(r"(\d+)", letters)
        if m2:
            return f"RNK{m2.group(1)}"
        return letters[:6] if letters else (code_t[:12] if code_t else "")

    if role == "shape":
        s = _shape_style_code(raw)
        return s if s else (letters[:8] if letters else (code_t[:12] if code_t else ""))

    # size_unit or generic
    return letters[:8] if letters else (code_t[:12] if code_t else "")


def _size_numeric_token_for_sku(sz_str: str) -> str:
    """Digits and at most one '.' so sizes like 1.5 stay 1.5 in the code (never 15)."""
    if not sz_str:
        return ""
    numtok = re.sub(r"[^0-9.]", "", str(sz_str).strip())
    if not numtok:
        return ""
    if numtok.count(".") > 1:
        first = numtok.index(".")
        numtok = numtok[: first + 1] + numtok[first + 1 :].replace(".", "")
    numtok = numtok.strip(".")
    return numtok or ""


def _size_sku_token(stone_size, size_unit) -> str:
    if stone_size is None:
        return ""
    sz_str = format(stone_size, "f").rstrip("0").rstrip(".")
    if not sz_str:
        return ""
    numtok = _size_numeric_token_for_sku(sz_str)
    if not numtok:
        return ""
    if size_unit is not None:
        utok = _sanitize_sku_token(size_unit.code) or _letters_only_upper((size_unit.label or "")[:4])
        if utok and numtok:
            return f"{numtok}{utok}"[:16]
    return numtok[:16]


def build_stone_identifiers(
    stone_type,
    stone_category,
    color,
    shape,
    rank,
    stone_group=None,
    clarity=None,
    cut=None,
    stone_size=None,
    size_unit=None,
    hsn=None,
):
    """
    Build display name + hyphenated stone_code from master selections.

    Code order: type, group, category, color, clarity, cut, [size], rank, shape, [HSN].
    Tokens are derived from each lookup's label (readable abbreviations); long master
    codes on lookups are not used when a label is present, so SKU stays short and consistent.
    """
    sku_parts = []
    name_parts = []

    def append_name_lv(lv):
        if lv is None:
            return
        lab = (lv.label or "").strip()
        if lab:
            name_parts.append(lab)

    def append_code(role, lv):
        if lv is None:
            return
        tok = _stone_code_token_from_lookup(role, lv)
        if tok:
            sku_parts.append(tok)
        append_name_lv(lv)

    append_code("stone_type", stone_type)
    if stone_group is not None:
        append_code("stone_group", stone_group)
    append_code("stone_category", stone_category)
    append_code("color", color)
    if clarity is not None:
        append_code("clarity", clarity)
    if cut is not None:
        append_code("cut", cut)

    if stone_size is not None:
        sz_str = format(stone_size, "f").rstrip("0").rstrip(".")
        if sz_str:
            if size_unit is not None:
                ulab = (size_unit.label or "").strip()
                name_parts.append(f"{sz_str} {ulab}".strip() if ulab else sz_str)
            else:
                name_parts.append(sz_str)
            stok = _size_sku_token(stone_size, size_unit)
            if stok:
                sku_parts.append(stok)

    append_code("rank", rank)
    append_code("shape", shape)

    hsn_str = (hsn.hsn_code or "").strip() if hsn is not None else ""
    if hsn_str:
        name_parts.append(f"HSN {hsn_str}")
        ht = _sanitize_sku_token(hsn_str.replace("/", "").replace("-", ""))[:10]
        if ht:
            sku_parts.append(ht)

    stone_name = " / ".join(name_parts)
    stone_code = "-".join(sku_parts)
    return stone_code, stone_name


def _allocate_unique_stone_code(base: str, exclude_pk=None):
    max_len = _stone_code_max_length()
    base = _clamp_stone_code_length((base or "").strip(), max_len)
    if not base:
        base = "STONE"
    candidate = base
    n = 2
    while True:
        qs = Stone.objects.filter(stone_code=candidate)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        if not qs.exists():
            return candidate
        suffix = f"-{n}"
        n += 1
        room = max_len - len(suffix)
        root = _clamp_stone_code_length(base, max(1, room))
        candidate = _clamp_stone_code_length(root + suffix, max_len)


@api_view(["POST"])
def preview_stone_identifiers(request):
    """
    POST /master/stones/preview-identifiers/
    Body: same lookup ids as create (stone_type, stone_category, color, shape, rank, optional ...).
    Returns generated stone_code / stone_name from lookup labels (readable SKU rules).
    """
    try:
        stone_type = LookupValue.objects.get(id=request.data.get("stone_type"))
        stone_category = LookupValue.objects.get(id=request.data.get("stone_category"))
        color = LookupValue.objects.get(id=request.data.get("color"))
        shape = LookupValue.objects.get(id=request.data.get("shape"))
        rank = LookupValue.objects.get(id=request.data.get("rank"))
        stone_group = _optional_lookup_id(request.data.get("stone_group"))
        clarity = _optional_lookup_id(request.data.get("clarity"))
        cut = _optional_lookup_id(request.data.get("cut"))
        size_unit = _optional_lookup_id(request.data.get("size_unit"))
        stone_size = _optional_decimal(request.data.get("stone_size"))
        hsn = _optional_hsn_fk(request.data.get("hsn_id"))
        code, name = build_stone_identifiers(
            stone_type,
            stone_category,
            color,
            shape,
            rank,
            stone_group=stone_group,
            clarity=clarity,
            cut=cut,
            stone_size=stone_size,
            size_unit=size_unit,
            hsn=hsn,
        )
        code = _clamp_stone_code_length(code, _stone_code_max_length())
        return Response(
            {
                "message": "Preview generated",
                "data": {
                    "stone_code": code,
                    "stone_name": name,
                },
            }
        )
    except LookupValue.DoesNotExist:
        return Response({"error": "Invalid lookup value provided"}, status=400)
    except Exception as e:
        return Response({"error": str(e)}, status=500)


@api_view(["POST"])
def create_stone(request):
    try:
        stone_type = LookupValue.objects.get(id=request.data.get("stone_type"))
        stone_category = LookupValue.objects.get(id=request.data.get("stone_category"))
        color = LookupValue.objects.get(id=request.data.get("color"))
        shape = LookupValue.objects.get(id=request.data.get("shape"))
        rank = LookupValue.objects.get(id=request.data.get("rank"))

        stone_group = _optional_lookup_id(request.data.get("stone_group"))
        clarity = _optional_lookup_id(request.data.get("clarity"))
        cut = _optional_lookup_id(request.data.get("cut"))
        size_unit = _optional_lookup_id(request.data.get("size_unit"))
        stone_size = _optional_decimal(request.data.get("stone_size"))
        default_rate = _optional_decimal(request.data.get("default_rate"))
        hsn = _optional_hsn_fk(request.data.get("hsn_id"))

        auto = _truthy_auto_generate(request.data.get("auto_generate"))
        manual_code = _clamp_stone_code_length(
            (request.data.get("stone_code") or "").strip(),
            _stone_code_max_length(),
        )
        if auto:
            gen_code, gen_name = build_stone_identifiers(
                stone_type,
                stone_category,
                color,
                shape,
                rank,
                stone_group=stone_group,
                clarity=clarity,
                cut=cut,
                stone_size=stone_size,
                size_unit=size_unit,
                hsn=hsn,
            )
            if not manual_code and not gen_code:
                return Response(
                    {
                        "error": "Could not build stone code from selections. "
                        "Check lookup labels or set short Code values on master lookups."
                    },
                    status=400,
                )
            if not gen_name:
                gen_name = gen_code or manual_code
            if manual_code:
                if Stone.objects.filter(stone_code=manual_code).exists():
                    return Response({"error": "Stone code already exists"}, status=400)
                stone_code = manual_code
            else:
                stone_code = _allocate_unique_stone_code(gen_code)
            stone_name = (gen_name or "")[:4096]
        else:
            stone_code = _clamp_stone_code_length((request.data.get("stone_code") or "").strip(), _stone_code_max_length())
            stone_name = (request.data.get("stone_name") or "").strip()
            if not stone_code or not stone_name:
                return Response(
                    {"error": "stone_code and stone_name are required unless auto_generate is true"},
                    status=400,
                )
            if Stone.objects.filter(stone_code=stone_code).exists():
                return Response({"error": "Stone code already exists"}, status=400)

        stone = Stone.objects.create(
            stone_code=stone_code,
            stone_name=stone_name,
            stone_type=stone_type,
            stone_category=stone_category,
            color=color,
            shape=shape,
            rank=rank,
            stone_group=stone_group,
            clarity=clarity,
            cut=cut,
            stone_size=stone_size,
            size_unit=size_unit,
            default_rate=default_rate,
            hsn=hsn,
            is_active=request.data.get("is_active", True),
        )

        return Response(
            {
                "message": "Stone created successfully",
                "stone_id": stone.id,
                "stone_code": stone.stone_code,
                "stone_name": stone.stone_name,
            },
            status=201,
        )

    except LookupValue.DoesNotExist:
        return Response(
            {
                "error": "Invalid lookup value provided"
            }, status=400)

    except Exception as e:
        return Response({
            "error": str(e)
        }, status=500)



@api_view(["GET"])
def get_stones(request):
    """
    GET /master/stones/
    By default returns **active** stones only (is_active=True).
    Pass inactive_only=true|1|yes (or legacy include_inactive) to return **inactive** stones only.
    """
    try:
        qs = Stone.objects.select_related(
            "stone_type",
            "stone_category",
            "color",
            "shape",
            "rank",
            "stone_group",
            "clarity",
            "cut",
            "size_unit",
            "hsn",
        )
        inactive_only_raw = (
            request.GET.get("inactive_only")
            or request.GET.get("include_inactive")
            or ""
        ).strip().lower()
        inactive_only = inactive_only_raw in ("1", "true", "yes")
        qs = qs.filter(is_active=False) if inactive_only else qs.filter(is_active=True)
        stones = qs.order_by("-id")

        data = []

        for stone in stones:
            data.append({
                "id": stone.id,
                "stone_code": stone.stone_code,
                "stone_name": stone.stone_name,

                "stone_type": stone.stone_type.label,
                "stone_category": stone.stone_category.label,
                "color": stone.color.label,
                "shape": stone.shape.label,
                "rank": stone.rank.label,

                "stone_group": stone.stone_group.label if stone.stone_group_id else "",
                "clarity": stone.clarity.label if stone.clarity_id else "",
                "cut": stone.cut.label if stone.cut_id else "",
                "stone_size": str(stone.stone_size) if stone.stone_size is not None else "",
                "size_unit": stone.size_unit.label if stone.size_unit_id else "",
                "default_rate": str(stone.default_rate) if stone.default_rate is not None else "",

                "hsn_id": stone.hsn_id,
                "hsn_code": stone.hsn.hsn_code if stone.hsn_id and stone.hsn else "",
                "is_active": stone.is_active
            })

        return Response({
            "message": "Stones fetched successfully",
            "data": data
        })

    except Exception as e:
        return Response({
            "error": str(e)
        }, status=500)


@api_view(["GET"])
def get_stone(request, stone_id):
    try:

        stone = Stone.objects.select_related(
            "stone_type",
            "stone_category",
            "color",
            "shape",
            "rank",
            "stone_group",
            "clarity",
            "cut",
            "size_unit",
            "hsn",
        ).get(id=stone_id)

        data = {
            "id": stone.id,
            "stone_code": stone.stone_code,
            "stone_name": stone.stone_name,
            "stone_type": stone.stone_type.id,
            "stone_category": stone.stone_category.id,
            "color": stone.color.id,
            "shape": stone.shape.id,
            "rank": stone.rank.id,
            "stone_group": stone.stone_group_id,
            "clarity": stone.clarity_id,
            "cut": stone.cut_id,
            "stone_size": str(stone.stone_size) if stone.stone_size is not None else None,
            "size_unit": stone.size_unit_id,
            "default_rate": str(stone.default_rate) if stone.default_rate is not None else None,
            "hsn_id": stone.hsn_id,
            "hsn_code": stone.hsn.hsn_code if stone.hsn_id and stone.hsn else "",
            "is_active": stone.is_active
        }

        return Response({
            "message": "Stone fetched successfully",
            "data": data
        })

    except Stone.DoesNotExist:
        return Response({
            "error": "Stone not found"
        }, status=404)

    except Exception as e:
        return Response({
            "error": str(e)
        }, status=500)

@api_view(["PUT"])
def update_stone(request, stone_id):
    try:

        stone = Stone.objects.get(id=stone_id)

        if "stone_name" in request.data:
            stone.stone_name = request.data.get("stone_name")

        if "stone_type" in request.data:
            stone.stone_type = LookupValue.objects.get(id=request.data.get("stone_type"))

        if "stone_category" in request.data:
            stone.stone_category = LookupValue.objects.get(id=request.data.get("stone_category"))

        if "color" in request.data:
            stone.color = LookupValue.objects.get(id=request.data.get("color"))

        if "shape" in request.data:
            stone.shape = LookupValue.objects.get(id=request.data.get("shape"))

        if "rank" in request.data:
            stone.rank = LookupValue.objects.get(id=request.data.get("rank"))

        if "stone_group" in request.data:
            stone.stone_group = _optional_lookup_id(request.data.get("stone_group"))

        if "clarity" in request.data:
            stone.clarity = _optional_lookup_id(request.data.get("clarity"))

        if "cut" in request.data:
            stone.cut = _optional_lookup_id(request.data.get("cut"))

        if "size_unit" in request.data:
            stone.size_unit = _optional_lookup_id(request.data.get("size_unit"))

        if "stone_size" in request.data:
            stone.stone_size = _optional_decimal(request.data.get("stone_size"))

        if "default_rate" in request.data:
            stone.default_rate = _optional_decimal(request.data.get("default_rate"))

        if "hsn_id" in request.data:
            stone.hsn = _optional_hsn_fk(request.data.get("hsn_id"))

        if "is_active" in request.data:
            stone.is_active = request.data.get("is_active")

        manual_code = ""
        if "stone_code" in request.data:
            manual_code = _clamp_stone_code_length(
                (request.data.get("stone_code") or "").strip(),
                _stone_code_max_length(),
            )
            if manual_code:
                if Stone.objects.filter(stone_code=manual_code).exclude(id=stone.id).exists():
                    return Response({"error": "Stone code already exists"}, status=400)
                stone.stone_code = manual_code

        if _truthy_auto_generate(request.data.get("auto_generate")):
            gen_code, gen_name = build_stone_identifiers(
                stone.stone_type,
                stone.stone_category,
                stone.color,
                stone.shape,
                stone.rank,
                stone_group=stone.stone_group,
                clarity=stone.clarity,
                cut=stone.cut,
                stone_size=stone.stone_size,
                size_unit=stone.size_unit,
                hsn=stone.hsn,
            )
            gen_code = _clamp_stone_code_length(gen_code, _stone_code_max_length())
            if gen_code and not manual_code:
                stone.stone_code = _allocate_unique_stone_code(gen_code, exclude_pk=stone.id)
            if gen_name:
                stone.stone_name = (gen_name or "")[:4096]

        stone.save()

        return Response(
            {
                "message": "Stone updated successfully",
                "stone_code": stone.stone_code,
                "stone_name": stone.stone_name,
            }
        )

    except Stone.DoesNotExist:
        return Response({
            "error": "Stone not found"
        }, status=404)

    except LookupValue.DoesNotExist:
        return Response({
            "error": "Invalid lookup value provided"
        }, status=400)

    except Exception as e:
        return Response({
            "error": str(e)
        }, status=500)
    
@api_view(["DELETE"])
def delete_stone(request, stone_id):
    try:

        stone = Stone.objects.get(id=stone_id)
        stone.is_active = False
        stone.save()

        return Response({
            "message": "Stone deleted successfully"
        })

    except Stone.DoesNotExist:
        return Response({
            "error": "Stone not found"
        }, status=404)

    except Exception as e:
        return Response({
            "error": str(e)
        }, status=500)

