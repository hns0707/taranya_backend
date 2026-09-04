"""
Views for admin users, roles & permissions in the master app.
"""
from rest_framework import generics, status
from master.permissions.permission_checker import admin_auth
from django.utils.decorators import method_decorator
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from shared.models import AdminUser, AuditLog, AdminUserRole
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from master.auth.admin_jwt import AdminJWTAuthentication
from django.db import transaction
from decimal import Decimal

from shared.services.catalogue_quote_visit_service import _parse_max_discount_percent


def _resolve_user_max_discount(data, role_ids=None) -> Decimal:
    """Prefer explicit user value; else highest role default; else 10."""
    if 'max_discount_percent' in data and data.get('max_discount_percent') not in (None, ''):
        return _parse_max_discount_percent(data.get('max_discount_percent'))
    from shared.models import Role
    ids = role_ids or data.get('role_ids') or data.get('roles') or []
    if ids:
        values = list(
            Role.objects.filter(id__in=ids).values_list('max_discount_percent', flat=True)
        )
        if values:
            return max((_parse_max_discount_percent(v) for v in values), default=Decimal('10'))
    return Decimal('10')

class AdminLoginView(generics.CreateAPIView):
    """
    API endpoint for admin login using username/email and password.
    """
    # Login endpoint - no authentication required
    
    def post(self, request, *args, **kwargs):
        """Authenticate admin user and return token."""
        username_or_email = request.data.get('username')
        password = request.data.get('password')
        
        if not username_or_email or not password:
            return Response({"error": "Username/email and password are required"}, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate credentials using master app's own logic
        admin_user = self.validate_admin_credentials(username_or_email, password)
        
        if not admin_user:
            return Response({"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)
        
        # Check if admin is active
        if not admin_user.is_active:
            return Response({"error": "Account is inactive"}, status=status.HTTP_403_FORBIDDEN)
        
        # Generate JWT token for admin
        token = AdminJWTAuthentication.generate_admin_token(admin_user)
        
        # Get user roles
        user_roles = AdminUserRole.objects.filter(admin_user=admin_user).select_related("role")
        roles = []
        for user_role in user_roles:
            roles.append({
                "id": user_role.role.id,
                "name": user_role.role.name
            })
        
        # Build user permissions tree
        from shared.helper import build_user_permission_tree
        permissions = build_user_permission_tree(admin_user)

        return Response({
            "message": "Login successful",
            "token": token,
            "user": {
                "id": admin_user.id,
                "username": admin_user.username,
                "name": admin_user.full_name,
                "full_name": admin_user.full_name,
                "email": admin_user.email,
                "is_active": admin_user.is_active,
                "is_super_admin": admin_user.is_super_admin,
            },
            "roles": roles,
            "permission_mapping": permissions
        }, status=status.HTTP_200_OK)
    
    def validate_admin_credentials(self, username_or_email, password):
        """
        Validate admin user credentials - Master app specific logic.
        
        Args:
            username_or_email (str): Username or email of the admin.
            password (str): Password to validate.
        
        Returns:
            AdminUser: Admin user object if credentials are valid, None otherwise.
        """
        try:
            # Try to find admin by username
            admin_user = AdminUser.objects.get(username=username_or_email)
        except AdminUser.DoesNotExist:
            try:
                # Try to find admin by email
                admin_user = AdminUser.objects.get(email=username_or_email)
            except AdminUser.DoesNotExist:
                return None
        
        # Verify password using Django's secure password hashing
        if check_password(password, admin_user.password_hash):
            return admin_user
        else:
            return None

class AdminUserListCreateView(generics.ListCreateAPIView):
    """
    API endpoint to list and create admin users.
    """
    queryset = AdminUser.objects.all()
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'is_super_admin']
    search_fields = ['username', 'full_name', 'email']
    ordering_fields = ['username', 'full_name', 'email', 'system_created_at', 'system_updated_at']
    ordering = ['-system_created_at']

    @method_decorator(admin_auth("CRM_SETTINGS_USERS_VIEW"))
    def get(self, request, *args, **kwargs):
        """Return all admin users."""
        # Prefetch user roles to avoid N+1 queries
        queryset = self.filter_queryset(self.get_queryset().prefetch_related('adminuserrole_set__role'))

        # Apply pagination
        page = self.paginate_queryset(queryset)
        if page is not None:
            data = [{
                "id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "email": user.email,
                "is_active": user.is_active,
                "is_super_admin": user.is_super_admin,
                "max_discount_percent": float(user.max_discount_percent) if user.max_discount_percent is not None else 10,
                "system_created_at": user.system_created_at,
                "roles": [user_role.role.name for user_role in user.adminuserrole_set.all()]
            } for user in page]
            return self.get_paginated_response(data)

        data = [{
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "email": user.email,
            "is_active": user.is_active,
            "is_super_admin": user.is_super_admin,
            "max_discount_percent": float(user.max_discount_percent) if user.max_discount_percent is not None else 10,
            "system_created_at": user.system_created_at,
            "roles": [user_role.role.name for user_role in user.adminuserrole_set.all()]
        } for user in queryset]
        return Response(data)

    @method_decorator(admin_auth("CRM_SETTINGS_USERS_CREATE"))
    def post(self, request, *args, **kwargs):
        """Create a new admin user with role assignment."""
        data = request.data
        
        # Validate required fields
        required_fields = ['username', 'full_name', 'email', 'password']
        for field in required_fields:
            if field not in data:
                return Response({
                    "error": f"{field.replace('_', ' ').title()} is required"
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Check if username or email already exists
        if AdminUser.objects.filter(username=data['username']).exists():
            return Response({
                "error": "Username already exists"
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if AdminUser.objects.filter(email=data['email']).exists():
            return Response({
                "error": "Email already exists"
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate roles if provided (frontend sends role_ids; legacy clients may send roles)
        roles = data.get('role_ids') or data.get('roles') or []
        if roles:
            if not isinstance(roles, list):
                return Response({
                    "error": "Roles must be provided as a list of integers"
                }, status=status.HTTP_400_BAD_REQUEST)
            # Check if all role IDs exist
            from shared.models import Role
            invalid_role_ids = []
            for role_id in roles:
                if not Role.objects.filter(id=role_id).exists():
                    invalid_role_ids.append(role_id)
            if invalid_role_ids:
                return Response({
                    "error": f"Invalid role IDs: {', '.join(map(str, invalid_role_ids))}"
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Hash the password before saving
        hashed_password = make_password(data['password'])
        
        try:
            with transaction.atomic():
                admin_user = AdminUser.objects.create(
                    username=data['username'],
                    full_name=data['full_name'],
                    email=data['email'],
                    password_hash=hashed_password,
                    is_active=data.get('is_active', True),
                    is_super_admin=data.get('is_super_admin', False),
                    max_discount_percent=_resolve_user_max_discount(data, roles),
                )
                
                # Assign roles if provided
                if roles:
                    from shared.models import AdminUserRole
                    for role_id in roles:
                        AdminUserRole.objects.create(
                            admin_user=admin_user,
                            role_id=role_id
                        )
                
                # Log the creation of the admin user
                # AuditLog.objects.create(
                #     admin=request.admin_user,
                #     action='CREATE_ADMIN_USER',
                #     entity_type='AdminUser',
                #     entity_id=admin_user.id,
                #     old_value=None,
                #     new_value={
                #         'username': admin_user.username,
                #         'full_name': admin_user.full_name,
                #         'email': admin_user.email,
                #         'is_active': admin_user.is_active,
                #         'is_super_admin': admin_user.is_super_admin,
                #     },
                #     ip_address=request.META.get('REMOTE_ADDR')
                # )
                
        except Exception as e:
            return Response({
                "error": f"Failed to create admin user: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Return assigned roles so clients can verify persistence immediately
        assigned_role_ids = list(
            AdminUserRole.objects.filter(admin_user=admin_user).values_list('role_id', flat=True)
        )

        return Response({
            "message": "Admin user created successfully",
            "data": {
                "id": admin_user.id,
                "username": admin_user.username,
                "full_name": admin_user.full_name,
                "email": admin_user.email,
                "is_active": admin_user.is_active,
                "is_super_admin": admin_user.is_super_admin,
                "max_discount_percent": float(admin_user.max_discount_percent),
                "role_ids": assigned_role_ids,
            },
        }, status=status.HTTP_201_CREATED)

class AdminUserRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    """
    API endpoint to retrieve, update, or delete a specific admin user.
    """
    queryset = AdminUser.objects.all()

    @method_decorator(admin_auth("CRM_SETTINGS_USERS_VIEW"))
    def get(self, request, *args, **kwargs):
        """Retrieve a specific admin user with assigned role IDs and department IDs."""
        admin_user = self.get_object()
        # Get assigned role IDs and department IDs from roles
        from shared.models import AdminUserRole
        user_roles = AdminUserRole.objects.filter(admin_user=admin_user).select_related('role')
        role_ids = []
        department_ids = set()
        
        for user_role in user_roles:
            role_ids.append(user_role.role_id)
            # Add department IDs from this role
            dept_ids = list(user_role.role.departments.values_list('id', flat=True))
            for dept_id in dept_ids:
                department_ids.add(dept_id)
        
        data = {
            "id": admin_user.id,
            "username": admin_user.username,
            "full_name": admin_user.full_name,
            "email": admin_user.email,
            "is_active": admin_user.is_active,
            "is_super_admin": admin_user.is_super_admin,
            "max_discount_percent": float(admin_user.max_discount_percent) if admin_user.max_discount_percent is not None else 10,
            "system_created_at": admin_user.system_created_at,
            "system_updated_at": admin_user.system_updated_at,
            "role_ids": role_ids,
            "department_ids": list(department_ids)
        }
        return Response(data)
    
    @method_decorator(admin_auth("CRM_SETTINGS_USERS_UPDATE"))
    def put(self, request, *args, **kwargs):
        """Update a specific admin user with role assignment."""
        admin_user = self.get_object()
        data = request.data
        
        # Update fields
        if 'username' in data and data['username'] != admin_user.username:
            if AdminUser.objects.filter(username=data['username']).exclude(id=admin_user.id).exists():
                return Response({
                    "error": "Username already exists"
                }, status=status.HTTP_400_BAD_REQUEST)
            admin_user.username = data['username']
        
        if 'full_name' in data:
            admin_user.full_name = data['full_name']
        
        if 'email' in data and data['email'] != admin_user.email:
            if AdminUser.objects.filter(email=data['email']).exclude(id=admin_user.id).exists():
                return Response({
                    "error": "Email already exists"
                }, status=status.HTTP_400_BAD_REQUEST)
            admin_user.email = data['email']
        
        if 'is_active' in data:
            admin_user.is_active = data['is_active']
        
        if 'is_super_admin' in data:
            admin_user.is_super_admin = data['is_super_admin']
        
        if 'password' in data:        
            admin_user.password_hash = make_password(data['password'])

        if 'max_discount_percent' in data:
            admin_user.max_discount_percent = _parse_max_discount_percent(data.get('max_discount_percent'))

        # Validate roles if provided
        roles = data.get('role_ids')
        if roles is not None:
            if not isinstance(roles, list):
                return Response({
                    "error": "Roles must be provided as a list of integers"
                }, status=status.HTTP_400_BAD_REQUEST)
            # Check if all role IDs exist
            from shared.models import Role
            invalid_role_ids = []
            for role_id in roles:
                if not Role.objects.filter(id=role_id).exists():
                    invalid_role_ids.append(role_id)
            if invalid_role_ids:
                return Response({
                    "error": f"Invalid role IDs: {', '.join(map(str, invalid_role_ids))}"
                }, status=status.HTTP_400_BAD_REQUEST)
            if 'max_discount_percent' not in data:
                admin_user.max_discount_percent = _resolve_user_max_discount(data, roles)
        
        # Log the old values before saving
        old_values = {
            'username': admin_user.username,
            'full_name': admin_user.full_name,
            'email': admin_user.email,
            'is_active': admin_user.is_active,
            'is_super_admin': admin_user.is_super_admin,
        }
        
        # Update user and roles in a transaction
        try:
            with transaction.atomic():
                admin_user.save()
                
                # Update roles if provided
                if roles is not None:
                    from shared.models import AdminUserRole
                    # Remove existing roles
                    AdminUserRole.objects.filter(admin_user=admin_user).delete()
                    # Add new roles
                    for role_id in roles:
                        AdminUserRole.objects.create(
                            admin_user=admin_user,
                            role_id=role_id
                        )
                
                # Log the update of the admin user
                new_values = {
                    'username': admin_user.username,
                    'full_name': admin_user.full_name,
                    'email': admin_user.email,
                    'is_active': admin_user.is_active,
                    'is_super_admin': admin_user.is_super_admin,
                }
                
                AuditLog.objects.create(
                    admin=request.admin_user,
                    action='UPDATE_ADMIN_USER',
                    entity_type='AdminUser',
                    entity_id=admin_user.id,
                    old_value=old_values,
                    new_value=new_values,
                    ip_address=request.META.get('REMOTE_ADDR')
                )
                
        except Exception as e:
            return Response({
                "error": f"Failed to update admin user: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        return Response({
            "message": "Admin user updated successfully",
            "data": {
                "id": admin_user.id,
                "username": admin_user.username,
                "full_name": admin_user.full_name,
                "email": admin_user.email,
                "is_active": admin_user.is_active,
                "is_super_admin": admin_user.is_super_admin,
                "system_created_at": admin_user.system_created_at,
                "system_updated_at": admin_user.system_updated_at,
            }
        })
    
    @method_decorator(admin_auth("CRM_SETTINGS_USERS_VIEW"))
    def delete(self, request, *args, **kwargs):
        """Delete a specific admin user."""
        admin_user = self.get_object()
        
        # Log the deletion of the admin user
        AuditLog.objects.create(
            admin=request.admin_user,
            action='DELETE_ADMIN_USER',
            entity_type='AdminUser',
            entity_id=admin_user.id,
            old_value={
                'username': admin_user.username,
                'full_name': admin_user.full_name,
                'email': admin_user.email,
                'is_active': admin_user.is_active,
                'is_super_admin': admin_user.is_super_admin,
            },
            new_value=None,
            ip_address=request.META.get('REMOTE_ADDR')
        )
        
        admin_user.delete()
        return Response({
            "message": "Admin user deleted successfully"
        }, status=status.HTTP_200_OK)
    
    def get_object(self):
        """Retrieve a specific admin user."""
        return AdminUser.objects.get(id=self.kwargs.get('pk'))
