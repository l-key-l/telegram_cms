from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from accounts import views as account_views


urlpatterns = [
    path("system-admin/", admin.site.urls),
    path("login/", account_views.login_view, name="login"),
    path("logout/", account_views.logout_view, name="logout"),
    path("password/change/", account_views.password_change, name="password-change"),
    path("", account_views.role_home, name="role-home"),
    path("", include("channel_manager.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
