"""Root URL configuration for the TLC RF Calculator project."""

from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    # Include URLs from the tlc app
    path("", include("tlc.urls")),
    # Serve user media (annotated images, CSVs). This is a low-traffic internal
    # tool, so serving media through Django is acceptable; a reverse proxy could
    # take this over in a higher-traffic deployment.
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
