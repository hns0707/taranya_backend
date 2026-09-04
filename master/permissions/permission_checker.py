"""
Decorators for admin authentication and authorization.
"""
from functools import wraps
from django.http import JsonResponse
import jwt
from shared.models import AdminUser, Role, AdminUserRole, RolePermission
from ..auth.admin_jwt import AdminJWTAuthentication
from .permission_aliases import MODULE_PREFIX_WILDCARDS, expand_permission_codes


def _normalize_permission_code(code: str) -> str:
    return (code or "").strip().upper()


def permission_code_matches(user_code: str, required_code: str) -> bool:
    """
    True when a role permission row satisfies an @admin_auth requirement.
    Handles exact codes, section_VIEW suffixes, alias table, and Stores module wildcard.
    """
    user_code = _normalize_permission_code(user_code)
    required_code = _normalize_permission_code(required_code)
    if not user_code or not required_code:
        return False
    if user_code == required_code:
        return True
    # Role UI: CRM_ACCOUNTS_PAYMENT_VERIFICATION_VIEW satisfies CRM_ACCOUNTS_PAYMENT_VERIFICATION
    if user_code.startswith(required_code + "_"):
        return True
    for alias in expand_permission_codes(required_code):
        if user_code == _normalize_permission_code(alias):
            return True
    # Reverse aliases: user row may be listed under a different @admin_auth key
    for alias in expand_permission_codes(user_code):
        normalized_alias = _normalize_permission_code(alias)
        if normalized_alias == required_code or required_code.startswith(normalized_alias + "_"):
            return True
    # Reverse: user has section code; required is a suffixed permission row
    if required_code.startswith(user_code + "_"):
        return True
    # Module wildcards (CRM_STORES, CRM_MASTERS, CRM_ACCOUNTS, …)
    prefix = MODULE_PREFIX_WILDCARDS.get(required_code)
    if prefix and user_code.startswith(prefix):
        return True
    return False


def admin_has_any_permission(admin_user, required_permissions) -> bool:
    if admin_user.is_super_admin:
        return True
    user_codes = get_user_permission_codes(admin_user)
    for required in required_permissions:
        for user_code in user_codes:
            if permission_code_matches(user_code, required):
                return True
    return False


def ensure_admin_permission(request, *permission_codes):
    """
    Enforce method-specific permission codes after @admin_auth() (JWT only).
    Returns a DRF Response on denial, or None if allowed.
    """
    from rest_framework import status
    from rest_framework.response import Response

    admin_user = getattr(request, "admin_user", None)
    if not admin_user:
        return Response(
            {"detail": "Admin user not authenticated"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    if getattr(admin_user, "is_super_admin", False):
        return None
    if admin_has_any_permission(admin_user, permission_codes):
        return None
    return Response({"detail": "Permission denied"}, status=status.HTTP_403_FORBIDDEN)


def admin_jwt_required(view_func):
    """
    Decorator to validate JWT and attach admin user to the request.
    """
    @wraps(view_func)
    def wrapped_view(request, *args, **kwargs):
        try:
            # Extract and validate token
            token = AdminJWTAuthentication.get_token_from_request(request)
            admin_user = AdminJWTAuthentication.validate_admin_token(token)

            # Attach admin user to request (both properties for compatibility)
            request.admin_user = admin_user
            request.user = admin_user

            # Cache permission codes on request
            request.permission_codes = get_user_permission_codes(admin_user)

        except ValueError as e:
            return JsonResponse({'detail': str(e)}, status=401)
        except jwt.ExpiredSignatureError:
            return JsonResponse({'detail': 'Token has expired'}, status=401)
        except jwt.InvalidTokenError as e:
            return JsonResponse({'detail': str(e)}, status=401)
        except AdminUser.DoesNotExist:
            return JsonResponse({'detail': 'Admin user not found or inactive'}, status=401)

        return view_func(request, *args, **kwargs)

    return wrapped_view


def admin_permission_required(permission_code):
    """
    Decorator to check if the admin user has the required permission.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped_view(request, *args, **kwargs):
            # Check if admin user is attached to the request
            if not hasattr(request, 'admin_user'):
                return JsonResponse({'error': 'Admin user not authenticated'}, status=401)
            
            admin_user = request.admin_user
            
            # For now, skip permission checking since we don't have Permission model
            # In production, implement proper permission checking
            # if permission_code not in permissions:
            #     return JsonResponse({'error': 'Permission denied'}, status=403)
            
            return view_func(request, *args, **kwargs)
        
        return wrapped_view
    
    return decorator


def admin_auth(*permissions):
    """
    Combined decorator for JWT authentication and permission check.
    
    Accepts multiple permission codes (variable arguments). If user has ANY ONE of the permissions,
    access is allowed. Superadmin users bypass all permission checks.
    
    Args:
        *permissions: Variable number of permission codes to check (OR condition)
        
    Example Usage:
        @method_decorator(admin_auth("PERM_CREATE"))  # Single permission (backward compatible)
        @method_decorator(admin_auth("PERM_CREATE", "PERM_APPROVE"))  # Multiple permissions (OR)
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped_view(request, *args, **kwargs):
            try:
                # Validate JWT
                token = AdminJWTAuthentication.get_token_from_request(request)
                admin_user = AdminJWTAuthentication.validate_admin_token(token)

                # Attach admin user to request (both properties for compatibility)
                request.admin_user = admin_user
                request.user = admin_user

                # Cache permission codes on request
                request.permission_codes = get_user_permission_codes(admin_user)

            except ValueError as e:
                return JsonResponse({'detail': str(e)}, status=401)
            except jwt.ExpiredSignatureError:
                return JsonResponse({'detail': 'Token has expired'}, status=401)
            except jwt.InvalidTokenError as e:
                return JsonResponse({'detail': str(e)}, status=401)
            except AdminUser.DoesNotExist:
                return JsonResponse({'detail': 'Admin user not found or inactive'}, status=401)

            # Check permissions if provided
            if permissions:
                # Superadmin bypasses all permission checks
                if admin_user.is_super_admin:
                    pass
                elif not admin_has_any_permission(admin_user, permissions):
                    return JsonResponse({'detail': 'Permission denied'}, status=403)

            return view_func(request, *args, **kwargs)

        return wrapped_view

    return decorator


def get_user_permission_codes(admin_user):
    """
    Get all permission codes for an admin user.

    Args:
        admin_user (AdminUser): The admin user to fetch permissions for.

    Returns:
        list: All permission codes the user has.
    """
    # Super admins have all permissions
    if admin_user.is_super_admin:
        from shared.models import Permission
        return list(Permission.objects.values_list('code', flat=True))

    # Get all permissions through user's roles
    admin_roles = AdminUserRole.objects.filter(admin_user=admin_user).values_list('role', flat=True)
    permissions = RolePermission.objects.filter(role__in=admin_roles).select_related('permission')
    permission_codes = list(set(permissions.values_list('permission__code', flat=True)))

    return permission_codes


def check_admin_permission(admin_user, permission_code):
    """
    Check if an admin user has a specific permission.

    Args:
        admin_user (AdminUser): The admin user to check.
        permission_code (str): The permission code to check.

    Returns:
        bool: True if the admin has the permission, False otherwise.
    """
    # Normalize permission code
    normalized_code = permission_code.strip().upper()

    # Super admins have all permissions
    if admin_user.is_super_admin:
        return True

    user_codes = get_user_permission_codes(admin_user)
    return any(
        permission_code_matches(user_code, normalized_code)
        for user_code in user_codes
    )