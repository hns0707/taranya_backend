"""
Views for permissions and roles in the master app.
"""
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from shared.models import Role, Permission, AdminUserRole, RolePermission, AdminUser, Module, Department
from master.permissions.permission_checker import admin_auth
from django.utils.decorators import method_decorator
from django.db import transaction
from shared.services.catalogue_quote_visit_service import _parse_max_discount_percent

class RoleListCreateView(generics.ListCreateAPIView):
    """
    API endpoint to list and create roles.
    """
    #  
    
    @method_decorator(admin_auth("CRM_SETTINGS_ROLES_VIEW"))
    def get(self, request, *args, **kwargs):
        """List all roles with department IDs."""
        roles = Role.objects.all()
        data = [{
            "id": role.id,
            "name": role.name,
            "description": role.description,
            "is_active": role.is_active,
            "max_discount_percent": float(role.max_discount_percent) if role.max_discount_percent is not None else 10,
            "department_ids": list(role.departments.values_list('id', flat=True)),
        } for role in roles]
        return Response(data)

    @method_decorator(admin_auth("CRM_SETTINGS_ROLES_CREATE"))
    def post(self, request, *args, **kwargs):
        """Create a new role with permission and department assignment."""
        data = request.data
        
        # Validate departments if provided
        department_ids = data.get('department_ids', [])
        if department_ids:
            if not isinstance(department_ids, list):
                return Response({
                    "error": "Department IDs must be provided as a list of integers"
                }, status=status.HTTP_400_BAD_REQUEST)
            # Check if all department IDs exist
            invalid_department_ids = []
            for dept_id in department_ids:
                if not Department.objects.filter(id=dept_id).exists():
                    invalid_department_ids.append(dept_id)
            if invalid_department_ids:
                return Response({
                    "error": f"Invalid department IDs: {', '.join(map(str, invalid_department_ids))}"
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate permissions if provided
        permissions = data.get('permission_ids', [])
        if permissions:
            if not isinstance(permissions, list):
                return Response({
                    "error": "Permissions must be provided as a list of integers"
                }, status=status.HTTP_400_BAD_REQUEST)
            # Check if all permission IDs exist
            from shared.models import Permission
            invalid_permission_ids = []
            for permission_id in permissions:
                if not Permission.objects.filter(id=permission_id).exists():
                    invalid_permission_ids.append(permission_id)
            if invalid_permission_ids:
                return Response({
                    "error": f"Invalid permission IDs: {', '.join(map(str, invalid_permission_ids))}"
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Create role and assign permissions/departments in a transaction
        try:
            with transaction.atomic():
                role = Role.objects.create(
                    name=data.get("name"),
                    description=data.get("description"),
                    is_active=data.get("is_active", True),
                    max_discount_percent=_parse_max_discount_percent(data.get("max_discount_percent", 10)),
                )
                
                # Assign departments if provided
                if department_ids:
                    role.departments.add(*department_ids)
                
                # Assign permissions if provided
                if permissions:
                    from shared.models import RolePermission
                    for permission_id in permissions:
                        RolePermission.objects.create(
                            role=role,
                            permission_id=permission_id
                        )
                
        except Exception as e:
            return Response({
                "error": f"Failed to create role: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Get assigned department IDs for response
        department_ids = list(role.departments.values_list('id', flat=True))
        
        return Response({
            "message": "Role created successfully",
            "data": {
                "id": role.id,
                "name": role.name,
                "description": role.description,
                "is_active": role.is_active,
                "max_discount_percent": float(role.max_discount_percent),
                "department_ids": department_ids,
            }
        }, status=status.HTTP_201_CREATED)


class RoleRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    """
    API endpoint to retrieve, update, or delete a specific role.
    """
     
 
    @method_decorator(admin_auth("CRM_SETTINGS_ROLES_VIEW"))
    def get(self, request, *args, **kwargs):
        """Retrieve a specific role with assigned permission and department IDs."""
        role = Role.objects.get(id=kwargs.get("pk"))
        # Get assigned permission IDs
        from shared.models import RolePermission
        permission_ids = list(RolePermission.objects.filter(role=role).values_list('permission_id', flat=True))
        # Get assigned department IDs
        department_ids = list(role.departments.values_list('id', flat=True))
        data = {
            "id": role.id,
            "name": role.name,
            "description": role.description,
            "is_active": role.is_active,
            "max_discount_percent": float(role.max_discount_percent) if role.max_discount_percent is not None else 10,
            "department_ids": department_ids,
            "permission_ids": permission_ids
        }
        return Response(data)

    @method_decorator(admin_auth("CRM_SETTINGS_ROLES_UPDATE"))
    def put(self, request, *args, **kwargs):
        """Update a specific role with permission and department assignment."""
        role = Role.objects.get(id=kwargs.get("pk"))
        data = request.data
        role.name = data.get("name", role.name)
        role.description = data.get("description", role.description)
        role.is_active = data.get("is_active", role.is_active)
        if "max_discount_percent" in data:
            role.max_discount_percent = _parse_max_discount_percent(data.get("max_discount_percent"))
        
        # Validate departments if provided
        department_ids = data.get('department_ids')
        if department_ids is not None:
            if not isinstance(department_ids, list):
                return Response({
                    "error": "Department IDs must be provided as a list of integers"
                }, status=status.HTTP_400_BAD_REQUEST)
            # Check if all department IDs exist
            invalid_department_ids = []
            for dept_id in department_ids:
                if not Department.objects.filter(id=dept_id).exists():
                    invalid_department_ids.append(dept_id)
            if invalid_department_ids:
                return Response({
                    "error": f"Invalid department IDs: {', '.join(map(str, invalid_department_ids))}"
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate permissions if provided
        permissions = data.get('permission_ids')
        if permissions is not None:
            if not isinstance(permissions, list):
                return Response({
                    "error": "Permissions must be provided as a list of integers"
                }, status=status.HTTP_400_BAD_REQUEST)
            # Check if all permission IDs exist
            from shared.models import Permission
            invalid_permission_ids = []
            for permission_id in permissions:
                if not Permission.objects.filter(id=permission_id).exists():
                    invalid_permission_ids.append(permission_id)
            if invalid_permission_ids:
                return Response({
                    "error": f"Invalid permission IDs: {', '.join(map(str, invalid_permission_ids))}"
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Update role, permissions, and departments in a transaction
        from django.db import transaction
        try:
            with transaction.atomic():
                role.save()
                
                # Update departments if provided
                if department_ids is not None:
                    role.departments.clear()
                    if department_ids:
                        role.departments.add(*department_ids)
                
                # Update permissions if provided
                if permissions is not None:
                    from shared.models import RolePermission
                    # Remove existing permissions
                    RolePermission.objects.filter(role=role).delete()
                    # Add new permissions
                    for permission_id in permissions:
                        RolePermission.objects.create(
                            role=role,
                            permission_id=permission_id
                        )
                
        except Exception as e:
            return Response({
                "error": f"Failed to update role: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Get assigned department IDs for response
        department_ids = list(role.departments.values_list('id', flat=True))
        
        return Response({
            "message": "Role updated successfully",
            "data": {
                "id": role.id,
                "name": role.name,
                "description": role.description,
                "is_active": role.is_active,
                "department_ids": department_ids,
            }
        })

    @method_decorator(admin_auth("CRM_SETTINGS_ROLES_VIEW"))
    def delete(self, request, *args, **kwargs):
        """Delete a specific role."""
        role = Role.objects.get(id=kwargs.get("pk"))
        role.delete()
        return Response({
            "message": "Role deleted successfully"
        }, status=status.HTTP_200_OK)



class RolesByDepartmentView(generics.GenericAPIView):
    """
    API endpoint to list roles by one or more department IDs.
    """

    @method_decorator(admin_auth("CRM_SETTINGS_ROLES_VIEW"))
    def get(self, request, *args, **kwargs):
        """List all roles assigned to one or more departments."""
        raw_department_ids = kwargs.get("department_ids", "")

        department_ids = []
        invalid_department_ids = []

        for value in raw_department_ids.split(","):
            value = value.strip()
            if not value:
                continue
            if not value.isdigit():
                invalid_department_ids.append(value)
                continue
            department_ids.append(int(value))

        if invalid_department_ids:
            return Response({
                "error": f"Invalid department IDs: {', '.join(invalid_department_ids)}"
            }, status=status.HTTP_400_BAD_REQUEST)

        if not department_ids:
            return Response({
                "error": "Please provide at least one department ID"
            }, status=status.HTTP_400_BAD_REQUEST)

        existing_department_ids = set(
            Department.objects.filter(id__in=department_ids).values_list("id", flat=True)
        )
        missing_department_ids = [str(dept_id) for dept_id in department_ids if dept_id not in existing_department_ids]

        if missing_department_ids:
            return Response({
                "error": f"Department IDs not found: {', '.join(missing_department_ids)}"
            }, status=status.HTTP_404_NOT_FOUND)

        roles = Role.objects.filter(departments__id__in=department_ids).distinct()
        data = [{
            "id": role.id,
            "name": role.name,
            "description": role.description,
            "is_active": role.is_active,
            "max_discount_percent": float(role.max_discount_percent) if role.max_discount_percent is not None else 10,
        } for role in roles]

        return Response({
            "department_ids": department_ids,
            "roles": data
        })

class PermissionListCreateView(generics.ListCreateAPIView):
    """
    API endpoint to list and create permissions.
    """
    #  

    @method_decorator(admin_auth("CRM_SETTINGS_ROLES_VIEW"))
    def get(self, request, *args, **kwargs):
        """List all permissions in module → section → permissions structure."""
        # Get all active modules, sections, permissions, and actions with necessary joins
        modules = Module.objects.filter(is_active=True).prefetch_related(
            'sections',
            'sections__permissions',
            'sections__permissions__action'
        ).order_by('name')
        
        data = []
        for module in modules:
            # Get active sections for this module
            sections = module.sections.filter(is_active=True).order_by('name')
            children = []
            
            for section in sections:
                # Get active permissions for this section
                permissions = section.permissions.filter(is_active=True).order_by('action__code')
                permission_list = []
                
                for permission in permissions:
                    permission_list.append({
                        "permission_id": permission.id,
                        "action": permission.action.code,
                        "code": permission.code
                    })
                
                children.append({
                    "section": section.name,
                    "code": section.code,
                    "permissions": permission_list
                })
            
            data.append({
                "module": module.name,
                "code": module.code,
                "children": children
            })
        
        return Response(data)

    @method_decorator(admin_auth("CRM_SETTINGS_PERMISSIONS_CREATE"))
    def post(self, request, *args, **kwargs):
        """Create a new permission."""
        data = request.data
        permission = Permission.objects.create(
            module=data.get("module"),
            sub_module=data.get("sub_module"),
            section=data.get("section"),
            action=data.get("action"),
            code=data.get("code"),
            name=data.get("name"),
            description=data.get("description"),
            is_active=data.get("is_active", True),
        )
        return Response({
            "message": "Permission created successfully",
            "data": {
                "id": permission.id,
                "module": permission.module,
                "sub_module": permission.sub_module,
                "section": permission.section,
                "action": permission.action,
                "code": permission.code,
                "name": permission.name,
                "description": permission.description,
                "is_active": permission.is_active,
            }
        }, status=status.HTTP_201_CREATED)





