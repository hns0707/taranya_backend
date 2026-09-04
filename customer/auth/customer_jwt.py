"""
Custom JWT authentication for customers using Customer model.
"""
import jwt
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from shared.models import Customer


class CustomerJWTAuthentication:
    """
    Custom JWT authentication class for customers.
    Generates and validates tokens using Customer model.
    """

    @staticmethod
    def generate_customer_token(customer):
        """
        Generate a JWT access token for a customer.

        Args:
            customer (Customer): The customer to generate token for.

        Returns:
            str: JWT access token.
        """
        payload = {
            'user_id': customer.id,
            'role': 'customer',
            'customer_id': customer.id,
            'mobile': customer.mobile,
            'exp': timezone.now() + timedelta(days=7),  # 7 days
            'iat': timezone.now(),
        }

        secret_key = getattr(settings, 'CUSTOMER_JWT_SECRET_KEY', settings.SECRET_KEY)
        token = jwt.encode(payload, secret_key, algorithm='HS256')
        return token

    @staticmethod
    def validate_customer_token(token):
        """
        Validate a JWT token and return the customer.

        Args:
            token (str): JWT token to validate.

        Returns:
            Customer: The authenticated customer.

        Raises:
            jwt.ExpiredSignatureError: If token is expired.
            jwt.InvalidTokenError: If token is invalid.
            Customer.DoesNotExist: If customer not found.
        """
        secret_key = getattr(settings, 'CUSTOMER_JWT_SECRET_KEY', settings.SECRET_KEY)

        try:
            payload = jwt.decode(token, secret_key, algorithms=['HS256'])
            customer_id = payload.get('customer_id')
            customer = Customer.objects.get(id=customer_id)
            return customer
        except jwt.ExpiredSignatureError:
            raise jwt.ExpiredSignatureError("Token has expired")
        except jwt.InvalidTokenError:
            raise jwt.InvalidTokenError("Invalid token")
        except Customer.DoesNotExist:
            raise Customer.DoesNotExist("Customer not found")

    @staticmethod
    def get_token_from_request(request):
        """
        Extract token from request headers.

        Args:
            request: Django request object.

        Returns:
            str: JWT token.

        Raises:
            ValueError: If token not found.
        """
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            raise ValueError("Authorization header missing or invalid")

        token = auth_header.split(' ')[1]
        return token