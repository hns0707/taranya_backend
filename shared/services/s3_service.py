import boto3
import logging
from urllib.parse import quote

from django.conf import settings
from botocore.exceptions import ClientError
from urllib.parse import quote

logger = logging.getLogger(__name__)


def build_public_object_url(object_key: str) -> str:
    """
    Build a stable HTTPS URL for an S3 object key stored in this project.

    Uses AWS_S3_PUBLIC_BASE_URL or AWS_S3_BASE_URL when set (e.g. CloudFront),
    otherwise virtual-hosted–style: https://{bucket}.s3.{region}.amazonaws.com/{key}
    Key path segments are percent-encoded for safe use in browsers and APIs.
    """
    key = (object_key or "").strip().lstrip("/")
    if not key:
        return ""

    base = getattr(settings, "AWS_S3_PUBLIC_BASE_URL", None) or getattr(
        settings, "AWS_S3_BASE_URL", None
    )
    bucket = getattr(settings, "AWS_S3_BUCKET_NAME", None) or ""
    region = (getattr(settings, "AWS_REGION", None) or "us-east-1").strip()
    if not base:
        if bucket:
            base = f"https://{bucket}.s3.{region}.amazonaws.com"
        else:
            base = "https://s3.amazonaws.com"

    base = base.rstrip("/")
    encoded_key = quote(key, safe="/")
    return f"{base}/{encoded_key}"


def get_s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION
    )


# 1️⃣ Upload File
def upload_file_to_s3(file_obj, object_name):
    s3 = get_s3_client()
    try:
        s3.upload_fileobj(
            file_obj,
            settings.AWS_S3_BUCKET_NAME,
            object_name,
            ExtraArgs={
                "ContentType": getattr(file_obj, "content_type", "application/octet-stream")
            }
        )
        return True
    except ClientError as e:
        logger.error(f"S3 Upload Failed: {str(e)}")
        return False


#  (Admin Use)
def list_files_from_s3(prefix):
    s3 = get_s3_client()
    try:
        response = s3.list_objects_v2(
            Bucket=settings.AWS_S3_BUCKET_NAME,
            Prefix=prefix
        )

        files = []

        for obj in response.get("Contents", []):
            files.append({
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": obj["LastModified"]
            })

        return files

    except ClientError as e:
        logger.error(f"S3 List Failed: {str(e)}")
        return []


# Generate Download URL
def generate_presigned_url(object_name, expiration=300):

    s3 = get_s3_client()

    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.AWS_S3_BUCKET_NAME,
                "Key": object_name,
                "ResponseContentDisposition": "attachment"
            },
            ExpiresIn=expiration,
        )

        return url

    except ClientError as e:
        logger.error(f"Presigned URL generation failed: {str(e)}")
        return None


def delete_file_from_s3(object_name):
    s3 = get_s3_client()

    try:
        s3.delete_object(
            Bucket=settings.AWS_S3_BUCKET_NAME,
            Key=object_name
        )
        return True
    except ClientError as e:
        logger.error(f"S3 Delete Failed: {str(e)}")
        return False


def build_s3_file_url(object_name: str) -> str:
    """
    Build a direct S3 URL for a stored object key.
    Assumes objects are publicly readable (or served by bucket/CDN policy).
    """
    bucket = (settings.AWS_S3_BUCKET_NAME or "").strip()
    region = (settings.AWS_REGION or "").strip()
    key = quote((object_name or "").lstrip("/"), safe="/")
    if not bucket:
        return key
    if region:
        return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
    return f"https://s3.amazonaws.com/{bucket}/{key}"
