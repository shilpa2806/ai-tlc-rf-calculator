"""Custom middleware."""

from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse


class ContentSecurityPolicyMiddleware:
    """Attach a Content-Security-Policy header from ``settings.CONTENT_SECURITY_POLICY``.

    Kept as a tiny local middleware rather than a dependency; the policy string
    is assembled in settings so it can vary by environment.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        policy = settings.CONTENT_SECURITY_POLICY
        if policy:
            response.setdefault("Content-Security-Policy", policy)
        return response
