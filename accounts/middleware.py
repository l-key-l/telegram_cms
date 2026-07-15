from __future__ import annotations

from django.shortcuts import redirect
from django.urls import reverse


class ForcePasswordChangeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated and user.force_password_change:
            allowed_paths = {reverse("password-change"), reverse("logout")}
            if request.path not in allowed_paths and not request.path.startswith(("/static/", "/media/")):
                return redirect("password-change")
        return self.get_response(request)
