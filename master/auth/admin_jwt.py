"""
Custom JWT authentication for admin users using AdminUser model only.
"""
import jwt
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from shared.models import AdminUser


class AdminJWTAuthentication:
    """
    Custom JWT authentication class for admin users.
    Generates and validates tokens using AdminUser model only.
    """

    @staticmethod
    def generate_admin_token(admin_user):
        """
        Generate a JWT access token for an admin user.

        Args:
            admin_user (AdminUser): The admin user to generate token for.

        Returns:
            str: JWT access token.
        """
        now = timezone.now()
        payload = {
            'user_id': admin_user.id,
            'role': 'admin',
            'admin_id': admin_user.id,
            'username': admin_user.username,
            'exp': now + timedelta(hours=8),
            'iat': now,
            'token_type': 'admin_access'
        }

        secret_key = getattr(settings, 'ADMIN_JWT_SECRET_KEY', settings.SECRET_KEY)
        token = jwt.encode(payload, secret_key, algorithm='HS256')
        return token

    @staticmethod
    def validate_admin_token(token):
        """
        Validate a JWT token and return the admin user.

        Args:
            token (str): JWT token to validate.

        Returns:
            AdminUser: The authenticated admin user.

        Raises:
            jwt.ExpiredSignatureError: If token is expired.
            jwt.InvalidTokenError: If token is invalid.
            AdminUser.DoesNotExist: If admin user doesn't exist or is inactive.
        """
        secret_key = getattr(settings, 'ADMIN_JWT_SECRET_KEY', settings.SECRET_KEY)

        try:
            payload = jwt.decode(token, secret_key, algorithms=['HS256'])
        except jwt.ExpiredSignatureError:
            raise jwt.ExpiredSignatureError("Token has expired")
        except jwt.InvalidTokenError:
            raise jwt.InvalidTokenError("Invalid token")

        # Validate token type
        if payload.get('token_type') != 'admin_access':
            raise jwt.InvalidTokenError("Invalid token type")

        # Get admin user
        admin_id = payload.get('admin_id')
        if not admin_id:
            raise jwt.InvalidTokenError("Invalid token payload")

        try:
            admin_user = AdminUser.objects.get(id=admin_id, is_active=True)
        except AdminUser.DoesNotExist:
            raise AdminUser.DoesNotExist("Admin user not found or inactive")

        return admin_user

    @staticmethod
    def get_token_from_request(request):
        """
        Extract token from Authorization header.
        Expected format: Authorization: Bearer <token>
        """
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            raise ValueError("Authorization header missing")

        parts = auth_header.strip().split()

        if len(parts) != 2 or parts[0] != 'Bearer':
            raise ValueError("Authorization header must be in format: Bearer <token>")

        token = parts[1].strip()
        if not token:
            raise ValueError("Token missing")

        return token
