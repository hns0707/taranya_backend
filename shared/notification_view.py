"""
Notification APIs shared across apps.
"""
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from master.permissions.permission_checker import admin_auth
from shared.models import AdminNotification, AdminUserNotification


def create_admin_notification(
    message,
    section_code,
    title,
    notification_type,
    customer_id,
    installment_id=None
):
    """
    Internal helper to create an admin notification record.

    This is not an API endpoint. Import and call this function directly
    from services/views/jobs wherever needed.
    """
    if not title:
        raise ValueError("title is required")
    if not section_code:
        raise ValueError("section_code is required")
    if not notification_type:
        raise ValueError("notification_type is required")
    if not customer_id:
        raise ValueError("customer_id is required")

    notification = AdminNotification.objects.create(
        title=str(title).strip(),
        section_code=str(section_code).strip(),
        type=str(notification_type).strip(),
        customer_id=customer_id,
        installment_id=installment_id,
        message=(str(message).strip() if message is not None else ""),
    )
    return notification


@api_view(["PUT", "POST"])
@admin_auth()
def get_user_notifications(request):
    """
    Get paginated notifications for the authenticated admin user.

    Payload:
    - page: int (optional, default=0, used as offset: 0, 10, 20...)
    """
    admin_user = getattr(request, "admin_user", None) or getattr(request, "user", None)
    payload = request.data
    page = payload.get("page", 0)

    if admin_user is None:
        return Response(
            {"error": "Authenticated user not found"},
            status=status.HTTP_401_UNAUTHORIZED
        )

    try:
        page = int(page)
    except (TypeError, ValueError):
        return Response(
            {"error": "page must be an integer"},
            status=status.HTTP_400_BAD_REQUEST
        )

    if page < 0:
        return Response(
            {"error": "page must be greater than or equal to 0"},
            status=status.HTTP_400_BAD_REQUEST
        )

    user_id = admin_user.id
    query_user_id = payload.get("user_id")
    notification_id = payload.get("notification_id")

    if request.method == "PUT" and query_user_id is not None and notification_id is not None:
        try:
            query_user_id = int(query_user_id)
            notification_id = int(notification_id)
        except (TypeError, ValueError):
            return Response(
                {"error": "user_id and notification_id must be integers"},
                status=status.HTTP_400_BAD_REQUEST
            )

        updated_count = AdminUserNotification.objects.filter(
            admin_user_id=query_user_id,
            notification_id=notification_id
        ).update(isView=True)

        return Response(
            {
                "user_id": query_user_id,
                "notification_id": notification_id,
                "updated": updated_count,
            },
            status=status.HTTP_200_OK
        )

    page_size = 10
    start = page * page_size
    end = start + page_size

    queryset = AdminUserNotification.objects.filter(
        admin_user_id=user_id
    ).select_related("notification").order_by("-system_created_at")

    notifications = queryset[start:end]
    data = [{
        "id": row.notification.id,
        "type": row.notification.type,
        "title": row.notification.title,
        "customer_id": row.notification.customer_id,
        "message": row.notification.message,
        "is_view": row.isView,
        "created_at": row.system_created_at,
    } for row in notifications]

    return Response({
        "user_id": user_id,
        "page": page,
        "page_size": page_size,
        "count": queryset.count(),
        "notifications": data
    }, status=status.HTTP_200_OK)
