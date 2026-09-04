"""
Views for customer addresses in the customer app.
"""
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from shared.models import CustomerAddress
from customer.auth.customer_auth import CustomerAuthentication


class CustomerAddressListCreateView(generics.ListCreateAPIView):
    """
    API endpoint to list and create customer addresses.
    """
    authentication_classes = [CustomerAuthentication]

    def get_queryset(self):
        """Return active addresses for the customer."""
        return CustomerAddress.objects.filter(customer=self.request.user, is_active=True)

    def list(self, request, *args, **kwargs):
        """List all active addresses for the customer."""
        addresses = self.get_queryset()
        data = [{
            "id": addr.id,
            "address_line1": addr.address_line1,
            "address_line2": addr.address_line2,
            "city": addr.city,
            "state": addr.state,
            "pincode": addr.pincode,
            "country": addr.country,
            "is_default": addr.is_default,
        } for addr in addresses]
        return Response(data)

    def create(self, request, *args, **kwargs):
        """Create a new address for the customer."""
        data = request.data

        # If this is the first address or marked as default, set as default
        is_default = data.get('is_default', False)
        if not CustomerAddress.objects.filter(customer=request.user, is_active=True).exists():
            is_default = True

        address = CustomerAddress.objects.create(
            customer=request.user,
            address_line1=data.get('address_line1'),
            address_line2=data.get('address_line2'),
            city=data.get('city'),
            state=data.get('state'),
            pincode=data.get('pincode'),
            country=data.get('country', 'India'),
            is_default=is_default,
            is_active=True
        )

        # If set as default, unset other defaults
        if is_default:
            CustomerAddress.objects.filter(customer=request.user, is_active=True).exclude(id=address.id).update(is_default=False)

        return Response({
            "id": address.id,
            "address_line1": address.address_line1,
            "address_line2": address.address_line2,
            "city": address.city,
            "state": address.state,
            "pincode": address.pincode,
            "country": address.country,
            "is_default": address.is_default,
        }, status=status.HTTP_201_CREATED)


class CustomerAddressRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    """
    API endpoint to retrieve, update, and delete a customer address.
    """
    authentication_classes = [CustomerAuthentication]

    def get_queryset(self):
        """Return active addresses for the customer."""
        return CustomerAddress.objects.filter(customer=self.request.user, is_active=True)

    def retrieve(self, request, *args, **kwargs):
        """Retrieve a specific address."""
        address = self.get_object()
        data = {
            "id": address.id,
            "address_line1": address.address_line1,
            "address_line2": address.address_line2,
            "city": address.city,
            "state": address.state,
            "pincode": address.pincode,
            "country": address.country,
            "is_default": address.is_default,
        }
        return Response(data)

    def update(self, request, *args, **kwargs):
        """Update a specific address."""
        address = self.get_object()
        data = request.data

        address.address_line1 = data.get('address_line1', address.address_line1)
        address.address_line2 = data.get('address_line2', address.address_line2)
        address.city = data.get('city', address.city)
        address.state = data.get('state', address.state)
        address.pincode = data.get('pincode', address.pincode)
        address.country = data.get('country', address.country)

        is_default = data.get('is_default', address.is_default)
        if is_default and not address.is_default:
            # Unset other defaults
            CustomerAddress.objects.filter(customer=request.user, is_active=True).exclude(id=address.id).update(is_default=False)
        address.is_default = is_default

        address.save()
        return self.retrieve(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """Soft delete an address."""
        address = self.get_object()
        address.is_active = False
        address.save()
        return Response(status=status.HTTP_204_NO_CONTENT)