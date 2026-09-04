"""
Views for CMS pages in the master app.
"""
from rest_framework import generics, status
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from shared.models import CMSPage
from master.permissions.permission_checker import admin_auth
from django.utils.decorators import method_decorator

class CMSPageListCreateView(generics.ListCreateAPIView):
    """
    API endpoint to list and create CMS pages.
    """
    queryset = CMSPage.objects.all()
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['page_key', 'title']
    ordering_fields = ['page_key', 'title', 'version', 'system_updated_at']
    ordering = ['-system_updated_at']
    
    @method_decorator(admin_auth("CRM_MASTERS_CMS_PAGES_VIEW"))
    def get(self, request, *args, **kwargs):
        """List all CMS pages with pagination, filtering and ordering."""
        queryset = self.filter_queryset(self.get_queryset())
        
        # Apply pagination
        page = self.paginate_queryset(queryset)
        if page is not None:
            data = [{
                "id": cms_page.id,
                "page_key": cms_page.page_key,
                "title": cms_page.title,
                "content": cms_page.content,
                "version": cms_page.version,
                "is_active": cms_page.is_active,
                "updated_by": cms_page.updated_by.id if cms_page.updated_by else None,
                "system_updated_at": cms_page.system_updated_at,
            } for cms_page in page]
            return self.get_paginated_response(data)
        
        data = [{
            "id": cms_page.id,
            "page_key": cms_page.page_key,
            "title": cms_page.title,
            "content": cms_page.content,
            "version": cms_page.version,
            "is_active": cms_page.is_active,
            "updated_by": cms_page.updated_by.id if cms_page.updated_by else None,
            "system_updated_at": cms_page.system_updated_at,
        } for cms_page in queryset]
        return Response(data)
    
    @method_decorator(admin_auth("CRM_MASTERS_CMS_PAGES_CREATE"))
    def post(self, request, *args, **kwargs):
        """Create a new CMS page."""
        data = request.data
        cms_page = CMSPage.objects.create(
            page_key=data.get("page_key"),
            title=data.get("title"),
            content=data.get("content"),
            version=data.get("version", 1),
            is_active=data.get("is_active", True),
            updated_by=request.admin_user if hasattr(request, 'admin_user') else None,
        )
        return Response({
            "message": "CMS page created successfully",
            "data": {
                "id": cms_page.id,
                "page_key": cms_page.page_key,
                "title": cms_page.title,
                "content": cms_page.content,
                "version": cms_page.version,
                "is_active": cms_page.is_active,
                "updated_by": cms_page.updated_by.id if cms_page.updated_by else None,
                "system_updated_at": cms_page.system_updated_at,
            }
        }, status=status.HTTP_201_CREATED)

class CMSPageRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    """
    API endpoint to retrieve, update, or delete a specific CMS page.
    """
    @method_decorator(admin_auth("CRM_MASTERS_CMS_PAGES_VIEW"))
    def get(self, request, *args, **kwargs):
        """Retrieve a specific CMS page."""
        cms_page = CMSPage.objects.get(id=kwargs.get("pk"))
        data = {
            "id": cms_page.id,
            "page_key": cms_page.page_key,
            "title": cms_page.title,
            "content": cms_page.content,
            "version": cms_page.version,
            "is_active": cms_page.is_active,
            "updated_by": cms_page.updated_by.id if cms_page.updated_by else None,
            "system_updated_at": cms_page.system_updated_at,
        }
        return Response(data)
    
    @method_decorator(admin_auth("CRM_MASTERS_CMS_PAGES_VIEW"))
    def put(self, request, *args, **kwargs):
        """Update a specific CMS page."""
        cms_page = CMSPage.objects.get(id=kwargs.get("pk"))
        data = request.data
        cms_page.page_key = data.get("page_key", cms_page.page_key)
        cms_page.title = data.get("title", cms_page.title)
        cms_page.content = data.get("content", cms_page.content)
        cms_page.version = data.get("version", cms_page.version)
        cms_page.is_active = data.get("is_active", cms_page.is_active)
        cms_page.updated_by = request.admin_user if hasattr(request, 'admin_user') else cms_page.updated_by
        cms_page.save()
        return Response({
            "message": "CMS page updated successfully",
            "data": {
                "id": cms_page.id,
                "page_key": cms_page.page_key,
                "title": cms_page.title,
                "content": cms_page.content,
                "version": cms_page.version,
                "is_active": cms_page.is_active,
                "updated_by": cms_page.updated_by.id if cms_page.updated_by else None,
                "system_updated_at": cms_page.system_updated_at,
            }
        })
    
    @method_decorator(admin_auth("CRM_MASTERS_CMS_PAGES_DELETE", "cms_page.delete"))
    def delete(self, request, *args, **kwargs):
        """Delete a specific CMS page."""
        cms_page = CMSPage.objects.get(id=kwargs.get("pk"))
        cms_page.delete()
        return Response({
            "message": "CMS page deleted successfully"
        }, status=status.HTTP_200_OK)