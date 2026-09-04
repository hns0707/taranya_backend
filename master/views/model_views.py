# from django.utils.timezone import now
# from rest_framework.decorators import api_view
# from rest_framework.response import Response

# from shared.models import LookupValue, ModelItem


# MODEL_ITEM_LOOKUP_FIELDS = [
#     "method",
#     "making_category",
#     "special_charges",
#     "charges_type",
#     "crafting_process",
#     "nature",
#     "finishing",
#     "discount_rank",
# ]

# MODEL_ITEM_BASE_FIELDS = [
#     "model_code",
#     "model_name",
# ]


# def _serialize_lookup(field_value):
#     if not field_value:
#         return None

#     return {
#         "id": field_value.id,
#         "label": field_value.label,
#     }


# def _serialize_model_item(item):
#     return {
#         "id": item.id,
#         "model_code": item.model_code,
#         "model_name": item.model_name,
#         "method": _serialize_lookup(item.method),
#         "method_id": item.method_id,
#         "making_category": _serialize_lookup(item.making_category),
#         "making_category_id": item.making_category_id,
#         "special_charges": _serialize_lookup(item.special_charges),
#         "special_charges_id": item.special_charges_id,
#         "charges_type": _serialize_lookup(item.charges_type),
#         "charges_type_id": item.charges_type_id,
#         "crafting_process": _serialize_lookup(item.crafting_process),
#         "crafting_process_id": item.crafting_process_id,
#         "nature": _serialize_lookup(item.nature),
#         "nature_id": item.nature_id,
#         "finishing": _serialize_lookup(item.finishing),
#         "finishing_id": item.finishing_id,
#         "discount_rank": _serialize_lookup(item.discount_rank),
#         "discount_rank_id": item.discount_rank_id,
#         "is_active": item.is_active,
#     }


# def _get_lookup_value(value_id):
#     if value_id in (None, ""):
#         return None

#     return LookupValue.objects.get(id=value_id)


# def _get_bool_value(value, default=None):
#     if value is None:
#         return default
#     if isinstance(value, bool):
#         return value
#     if isinstance(value, str):
#         normalized = value.strip().lower()
#         if normalized in ("true", "1", "yes", "on"):
#             return True
#         if normalized in ("false", "0", "no", "off"):
#             return False
#     return value


# @api_view(["POST"])
# def create_model_item(request):
#     try:
#         create_values = {
#             field_name: request.data.get(field_name)
#             for field_name in MODEL_ITEM_BASE_FIELDS
#         }
#         create_values.update(
#             {
#                 field_name: _get_lookup_value(request.data.get(field_name))
#                 for field_name in MODEL_ITEM_LOOKUP_FIELDS
#             }
#         )
#         create_values["is_active"] = _get_bool_value(request.data.get("is_active"), True)
#         create_values["system_created_at"] = now()
#         create_values["system_updated_at"] = now()

#         model_item = ModelItem.objects.create(**create_values)

#         return Response(
#             {
#                 "message": "Model item created successfully",
#                 "id": model_item.id,
#             }
#         )

#     except Exception as e:
#         return Response({"error": str(e)}, status=400)


# @api_view(["GET"])
# def get_model_items(request):
#     items = ModelItem.objects.select_related(*MODEL_ITEM_LOOKUP_FIELDS).all()

#     data = [_serialize_model_item(item) for item in items]

#     return Response({"data": data})


# @api_view(["GET"])
# def get_model_item(request, id):
#     try:
#         item = ModelItem.objects.select_related(*MODEL_ITEM_LOOKUP_FIELDS).get(id=id)
#         return Response(_serialize_model_item(item))

#     except ModelItem.DoesNotExist:
#         return Response({"error": "Model item not found"}, status=404)


# @api_view(["PUT"])
# def update_model_item(request, id):
#     try:
#         item = ModelItem.objects.get(id=id)

#         for field_name in MODEL_ITEM_BASE_FIELDS:
#             if field_name in request.data:
#                 setattr(item, field_name, request.data.get(field_name))

#         for field_name in MODEL_ITEM_LOOKUP_FIELDS:
#             if field_name in request.data:
#                 setattr(item, field_name, _get_lookup_value(request.data.get(field_name)))

#         if "is_active" in request.data:
#             item.is_active = _get_bool_value(request.data.get("is_active"), item.is_active)

#         item.system_updated_at = now()
#         item.save()

#         return Response({"message": "Model item updated successfully"})

#     except ModelItem.DoesNotExist:
#         return Response({"error": "Model item not found"}, status=404)
#     except Exception as e:
#         return Response({"error": str(e)}, status=400)
