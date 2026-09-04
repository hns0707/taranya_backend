"""
Admin views for managing scheme variations.
"""
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from shared.models import SchemeMaster, CustomerScheme, SchemeBenefit
from shared.serializers import SchemeMasterSerializer, CustomerSchemeSerializer
from master.permissions.permission_checker import admin_auth
from django.utils import timezone
from shared.services.maturity_service import calculate_maturity_value, apply_bonus_at_maturity


@api_view(['GET'])
@admin_auth("CRM_SCHEME_ENROLLMENT_VIEW", "CRM_MASTERS_SCHEME_MASTER_VIEW")
def list_schemes(request):
    """List all schemes with variations. Admin-only access."""
    schemes = SchemeMaster.objects.all().prefetch_related('benefits')
    serializer = SchemeMasterSerializer(schemes, many=True)
    return Response(serializer.data)


@api_view(['POST'])
@admin_auth("CRM_MASTERS_SCHEME_MASTER_CREATE")
def create_scheme(request):
    """
    Create a new scheme with variation configuration.
    Admin-only access.
    """
    serializer = SchemeMasterSerializer(data=request.data)
    if serializer.is_valid():
        scheme = serializer.save()
        
        # Create scheme benefits
        benefits = request.data.get('benefits', [])
        for benefit in benefits:
            SchemeBenefit.objects.create(
                scheme=scheme,
                benefit_type=benefit.get('benefit_type'),
                benefit_value=benefit.get('benefit_value'),
                benefit_percentage=benefit.get('benefit_percentage'),
                benefit_months=benefit.get('benefit_months', 0)
            )
            
        # Get full scheme data with benefits
        scheme = SchemeMaster.objects.prefetch_related('benefits').get(id=scheme.id)
        serializer = SchemeMasterSerializer(scheme)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
        
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@admin_auth("CRM_MASTERS_SCHEME_MASTER_VIEW")
def get_scheme(request, pk):
    """Retrieve a scheme. Admin-only access."""
    try:
        scheme = SchemeMaster.objects.prefetch_related('benefits').get(id=pk)
        serializer = SchemeMasterSerializer(scheme)
        return Response(serializer.data)
    except SchemeMaster.DoesNotExist:
        return Response({"error": "Scheme not found"}, status=status.HTTP_404_NOT_FOUND)


@api_view(['PUT'])
@admin_auth("CRM_MASTERS_SCHEME_MASTER_UPDATE")
def update_scheme(request, pk):
    """
    Only allow updating active status (0/1).
    Admin-only access.
    """
    try:
        instance = SchemeMaster.objects.get(id=pk)
    except SchemeMaster.DoesNotExist:
        return Response({"error": "Scheme not found"}, status=status.HTTP_404_NOT_FOUND)
        
    is_active = request.data.get("is_active")

    if is_active is None:
        return Response(
            {"error": "Only 'is_active' field can be updated."},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Validate boolean/integer input
    if str(is_active) not in ["0", "1", "true", "false", "True", "False"]:
        return Response(
            {"error": "Invalid value for is_active. Must be 0 or 1."},
            status=status.HTTP_400_BAD_REQUEST
        )

    instance.is_active = bool(int(is_active))
    instance.save(update_fields=["is_active"])

    return Response({
        "id": instance.id,
        "is_active": instance.is_active
    }, status=status.HTTP_200_OK)


@api_view(['DELETE'])
@admin_auth("CRM_MASTERS_SCHEME_MASTER_DELETE")
def delete_scheme(request, pk):
    """Delete a scheme. Admin-only access."""
    try:
        scheme = SchemeMaster.objects.get(id=pk)
        scheme.delete()
        return Response({"message": "Scheme deleted successfully"}, status=status.HTTP_204_NO_CONTENT)
    except SchemeMaster.DoesNotExist:
        return Response({"error": "Scheme not found"}, status=status.HTTP_404_NOT_FOUND)


@api_view(['GET'])
@admin_auth("CRM_MASTERS_SCHEME_MASTER_VIEW")
def get_customer_scheme_maturity(request, pk):
    """
    Get detailed maturity information for a customer scheme.
    Admin-only access.
    """
    try:
        instance = CustomerScheme.objects.get(id=pk)
        maturity_details = calculate_maturity_value(instance)
        
        serializer = CustomerSchemeSerializer(instance)
        data = serializer.data
        data['maturity_details'] = maturity_details
        
        return Response(data)
    except CustomerScheme.DoesNotExist:
        return Response({"error": "Customer scheme not found"}, status=status.HTTP_404_NOT_FOUND)


@api_view(['PUT'])
@admin_auth("CRM_MASTERS_SCHEME_MASTER_UPDATE")
def apply_maturity_bonus(request, pk):
    """
    Apply maturity bonus logic.
    Admin-only access.
    """
    try:
        instance = CustomerScheme.objects.get(id=pk)
        
        # Check if scheme is eligible
        if instance.scheme_status != 'ACTIVE':
            return Response({
                "error": "Scheme must be active to apply maturity bonus"
            }, status=status.HTTP_400_BAD_REQUEST)
            
        if instance.end_date > timezone.localdate():
            return Response({
                "error": "Scheme has not reached maturity date"
            }, status=status.HTTP_400_BAD_REQUEST)
            
        apply_bonus_at_maturity(instance)
        serializer = CustomerSchemeSerializer(instance)
        
        return Response(serializer.data)
    except CustomerScheme.DoesNotExist:
        return Response({"error": "Customer scheme not found"}, status=status.HTTP_404_NOT_FOUND)
