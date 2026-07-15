from __future__ import annotations

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect

from accounts.models import User


def master_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if request.user.is_superuser or request.user.role == User.Role.MASTER:
            return view_func(request, *args, **kwargs)
        messages.error(request, "此页面仅主账号可访问。")
        return redirect("user-contents")

    return wrapped


def sub_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if request.user.role == User.Role.SUB:
            return view_func(request, *args, **kwargs)
        messages.error(request, "此页面仅子账号可访问。")
        return redirect("master-dashboard")

    return wrapped
