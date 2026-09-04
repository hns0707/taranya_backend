import json

from django.db import IntegrityError
from django.http import JsonResponse
from rest_framework.decorators import api_view

from master.permissions.permission_checker import admin_auth
from shared.models import HSNMaster


def _admin_user(request):
    return getattr(request, "admin_user", None) or (
        request.user if request.user.is_authenticated else None
    )


@api_view(["POST"])
@admin_auth("CRM_MASTERS_HSN_MASTER_CREATE")
def create_hsn(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:
        data = json.loads(request.body)

        hsn = HSNMaster.objects.create(
            hsn_code=data["hsn_code"],
            description=data.get("description"),
            gst_rate=data["gst_rate"],
            category=data.get("category"),
            gst_type=data.get("gst_type", "GST"),
            cgst_rate=data.get("cgst_rate", 0),
            sgst_rate=data.get("sgst_rate", 0),
            igst_rate=data.get("igst_rate", 0),
            making_charge_taxable=data.get("making_charge_taxable", True),
            stone_tax_applicable=data.get("stone_tax_applicable", False),
            cess_percentage=data.get("cess_percentage", 0),
            remarks=data.get("remarks"),
            created_by=_admin_user(request),
        )

        return JsonResponse({
            "message": "HSN created successfully",
            "id": hsn.id
        }, status=201)

    except KeyError as e:
        return JsonResponse({"error": f"Missing field: {str(e)}"}, status=400)

    except IntegrityError:
        return JsonResponse({"error": "HSN code already exists"}, status=400)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["GET"])
@admin_auth("CRM_MASTERS_HSN_MASTER_VIEW")
def list_hsn(request):
    try:
        is_active = request.GET.get("is_active", "true").lower() == "true"

        hsn_list = HSNMaster.objects.filter(is_active=is_active).values()

        return JsonResponse(list(hsn_list), safe=False, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["GET"])
@admin_auth("CRM_MASTERS_HSN_MASTER_VIEW", "CRM_MASTERS_HSN_MASTER_UPDATE")
def get_hsn(request, hsn_id):
    try:
        hsn = HSNMaster.objects.get(id=hsn_id)

        return JsonResponse({
            "id": hsn.id,
            "hsn_code": hsn.hsn_code,
            "description": hsn.description,
            "gst_rate": float(hsn.gst_rate),
            "category": hsn.category,
            "gst_type": hsn.gst_type,
            "cgst_rate": float(hsn.cgst_rate),
            "sgst_rate": float(hsn.sgst_rate),
            "igst_rate": float(hsn.igst_rate),
            "making_charge_taxable": hsn.making_charge_taxable,
            "stone_tax_applicable": hsn.stone_tax_applicable,
            "cess_percentage": float(hsn.cess_percentage),
            "remarks": hsn.remarks,
            "is_active": hsn.is_active
        }, status=200)

    except HSNMaster.DoesNotExist:
        return JsonResponse({"error": "HSN not found"}, status=404)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["PUT"])
@admin_auth("CRM_MASTERS_HSN_MASTER_UPDATE")
def update_hsn(request, hsn_id):
    if request.method != "PUT":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:
        data = json.loads(request.body)

        hsn = HSNMaster.objects.get(id=hsn_id)

        hsn.hsn_code = data.get("hsn_code", hsn.hsn_code)
        hsn.description = data.get("description", hsn.description)
        hsn.gst_rate = data.get("gst_rate", hsn.gst_rate)
        hsn.category = data.get("category", hsn.category)
        hsn.gst_type = data.get("gst_type", hsn.gst_type)

        hsn.cgst_rate = data.get("cgst_rate", hsn.cgst_rate)
        hsn.sgst_rate = data.get("sgst_rate", hsn.sgst_rate)
        hsn.igst_rate = data.get("igst_rate", hsn.igst_rate)

        hsn.making_charge_taxable = data.get("making_charge_taxable", hsn.making_charge_taxable)
        hsn.stone_tax_applicable = data.get("stone_tax_applicable", hsn.stone_tax_applicable)

        hsn.cess_percentage = data.get("cess_percentage", hsn.cess_percentage)
        hsn.remarks = data.get("remarks", hsn.remarks)

        hsn.updated_by = _admin_user(request)

        hsn.save()

        return JsonResponse({"message": "HSN updated successfully"}, status=200)

    except HSNMaster.DoesNotExist:
        return JsonResponse({"error": "HSN not found"}, status=404)

    except IntegrityError:
        return JsonResponse({"error": "HSN code already exists"}, status=400)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["DELETE"])
@admin_auth("CRM_MASTERS_HSN_MASTER_DELETE")
def delete_hsn(request, hsn_id):
    if request.method != "DELETE":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:
        hsn = HSNMaster.objects.get(id=hsn_id)

        hsn.is_active = False
        hsn.updated_by = _admin_user(request)
        hsn.save()

        return JsonResponse({"message": "HSN deactivated successfully"}, status=200)

    except HSNMaster.DoesNotExist:
        return JsonResponse({"error": "HSN not found"}, status=404)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
