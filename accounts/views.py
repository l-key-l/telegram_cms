from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from channel_manager.audit import audit_event
from channel_manager.models import AuditEvent

from .forms import LoginForm, PasswordChangeForm
from .models import User


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("role-home")

    form = LoginForm(request.POST or None, request=request)
    if request.method == "POST":
        username = request.POST.get("username", "")[:150]
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        client_ip = forwarded.split(",", 1)[0].strip() if forwarded else request.META.get("REMOTE_ADDR")
        recent_failures = AuditEvent.objects.filter(
            action="auth.login",
            outcome=AuditEvent.Outcome.FAILED,
            object_id=username,
            ip=client_ip,
            created_at__gte=timezone.now() - timedelta(minutes=10),
        ).count()
        if recent_failures >= 10:
            form.add_error(None, "登录失败次数过多，请 10 分钟后重试。")
            audit_event(
                request=request,
                actor=None,
                owner_user=None,
                action="auth.login",
                object_type="user",
                object_id=username,
                outcome="DENIED",
                error_code="RATE_LIMITED",
            )
            return render(request, "accounts/login.html", {"form": form}, status=429)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            audit_event(
                request=request,
                actor=user,
                owner_user=user,
                action="auth.login",
                object_type="user",
                object_id=str(user.pk),
                outcome="SUCCESS",
            )
            return redirect("role-home")
        audit_event(
            request=request,
            actor=None,
            owner_user=None,
            action="auth.login",
            object_type="user",
            object_id=request.POST.get("username", "")[:150],
            outcome="FAILED",
            error_code="INVALID_CREDENTIALS",
        )
    return render(request, "accounts/login.html", {"form": form})


@login_required
@require_POST
def logout_view(request):
    user = request.user
    audit_event(
        request=request,
        actor=user,
        owner_user=user,
        action="auth.logout",
        object_type="user",
        object_id=str(user.pk),
        outcome="SUCCESS",
    )
    logout(request)
    messages.success(request, "已安全退出。")
    return redirect("login")


@login_required
def role_home(request):
    if request.user.is_superuser:
        return redirect("admin:index")
    if request.user.role == User.Role.MASTER:
        return redirect("master-dashboard")
    return redirect("user-contents")


@login_required
@require_http_methods(["GET", "POST"])
def password_change(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        user.force_password_change = False
        user.save(update_fields=("force_password_change",))
        update_session_auth_hash(request, user)
        audit_event(
            request=request,
            actor=user,
            owner_user=user,
            action="auth.password.change",
            object_type="user",
            object_id=str(user.pk),
        )
        messages.success(request, "密码已更新。")
        return redirect("role-home")
    return render(request, "accounts/password_change.html", {"form": form})
