from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from shared.models import CustomerAddress
from master.permissions.permission_checker import admin_auth, ensure_admin_permission
from master.permissions.section_auth import CUSTOMER_READ_AUTH, CUSTOMER_WRITE_AUTH


@api_view(["GET", "POST", "PUT", "DELETE"])
@admin_auth()
def admin_customer_address(request, customer_id=None):

    # -------------------- GET --------------------
    if request.method == "GET":
        denied = ensure_admin_permission(request, *CUSTOMER_READ_AUTH, "CRM_SCHEME_ENROLLMENT_CREATE")
        if denied:
            return denied

        if customer_id:
            try:
                address = CustomerAddress.objects.get(
                    customer=customer_id,
                    is_active=True
                )
            except CustomerAddress.DoesNotExist:
                return Response(
                    {"detail": "Address not found"},
                    status=status.HTTP_404_NOT_FOUND
                )

            return Response({
                "id": address.id,
                "customer_id": address.customer_id,
                "address_line1": address.address_line1,
                "address_line2": address.address_line2,
                "city": address.city,
                "state": address.state,
                "pincode": address.pincode,
                "country": address.country,
                "is_default": address.is_default,
            })

        # List addresses by customer_id
        if not customer_id:
            return Response(
                {"detail": "customer_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        addresses = CustomerAddress.objects.filter(
            customer_id=customer_id,
            is_active=True
        ).values(
            "id", "address_line1", "address_line2",
            "city", "state", "pincode",
            "country", "is_default"
        )

        return Response(list(addresses))

    # -------------------- POST --------------------
    if request.method == "POST":
        denied = ensure_admin_permission(request, *CUSTOMER_WRITE_AUTH, "CRM_SCHEME_ENROLLMENT_CREATE")
        if denied:
            return denied

        if not customer_id:
            return Response(
                {"detail": "customer_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data
        is_default = data.get("is_default", False)

        # First address must be default
        if not CustomerAddress.objects.filter(
            customer_id=customer_id,
            is_active=True
        ).exists():
            is_default = True

        address = CustomerAddress.objects.create(
            customer_id=customer_id,
            address_line1=data.get("address_line1"),
            address_line2=data.get("address_line2"),
            city=data.get("city"),
            state=data.get("state"),
            pincode=data.get("pincode"),
            country=data.get("country", "India"),
            is_default=is_default,
            is_active=True
        )

        # Unset other defaults
        if is_default:
            CustomerAddress.objects.filter(
                customer_id=customer_id,
                is_active=True
            ).exclude(id=address.id).update(is_default=False)

        return Response({
            "id": address.id,
            "customer_id": address.customer_id,
            "address_line1": address.address_line1,
            "address_line2": address.address_line2,
            "city": address.city,
            "state": address.state,
            "pincode": address.pincode,
            "country": address.country,
            "is_default": address.is_default,
        }, status=status.HTTP_201_CREATED)

    # -------------------- PUT --------------------
    if request.method == "PUT":
        denied = ensure_admin_permission(request, *CUSTOMER_WRITE_AUTH, "CRM_SCHEME_ENROLLMENT_CREATE")
        if denied:
            return denied

        if not customer_id:
            return Response(
                {"detail": "customer id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data

        # Try to get an existing active address
        address = CustomerAddress.objects.filter(
            customer_id=customer_id,
            is_active=True
        ).first()

        is_creating = False

        # If no address exists → create one
        if not address:
            is_creating = True

            is_default = data.get("is_default", True)

            address = CustomerAddress.objects.create(
                customer_id=customer_id,
                address_line1=data.get("address_line_1"),
                address_line2=data.get("address_line_2"),
                city=data.get("city"),
                state=data.get("state"),
                pincode=data.get("pincode"),
                country=data.get("country", "India"),
                is_default=is_default,
                is_active=True
            )

            # Unset other defaults if needed
            if is_default:
                CustomerAddress.objects.filter(
                    customer_id=customer_id,
                    is_active=True
                ).exclude(id=address.id).update(is_default=False)

            return Response({
                "message": "Address created successfully",
                "address_id": address.id
            }, status=status.HTTP_201_CREATED)

        # ---------------- UPDATE EXISTING ----------------
        address.address_line1 = data.get("address_line_1", address.address_line1)
        address.address_line2 = data.get("address_line_2", address.address_line2)
        address.city = data.get("city", address.city)
        address.state = data.get("state", address.state)
        address.pincode = data.get("pincode", address.pincode)
        address.country = data.get("country", address.country)

        is_default = data.get("is_default", address.is_default)

        if is_default and not address.is_default:
            CustomerAddress.objects.filter(
                customer_id=customer_id,
                is_active=True
            ).exclude(id=address.id).update(is_default=False)

        address.is_default = is_default
        address.save()

        return Response({
            "message": "Address updated successfully",
            "address_id": address.id
        })

    # -------------------- DELETE --------------------
    if request.method == "DELETE":
        denied = ensure_admin_permission(request, *CUSTOMER_WRITE_AUTH)
        if denied:
            return denied

        if not customer_id:
            return Response(
                {"detail": "customer id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            address = CustomerAddress.objects.get(
                customer=customer_id,
                is_active=True
            )
        except CustomerAddress.DoesNotExist:
            return Response(
                {"detail": "Address not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        address.is_active = False
        address.save()

        return Response(
            {"message": "Address deleted successfully"},
            status=status.HTTP_204_NO_CONTENT
        )
