"""
Product creation and management views for the master app.
"""
import json
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from master.auth.admin_jwt import AdminJWTAuthentication
from master.permissions.permission_checker import admin_auth, ensure_admin_permission
from master.permissions.section_auth import (
    MASTERS_PRODUCTS_READ_AUTH,
    MASTERS_PRODUCTS_WRITE_AUTH,
)
from shared.models import (
    ProductDraft,
    ProductGroup,
    ProductSKU,
    ProductItem,
    ProductStone,
    ProductImage,
    ProductBOM,
    ProductAttribute,
    ProductOperationCharge,
    ProductPattern,
    ProductOccasion,
    AdminUser,
    Category,
    Subcategory,
    LookupValue,
    HSNMaster,
    Stone,
    VendorAddress,
    ProductItemLinkedVendor,
)
from shared.services.product_code_prefix import ensure_prefix_row, validate_product_code_mapping
from shared.services.pattern_code_registry import (
    bind_pattern_store_variant,
    validate_pattern_store_mapping,
)
from shared.product_item_size import (
    SIZE_NUMBER,
    infer_item_size_type,
    infer_sku_size_type_from_step4,
    product_item_search_q,
    serialize_product_item_size_for_api,
    size_fields_from_wizard_row,
)
from shared.services.product_sku_resolve import (
    build_sku_code_final,
    resolve_product_sku_by_code,
)
from shared.services.product_item_vendors import (
    save_linked_vendors_on_item,
    vendor_step_payload_for_item,
    vendor_variant_name_for_item,
)
from shared.services.product_item_parent import (
    apply_variant_flags_to_items,
    config_parent_item,
    materialize_variant_children,
    parse_variant_step,
    validate_variant_step,
    variant_step_payload_for_item,
)


def _is_structure_step(d):
    if not isinstance(d, dict) or not d:
        return False
    return any(
        k in d
        for k in (
            "model_rows",
            "operational_charges",
            "operational_charge_applicable",
            "bom_entries",
        )
    )


def _is_variant_step(d):
    if not isinstance(d, dict) or not d:
        return False
    return any(
        k in d
        for k in (
            "is_parent_product",
            "parent_product_item_id",
            "variant_options",
            "child_variants",
        )
    )


def _is_vendor_step(d):
    if not isinstance(d, dict):
        return False
    return ("vendor_id" in d or "vendor_entries" in d) and not _is_variant_step(d)


def _resolve_structure_step(data):
    """Product structure (model rows + operational charges) — step3 (new) or step2 (legacy)."""
    data = data or {}
    step2 = data.get("step2") or {}
    step3 = data.get("step3") or {}
    if _is_structure_step(step2) and not _is_vendor_step(step2):
        return step2 if isinstance(step2, dict) else {}
    if _is_structure_step(step3):
        return step3 if isinstance(step3, dict) else {}
    return step2 if isinstance(step2, dict) else {}


def _parse_wizard_steps_payload(data):
    """
    Map request/draft JSON to logical wizard sections.
    Supports legacy layouts and current 6-step layout:
    step2 vendor, step3 structure, step4 variant (new);
    step2 structure, step3 vendor, step4 variant (legacy).
    """
    data = data or {}
    step2 = data.get("step2") or {}
    step3 = data.get("step3") or {}
    step4 = data.get("step4") or {}

    if _is_vendor_step(step2):
        return {
            "customizable": step4 if _is_variant_step(step4) else {},
            "vendor": step2 if isinstance(step2, dict) else {},
            "attributes": data.get("step5") or {},
            "media": data.get("step6") or data.get("step5") or {},
        }

    if _is_variant_step(step3) and not _is_vendor_step(step3):
        return {
            "customizable": step3 if isinstance(step3, dict) else {},
            "vendor": step4 if isinstance(step4, dict) else {},
            "attributes": data.get("step5") or {},
            "media": data.get("step6") or data.get("step5") or {},
        }

    if _is_vendor_step(step3):
        return {
            "customizable": step4 if _is_variant_step(step4) else {},
            "vendor": step3 if isinstance(step3, dict) else {},
            "attributes": data.get("step5") or data.get("step4") or {},
            "media": data.get("step6") or data.get("step5") or {},
        }

    return {
        "customizable": step4 if _is_variant_step(step4) else (
            step3 if _is_variant_step(step3) else {}
        ),
        "vendor": step3 if _is_vendor_step(step3) else (
            step4 if _is_vendor_step(step4) else {}
        ),
        "attributes": data.get("step5") or data.get("step4") or {},
        "media": data.get("step6") or data.get("step5") or {},
    }


def get_admin_user_from_request(request):
    """
    Extract and validate admin user from JWT token in request header.
    Returns the AdminUser object or None if not authenticated.
    """
    if hasattr(request, "admin_user"):
        return request.admin_user

    try:
        token = AdminJWTAuthentication.get_token_from_request(request)
        admin_user = AdminJWTAuthentication.validate_admin_token(token)
        return admin_user
    except ValueError as e:
        return None
    except Exception as e:
        return None


