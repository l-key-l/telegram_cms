from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("业务账号", {"fields": ("role", "parent", "display_name", "force_password_change", "created_by")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("业务账号", {"fields": ("role", "parent", "display_name")}),
    )
    list_display = ("username", "display_name", "role", "parent", "is_active", "is_staff")
    list_filter = UserAdmin.list_filter + ("role",)
