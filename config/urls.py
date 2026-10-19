from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
    path("funds/", include("core.urls_funds")),
    path("tools/", include("core.urls_portfolio")),
]
