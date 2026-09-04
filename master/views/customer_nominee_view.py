"""
Views for customer nominees in the master app.
"""
from rest_framework import generics, status
from rest_framework.response import Response
from shared.models import CustomerScheme, CustomerNominee
from master.permissions.permission_checker import admin_auth
from django.utils.decorators import method_decorator


class AdminSchemeNomineeListCreateView(generics.ListCreateAPIView):
    """
    API endpoint to list and create nominees for a specific scheme (admin).
    """
    @method_decorator(admin_auth("CRM_CUSTOMER_LIST_UPDATE"))
    def get(self, request, *args, **kwargs):
        """List nominees for the scheme."""
        scheme_id = kwargs['scheme_id']
        nominees = CustomerNominee.objects.filter(customer_scheme_id=scheme_id).values(
            'id', 'full_name', 'relationship', 'mobile', 'share_percentage'
        )
        return Response(list(nominees))

    @method_decorator(admin_auth("CRM_CUSTOMER_LIST_UPDATE"))
    def post(self, request, *args, **kwargs):
        """Create a nominee for the scheme."""
        scheme_id = kwargs['scheme_id']
        try:
            scheme = CustomerScheme.objects.get(id=scheme_id)
        except CustomerScheme.DoesNotExist:
            return Response({"error": "Scheme not found"}, status=status.HTTP_404_NOT_FOUND)

        data = request.data

        # Validate share percentage
        share_percentage = data.get('share_percentage', 100)
        total_share = sum(nom.share_percentage for nom in CustomerNominee.objects.filter(customer_scheme_id=scheme_id)) + share_percentage
        if total_share > 100:
            return Response({"error": "Total share percentage cannot exceed 100%"}, status=status.HTTP_400_BAD_REQUEST)

        nominee = CustomerNominee.objects.create(
            customer_scheme_id=scheme_id,
            full_name=data.get('full_name'),
            relationship=data.get('relationship'),
            mobile=data.get('mobile'),
            share_percentage=share_percentage,
        )

        return Response({
            "id": nominee.id,
            "full_name": nominee.full_name,
            "relationship": nominee.relationship,
            "mobile": nominee.mobile,
            "share_percentage": nominee.share_percentage,
        }, status=status.HTTP_201_CREATED)


class AdminNomineeRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    """
    API endpoint to retrieve, update, and delete a nominee (admin).
    """
    queryset = CustomerNominee.objects.all()

    @method_decorator(admin_auth("CRM_CUSTOMER_LIST_UPDATE"))
    def get(self, request, *args, **kwargs):
        """Retrieve a specific nominee."""
        nominee = self.get_object()
        data = {
            "id": nominee.id,
            "full_name": nominee.full_name,
            "relationship": nominee.relationship,
            "mobile": nominee.mobile,
            "share_percentage": nominee.share_percentage,
        }
        return Response(data)

    @method_decorator(admin_auth("CRM_CUSTOMER_LIST_UPDATE"))
    def put(self, request, *args, **kwargs):
        """Update a specific nominee."""
        nominee = self.get_object()
        data = request.data

        # Validate share percentage if changed
        share_percentage = data.get('share_percentage', nominee.share_percentage)
        if share_percentage != nominee.share_percentage:
            other_nominees = CustomerNominee.objects.filter(
                customer_scheme=nominee.customer_scheme
            ).exclude(id=nominee.id)
            total_other_share = sum(nom.share_percentage for nom in other_nominees)
            if total_other_share + share_percentage > 100:
                return Response({"error": "Total share percentage cannot exceed 100%"}, status=status.HTTP_400_BAD_REQUEST)

        nominee.full_name = data.get('full_name', nominee.full_name)
        nominee.relationship = data.get('relationship', nominee.relationship)
        nominee.mobile = data.get('mobile', nominee.mobile)
        nominee.share_percentage = share_percentage
        nominee.save()

        return self.get(request, *args, **kwargs)

    @method_decorator(admin_auth("CRM_CUSTOMER_LIST_UPDATE"))
    def delete(self, request, *args, **kwargs):
        """Delete a nominee."""
        nominee = self.get_object()
        nominee.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)