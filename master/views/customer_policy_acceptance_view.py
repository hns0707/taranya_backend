"""
Views for customer policy acceptance in the master app.
"""
from rest_framework import generics, status
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from shared.models import CustomerPolicyAcceptance
from master.permissions.permission_checker import admin_auth
from django.utils.decorators import method_decorator

class CustomerPolicyAcceptanceListCreateView(generics.ListCreateAPIView):
    """
    API endpoint to list and create customer policy acceptances.
    """
    queryset = CustomerPolicyAcceptance.objects.all()
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['page_key']
    search_fields = ['page_key']
    ordering_fields = ['page_key', 'version', 'accepted_at']
    ordering = ['-accepted_at']
    
    @method_decorator(admin_auth("CRM_MASTERS_TERMS_CONDITIONS_VIEW", "customer_policy_acceptance.view"))
    def get(self, request, *args, **kwargs):
        """List all customer policy acceptances with pagination, filtering and ordering."""
        queryset = self.filter_queryset(self.get_queryset())
        
        # Apply pagination
        page = self.paginate_queryset(queryset)
        if page is not None:
            data = [{
                "id": acceptance.id,
                "customer": acceptance.customer.id,
                "page_key": acceptance.page_key,
                "version": acceptance.version,
                "accepted_at": acceptance.accepted_at,
            } for acceptance in page]
            return self.get_paginated_response(data)
        
        data = [{
            "id": acceptance.id,
            "customer": acceptance.customer.id,
            "page_key": acceptance.page_key,
            "version": acceptance.version,
            "accepted_at": acceptance.accepted_at,
        } for acceptance in acceptances]
        return Response(data)
    
    @method_decorator(admin_auth("CRM_MASTERS_TERMS_CONDITIONS_CREATE", "customer_policy_acceptance.create"))
    def post(self, request, *args, **kwargs):
        """Create a new customer policy acceptance."""
        data = request.data
        acceptance = CustomerPolicyAcceptance.objects.create(
            customer_id=data.get("customer_id"),
            page_key=data.get("page_key"),
            version=data.get("version"),
        )
        return Response({
            "message": "Customer policy acceptance created successfully",
            "data": {
                "id": acceptance.id,
                "customer": acceptance.customer.id,
                "page_key": acceptance.page_key,
                "version": acceptance.version,
                "accepted_at": acceptance.accepted_at,
            }
        }, status=status.HTTP_201_CREATED)

class CustomerPolicyAcceptanceRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    """
    API endpoint to retrieve, update, or delete a specific customer policy acceptance.
    """
    @method_decorator(admin_auth("CRM_MASTERS_TERMS_CONDITIONS_VIEW", "customer_policy_acceptance.view"))
    def get(self, request, *args, **kwargs):
        """Retrieve a specific customer policy acceptance."""
        acceptance = CustomerPolicyAcceptance.objects.get(id=kwargs.get("pk"))
        data = {
            "id": acceptance.id,
            "customer": acceptance.customer.id,
            "page_key": acceptance.page_key,
            "version": acceptance.version,
            "accepted_at": acceptance.accepted_at,
        }
        return Response(data)
    
    @method_decorator(admin_auth("CRM_MASTERS_TERMS_CONDITIONS_UPDATE", "customer_policy_acceptance.edit"))
    def put(self, request, *args, **kwargs):
        """Update a specific customer policy acceptance."""
        acceptance = CustomerPolicyAcceptance.objects.get(id=kwargs.get("pk"))
        data = request.data
        acceptance.customer_id = data.get("customer_id", acceptance.customer_id)
        acceptance.page_key = data.get("page_key", acceptance.page_key)
        acceptance.version = data.get("version", acceptance.version)
        acceptance.save()
        return Response({
            "message": "Customer policy acceptance updated successfully",
            "data": {
                "id": acceptance.id,
                "customer": acceptance.customer.id,
                "page_key": acceptance.page_key,
                "version": acceptance.version,
                "accepted_at": acceptance.accepted_at,
            }
        })
    
    @method_decorator(admin_auth("CRM_MASTERS_TERMS_CONDITIONS_DELETE", "customer_policy_acceptance.delete"))
    def delete(self, request, *args, **kwargs):
        """Delete a specific customer policy acceptance."""
        acceptance = CustomerPolicyAcceptance.objects.get(id=kwargs.get("pk"))
        acceptance.delete()
        return Response({
            "message": "Customer policy acceptance deleted successfully"
        }, status=status.HTTP_200_OK)