"""
Custom authentication for customers.
"""
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from .customer_jwt import CustomerJWTAuthentication


class CustomerAuthentication(BaseAuthentication):
    """
    Custom authentication class for customers using JWT.
    """

    def authenticate(self, request):
        """
        Authenticate the request.

        Args:
            request: Django request object.

        Returns:
            tuple: (customer, token) if authenticated.

        Raises:
            AuthenticationFailed: If authentication fails.
        """
        try:
            token = CustomerJWTAuthentication.get_token_from_request(request)
            customer = CustomerJWTAuthentication.validate_customer_token(token)
            return (customer, token)
        except (ValueError, Exception) as e:
            raise AuthenticationFailed(str(e))