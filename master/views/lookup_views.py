"""
Views for managing lookups and lookup values in the master app.
"""
from django.core.cache import cache
from django.db.models import Prefetch
from django.views.decorators.cache import cache_page
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from shared.models import Lookup, LookupValue
from shared.serializers import (
    LookupCategoryListSerializer,
    LookupDropdownValueSerializer,
    BulkLookupRequestSerializer,
)
from master.permissions.permission_checker import admin_auth
from django.utils.decorators import method_decorator


LOOKUP_LIST_CACHE_SECONDS = 60 * 15
LOOKUP_VALUES_CACHE_SECONDS = 60 * 10
BULK_LOOKUP_CACHE_SECONDS = 60 * 10


class ApiLookupListView(generics.ListAPIView):
    """
    API endpoint for active lookup categories.
    """
    serializer_class = LookupCategoryListSerializer

    def get_queryset(self):
        return Lookup.objects.filter(is_active=True).only(
            'code', 'name', 'description'
        ).order_by('code')

    @method_decorator(admin_auth())
    @method_decorator(cache_page(LOOKUP_LIST_CACHE_SECONDS))
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ApiLookupValueListView(generics.ListAPIView):
    """
    API endpoint for lookup dropdown values by lookup code.
    """
    serializer_class = LookupDropdownValueSerializer

    def _is_active_filter(self):
        active_param = self.request.query_params.get('active', 'true').strip().lower()
        return active_param not in ('false', '0', 'no')

    def _lookup_exists(self, lookup_code):
        return Lookup.objects.filter(code=lookup_code.upper(), is_active=True).exists()

    def get_queryset(self):
        lookup_code = self.kwargs.get('lookup_code', '').upper()
        return LookupValue.objects.select_related('lookup').filter(
            lookup__code=lookup_code,
            lookup__is_active=True,
            is_active=self._is_active_filter()
        ).only(
            'code',
            'label',
            'sort_order',
            'lookup__code',
        ).order_by('sort_order', 'label')

    @method_decorator(admin_auth())
    @method_decorator(cache_page(LOOKUP_VALUES_CACHE_SECONDS))
    def get(self, request, *args, **kwargs):
        lookup_code = kwargs.get('lookup_code', '').upper()
        if not self._lookup_exists(lookup_code):
            return Response(
                {"error": f"Lookup '{lookup_code}' not found"},
                status=status.HTTP_404_NOT_FOUND
            )
        return super().get(request, *args, **kwargs)


class ApiBulkLookupValueView(APIView):
    """
    API endpoint to fetch dropdown values for multiple lookup codes.
    """
    @method_decorator(admin_auth())
    def post(self, request, *args, **kwargs):
        serializer = BulkLookupRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        codes = serializer.validated_data['codes']
        active_param = request.query_params.get('active', 'true').strip().lower()
        active_only = active_param not in ('false', '0', 'no')

        cache_key = f"lookup_bulk:{active_only}:{','.join(sorted(codes))}"
        cached_response = cache.get(cache_key)
        if cached_response is not None:
            return Response(cached_response)

        value_queryset = LookupValue.objects.filter(
            is_active=active_only
        ).only(
            'lookup_id',
            'code',
            'label',
            'sort_order',
            'is_active',
        ).order_by('sort_order', 'label')

        lookup_queryset = Lookup.objects.filter(
            code__in=codes,
            is_active=True
        ).only(
            'id',
            'code'
        ).prefetch_related(
            Prefetch('values', queryset=value_queryset)
        )

        lookup_map = {
            code: [] for code in codes
        }

        for lookup in lookup_queryset:
            lookup_map[lookup.code] = LookupDropdownValueSerializer(
                lookup.values.all(),
                many=True
            ).data

        cache.set(cache_key, lookup_map, BULK_LOOKUP_CACHE_SECONDS)
        return Response(lookup_map)


