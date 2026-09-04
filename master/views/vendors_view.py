import json
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view
from shared.models import Vendor,VendorBankDetails,VendorAddress


@api_view(['POST'])
def create_vendor(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:
        data = json.loads(request.body)

        vendor = Vendor.objects.create(
            vendor_code=data.get("vendor_code"),
            vendor_name=data.get("vendor_name"),
            contact_person=data.get("contact_person"),
            email=data.get("email"),
            phone=data.get("phone"),
            gst_number=data.get("gst_number"),
            pan_number=data.get("pan_number")
        )

        return JsonResponse({
            "message": "Vendor created successfully",
            "vendor_id": vendor.id
        }, status=201)

    except Exception as e:
        return JsonResponse({
            "error": str(e)
        }, status=500)

@api_view(['POST'])
@csrf_exempt
@transaction.atomic
def create_vendor_with_address(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:
        data = json.loads(request.body) if request.body else {}

        vendor_data = data.get("vendor") or data.get("vendor_details") or {}
        address_data = data.get("address") or data.get("address_details") or {}

        if not vendor_data:
            return JsonResponse({"error": "Vendor details are required"}, status=400)

        if not address_data:
            return JsonResponse({"error": "Address details are required"}, status=400)

        required_vendor_fields = ["vendor_code", "vendor_name"]
        missing_vendor_fields = [field for field in required_vendor_fields if not vendor_data.get(field)]
        if missing_vendor_fields:
            return JsonResponse(
                {"error": f"Missing vendor fields: {', '.join(missing_vendor_fields)}"},
                status=400
            )

        required_address_fields = ["address_line1", "city", "state", "country", "pincode"]
        missing_address_fields = [field for field in required_address_fields if not address_data.get(field)]
        if missing_address_fields:
            return JsonResponse(
                {"error": f"Missing address fields: {', '.join(missing_address_fields)}"},
                status=400
            )

        vendor = Vendor.objects.create(
            vendor_code=vendor_data.get("vendor_code"),
            vendor_name=vendor_data.get("vendor_name"),
            contact_person=vendor_data.get("contact_person"),
            email=vendor_data.get("email"),
            phone=vendor_data.get("phone"),
            gst_number=vendor_data.get("gst_number"),
            pan_number=vendor_data.get("pan_number")
        )

        address = VendorAddress.objects.create(
            vendor_id=vendor.id,
            address_line1=address_data.get("address_line1"),
            address_line2=address_data.get("address_line2"),
            city=address_data.get("city"),
            state=address_data.get("state"),
            country=address_data.get("country"),
            pincode=address_data.get("pincode")
        )

        return JsonResponse({
            "message": "Vendor and address created successfully",
            "vendor_id": vendor.id,
            "address_id": address.id,
            "vendor": {
                "id": vendor.id,
                "vendor_code": vendor.vendor_code,
                "vendor_name": vendor.vendor_name,
                "contact_person": vendor.contact_person or "",
                "email": vendor.email or "",
                "phone": vendor.phone or "",
                "gst_number": vendor.gst_number or "",
                "pan_number": vendor.pan_number or "",
                "is_active": vendor.is_active,
            },
        }, status=201)

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    except Exception as e:
        return JsonResponse({
            "error": str(e)
        }, status=500)

@api_view(['GET'])
def get_vendors(request):
    if request.method != "GET":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:
        vendors = Vendor.objects.all()

        data = []
        for v in vendors:
            data.append({
                "id": v.id,
                "vendor_code": v.vendor_code,
                "vendor_name": v.vendor_name,
                "contact_person": v.contact_person,
                "email": v.email,
                "phone": v.phone,
                "gst_number": v.gst_number,
                "is_active": v.is_active
            })

        return JsonResponse({
            "vendors": data
        })

    except Exception as e:
        return JsonResponse({
            "error": str(e)
        }, status=500)

@api_view(['PUT'])
@csrf_exempt
def update_vendor(request, vendor_id):

    if request.method != "PUT":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:
        data = json.loads(request.body)

        vendor = Vendor.objects.get(id=vendor_id)

        vendor.vendor_name = data.get("vendor_name", vendor.vendor_name)
        vendor.contact_person = data.get("contact_person", vendor.contact_person)
        vendor.email = data.get("email", vendor.email)
        vendor.phone = data.get("phone", vendor.phone)
        vendor.gst_number = data.get("gst_number", vendor.gst_number)
        vendor.pan_number = data.get("pan_number", vendor.pan_number)

        vendor.save()

        return JsonResponse({
            "message": "Vendor updated successfully"
        })

    except Vendor.DoesNotExist:
        return JsonResponse({
            "error": "Vendor not found"
        }, status=404)

    except Exception as e:
        return JsonResponse({
            "error": str(e)
        }, status=500)

@api_view(['DELETE'])
@csrf_exempt
def delete_vendor(request, vendor_id):

    if request.method != "DELETE":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:
        vendor = Vendor.objects.get(id=vendor_id)
        vendor.delete()

        return JsonResponse({
            "message": "Vendor deleted successfully"
        })

    except Vendor.DoesNotExist:
        return JsonResponse({
            "error": "Vendor not found"
        }, status=404)

    except Exception as e:
        return JsonResponse({
            "error": str(e)
        }, status=500)


@api_view(['POST'])
@csrf_exempt
def add_vendor_bank(request):

    if request.method != "POST":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:
        data = json.loads(request.body)

        vendor_id = data.get("vendor_id")

        if not Vendor.objects.filter(id=vendor_id).exists():
            return JsonResponse({"error": "Vendor not found"}, status=404)

        bank = VendorBankDetails.objects.create(
            vendor_id=vendor_id,
            account_holder_name=data.get("account_holder_name"),
            account_number=data.get("account_number"),
            bank_name=data.get("bank_name"),
            ifsc_code=data.get("ifsc_code"),
            branch_name=data.get("branch_name")
        )

        return JsonResponse({
            "message": "Bank added successfully",
            "bank_id": bank.id
        }, status=201)

    except Exception as e:
        return JsonResponse({
            "error": str(e)
        }, status=500)

@api_view(['GET'])
def get_vendor_banks(request, vendor_id):

    if request.method != "GET":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:

        if not Vendor.objects.filter(id=vendor_id).exists():
            return JsonResponse({"error": "Vendor not found"}, status=404)

        banks = VendorBankDetails.objects.filter(vendor_id=vendor_id)

        data = []

        for b in banks:
            data.append({
                "id": b.id,
                "account_holder_name": b.account_holder_name,
                "account_number": b.account_number,
                "bank_name": b.bank_name,
                "ifsc_code": b.ifsc_code,
                "branch_name": b.branch_name
            })

        return JsonResponse({
            "banks": data
        })

    except Exception as e:
        return JsonResponse({
            "error": str(e)
        }, status=500)

@api_view(['PUT'])
@csrf_exempt
def update_vendor_bank(request, bank_id):
    if request.method == "PUT":
        try:
            data = json.loads(request.body)

            bank = VendorBankDetails.objects.get(id=bank_id)

            bank.account_holder_name = data.get("account_holder_name", bank.account_holder_name)
            bank.account_number = data.get("account_number", bank.account_number)
            bank.bank_name = data.get("bank_name", bank.bank_name)
            bank.ifsc_code = data.get("ifsc_code", bank.ifsc_code)
            bank.branch_name = data.get("branch_name", bank.branch_name)

            bank.save()

            return JsonResponse({
                "message": "Vendor bank updated successfully"
            })

        except VendorBankDetails.DoesNotExist:
            return JsonResponse({"error": "Bank record not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def delete_vendor_bank(request, bank_id):
    if request.method == "DELETE":
        try:
            bank = VendorBankDetails.objects.get(id=bank_id)
            bank.delete()

            return JsonResponse({
                "message": "Vendor bank deleted successfully"
            })

        except VendorBankDetails.DoesNotExist:
            return JsonResponse({"error": "Bank record not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)


@api_view(['POST'])
@csrf_exempt
def add_vendor_address(request):

    if request.method != "POST":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:
        data = json.loads(request.body)

        vendor_id = data.get("vendor_id")

        if not Vendor.objects.filter(id=vendor_id).exists():
            return JsonResponse({"error": "Vendor not found"}, status=404)

        address = VendorAddress.objects.create(
            vendor_id=vendor_id,
            address_line1=data.get("address_line1"),
            address_line2=data.get("address_line2"),
            city=data.get("city"),
            state=data.get("state"),
            country=data.get("country"),
            pincode=data.get("pincode")
        )

        return JsonResponse({
            "message": "Address added successfully",
            "address_id": address.id
        }, status=201)

    except Exception as e:
        return JsonResponse({
            "error": str(e)
        }, status=500)

@api_view(['GET'])
def get_vendor_addresses(request, vendor_id):

    if request.method != "GET":
        return JsonResponse({"error": "Invalid HTTP method"}, status=405)

    try:

        if not Vendor.objects.filter(id=vendor_id).exists():
            return JsonResponse({"error": "Vendor not found"}, status=404)

        addresses = VendorAddress.objects.filter(vendor_id=vendor_id)

        data = []

        for a in addresses:
            data.append({
                "id": a.id,
                "address_line1": a.address_line1,
                "address_line2": a.address_line2,
                "city": a.city,
                "state": a.state,
                "country": a.country,
                "pincode": a.pincode
            })

        return JsonResponse({
            "addresses": data
        })

    except Exception as e:
        return JsonResponse({
            "error": str(e)
        }, status=500)

@api_view(['PUT'])
@csrf_exempt
def update_vendor_address(request, address_id):
    if request.method == "PUT":
        try:
            data = json.loads(request.body)

            address = VendorAddress.objects.get(id=address_id)

            address.address_line1 = data.get("address_line1", address.address_line1)
            address.address_line2 = data.get("address_line2", address.address_line2)
            address.city = data.get("city", address.city)
            address.state = data.get("state", address.state)
            address.country = data.get("country", address.country)
            address.pincode = data.get("pincode", address.pincode)

            address.save()

            return JsonResponse({
                "message": "Vendor address updated successfully"
            })

        except VendorAddress.DoesNotExist:
            return JsonResponse({"error": "Address not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def delete_vendor_address(request, address_id):
    if request.method == "DELETE":
        try:
            address = VendorAddress.objects.get(id=address_id)
            address.delete()

            return JsonResponse({
                "message": "Vendor address deleted successfully"
            })

        except VendorAddress.DoesNotExist:
            return JsonResponse({"error": "Address not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)
        
@api_view(['GET'])     
def get_vendor_profile(request, vendor_id):
    if request.method == "GET":
        try:

            vendor = Vendor.objects.get(id=vendor_id)

            vendor_data = {
                "id": vendor.id,
                "vendor_code": vendor.vendor_code,
                "vendor_name": vendor.vendor_name,
                "contact_person": vendor.contact_person,
                "email": vendor.email,
                "phone": vendor.phone,
                "gst_number": vendor.gst_number,
                "pan_number": vendor.pan_number
            }

            # Vendor Banks
            banks = VendorBankDetails.objects.filter(vendor_id=vendor_id)

            bank_list = []

            for b in banks:
                bank_list.append({
                    "id": b.id,
                    "account_holder_name": b.account_holder_name,
                    "account_number": b.account_number,
                    "bank_name": b.bank_name,
                    "ifsc_code": b.ifsc_code,
                    "branch_name": b.branch_name
                })

            # Vendor Addresses
            addresses = VendorAddress.objects.filter(vendor_id=vendor_id)

            address_list = []

            for a in addresses:
                address_list.append({
                    "id": a.id,
                    "address_line1": a.address_line1,
                    "address_line2": a.address_line2,
                    "city": a.city,
                    "state": a.state,
                    "country": a.country,
                    "pincode": a.pincode
                })

            response = {
                "vendor": vendor_data,
                "banks": bank_list,
                "addresses": address_list
            }

            return JsonResponse(response)

        except Vendor.DoesNotExist:
            return JsonResponse({"error": "Vendor not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)
