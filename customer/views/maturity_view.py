"""
Views for scheme maturity calculations and bonus application.
"""
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from datetime import date

from customer.auth.customer_auth import CustomerAuthentication
from shared.models import CustomerScheme
from shared.services.maturity_service import calculate_maturity_value, apply_bonus_at_maturity


@api_view(["GET"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def get_maturity_details(request):
    """
    Get maturity details for the logged-in customer's matured scheme.
    Customer authentication required.

    Returns:
        Maturity value details including cash amount, gold grams, and gold rate
    """

    # Fetch matured scheme for logged-in customer
    customer_scheme = CustomerScheme.objects.filter(
        customer=request.user,
        end_date__lte=timezone.localdate()
    ).order_by('-end_date').first()

    if not customer_scheme:
        return Response({
            "message": "No matured scheme found for this customer"
        }, status=status.HTTP_404_NOT_FOUND)

    maturity_details = calculate_maturity_value(customer_scheme)

    return Response({
        "scheme_name": customer_scheme.scheme.scheme_name,
        "maturity_date": customer_scheme.end_date,
        "total_value": maturity_details['total_value'],
        "cash_amount": maturity_details['cash_amount'],
        "gold_grams": maturity_details['gold_grams'],
        "gold_rate": maturity_details['gold_rate_value'],
        "gold_purity": customer_scheme.scheme.gold_purity or '22K'
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def apply_maturity_bonus(request, customer_scheme_id):
    """
    Apply maturity bonus to a customer scheme.
    Requires customer authentication.
    
    Args:
        customer_scheme_id: ID of the customer scheme to apply bonus for
        
    Returns:
        Success message
    """
    try:
        customer_scheme = CustomerScheme.objects.get(
            id=customer_scheme_id,
            customer=request.user
        )
        
        # Check if scheme is eligible for maturity
        if customer_scheme.scheme_status != 'ACTIVE':
            return Response({
                "error": "Scheme must be active to apply maturity bonus"
            }, status=status.HTTP_400_BAD_REQUEST)
            
        if customer_scheme.end_date > timezone.localdate():
            return Response({
                "error": "Scheme has not reached maturity date"
            }, status=status.HTTP_400_BAD_REQUEST)

        apply_bonus_at_maturity(customer_scheme)
        
        return Response({
            "message": "Maturity bonus applied successfully"
        }, status=status.HTTP_200_OK)
        
    except CustomerScheme.DoesNotExist:
        return Response({
            "error": "Customer scheme not found"
        }, status=status.HTTP_404_NOT_FOUND)