class LookupListView(APIView):
    """
    GET: List lookups. Query param ?all=true returns all (including inactive) for admin.
    POST: Create a new lookup category.
    """
    @method_decorator(admin_auth())
    def get(self, request, *args, **kwargs):
        all_param = request.query_params.get("all", "false").strip().lower() in ("true", "1", "yes")
        qs = Lookup.objects.all().order_by("code")
        if not all_param:
            qs = qs.filter(is_active=True)
        from django.db.models import Count
        qs = qs.annotate(values_count=Count("values"))
        data = [
            {
                "id": lookup.id,
                "code": lookup.code,
                "name": lookup.name,
                "description": lookup.description or "",
                "is_active": lookup.is_active,
                "values_count": lookup.values_count,
            }
            for lookup in qs
        ]
        return Response(data)

    @method_decorator(admin_auth())
    def post(self, request, *args, **kwargs):
        code = (request.data.get("code") or "").strip().upper()
        if not code:
            return Response({"error": "Code is required"}, status=status.HTTP_400_BAD_REQUEST)
        if Lookup.objects.filter(code=code).exists():
            return Response({"error": f"Lookup code '{code}' already exists"}, status=status.HTTP_400_BAD_REQUEST)
        name = (request.data.get("name") or "").strip() or code
        description = (request.data.get("description") or "").strip() or None
        is_active = request.data.get("is_active", True)
        lookup = Lookup.objects.create(
            code=code,
            name=name,
            description=description,
            is_active=bool(is_active),
            created_by=request.user,
        )
        return Response(
            {"message": "Lookup created", "data": {"id": lookup.id, "code": lookup.code, "name": lookup.name, "description": lookup.description or "", "is_active": lookup.is_active}},
            status=status.HTTP_201_CREATED,
        )


class LookupDetailView(APIView):
    """
    GET: Retrieve one lookup by code.
    PUT/PATCH: Update lookup (name, description, is_active).
    """
    @method_decorator(admin_auth())
    def get(self, request, lookup_code, *args, **kwargs):
        lookup_code = (lookup_code or "").upper()
        try:
            lookup = Lookup.objects.get(code=lookup_code)
        except Lookup.DoesNotExist:
            return Response({"error": f"Lookup '{lookup_code}' not found"}, status=status.HTTP_404_NOT_FOUND)
        from django.db.models import Count
        values_count = lookup.values.count()
        return Response({
            "id": lookup.id,
            "code": lookup.code,
            "name": lookup.name,
            "description": lookup.description or "",
            "is_active": lookup.is_active,
            "values_count": values_count,
        })

    @method_decorator(admin_auth())
    def put(self, request, lookup_code, *args, **kwargs):
        return self._update(request, lookup_code)

    @method_decorator(admin_auth())
    def patch(self, request, lookup_code, *args, **kwargs):
        return self._update(request, lookup_code)

    def _update(self, request, lookup_code):
        lookup_code = (lookup_code or "").upper()
        try:
            lookup = Lookup.objects.get(code=lookup_code)
        except Lookup.DoesNotExist:
            return Response({"error": f"Lookup '{lookup_code}' not found"}, status=status.HTTP_404_NOT_FOUND)
        data = request.data
        if "name" in data:
            lookup.name = (data["name"] or "").strip() or lookup.name
        if "description" in data:
            lookup.description = (data["description"] or "").strip() or None
        if "is_active" in data:
            lookup.is_active = bool(data["is_active"])
        lookup.updated_by = request.user
        lookup.save()
        return Response({
            "message": "Lookup updated",
            "data": {"id": lookup.id, "code": lookup.code, "name": lookup.name, "description": lookup.description or "", "is_active": lookup.is_active},
        })


class LookupValueListView(APIView):
    """
    List lookup values for a lookup. Query param ?all=true returns all values (including inactive) for admin.
    """
    @method_decorator(admin_auth())
    def get(self, request, lookup_code, *args, **kwargs):
        lookup_code = (lookup_code or "").upper()
        all_param = request.query_params.get("all", "false").strip().lower() in ("true", "1", "yes")
        try:
            lookup = Lookup.objects.get(code=lookup_code, is_active=True)
            values = LookupValue.objects.filter(
                lookup=lookup,
                is_active=True
            ).order_by('sort_order')
            
            data = [{
                "id": value.id,
                "code": value.code,
                "label": value.label,
                "is_active": value.is_active,
                "sort_order": value.sort_order
            } for value in values]
            
            return Response(data)
            
        except Lookup.DoesNotExist:
            return Response({"error": f"Lookup '{lookup_code}' not found"}, status=status.HTTP_404_NOT_FOUND)
        qs = LookupValue.objects.filter(lookup=lookup).order_by("sort_order", "label")
        if not all_param:
            qs = qs.filter(is_active=True)
        data = [
            {
                "id": v.id,
                "code": v.code,
                "label": v.label,
                "is_active": v.is_active,
                "sort_order": v.sort_order,
            }
            for v in qs
        ]
        return Response(data)