@csrf_exempt
@admin_auth()
def list_drafts(request):
    """
    List all drafts created by the logged-in user.
    GET /api/products/draft/
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    denied = ensure_admin_permission(request, *MASTERS_PRODUCTS_READ_AUTH)
    if denied:
        return denied

    try:
        # Get admin user from JWT token
        admin_user = get_admin_user_from_request(request)
        if not admin_user:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Fetch drafts for the authenticated user
        drafts = ProductDraft.objects.filter(
            created_by_id=admin_user.id
        ).order_by('-system_updated_at')
        
        # Build summary response
        draft_list = []
        for draft in drafts:
            step1_data = normalize_step1(draft.draft_data.get('step1', {}))
            draft_list.append({
                'id': draft.id,
                'current_step': draft.current_step,
                'style_name': step1_data.get('style_name', ''),
                'category_id': step1_data.get('category_id'),
                'subcategory_id': step1_data.get('subcategory_id'),
                'gender_id': step1_data.get('gender_id'),
                'product_code': step1_data.get('product_code', ''),
                'description': step1_data.get('description', ''),
                'created_at': draft.system_created_at.isoformat() if draft.system_created_at else None,
                'updated_at': draft.system_updated_at.isoformat() if draft.system_updated_at else None
            })
        
        return JsonResponse({'drafts': draft_list}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@admin_auth()
def get_draft(request, draft_id):
    """
    Get full draft details by ID.
    GET /api/products/draft/<draft_id>/
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    denied = ensure_admin_permission(request, *MASTERS_PRODUCTS_READ_AUTH)
    if denied:
        return denied

    try:
        # Get admin user from JWT token
        admin_user = get_admin_user_from_request(request)
        if not admin_user:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Fetch draft with ownership check
        draft = ProductDraft.objects.filter(
            id=draft_id,
            created_by_id=admin_user.id
        ).first()
        
        if not draft:
            return JsonResponse({'error': 'Draft not found or access denied'}, status=404)
        
        # Build full response
        response = {
            'id': draft.id,
            'current_step': draft.current_step,
            'created_by': draft.created_by_id,
            'draft_data': draft.draft_data,
            'created_at': draft.system_created_at.isoformat() if draft.system_created_at else None,
            'updated_at': draft.system_updated_at.isoformat() if draft.system_updated_at else None
        }
        
        return JsonResponse(response, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def _metal_purity_labels_for_sku(sku):
    """
    ProductSKU rows do not store metal/purity; publish writes them on ProductBOM (METAL).
    Build display strings for hierarchy / SKU detail from the first item's metal BOM rows.
    """
    item = ProductItem.objects.filter(sku=sku).order_by("id").first()
    if not item:
        return "", ""
    metal_parts = []
    purity_parts = []
    boms = (
        ProductBOM.objects.filter(product=item, material_type="METAL")
        .select_related("metal", "purity")
        .order_by("id")
    )
    for bom in boms:
        if bom.metal_id and bom.metal:
            name = (bom.metal.metal_name or "").strip()
            if name:
                metal_parts.append(name)
        if bom.purity_id and bom.purity:
            pr = bom.purity
            label = (pr.purity_name or pr.type or "").strip()
            if not label and pr.purity_percentage is not None:
                label = format(pr.purity_percentage, "f").rstrip("0").rstrip(".")
            if label:
                purity_parts.append(label)

    def _uniq_join(parts):
        seen = set()
        out = []
        for p in parts:
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return ", ".join(out)

    return _uniq_join(metal_parts), _uniq_join(purity_parts)


@csrf_exempt
@admin_auth()
def list_products_hierarchy(request):
    """
    List published products in hierarchy:
    ProductGroup -> ProductSKU -> ProductItem

    GET /api/products/hierarchy/
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    denied = ensure_admin_permission(request, *MASTERS_PRODUCTS_READ_AUTH)
    if denied:
        return denied

    try:
        admin_user = get_admin_user_from_request(request)
        if not admin_user:
            return JsonResponse({"error": "Authentication required"}, status=401)

        # In absence of an explicit "published" flag in ProductGroup model,
        # we assume all records represent published data.
        product_groups = (
            ProductGroup.objects.filter(created_by_id=admin_user.id)
            .select_related("category", "subcategory", "gender")
            .order_by("-system_updated_at")
        )

        payload = []
        for pg in product_groups:
            skus = (
                ProductSKU.objects.filter(product_group=pg)
                .select_related("color", "hsn")
                .order_by("-system_updated_at")
            )

            sku_payload = []
            for sku in skus:
                items = ProductItem.objects.filter(sku=sku).order_by("-system_updated_at")
                item_payload = []
                for item in items:
                    item_payload.append(
                        {
                            "id": item.id,
                            "product_code": (sku.product_code or "") if sku else "",
                            "qty": item.qty,
                            "store_variant_name": item.store_variant_name or "",
                            "vendor_variant_name": vendor_variant_name_for_item(
                                item, ProductItemLinkedVendor=ProductItemLinkedVendor
                            ),
                            "customer_variant_name": item.customer_variant_name or "",
                            "net_weight": str(item.net_weight),
                            "gross_weight": str(item.gross_weight),
                            **serialize_product_item_size_for_api(item),
                        }
                    )

                metal_lbl, purity_lbl = _metal_purity_labels_for_sku(sku)
                sku_payload.append(
                    {
                        "id": sku.id,
                        "sku_code": sku.sku_code or "",
                        "pattern_code": sku.pattern_code or "",
                        "style_code": sku.style_code or "",
                        "metal": metal_lbl,
                        "purity": purity_lbl,
                        "color": sku.color.label if sku.color else "",
                        # Safe HSN access pattern - handle missing HSN gracefully
                        "hsn": getattr(sku.hsn, "hsn_code", None) if sku.hsn else None,
                        "items": item_payload,
                    }
                )

            payload.append(
                {
                    "product_group": {
                        "id": pg.id,
                        "style_name": pg.style_name,
                        "category_id": pg.category.name,
                        "subcategory_id": pg.subcategory.name,
                        "gender_id": pg.gender.label,
                        "description": pg.description,
                    },
                    "skus": sku_payload,
                }
            )

        return JsonResponse({"product_groups": payload}, status=200)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@admin_auth()
def search_product_items(request):
    """
    Search published product items by SKU code, product code, style, or size.
    GET /master/products/search/?q=
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    denied = ensure_admin_permission(request, *MASTERS_PRODUCTS_READ_AUTH)
    if denied:
        return denied

    try:
        admin_user = get_admin_user_from_request(request)
        if not admin_user:
            return JsonResponse({"error": "Authentication required"}, status=401)

        q = (request.GET.get("q") or "").strip()
        if len(q) < 1:
            return JsonResponse({"results": []}, status=200)

        scope_all = str(request.GET.get("all") or "").lower() in ("1", "true", "yes")
        parents_only = str(request.GET.get("parents_only") or "").lower() in ("1", "true", "yes")

        qs = (
            ProductItem.objects.select_related(
                "sku",
                "sku__product_group",
                "sku__product_group__category",
                "sku__product_group__subcategory",
                "sku__color",
            )
            .prefetch_related("bom_items__metal", "bom_items__purity")
        )
        if not scope_all:
            qs = qs.filter(sku__product_group__created_by_id=admin_user.id)
        if parents_only:
            qs = qs.filter(is_parent_product=True)
        qs = (
            qs.filter(
                Q(sku__product_code__icontains=q)
                | Q(sku__sku_code__icontains=q)
                | Q(sku__pattern_code__icontains=q)
                | Q(sku__product_group__style_name__icontains=q)
                | Q(store_variant_name__icontains=q)
                | product_item_search_q(q)
            )
            .order_by("-system_updated_at", "-id")[:40]
        )

        results = []
        for item in qs:
            sku = item.sku
            if not sku:
                continue

            style = ""
            if sku.product_group_id and sku.product_group:
                style = sku.product_group.style_name or ""

            metal_name = ""
            purity_name = ""
            bom_items_list = list(item.bom_items.all())
            metal_bom = next(
                (b for b in bom_items_list if b.material_type == "METAL" and b.metal_id),
                None,
            )
            if metal_bom:
                if metal_bom.metal_id and metal_bom.metal:
                    metal_name = metal_bom.metal.metal_name or ""
                if metal_bom.purity_id and metal_bom.purity:
                    pr = metal_bom.purity
                    purity_name = (pr.purity_name or pr.type or "").strip()

            color_label = ""
            if sku.color_id and sku.color:
                color_label = sku.color.label or ""

            item_group = ""
            item_type = ""
            category_id = None
            subcategory_id = None
            if sku.product_group_id and sku.product_group:
                pg = sku.product_group
                if pg.category_id and pg.category:
                    item_group = pg.category.name or ""
                    category_id = pg.category_id
                if pg.subcategory_id and pg.subcategory:
                    item_type = pg.subcategory.name or ""
                    subcategory_id = pg.subcategory_id

            row = {
                "id": item.id,
                "product_code": sku.product_code or "",
                "pattern_code": sku.pattern_code or "",
                "sku_code": sku.sku_code or "",
                "style_name": style,
                "metal_name": metal_name,
                "purity_name": purity_name,
                "color_label": color_label,
                "store_variant_name": item.store_variant_name or "",
                "vendor_variant_name": vendor_variant_name_for_item(
                    item, ProductItemLinkedVendor=ProductItemLinkedVendor
                ),
                "is_parent_product": bool(item.is_parent_product),
                "parent_product_item_id": item.parent_product_item_id,
                "sku_id": sku.id,
                "item_group": item_group,
                "item_type": item_type,
                "category_id": category_id,
                "subcategory_id": subcategory_id,
            }
            row.update(serialize_product_item_size_for_api(item))
            results.append(row)

        return JsonResponse({"results": results}, status=200)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@admin_auth()
def create_draft(request):
    """
    Create a new product draft.
    POST /api/products/draft/create/
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    denied = ensure_admin_permission(request, "CRM_MASTERS_PRODUCTS_CREATE")
    if denied:
        return denied

    try:
        # Get admin user from JWT token
        admin_user = get_admin_user_from_request(request)
        if not admin_user:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Parse request body
        data = json.loads(request.body) if request.body else {}
        
        # Get step1 data - accept both formats (nested under step1 or at root level)
        step1_data = data.get('step1', {})
        
        # If step1 is empty, check for root level fields (front-end sends flat structure)
        if not step1_data and data:
            # Copy all root-level fields to step1
            step1_data = {k: v for k, v in data.items() if k != 'step1'}
        
        # Build draft_data with step1 if provided
        draft_data = {
            "step1": step1_data,
            "step2": {},
            "step3": {},
            "step4": {},
            "step5": {},
            "step6": {},
        }
        
        # Determine current_step based on whether step1 data exists
        current_step = 1 if step1_data else 1
        
        draft = ProductDraft.objects.create(
            created_by=admin_user,
            current_step=current_step,
            draft_data=draft_data
        )

        return JsonResponse({'draft_id': draft.id, 'current_step': draft.current_step}, status=201)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@admin_auth()
def save_step_data(request, draft_id):
    """
    Save step data to existing draft.
    PATCH /api/products/draft/<draft_id>/save/
    """
    if request.method != 'PATCH':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    denied = ensure_admin_permission(request, *MASTERS_PRODUCTS_WRITE_AUTH)
    if denied:
        return denied

    try:
        data = json.loads(request.body)
        
        # Get admin user from JWT token
        admin_user = get_admin_user_from_request(request)
        if not admin_user:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Fetch draft with ownership check
        draft = ProductDraft.objects.filter(
            id=draft_id,
            created_by_id=admin_user.id
        ).first()
        
        if not draft:
            return JsonResponse({'error': 'Draft not found or access denied'}, status=404)

        step = data.get('step')
        step_data = data.get('data')

        if not step or step_data is None:
            return JsonResponse({'error': 'step and data are required'}, status=400)

        if not 1 <= step <= 6:
            return JsonResponse({'error': 'step must be between 1 and 6'}, status=400)

        # Merge incoming step_data with existing draft_data (not overwrite)
        existing_step_data = draft.draft_data.get(f'step{step}', {})
        merged_step_data = {**existing_step_data, **step_data}
        draft.draft_data[f'step{step}'] = merged_step_data
        
        draft.current_step = max(draft.current_step, step)
        draft.save()

        return JsonResponse({'success': True, 'current_step': draft.current_step})

    except ProductDraft.DoesNotExist:
        return JsonResponse({'error': 'Draft not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@admin_auth()
def upload_media(request, draft_id):
    """
    Upload media files and store URLs in step6 (legacy drafts: step5).
    POST /api/products/draft/<draft_id>/media/
    
    Accepts multipart/form-data with 'images' field (multiple files).
    Files are uploaded to S3 and URLs are stored in step6.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    denied = ensure_admin_permission(request, *MASTERS_PRODUCTS_WRITE_AUTH)
    if denied:
        return denied

    try:
        # Get admin user from JWT token
        admin_user = get_admin_user_from_request(request)
        if not admin_user:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Fetch draft with ownership check
        draft = ProductDraft.objects.filter(
            id=draft_id,
            created_by_id=admin_user.id
        ).first()
        
        if not draft:
            return JsonResponse({'error': 'Draft not found or access denied'}, status=404)

        # Handle multipart/form-data file upload
        uploaded_files = request.FILES.getlist('images')
        
        # Also support base64 JSON format for backward compatibility
        if not uploaded_files:
            try:
                data = json.loads(request.body)
                image_urls = data.get('image_urls', [])
            except (json.JSONDecodeError, UnicodeDecodeError):
                return JsonResponse({'error': 'Invalid request. Provide either file upload (multipart/form-data) or image_urls in JSON.'}, status=400)
        else:
            # Upload files to S3
            image_urls = []
            from shared.services.s3_service import upload_file_to_s3, build_public_object_url
            from datetime import datetime
            
            for file_obj in uploaded_files:
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                object_name = f"Taranya/products/draft_{draft_id}/{timestamp}_{file_obj.name}"
                
                success = upload_file_to_s3(file_obj, object_name)
                if success:
                    image_url = build_public_object_url(object_name)
                    image_urls.append(image_url)
                else:
                    return JsonResponse({'error': f'Failed to upload file: {file_obj.name}'}, status=500)

        if not image_urls:
            return JsonResponse({'error': 'No images provided'}, status=400)

        step5_data = draft.draft_data.get('step6') or draft.draft_data.get('step5', {})

        if uploaded_files:
            # Multipart: append new S3 URLs to whatever is already saved
            existing_images = step5_data.get('images', [])
            all_images = existing_images + image_urls
            step5_data['images'] = all_images
        else:
            # JSON body: full ordered list from client (authoritative; avoids duplicates after multipart + finalize)
            step5_data['images'] = image_urls
            all_images = image_urls

        draft.draft_data['step6'] = step5_data
        draft.current_step = max(draft.current_step, 6)
        draft.save()

        # image_urls = URLs from this request; all_image_urls = full draft step5 list after save
        return JsonResponse({
            'success': True,
            'images_count': len(image_urls),
            'total_images': len(all_images),
            'image_urls': image_urls,
            'all_image_urls': all_images,
        })

    except ProductDraft.DoesNotExist:
        return JsonResponse({'error': 'Draft not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def normalize_step1(step1):
    """
    Flatten wizard Step 1 payload: { basic_info, base_metal, other_metal, stone_details }
    into legacy flat keys expected by validate_draft / publish_draft.
    If already flat (no basic_info/base_metal), return a shallow copy.
    """
    if not isinstance(step1, dict):
        return {}
    is_nested = isinstance(step1.get('basic_info'), dict) or isinstance(step1.get('base_metal'), dict)
    if not is_nested:
        out = dict(step1)
        if not isinstance(out.get('stones'), list):
            out['stones'] = []
        return out

    basic = step1.get('basic_info') or {}
    base = step1.get('base_metal') or {}
    other = step1.get('other_metal')
    if not isinstance(other, list):
        other = []
    stone_details = step1.get('stone_details')
    if not isinstance(stone_details, list):
        stone_details = []

    flat = {}
    for k, v in basic.items():
        if v is not None and v != '':
            flat[k] = v

    metal_fields = (
        'metal', 'metal_id', 'purity', 'purity_id', 'color', 'color_id',
        'hsn_code', 'hsn_id', 'gst_rate', 'cgst', 'sgst',
        'net_weight', 'gross_weight', 'less_weight',
    )
    for k in metal_fields:
        if k in base and base[k] is not None and base[k] != '':
            flat[k] = base[k]

    metal_ids = []
    purity_ids = []
    if base.get('metal_id') not in (None, ''):
        metal_ids.append(base['metal_id'])
    if base.get('purity_id') not in (None, ''):
        purity_ids.append(base['purity_id'])
    for row in other:
        if not isinstance(row, dict):
            continue
        if row.get('metal_id') not in (None, ''):
            metal_ids.append(row['metal_id'])
        if row.get('purity_id') not in (None, ''):
            purity_ids.append(row['purity_id'])

    if metal_ids:
        flat['metal_ids'] = metal_ids
    if purity_ids:
        flat['purity_ids'] = purity_ids

    stones = []
    for s in stone_details:
        if not isinstance(s, dict):
            continue
        sid = s.get('stone_id')
        if sid is None or sid == '':
            continue
        stones.append({
            'stone_id': sid,
            'quantity': s.get('quantity', s.get('pcs', '1')),
            'weight_carat': s.get('weight_carat', s.get('weight', '')),
        })
    flat['stones'] = stones

    if isinstance(basic, dict) and basic.get('product_name') and 'vendor_variant_name' not in basic:
        flat['store_variant_name'] = str(basic.get('product_name') or '').strip()
        if basic.get('store_variant_name'):
            flat['vendor_variant_name'] = str(basic.get('store_variant_name') or '').strip()

    return flat


def _geometrical_shape_id_from_steps(step1, step4=None):
    """Resolve GEOMETRICAL_SHAPE lookup id from step1 (preferred) or step4."""
    step4 = step4 or {}
    raw = step1.get('geometrical_shape_id')
    if raw in (None, ''):
        raw = step4.get('geometrical_shape_id')
    if raw in (None, ''):
        return None
    try:
        n = int(raw)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _dec_val(val, default='0'):
    if val is None or val == '':
        val = default
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _dec_optional(val):
    if val is None or val == '':
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int_safe(val, default=1):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _stone_bom_material_key(stone_id):
    """Align with frontend material targets: one STONE BOM per master stone (`s-{stone_id}-`)."""
    return f"s-{_id_token(stone_id)}-"


def _id_token(val):
    """Normalize metal/purity/stone ids so material_key matches FE/BE."""
    if val in (None, ""):
        return ""
    try:
        return str(int(val))
    except (TypeError, ValueError):
        s = str(val).strip()
        if not s:
            return ""
        try:
            return str(int(float(s)))
        except (TypeError, ValueError):
            return s


def _metal_material_key(metal_id, purity_id):
    return f"m-{_id_token(metal_id)}-{_id_token(purity_id)}"


def _register_bom_key(mapping, key, bom):
    if not key:
        return
    mapping[str(key)] = bom


def _resolve_bom_for_model_row(row, material_key_to_bom):
    """Match model_rows.material_key (and ref ids) to BOM created from step1 metals/stones."""
    if not isinstance(row, dict) or not material_key_to_bom:
        return None

    candidates = []
    mk = (row.get("material_key") or "").strip()
    if mk:
        candidates.append(mk)
        if mk.startswith("m-"):
            parts = mk.split("-")
            if len(parts) >= 3:
                candidates.append(_metal_material_key(parts[1], "-".join(parts[2:]) if len(parts) > 3 else parts[2]))
                # Common case: m-{metal}-{purity}
                if len(parts) == 3:
                    candidates.append(_metal_material_key(parts[1], parts[2]))
        elif mk.startswith("s-"):
            parts = mk.split("-")
            if len(parts) >= 2 and parts[1]:
                candidates.append(_stone_bom_material_key(parts[1]))

    mid = row.get("ref_metal_id")
    if mid in (None, ""):
        mid = row.get("base_value_id") if (row.get("source") or "") == "metal" else None
    pid = row.get("ref_purity_id")
    if mid not in (None, "") and pid not in (None, ""):
        candidates.append(_metal_material_key(mid, pid))

    sid = row.get("ref_stone_id")
    if sid in (None, "") and (row.get("source") or "") == "stone":
        sid = row.get("base_value_id")
    if sid not in (None, ""):
        candidates.append(_stone_bom_material_key(sid))

    seen = set()
    for key in candidates:
        if not key or key in seen:
            continue
        seen.add(key)
        bom = material_key_to_bom.get(key)
        if bom:
            return bom
    return None


def _lookup_value_or_none(rid):
    if rid in (None, ""):
        return None
    try:
        return LookupValue.objects.get(id=rid)
    except (LookupValue.DoesNotExist, TypeError, ValueError):
        try:
            return LookupValue.objects.get(id=int(rid))
        except (LookupValue.DoesNotExist, TypeError, ValueError):
            return None


def _stone_master_spec_readonly(stone):
    """Short line from expanded Stone master for admin UI (read-only on product load)."""
    if not stone:
        return ''
    parts = []
    if stone.stone_size is not None:
        sz = format(stone.stone_size, 'f').rstrip('0').rstrip('.')
        u = ''
        if getattr(stone, 'size_unit_id', None) and getattr(stone, 'size_unit', None):
            u = (stone.size_unit.label or '').strip()
        parts.append(f'{sz} {u}'.strip() if u else sz)
    if getattr(stone, 'stone_group_id', None) and getattr(stone, 'stone_group', None):
        g = (stone.stone_group.label or '').strip()
        if g:
            parts.append(g)
    if getattr(stone, 'clarity_id', None) and getattr(stone, 'clarity', None):
        c = (stone.clarity.label or '').strip()
        if c:
            parts.append(c)
    if getattr(stone, 'cut_id', None) and getattr(stone, 'cut', None):
        c = (stone.cut.label or '').strip()
        if c:
            parts.append(c)
    if stone.default_rate is not None:
        dr = format(stone.default_rate, 'f').rstrip('0').rstrip('.')
        if dr:
            parts.append(f'Rate {dr}')
    return ' · '.join(parts)


def merge_step4_stones_into_step1(step1, step4):
    """Wizard may put stones only under step4; merge into step1['stones'] for publish."""
    if not isinstance(step4, dict):
        return
    stones = [dict(s) for s in (step1.get('stones') or []) if isinstance(s, dict)]
    keys = {str(s.get('stone_id')) for s in stones if s.get('stone_id') not in (None, '')}
    for s in step4.get('stones') or []:
        if not isinstance(s, dict):
            continue
        sid = s.get('stone_id')
        if sid in (None, ''):
            continue
        k = str(sid)
        if k in keys:
            continue
        stones.append({
            'stone_id': sid,
            'quantity': s.get('quantity', s.get('pcs', '1')),
            'weight_carat': s.get('weight_carat', s.get('weight', '0')),
        })
        keys.add(k)
    step1['stones'] = stones


def validate_draft(draft):
    """
    Validate all steps in the draft data.
    Returns tuple (is_valid, errors)
    """
    errors = []

    # Validate step 1: Basic Info
    step1 = normalize_step1(draft.draft_data.get('step1', {}))
    if not step1.get('style_name'):
        errors.append('Style name is required')
    if not step1.get('category_id'):
        errors.append('Category is required')
    if not step1.get('subcategory_id'):
        errors.append('Subcategory is required')
    if not step1.get('gender_id'):
        errors.append('Gender is required')
    
    # Support both single purity_id (legacy) and multiple purities array
    purity_ids = step1.get('purity_ids', [])
    purity_id = step1.get('purity_id')  # Legacy single purity
    if not purity_ids and purity_id:
        purity_ids = [purity_id]
    if not purity_ids:
        errors.append('Purity is required')
    
    if not step1.get('color_id'):
        errors.append('Color is required')
    if not step1.get('hsn_id'):
        errors.append('HSN code is required')
    # Stones optional (metal-only products; stone_details may be empty)
    if not step1.get('net_weight'):
        errors.append('Net weight is required')
    if not step1.get('gross_weight'):
        errors.append('Gross weight is required')

    product_code = (step1.get('product_code') or '').strip()
    category_id = step1.get('category_id')
    subcategory_id = step1.get('subcategory_id')
    if not product_code:
        errors.append('Product code is required')
    if product_code and category_id and subcategory_id:
        try:
            validate_product_code_mapping(product_code, category_id, subcategory_id)
        except ValidationError as e:
            msg = e.messages[0] if getattr(e, 'messages', None) else str(e)
            errors.append(msg)

    pattern_code = (step1.get('pattern_code') or '').strip()
    store_variant_name = (step1.get('store_variant_name') or '').strip()
    if not pattern_code:
        errors.append('Pattern code is required')
    if not store_variant_name:
        errors.append('Store variant name is required')
    if pattern_code and store_variant_name:
        try:
            validate_pattern_store_mapping(
                pattern_code,
                store_variant_name,
                category_id=category_id,
                subcategory_id=subcategory_id,
            )
        except ValidationError as e:
            msg = e.messages[0] if getattr(e, 'messages', None) else str(e)
            errors.append(msg)

    # Step 2: model_rows / operational_charges (persisted in draft; no publish validation)

    # Step 3: Customizable options (optional)

    # Step 4: Vendor Setup (optional)

    # Validate attributes step: patterns + sizes (ring size stays here, not in customizable tab)
    wizard = _parse_wizard_steps_payload(draft.draft_data)
    step_attributes = wizard['attributes']
    if not step_attributes.get('sizes'):
        errors.append('Sizes are required')
    if step_attributes.get('attributes'):
        for attr in step_attributes['attributes']:
            if not any([attr.get('making_category_id'), attr.get('crafting_process_id'), 
                        attr.get('method_id'), attr.get('nature_id'), attr.get('finishing_id')]):
                errors.append('At least one attribute field is required for each attribute entry')

    # Media step: images optional at publish (catalogue photos can be added later).

    return len(errors) == 0, errors


@csrf_exempt
@admin_auth()
@transaction.atomic
def publish_draft(request, draft_id):
    """
    Publish a product draft.
    POST /api/products/publish/<draft_id>/
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    denied = ensure_admin_permission(request, "CRM_MASTERS_PRODUCTS_CREATE")
    if denied:
        return denied

    try:
        data = json.loads(request.body) if request.body else {}
        
        # Get admin user from JWT token
        admin_user = get_admin_user_from_request(request)
        if not admin_user:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Fetch draft with ownership check
        draft = ProductDraft.objects.select_for_update().filter(
            id=draft_id,
            created_by_id=admin_user.id
        ).first()
        
        if not draft:
            return JsonResponse({'error': 'Draft not found or access denied'}, status=404)

        # Validate all steps
        is_valid, errors = validate_draft(draft)
        if not is_valid:
            return JsonResponse({'error': 'Validation failed', 'errors': errors}, status=400)

        # Extract data from draft (nested step1 from wizard -> flat)
        step1 = normalize_step1(draft.draft_data.get('step1', {}))
        step2 = _resolve_structure_step(draft.draft_data)
        wizard = _parse_wizard_steps_payload(draft.draft_data)
        step_custom = wizard['customizable']
        step_vendor = wizard['vendor']
        step_attributes = wizard['attributes']
        step_media = wizard['media']
        raw_step1 = draft.draft_data.get('step1') or {}
        other_metal_rows = raw_step1.get('other_metal') if isinstance(raw_step1.get('other_metal'), list) else []

        if step_attributes.get('occasion_ids') and not step1.get('occasion_ids'):
            step1['occasion_ids'] = step_attributes['occasion_ids']
        if step_attributes.get('geometrical_shape_id') and not step1.get('geometrical_shape_id'):
            step1['geometrical_shape_id'] = step_attributes['geometrical_shape_id']

        merge_step4_stones_into_step1(step1, step_attributes)

        geo_shape_id = _geometrical_shape_id_from_steps(step1, step_attributes)

        # Get or create ProductGroup — reuse if same style_name + category + subcategory + gender
        product_group, _ = ProductGroup.objects.get_or_create(
            style_name=step1['style_name'],
            category=Category.objects.get(id=step1['category_id']),
            subcategory=Subcategory.objects.get(id=step1['subcategory_id']),
            gender=LookupValue.objects.get(id=step1['gender_id']),
            defaults={
                'description': step1.get('description', ''),
                'created_by': draft.created_by,
                'updated_by': draft.created_by,
            }
        )

        base_product_code = (step1.get('product_code') or '').strip()
        pattern_code = (step1.get('pattern_code') or '').strip()
        sku_code_final = build_sku_code_final(
            step1, step_vendor, product_group_id=product_group.id
        )
        # Reuse SKU when this exact sku_code already exists (e.g. duplicate publish / same details).
        product_sku, sku_created = resolve_product_sku_by_code(
            sku_code_val=sku_code_final,
            product_group=product_group,
            color_id=step1['color_id'],
            hsn_id=step1['hsn_id'],
            base_product_code=base_product_code,
            pattern_code=pattern_code,
            style_code=(step1.get('style_code') or '')[:100] or None,
            admin_user=draft.created_by,
        )

        charge_apply = step2.get('operational_charge_applicable')
        if charge_apply:
            charge_apply = str(charge_apply).strip()[:20] or None
        else:
            charge_apply = None

        def _str255(key):
            v = step1.get(key)
            if v is None:
                return ""
            s = str(v).strip()
            return s[:255] if s else ""

        raw_sizes = [x for x in (step_attributes.get("sizes") or []) if isinstance(x, dict)]
        if not raw_sizes:
            return JsonResponse(
                {'error': 'Attributes step must include at least one valid size row before publishing.'},
                status=400,
            )

        seen_keys = set()
        product_rows = []
        for raw in raw_sizes:
            try:
                size_kw = size_fields_from_wizard_row(raw)
            except ValidationError as e:
                msg = e.messages[0] if getattr(e, 'messages', None) else str(e)
                return JsonResponse({'error': msg}, status=400)
            key = (
                size_kw.get('size_number'),
                size_kw.get('size_mm'),
                size_kw.get('height_mm'),
                size_kw.get('width_mm'),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            product_rows.append(
                ProductItem.objects.create(
                    sku=product_sku,
                    qty=0,
                    store_variant_name=_str255("store_variant_name"),
                    customer_variant_name=_str255("customer_variant_name"),
                    net_weight=_dec_val(step1.get("net_weight"), "0"),
                    gross_weight=_dec_val(step1.get("gross_weight"), "0"),
                    charge_apply=charge_apply,
                    geometrical_shape_id=geo_shape_id,
                    created_by=draft.created_by,
                    updated_by=draft.created_by,
                    **size_kw,
                )
            )
        if not product_rows:
            return JsonResponse(
                {'error': 'No distinct size rows could be created from step 4.'},
                status=400,
            )

        is_parent, parent_id = parse_variant_step(step_custom)
        if parent_id:
            parent_id = None
        is_parent = True
        variant_err = validate_variant_step(is_parent, parent_id)
        if variant_err:
            return JsonResponse({'error': variant_err}, status=400)
        try:
            apply_variant_flags_to_items(
                product_rows,
                is_parent=is_parent,
                parent_id=parent_id,
                ProductItem=ProductItem,
            )
        except ValueError as exc:
            return JsonResponse({'error': str(exc)}, status=400)

        product_item = product_rows[0]

        save_linked_vendors_on_item(
            product_item,
            step_vendor,
            admin_user=draft.created_by,
            ProductItemLinkedVendor=ProductItemLinkedVendor,
        )

        # --- BOM rows (metal + stone) for material_key matching & attributes ---
        material_key_to_bom = {}
        mid_list = list(step1.get('metal_ids') or [])
        pid_list = list(step1.get('purity_ids') or [])
        if not mid_list and step1.get('metal_id') not in (None, ''):
            mid_list = [step1['metal_id']]
        if not pid_list and step1.get('purity_id') not in (None, ''):
            pid_list = [step1['purity_id']]
        n_pairs = min(len(mid_list), len(pid_list)) if mid_list and pid_list else 0
        for idx in range(n_pairs):
            mid = mid_list[idx]
            pid = pid_list[idx]
            if mid in (None, '') or pid in (None, ''):
                continue
            if idx == 0:
                wsrc = step1.get('net_weight')
            else:
                wsrc = other_metal_rows[idx - 1].get('net_weight') if idx - 1 < len(other_metal_rows) else '0'
            mk = _metal_material_key(mid, pid)
            bom = ProductBOM.objects.create(
                product=product_item,
                material_type='METAL',
                metal_id=mid,
                purity_id=pid,
                weight=_dec_val(wsrc, '0'),
                quantity=1,
                created_by=draft.created_by,
                updated_by=draft.created_by,
            )
            _register_bom_key(material_key_to_bom, mk, bom)
            _register_bom_key(material_key_to_bom, f"m-{mid}-{pid}", bom)

        # Create ProductStones + stone BOMs (master stone only)
        for stone_data in step1.get('stones') or []:
            if not isinstance(stone_data, dict):
                continue
            sid = stone_data.get('stone_id')
            if sid in (None, ''):
                continue
            qty = _int_safe(stone_data.get('quantity'), 1)
            wt = _dec_val(stone_data.get('weight_carat'), '0')
            ProductStone.objects.create(
                product=product_item,
                stone=Stone.objects.get(id=sid),
                quantity=qty,
                weight=wt,
                created_by=draft.created_by,
                updated_by=draft.created_by,
            )
            mk = _stone_bom_material_key(sid)
            bom = ProductBOM.objects.create(
                product=product_item,
                material_type='STONE',
                stone_id=sid,
                weight=wt,
                quantity=qty,
                created_by=draft.created_by,
                updated_by=draft.created_by,
            )
            _register_bom_key(material_key_to_bom, mk, bom)
            _register_bom_key(material_key_to_bom, f"s-{sid}-", bom)

        # Create ProductImages
        for i, image_url in enumerate(step_media.get('images') or []):
            u = str(image_url).strip()
            if not u:
                continue
            ProductImage.objects.create(
                product=product_item,
                image_url=u,
                is_primary=(i == 0),
                created_by=draft.created_by,
                updated_by=draft.created_by,
            )

        # Operational charges (step2)
        for op in step2.get('operational_charges') or []:
            if not isinstance(op, dict):
                continue
            label = (op.get('component_label') or op.get('component_name') or '').strip() or 'Charge'
            val = op.get('value')
            ProductOperationCharge.objects.create(
                product=product_item,
                component_name=label[:255],
                charge_value=str(val) if val not in (None, '') else None,
                description=(op.get('description') or '') or None,
                created_by=draft.created_by,
                updated_by=draft.created_by,
            )

        # Patterns (attributes step)
        for pat in step_attributes.get('patterns') or []:
            if not isinstance(pat, dict):
                continue
            pname = (pat.get('pattern_name') or pat.get('name') or '').strip()
            if not pname:
                continue
            ProductPattern.objects.create(
                product=product_item,
                pattern_name=pname[:255],
                description=(pat.get('description') or '') or None,
                created_by=draft.created_by,
                updated_by=draft.created_by,
            )

        # Model row attributes -> ProductAttribute (linked to BOM via material_key)
        for row in step2.get('model_rows') or []:
            if not isinstance(row, dict):
                continue
            bom = _resolve_bom_for_model_row(row, material_key_to_bom)
            if not bom:
                continue

            ProductAttribute.objects.create(
                product_bom=bom,
                making_category=_lookup_value_or_none(row.get('mkg_category_id')),
                crafting_process=_lookup_value_or_none(row.get('crafting_process_id')),
                method=_lookup_value_or_none(row.get('method_id')),
                nature=_lookup_value_or_none(row.get('nature_id')),
                finishing=_lookup_value_or_none(row.get('finishing_id')),
                special_charge=(row.get('special_charge') or None),
                charge_type=_lookup_value_or_none(row.get('charge_type_id')),
                detail_number=_int_safe(row.get('detail_number'), 1),
                created_by=draft.created_by,
                updated_by=draft.created_by,
            )

        # Persist variant option cards as child product_items (parent/child model).
        try:
            materialize_variant_children(
                parent_item=product_item,
                step1=step1,
                step_custom=step_custom,
                step_vendor=step_vendor,
                product_group=product_group,
                admin_user=draft.created_by,
                ProductItem=ProductItem,
                ProductBOM=ProductBOM,
                ProductItemLinkedVendor=ProductItemLinkedVendor,
                resolve_product_sku_by_code=resolve_product_sku_by_code,
                build_sku_code_final=build_sku_code_final,
                save_linked_vendors_on_item=save_linked_vendors_on_item,
            )
        except ValueError as exc:
            return JsonResponse({'error': str(exc)}, status=400)

        # Create ProductOccasions
        if step1.get('occasion_ids'):
            for occasion_id in step1['occasion_ids']:
                ProductOccasion.objects.create(
                    product=product_item,
                    occasion=LookupValue.objects.get(id=occasion_id),
                    created_by=draft.created_by,
                    updated_by=draft.created_by
                )

        published_code = (product_sku.product_code or base_product_code or "").strip()
        if published_code:
            ensure_prefix_row(
                published_code,
                admin_user=draft.created_by,
                category_id=step1.get('category_id'),
                subcategory_id=step1.get('subcategory_id'),
            )

        store_variant_name = (step1.get('store_variant_name') or '').strip()
        if pattern_code and store_variant_name:
            bind_pattern_store_variant(
                pattern_code,
                store_variant_name,
                category_id=step1.get('category_id'),
                subcategory_id=step1.get('subcategory_id'),
                admin_user=draft.created_by,
            )

        # Delete draft after successful publish
        draft.delete()

        return JsonResponse({
            'success': True,
            'product_id': product_item.id,
            'product_item_ids': [p.id for p in product_rows],
            'product_code': published_code,
            'product_group_id': product_group.id,
            'sku_code': product_sku.sku_code,
            'sku_reused': not sku_created,
        }, status=201)

    except ProductDraft.DoesNotExist:
        return JsonResponse({'error': 'Draft not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@admin_auth()
def get_product_item(request, product_item_id):
    """
    Fetch full published product item details in draft-compatible format.
    GET /master/products/item/<product_item_id>/detail/
    Returns data shaped identically to getDraft so ProductCreate can pre-fill all tabs.
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    denied = ensure_admin_permission(request, *MASTERS_PRODUCTS_READ_AUTH)
    if denied:
        return denied

    try:
        admin_user = get_admin_user_from_request(request)
        if not admin_user:
            return JsonResponse({'error': 'Authentication required'}, status=401)

        item = (
            ProductItem.objects            .select_related(
                'sku__product_group__category',
                'sku__product_group__subcategory',
                'sku__product_group__gender',
                'sku__color',
                'sku__hsn',
                'geometrical_shape',
                'parent_product_item',
            )
            .filter(id=product_item_id, sku__product_group__created_by_id=admin_user.id)
            .first()
        )
        if not item:
            return JsonResponse({'error': 'Product not found or access denied'}, status=404)

        sku = item.sku
        group = sku.product_group

        # ── Step 1: basic_info + base_metal + other_metal + stone_details ──
        metal_boms = list(
            ProductBOM.objects.filter(product=item, material_type='METAL')
            .select_related('metal', 'purity')
            .order_by('id')
        )

        def _purity_label(purity_obj):
            if not purity_obj:
                return ''
            return (purity_obj.purity_name or purity_obj.type or '').strip()

        # GST values are derived from the linked HSN master row
        hsn_obj = sku.hsn
        gst_rate_val = str(hsn_obj.igst_rate) if hsn_obj and hsn_obj.igst_rate is not None else ''
        cgst_val = str(hsn_obj.cgst_rate) if hsn_obj and hsn_obj.cgst_rate is not None else ''
        sgst_val = str(hsn_obj.sgst_rate) if hsn_obj and hsn_obj.sgst_rate is not None else ''
        less_str = str(item.less_weight)

        base_metal = {}
        other_metal = []
        if metal_boms:
            b = metal_boms[0]
            base_metal = {
                'metal_id': b.metal_id,
                'metal': b.metal.metal_name if b.metal else '',
                'purity_id': b.purity_id,
                'purity': _purity_label(b.purity),
                'color_id': sku.color_id,
                'color': sku.color.label if sku.color else '',
                'hsn_id': sku.hsn_id,
                'hsn_code': hsn_obj.hsn_code if hsn_obj else '',
                'gst_rate': gst_rate_val,
                'cgst': cgst_val,
                'sgst': sgst_val,
                'net_weight': str(item.net_weight),
                'gross_weight': str(item.gross_weight),
                'less_weight': less_str,
            }
            for bom in metal_boms[1:]:
                other_metal.append({
                    'id': f'metal-{bom.id}',
                    'metal_id': bom.metal_id,
                    'metal': bom.metal.metal_name if bom.metal else '',
                    'purity_id': bom.purity_id,
                    'purity': _purity_label(bom.purity),
                    'net_weight': str(bom.weight),
                })
        else:
            base_metal = {
                'color_id': sku.color_id,
                'color': sku.color.label if sku.color else '',
                'hsn_id': sku.hsn_id,
                'hsn_code': hsn_obj.hsn_code if hsn_obj else '',
                'gst_rate': gst_rate_val,
                'cgst': cgst_val,
                'sgst': sgst_val,
                'net_weight': str(item.net_weight),
                'gross_weight': str(item.gross_weight),
                'less_weight': less_str,
            }

        occasion_ids = list(
            ProductOccasion.objects.filter(product=item).values_list('occasion_id', flat=True)
        )

        stone_rows = list(
            ProductStone.objects.filter(product=item)
            .select_related(
                'stone',
                'stone__size_unit',
                'stone__stone_group',
                'stone__clarity',
                'stone__cut',
            )
            .order_by('id')
        )
        stone_details = [
            {
                'stone_id': s.stone_id,
                'stone_name': s.stone.stone_name if s.stone else '',
                'stone_code': s.stone.stone_code if s.stone else '',
                'master_spec': _stone_master_spec_readonly(s.stone) if s.stone else '',
                'quantity': str(s.quantity),
                'weight_carat': str(s.weight),
            }
            for s in stone_rows
        ]

        step1 = {
            'basic_info': {
                'style_name': group.style_name,
                'store_variant_name': item.store_variant_name or '',
                'product_code': (sku.product_code or '') if sku else '',
                'pattern_code': (sku.pattern_code or '') if sku else '',
                'style_code': sku.style_code or '',
                'sku_code': sku.sku_code or '',
                'category_id': group.category_id,
                'subcategory_id': group.subcategory_id,
                'gender_id': group.gender_id,
                'description': group.description or '',
                'customer_variant_name': item.customer_variant_name or '',
                'occasion_ids': occasion_ids,
            },
            'base_metal': base_metal,
            'other_metal': other_metal,
            'stone_details': stone_details,
        }

        # ── Step 2: model_rows (from BOM + attributes) + operational_charges ──
        all_boms = list(
            ProductBOM.objects.filter(product=item)
            .select_related('metal', 'purity', 'stone')
            .prefetch_related(
                'attributes',
                'attributes__making_category',
                'attributes__crafting_process',
                'attributes__method',
                'attributes__nature',
                'attributes__finishing',
                'attributes__charge_type',
            )
            .order_by('id')
        )

        model_rows = []
        for bom in all_boms:
            if bom.material_type == 'METAL':
                mk = _metal_material_key(bom.metal_id, bom.purity_id)
                metal_name = bom.metal.metal_name if bom.metal else ''
                purity_lbl = _purity_label(bom.purity)
                display = f"{metal_name} · {purity_lbl}" if (metal_name and purity_lbl) else (metal_name or purity_lbl)
                source = 'metal'
                ref_metal_id = str(bom.metal_id or '')
                ref_purity_id = str(bom.purity_id or '')
                ref_stone_id = ''
                ref_variant_id = ''
            else:
                mk = _stone_bom_material_key(bom.stone_id)
                stone_name = bom.stone.stone_name if bom.stone else ''
                spec = _stone_master_spec_readonly(bom.stone) if bom.stone else ''
                display = f"{stone_name} · {spec}" if (stone_name and spec) else (stone_name or spec)
                source = 'stone'
                ref_metal_id = ''
                ref_purity_id = ''
                ref_stone_id = str(bom.stone_id or '')
                ref_variant_id = ''

            attr = bom.attributes.first()
            # Resolve lookup labels from prefetched FK objects (no extra queries).
            def _lv_label(fk_obj):
                """LookupValue label from a prefetched FK, or empty string."""
                return fk_obj.label if fk_obj else ''

            model_rows.append({
                'id': f'model-{bom.id}',
                'material_key': mk,
                'source': source,
                'base_value': display,
                'base_value_id': str(bom.metal_id or bom.stone_id or ''),
                'weight': str(bom.weight),
                'ref_metal_id': ref_metal_id,
                'ref_purity_id': ref_purity_id,
                'ref_stone_id': ref_stone_id,
                'ref_variant_id': ref_variant_id,
                'model_id': '',
                'source_metal_entry_id': '',
                'source_stone_entry_id': '',
                'special_charge': (attr.special_charge or '') if attr else '',
                'charge_type_id': str(attr.charge_type_id or '') if attr else '',
                'charge_type_label': _lv_label(attr.charge_type) if attr else '',
                'mkg_category_id': str(attr.making_category_id or '') if attr else '',
                'mkg_category_label': _lv_label(attr.making_category) if attr else '',
                'crafting_process_id': str(attr.crafting_process_id or '') if attr else '',
                'crafting_process_label': _lv_label(attr.crafting_process) if attr else '',
                'method_id': str(attr.method_id or '') if attr else '',
                'method_label': _lv_label(attr.method) if attr else '',
                'nature_id': str(attr.nature_id or '') if attr else '',
                'nature_label': _lv_label(attr.nature) if attr else '',
                'finishing_id': str(attr.finishing_id or '') if attr else '',
                'finishing_label': _lv_label(attr.finishing) if attr else '',
            })

        op_charges = list(ProductOperationCharge.objects.filter(product=item).order_by('id'))
        operational_charges = [
            {
                'id': f'opchg-{op.id}',
                'component_label': op.component_name or '',
                'component_name': op.component_name or '',
                'value': op.charge_value or '',
                'description': op.description or '',
            }
            for op in op_charges
        ]

        step3_structure = {
            'model_rows': model_rows,
            'operational_charges': operational_charges,
            'operational_charge_applicable': item.charge_apply or '',
        }

        # ── Step 2: vendor (UI step 2) ──
        config_item = config_parent_item(item, ProductItem=ProductItem) or item
        step2_vendor = vendor_step_payload_for_item(
            config_item,
            ProductItemLinkedVendor=ProductItemLinkedVendor,
            VendorAddress=VendorAddress,
        )

        # ── Step 3: product structure (UI step 3) ──
        step3 = step3_structure

        # ── Step 4: parent / variant linking ──
        step4 = variant_step_payload_for_item(
            item,
            ProductItem=ProductItem,
            ProductBOM=ProductBOM,
            ProductItemLinkedVendor=ProductItemLinkedVendor,
            VendorAddress=VendorAddress,
        )

        # ── Step 5: sizes + patterns + occasion_ids (from ProductItem size columns) ──
        st = infer_item_size_type(
            size_number=item.size_number,
            size_mm=item.size_mm,
            height_mm=item.height_mm,
            width_mm=item.width_mm,
        ) or infer_sku_size_type_from_step4({'size_type': 'number', 'sizes': []})
        wiz_size_type = 'number' if st == SIZE_NUMBER else 'dimension'
        sz_row = {
            'size_type': wiz_size_type,
            'size_number': str(item.size_number) if item.size_number is not None else '',
            'size_mm': (
                format(item.size_mm, 'f').rstrip('0').rstrip('.') if item.size_mm is not None else ''
            ),
            'height_mm': (
                format(item.height_mm, 'f').rstrip('0').rstrip('.') if item.height_mm is not None else ''
            ),
            'width_mm': (
                format(item.width_mm, 'f').rstrip('0').rstrip('.') if item.width_mm is not None else ''
            ),
        }
        sizes = [sz_row]

        patterns_qs = list(ProductPattern.objects.filter(product=item).order_by('id'))
        patterns = [
            {
                'id': f'pat-{p.id}',
                'name': p.pattern_name or '',
                'pattern_name': p.pattern_name or '',
                'description': p.description or '',
            }
            for p in patterns_qs
        ]

        step5 = {
            'size_type': wiz_size_type,
            'sizes': sizes,
            'patterns': patterns,
            'occasion_ids': occasion_ids,
            'geometrical_shape_id': item.geometrical_shape_id,
        }

        # ── Step 6: images ──
        images_qs = list(ProductImage.objects.filter(product=item).order_by('id'))
        image_urls = [img.image_url for img in images_qs]
        primary_idx = next((i for i, img in enumerate(images_qs) if img.is_primary), 0)

        step6 = {
            'images': image_urls,
            'primary_image_idx': primary_idx,
        }

        return JsonResponse({
            'product_item_id': item.id,
            'current_step': 6,
            'draft_data': {
                'step1': step1,
                'step2': step2_vendor,
                'step3': step3,
                'step4': step4,
                'step5': step5,
                'step6': step6,
            },
        }, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@admin_auth()
def upload_item_media(request, product_item_id):
    """
    Upload images for a published product item to S3.
    POST /master/products/item/<product_item_id>/media/
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    denied = ensure_admin_permission(request, *MASTERS_PRODUCTS_WRITE_AUTH)
    if denied:
        return denied

    try:
        admin_user = get_admin_user_from_request(request)
        if not admin_user:
            return JsonResponse({'error': 'Authentication required'}, status=401)

        item = ProductItem.objects.filter(
            id=product_item_id,
            sku__product_group__created_by_id=admin_user.id,
        ).first()
        if not item:
            return JsonResponse({'error': 'Product not found or access denied'}, status=404)

        uploaded_files = request.FILES.getlist('images')
        if not uploaded_files:
            return JsonResponse({'error': 'No images provided'}, status=400)

        from shared.services.s3_service import upload_file_to_s3, build_public_object_url
        from datetime import datetime

        image_urls = []
        for file_obj in uploaded_files:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            object_name = f"Taranya/products/item_{product_item_id}/{timestamp}_{file_obj.name}"
            success = upload_file_to_s3(file_obj, object_name)
            if success:
                image_urls.append(build_public_object_url(object_name))
            else:
                return JsonResponse({'error': f'Failed to upload file: {file_obj.name}'}, status=500)

        return JsonResponse({
            'success': True,
            'images_count': len(image_urls),
            'image_urls': image_urls,
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@admin_auth()
@transaction.atomic
def update_product_item(request, product_item_id):
    """
    Update a published product item directly — no draft involved.
    PATCH /master/products/item/<product_item_id>/update/
    Accepts the same step1-5 payload format as publish_draft.
    """
    if request.method != 'PATCH':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    denied = ensure_admin_permission(request, *MASTERS_PRODUCTS_WRITE_AUTH)
    if denied:
        return denied

    try:
        admin_user = get_admin_user_from_request(request)
        if not admin_user:
            return JsonResponse({'error': 'Authentication required'}, status=401)

        item = (
            ProductItem.objects.select_related(
                'sku__product_group',
                'sku__color',
                'sku__hsn',
            )
            .filter(id=product_item_id, sku__product_group__created_by_id=admin_user.id)
            .first()
        )
        if not item:
            return JsonResponse({'error': 'Product not found or access denied'}, status=404)

        data = json.loads(request.body)
        step1 = normalize_step1(data.get('step1', {}))
        step2 = _resolve_structure_step(data)
        wizard = _parse_wizard_steps_payload(data)
        step_custom = wizard['customizable']
        step_vendor = wizard['vendor']
        step_attributes = wizard['attributes']
        step_media = wizard['media']
        raw_step1 = data.get('step1') or {}
        other_metal_rows = raw_step1.get('other_metal') if isinstance(raw_step1.get('other_metal'), list) else []

        if step_attributes.get('occasion_ids') and not step1.get('occasion_ids'):
            step1['occasion_ids'] = step_attributes['occasion_ids']
        if step_attributes.get('geometrical_shape_id') is not None and step1.get('geometrical_shape_id') in (None, ''):
            step1['geometrical_shape_id'] = step_attributes.get('geometrical_shape_id')

        merge_step4_stones_into_step1(step1, step_attributes)

        geo_shape_id = _geometrical_shape_id_from_steps(step1, step_attributes)

        old_sku = item.sku
        old_group = old_sku.product_group

        # ── ProductGroup: find-or-create, never mutate existing ──
        # Use incoming values with fallback to current group's values.
        grp_style = (step1.get('style_name') or '').strip() or old_group.style_name
        grp_cat_id = step1.get('category_id') or old_group.category_id
        grp_sub_id = step1.get('subcategory_id') or old_group.subcategory_id
        grp_gen_id = step1.get('gender_id') or old_group.gender_id

        group, _ = ProductGroup.objects.get_or_create(
            style_name=grp_style,
            category=Category.objects.get(id=grp_cat_id),
            subcategory=Subcategory.objects.get(id=grp_sub_id),
            gender=LookupValue.objects.get(id=grp_gen_id),
            defaults={
                'description': step1.get('description', '') or old_group.description,
                'created_by': admin_user,
                'updated_by': admin_user,
            },
        )

        # ── ProductSKU: find-or-create under the (possibly new) group ──
        sku_color_id = step1.get('color_id') or old_sku.color_id
        sku_hsn_id = step1.get('hsn_id') or old_sku.hsn_id
        sku_style_code = (step1.get('style_code') or '')[:100] or old_sku.style_code
        base_product_code = (step1.get('product_code') or '').strip() or old_sku.product_code or ''
        pattern_code = (step1.get('pattern_code') or '').strip() or (old_sku.pattern_code or '')
        if base_product_code and grp_cat_id and grp_sub_id:
            try:
                validate_product_code_mapping(base_product_code, grp_cat_id, grp_sub_id)
            except ValidationError as e:
                msg = e.messages[0] if getattr(e, 'messages', None) else str(e)
                return JsonResponse({'error': msg}, status=400)

        store_variant_name_val = (
            (step1.get('store_variant_name') or '').strip() or (item.store_variant_name or '')
        )
        if pattern_code and store_variant_name_val:
            try:
                validate_pattern_store_mapping(
                    pattern_code,
                    store_variant_name_val,
                    category_id=grp_cat_id,
                    subcategory_id=grp_sub_id,
                )
            except ValidationError as e:
                msg = e.messages[0] if getattr(e, 'messages', None) else str(e)
                return JsonResponse({'error': msg}, status=400)

        sku_code_val = build_sku_code_final(
            step1,
            step_vendor,
            product_group_id=group.id,
            fallback_sku_code=old_sku.sku_code,
        )

        # Pattern transfer: keep the same SKU row when the new sku_code is free
        # (product code stays GC; pattern FMC → HMC updates this SKU in place).
        sku_code_clash = (
            ProductSKU.objects.filter(sku_code=sku_code_val)
            .exclude(pk=old_sku.pk)
            .first()
        )
        if sku_code_clash:
            sku, sku_created = resolve_product_sku_by_code(
                sku_code_val=sku_code_val,
                product_group=group,
                color_id=sku_color_id,
                hsn_id=sku_hsn_id,
                base_product_code=base_product_code,
                pattern_code=pattern_code,
                style_code=sku_style_code,
                admin_user=admin_user,
            )
        else:
            sku = old_sku
            sku.product_group = group
            sku.sku_code = sku_code_val
            if base_product_code:
                sku.product_code = base_product_code
            sku.pattern_code = pattern_code
            if sku_color_id and sku.color_id != sku_color_id:
                sku.color = LookupValue.objects.get(id=sku_color_id)
            if sku_hsn_id and sku.hsn_id != sku_hsn_id:
                sku.hsn = HSNMaster.objects.get(id=sku_hsn_id)
            if sku_style_code:
                sku.style_code = sku_style_code
            sku.updated_by = admin_user
            sku.save()
            sku_created = False

        item.sku = sku

        # Update ProductItem
        def _str255(key):
            v = step1.get(key)
            if v is None:
                return None
            s = str(v).strip()
            return s[:255] if s else None

        if step1.get('store_variant_name') is not None:
            item.store_variant_name = _str255('store_variant_name') or ''
        if step1.get('customer_variant_name') is not None:
            item.customer_variant_name = _str255('customer_variant_name') or ''
        if step1.get('net_weight') not in (None, ''):
            item.net_weight = _dec_val(step1.get('net_weight'), '0')
        if step1.get('gross_weight') not in (None, ''):
            item.gross_weight = _dec_val(step1.get('gross_weight'), '0')
        charge_apply = step2.get('operational_charge_applicable')
        if charge_apply is not None:
            item.charge_apply = str(charge_apply).strip()[:20] or None

        item.geometrical_shape_id = geo_shape_id

        # Structured size from attributes step first row (optional on ProductItem)
        sizes_in = step_attributes.get('sizes') or []
        if sizes_in and isinstance(sizes_in[0], dict):
            try:
                size_kw = size_fields_from_wizard_row(sizes_in[0])
            except ValidationError as e:
                msg = e.messages[0] if getattr(e, 'messages', None) else str(e)
                return JsonResponse({'error': msg}, status=400)
            item.size_number = size_kw['size_number']
            item.size_mm = size_kw['size_mm']
            item.height_mm = size_kw['height_mm']
            item.width_mm = size_kw['width_mm']

        item.updated_by = admin_user
        item.save()

        # Wizard products are parent shells. Never promote an existing child variant to parent.
        is_child_row = bool(getattr(item, "parent_product_item_id", None))
        if not is_child_row:
            is_parent, parent_id = True, None
            variant_err = validate_variant_step(is_parent, parent_id)
            if variant_err:
                return JsonResponse({'error': variant_err}, status=400)
            try:
                apply_variant_flags_to_items(
                    [item],
                    is_parent=is_parent,
                    parent_id=parent_id,
                    ProductItem=ProductItem,
                )
            except ValueError as exc:
                return JsonResponse({'error': str(exc)}, status=400)

        config_item = config_parent_item(item, ProductItem=ProductItem) or item
        save_linked_vendors_on_item(
            config_item,
            step_vendor,
            admin_user=admin_user,
            ProductItemLinkedVendor=ProductItemLinkedVendor,
        )

        ProductBOM.objects.filter(product=item).delete()

        material_key_to_bom = {}
        mid_list = list(step1.get('metal_ids') or [])
        pid_list = list(step1.get('purity_ids') or [])
        if not mid_list and step1.get('metal_id') not in (None, ''):
            mid_list = [step1['metal_id']]
        if not pid_list and step1.get('purity_id') not in (None, ''):
            pid_list = [step1['purity_id']]
        n_pairs = min(len(mid_list), len(pid_list)) if mid_list and pid_list else 0
        for idx in range(n_pairs):
            mid = mid_list[idx]
            pid = pid_list[idx]
            if mid in (None, '') or pid in (None, ''):
                continue
            wsrc = step1.get('net_weight') if idx == 0 else (
                other_metal_rows[idx - 1].get('net_weight') if idx - 1 < len(other_metal_rows) else '0'
            )
            mk = _metal_material_key(mid, pid)
            bom = ProductBOM.objects.create(
                product=item,
                material_type='METAL',
                metal_id=mid,
                purity_id=pid,
                weight=_dec_val(wsrc, '0'),
                quantity=1,
                created_by=admin_user,
                updated_by=admin_user,
            )
            _register_bom_key(material_key_to_bom, mk, bom)
            _register_bom_key(material_key_to_bom, f"m-{mid}-{pid}", bom)

        # Replace stones
        ProductStone.objects.filter(product=item).delete()
        for stone_data in step1.get('stones') or []:
            if not isinstance(stone_data, dict):
                continue
            sid = stone_data.get('stone_id')
            if sid in (None, ''):
                continue
            qty = _int_safe(stone_data.get('quantity'), 1)
            wt = _dec_val(stone_data.get('weight_carat'), '0')
            ProductStone.objects.create(
                product=item,
                stone=Stone.objects.get(id=sid),
                quantity=qty,
                weight=wt,
                created_by=admin_user,
                updated_by=admin_user,
            )
            mk = _stone_bom_material_key(sid)
            bom = ProductBOM.objects.create(
                product=item,
                material_type='STONE',
                stone_id=sid,
                weight=wt,
                quantity=qty,
                created_by=admin_user,
                updated_by=admin_user,
            )
            _register_bom_key(material_key_to_bom, mk, bom)
            _register_bom_key(material_key_to_bom, f"s-{sid}-", bom)

        # Re-create attributes linked to BOM via material_key
        for row in step2.get('model_rows') or []:
            if not isinstance(row, dict):
                continue
            bom = _resolve_bom_for_model_row(row, material_key_to_bom)
            if not bom:
                continue

            ProductAttribute.objects.create(
                product_bom=bom,
                making_category=_lookup_value_or_none(row.get('mkg_category_id')),
                crafting_process=_lookup_value_or_none(row.get('crafting_process_id')),
                method=_lookup_value_or_none(row.get('method_id')),
                nature=_lookup_value_or_none(row.get('nature_id')),
                finishing=_lookup_value_or_none(row.get('finishing_id')),
                special_charge=(row.get('special_charge') or None),
                charge_type=_lookup_value_or_none(row.get('charge_type_id')),
                detail_number=_int_safe(row.get('detail_number'), 1),
                created_by=admin_user,
                updated_by=admin_user,
            )

        # Sync variant option cards → child product_items (always on parent shell)
        variant_parent = config_item if getattr(config_item, "is_parent_product", False) else item
        if not getattr(variant_parent, "is_parent_product", False):
            # Ensure parent flag before creating children
            variant_parent.is_parent_product = True
            variant_parent.parent_product_item_id = None
            variant_parent.save(update_fields=["is_parent_product", "parent_product_item_id"])
        try:
            materialize_variant_children(
                parent_item=variant_parent,
                step1=step1,
                step_custom=step_custom,
                step_vendor=step_vendor,
                product_group=group,
                admin_user=admin_user,
                ProductItem=ProductItem,
                ProductBOM=ProductBOM,
                ProductItemLinkedVendor=ProductItemLinkedVendor,
                resolve_product_sku_by_code=resolve_product_sku_by_code,
                build_sku_code_final=build_sku_code_final,
                save_linked_vendors_on_item=save_linked_vendors_on_item,
            )
        except ValueError as exc:
            return JsonResponse({'error': str(exc)}, status=400)

        # Replace images when step6.images is provided (including empty = clear all)
        if isinstance(step_media, dict) and "images" in step_media and isinstance(step_media.get("images"), list):
            ProductImage.objects.filter(product=item).delete()
            for i, image_url in enumerate(step_media["images"]):
                u = str(image_url).strip()
                if not u:
                    continue
                ProductImage.objects.create(
                    product=item,
                    image_url=u,
                    is_primary=(i == 0),
                    created_by=admin_user,
                    updated_by=admin_user,
                )

        # Replace operational charges
        ProductOperationCharge.objects.filter(product=item).delete()
        for op in step2.get('operational_charges') or []:
            if not isinstance(op, dict):
                continue
            label = (op.get('component_label') or op.get('component_name') or '').strip() or 'Charge'
            val = op.get('value')
            ProductOperationCharge.objects.create(
                product=item,
                component_name=label[:255],
                charge_value=str(val)[:50] if val not in (None, '') else None,
                description=(op.get('description') or '') or None,
                created_by=admin_user,
                updated_by=admin_user,
            )

        # Replace patterns
        ProductPattern.objects.filter(product=item).delete()
        for pat in step_attributes.get('patterns') or []:
            if not isinstance(pat, dict):
                continue
            pname = (pat.get('pattern_name') or pat.get('name') or '').strip()
            if not pname:
                continue
            ProductPattern.objects.create(
                product=item,
                pattern_name=pname[:255],
                description=(pat.get('description') or '') or None,
                created_by=admin_user,
                updated_by=admin_user,
            )

        # Replace occasions
        ProductOccasion.objects.filter(product=item).delete()
        for occasion_id in (step1.get('occasion_ids') or []):
            ProductOccasion.objects.create(
                product=item,
                occasion=LookupValue.objects.get(id=occasion_id),
                created_by=admin_user,
                updated_by=admin_user,
            )

        final_store_name = (item.store_variant_name or "").strip()
        if pattern_code and final_store_name:
            bind_pattern_store_variant(
                pattern_code,
                final_store_name,
                category_id=grp_cat_id,
                subcategory_id=grp_sub_id,
                admin_user=admin_user,
            )

        return JsonResponse({
            'success': True,
            'product_id': item.id,
            'product_code': sku.product_code or '',
            'product_group_id': group.id,
            'sku_code': sku.sku_code,
            'sku_reused': not sku_created,
        }, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@admin_auth()
def vendor_previous_bom(request):
    """
    GET /master/products/vendor-previous-bom/?vendor_id=&product_code=&pattern_code=&exclude_item_id=
    Last BOM this vendor used for the same product code + pattern code.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    denied = ensure_admin_permission(request, *MASTERS_PRODUCTS_READ_AUTH)
    if denied:
        return denied

    try:
        vendor_id = int(request.GET.get("vendor_id") or 0)
    except (TypeError, ValueError):
        vendor_id = 0
    product_code = (request.GET.get("product_code") or "").strip()
    pattern_code = (request.GET.get("pattern_code") or "").strip()
    if not vendor_id or not product_code or not pattern_code:
        return JsonResponse(
            {"error": "vendor_id, product_code and pattern_code are required."},
            status=400,
        )

    try:
        exclude_item_id = int(request.GET.get("exclude_item_id") or 0)
    except (TypeError, ValueError):
        exclude_item_id = 0

    links = (
        ProductItemLinkedVendor.objects.filter(
            vendor_id=vendor_id,
            product_item__sku__product_code__iexact=product_code,
            product_item__sku__pattern_code__iexact=pattern_code,
        )
        .select_related("product_item", "product_item__sku")
        .order_by("-product_item__system_updated_at", "-id")
    )
    if exclude_item_id:
        links = links.exclude(product_item_id=exclude_item_id)

    link = links.first()
    if not link:
        return JsonResponse({"found": False})

    item = link.product_item
    metal_boms = list(
        ProductBOM.objects.filter(product=item, material_type="METAL")
        .select_related("metal", "purity")
        .order_by("id")
    )
    stone_boms = list(
        ProductBOM.objects.filter(product=item, material_type="STONE")
        .select_related("stone")
        .order_by("id")
    )

    def _purity_label(p):
        if not p:
            return ""
        return (p.purity_name or p.type or "").strip()

    metal_entries = []
    for i, b in enumerate(metal_boms):
        metal_entries.append(
            {
                "id": f"prev-metal-{b.id}",
                "metal_id": str(b.metal_id or ""),
                "metal": b.metal.metal_name if b.metal else "",
                "purity_id": str(b.purity_id or ""),
                "purity": _purity_label(b.purity),
                "net_weight": str(b.weight) if b.weight is not None else "",
            }
        )

    stones = []
    for b in stone_boms:
        stones.append(
            {
                "stone_id": str(b.stone_id or ""),
                "stone_name": b.stone.stone_name if b.stone else "",
                "quantity": str(b.quantity or 1),
                "weight_carat": str(b.weight) if b.weight is not None else "",
            }
        )

    op_charges = list(ProductOperationCharge.objects.filter(product=item).order_by("id"))
    operational_charges = [
        {
            "id": f"prev-op-{op.id}",
            "component_label": op.component_name or "",
            "component_name": op.component_name or "",
            "value": op.charge_value or "",
            "description": op.description or "",
        }
        for op in op_charges
    ]

    less = item.less_weight

    return JsonResponse(
        {
            "found": True,
            "product_item_id": item.id,
            "gross_weight": str(item.gross_weight or ""),
            "net_weight": str(item.net_weight or ""),
            "less_weight": str(less) if less is not None else "",
            "metal_entries": metal_entries,
            "stones": stones,
            "operational_charges": operational_charges,
        }
    )

