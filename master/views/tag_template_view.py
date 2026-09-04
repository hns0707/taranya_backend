"""
Single render endpoint for the jewellery tag print flow.
Layout is hardcoded in `shared.services.tag_print` (no DB-driven templates).
"""
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from shared.models import ProductTag
from shared.services.tag_print import render_tag


@api_view(["GET"])
@admin_auth("CRM_MASTERS")
def render_tag_view(request, tag_id: int):
    """
    GET /master/tag-templates/render/<tag_id>/
    Returns the hardcoded tag layout HTML + CSS with placeholders substituted
    against the chosen ProductTag.
    """
    tag = get_object_or_404(
        ProductTag.objects.select_related(
            "product_item",
            "product_item__sku",
            "product_item__sku__product_group",
            "product_item__sku__product_group__subcategory",
        ),
        pk=tag_id,
    )
    hallmark_param = (request.GET.get("hallmark") or "").strip().lower()
    if hallmark_param in ("1", "true", "yes"):
        show_hallmark = True
    elif hallmark_param in ("0", "false", "no"):
        show_hallmark = False
    else:
        show_hallmark = bool((tag.huid or "").strip())
    return Response(
        {"data": render_tag(tag, show_hallmark=show_hallmark)},
        status=status.HTTP_200_OK,
    )