class LookupValueCreateView(generics.CreateAPIView):
    """
    API endpoint to create a new lookup value.
    """
    @method_decorator(admin_auth())
    def post(self, request, *args, **kwargs):
        """Create a new lookup value under an existing lookup."""
        lookup_code = kwargs.get('lookup_code')
        
        try:
            lookup = Lookup.objects.get(code=lookup_code)
        except Lookup.DoesNotExist:
            return Response({"error": f"Lookup '{lookup_code}' not found"}, status=status.HTTP_404_NOT_FOUND)
        data = request.data
        code = (data.get("code") or "").strip().upper()
        if not code:
            return Response({"error": "Code is required"}, status=status.HTTP_400_BAD_REQUEST)
        if LookupValue.objects.filter(lookup=lookup, code=code).exists():
            return Response(
                {"error": f"Value code '{code}' already exists for this lookup"},
                status=status.HTTP_400_BAD_REQUEST
            )
        label = (data.get("label") or "").strip() or code
        sort_order = data.get("sort_order", 0)
        is_active = data.get("is_active", True)
        lookup_value = LookupValue.objects.create(
            lookup=lookup,
            code=code,
            label=label,
            is_active=bool(is_active),
            sort_order=int(sort_order) if sort_order is not None else 0,
            created_by=request.user,
        )
        if (lookup_code or "").upper() == "DAY_BOOK_GROUP":
            try:
                from shared.day_book_groups import clear_day_book_group_cache
                clear_day_book_group_cache()
            except Exception:
                pass
        return Response({
            "message": "Lookup value created successfully",
            "data": {
                "id": lookup_value.id,
                "code": lookup_value.code,
                "label": lookup_value.label,
                "is_active": lookup_value.is_active,
                "sort_order": lookup_value.sort_order,
            },
        }, status=status.HTTP_201_CREATED)


class LookupValueUpdateView(APIView):
    """
    Update an existing lookup value (label, is_active, sort_order). PUT and PATCH supported.
    """
    @method_decorator(admin_auth())
    def put(self, request, lookup_code, value_code, *args, **kwargs):
        return self._update(request, lookup_code, value_code)

    @method_decorator(admin_auth())
    def patch(self, request, lookup_code, value_code, *args, **kwargs):
        return self._update(request, lookup_code, value_code)

    def _update(self, request, lookup_code, value_code):
        lookup_code = (lookup_code or "").upper()
        value_code = (value_code or "").upper()
        try:
            lookup = Lookup.objects.get(code=lookup_code)
            lookup_value = LookupValue.objects.get(lookup=lookup, code=value_code)
        except Lookup.DoesNotExist:
            return Response({"error": f"Lookup '{lookup_code}' not found"}, status=status.HTTP_404_NOT_FOUND)
        except LookupValue.DoesNotExist:
            return Response({"error": f"Value '{value_code}' not found for lookup '{lookup_code}'"}, status=status.HTTP_404_NOT_FOUND)
        data = request.data
        if "label" in data:
            lookup_value.label = (data["label"] or "").strip() or lookup_value.label
        if "is_active" in data:
            lookup_value.is_active = bool(data["is_active"])
        if "sort_order" in data:
            lookup_value.sort_order = int(data["sort_order"]) if data["sort_order"] is not None else 0
        lookup_value.updated_by = request.user
        lookup_value.save()
        if lookup_code == "DAY_BOOK_GROUP":
            try:
                from shared.day_book_groups import clear_day_book_group_cache
                clear_day_book_group_cache()
            except Exception:
                pass
        return Response({
            "message": "Lookup value updated successfully",
            "data": {
                "id": lookup_value.id,
                "code": lookup_value.code,
                "label": lookup_value.label,
                "is_active": lookup_value.is_active,
                "sort_order": lookup_value.sort_order,
            },
        })
