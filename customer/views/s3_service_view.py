import boto3
import logging
import os
from datetime import datetime
from django.conf import settings
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from botocore.exceptions import ClientError
from shared.services.s3_service import upload_file_to_s3, list_files_from_s3, generate_presigned_url

from customer.auth.customer_auth import CustomerAuthentication

logger = logging.getLogger(__name__)

@api_view(["POST"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def upload_document(request):
    file = request.FILES.get("document")

    if not file:
        return Response({"error": "No document uploaded"}, status=400)

    customer_id = request.user.id
    file_extension = os.path.splitext(file.name)[1]
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")

    object_name = f"Taranya/verification/customer_{customer_id}/{timestamp}{file_extension}"

    success = upload_file_to_s3(file, object_name)

    if not success:
        return Response({"error": "Upload failed"}, status=500)

    return Response({
        "message": "Uploaded successfully",
        "file_key": object_name
    })

@api_view(["GET"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def admin_list_customer_files(request):
    customer_id = request.query_params.get("customer_id")

    if not customer_id:
        return Response({"error": "customer_id required"}, status=400)

    prefix = f"Taranya/"

    files = list_files_from_s3(prefix)

    return Response({
        "customer_id": customer_id,
        "files": files
    })

@api_view(["GET"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def generate_download_link(request):
    object_name = request.query_params.get("file_key")

    if not object_name:
        return Response({"error": "file_key required"}, status=400)

    url = generate_presigned_url(object_name)

    if not url:
        return Response({"error": "Could not generate link"}, status=500)

    return Response({
        "download_url": url
    })