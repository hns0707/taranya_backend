"""
Package for permission decorators and DRF permissions.
"""
from rest_framework.permissions import BasePermission

from .permission_checker import admin_jwt_required, admin_permission_required, admin_auth


class IsAdminUser(BasePermission):
    """
    Custom DRF permission to check if user is an admin.
    """
    def has_permission(self, request, view):
        return hasattr(request, 'admin_user')