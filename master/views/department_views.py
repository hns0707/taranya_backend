"""
Views for department management in the master app.
"""
# Django framework
from django.utils.decorators import method_decorator
from rest_framework import generics, status
from rest_framework.response import Response

# Project / app imports
from master.permissions.permission_checker import admin_auth
from shared.models import Department


class DepartmentCreateView(generics.CreateAPIView):
    """
    API endpoint to create a new department.
    """
    @method_decorator(admin_auth("CRM_SETTINGS_DEPARTMENTS_CREATE"))
    def post(self, request, *args, **kwargs):
        """Create a new department."""
        data = request.data
        
        # Validate required fields
        if not data.get("name") or not data.get("code"):
            return Response(
                {"error": "Name and code are required fields"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Check for unique code
        code = data.get("code")
        if Department.objects.filter(code=code).exists():
            return Response(
                {"error": f"Department with code '{code}' already exists"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Create department
        try:
            department = Department.objects.create(
                name=data.get("name"),
                code=code,
                description=data.get("description"),
                is_active=data.get("is_active", True)
            )
            
            return Response({
                "message": "Department created successfully",
                "data": {
                    "id": department.id,
                    "name": department.name,
                    "code": department.code,
                    "description": department.description,
                    "is_active": department.is_active,
                    "system_created_at": department.system_created_at,
                    "system_updated_at": department.system_updated_at
                }
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class DepartmentUpdateView(generics.UpdateAPIView):
    """
    API endpoint to update a department.
    Supports partial updates.
    """
    @method_decorator(admin_auth("CRM_SETTINGS_DEPARTMENTS_UPDATE"))
    def put(self, request, *args, **kwargs):
        """Update department details (partial update)."""
        department_id = kwargs.get("pk")
        
        try:
            department = Department.objects.get(id=department_id)
        except Department.DoesNotExist:
            return Response(
                {"error": "Department not found"},
                status=status.HTTP_404_NOT_FOUND
            )
            
        data = request.data
        
        # Update fields if provided
        if "name" in data:
            department.name = data.get("name")
            
        if "code" in data:
            new_code = data.get("code")
            if new_code != department.code and Department.objects.filter(code=new_code).exists():
                return Response(
                    {"error": f"Department with code '{new_code}' already exists"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            department.code = new_code
            
        if "description" in data:
            department.description = data.get("description")
            
        if "is_active" in data:
            department.is_active = data.get("is_active")
            
        # Save changes
        department.save()
        
        return Response({
            "message": "Department updated successfully",
            "data": {
                "id": department.id,
                "name": department.name,
                "code": department.code,
                "description": department.description,
                "is_active": department.is_active,
                "system_created_at": department.system_created_at,
                "system_updated_at": department.system_updated_at
            }
        }, status=status.HTTP_200_OK)


class DepartmentListView(generics.ListAPIView):
    """
    API endpoint to list all departments.
    """
    @method_decorator(admin_auth("CRM_SETTINGS_DEPARTMENTS_VIEW"))
    def get(self, request, *args, **kwargs):
        """List all active departments."""
        departments = Department.objects.filter(is_active=True).order_by("name")
        
        data = [{
            "id": dept.id,
            "name": dept.name,
            "code": dept.code,
            "description": dept.description,
            "is_active": dept.is_active,
            "system_created_at": dept.system_created_at,
            "system_updated_at": dept.system_updated_at
        } for dept in departments]
        
        return Response(data)


class DepartmentDetailView(generics.RetrieveAPIView):
    """
    API endpoint to retrieve a single department.
    """
    @method_decorator(admin_auth("CRM_SETTINGS_DEPARTMENTS_VIEW"))
    def get(self, request, *args, **kwargs):
        """Get department details."""
        department_id = kwargs.get("pk")
        
        try:
            department = Department.objects.get(id=department_id)
            
            data = {
                "id": department.id,
                "name": department.name,
                "code": department.code,
                "description": department.description,
                "is_active": department.is_active,
                "system_created_at": department.system_created_at,
                "system_updated_at": department.system_updated_at
            }
            
            return Response(data)
            
        except Department.DoesNotExist:
            return Response(
                {"error": "Department not found"},
                status=status.HTTP_404_NOT_FOUND
            )
