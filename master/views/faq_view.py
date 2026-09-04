"""
Views for FAQs in the master app.
"""
from rest_framework import generics, status
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from shared.models import FAQ
from master.permissions.permission_checker import admin_auth
from django.utils.decorators import method_decorator

class FAQListCreateView(generics.ListCreateAPIView):
    """
    API endpoint to list and create FAQs.
    """
    queryset = FAQ.objects.all()
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['question', 'answer']
    ordering_fields = ['question', 'display_order', 'system_created_at']
    ordering = ['display_order']
    
    @method_decorator(admin_auth("CRM_MASTERS_FAQS_VIEW"))
    def get(self, request, *args, **kwargs):
        """List all FAQs with pagination, filtering and ordering."""
        queryset = self.filter_queryset(self.get_queryset())
        
        # Apply pagination
        page = self.paginate_queryset(queryset)
        if page is not None:
            data = [{
                "id": faq.id,
                "question": faq.question,
                "answer": faq.answer,
                "is_active": faq.is_active,
                "display_order": faq.display_order,
                "system_created_at": faq.system_created_at,
            } for faq in page]
            return self.get_paginated_response(data)
        
        data = [{
            "id": faq.id,
            "question": faq.question,
            "answer": faq.answer,
            "is_active": faq.is_active,
            "display_order": faq.display_order,
            "system_created_at": faq.system_created_at,
        } for faq in queryset]
        return Response(data)
    
    @method_decorator(admin_auth("CRM_MASTERS_FAQS_CREATE"))
    def post(self, request, *args, **kwargs):
        """Create a new FAQ."""
        data = request.data
        faq = FAQ.objects.create(
            question=data.get("question"),
            answer=data.get("answer"),
            is_active=data.get("is_active", True),
            display_order=data.get("display_order", 1),
        )
        return Response({
            "message": "FAQ created successfully",
            "data": {
                "id": faq.id,
                "question": faq.question,
                "answer": faq.answer,
                "is_active": faq.is_active,
                "display_order": faq.display_order,
                "system_created_at": faq.system_created_at,
            }
        }, status=status.HTTP_201_CREATED)

class FAQRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    """
    API endpoint to retrieve, update, or delete a specific FAQ.
    """
    @method_decorator(admin_auth("CRM_MASTERS_FAQS_VIEW"))
    def get(self, request, *args, **kwargs):
        """Retrieve a specific FAQ."""
        faq = FAQ.objects.get(id=kwargs.get("pk"))
        data = {
            "id": faq.id,
            "question": faq.question,
            "answer": faq.answer,
            "is_active": faq.is_active,
            "display_order": faq.display_order,
            "system_created_at": faq.system_created_at,
        }
        return Response(data)
    
    @method_decorator(admin_auth("CRM_MASTERS_FAQS_UPDATE"))
    def put(self, request, *args, **kwargs):
        """Update a specific FAQ."""
        faq = FAQ.objects.get(id=kwargs.get("pk"))
        data = request.data
        faq.question = data.get("question", faq.question)
        faq.answer = data.get("answer", faq.answer)
        faq.is_active = data.get("is_active", faq.is_active)
        faq.display_order = data.get("display_order", faq.display_order)
        faq.save()
        return Response({
            "message": "FAQ updated successfully",
            "data": {
                "id": faq.id,
                "question": faq.question,
                "answer": faq.answer,
                "is_active": faq.is_active,
                "display_order": faq.display_order,
                "system_created_at": faq.system_created_at,
            }
        })
    
    @method_decorator(admin_auth("CRM_MASTERS_FAQS_DELETE", "faq.delete"))
    def delete(self, request, *args, **kwargs):
        """Delete a specific FAQ."""
        faq = FAQ.objects.get(id=kwargs.get("pk"))
        faq.delete()
        return Response({
            "message": "FAQ deleted successfully"
        }, status=status.HTTP_200_OK)