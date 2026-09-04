from django.urls import re_path

from shared.services.scale import ScaleConsumer

websocket_urlpatterns = [
    re_path(r"^ws/scale/(?P<machine_id>[\w-]+)/$", ScaleConsumer.as_asgi()),
]
