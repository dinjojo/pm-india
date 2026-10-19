from django.urls import path

from core import views_screener

urlpatterns = [
    path("", views_screener.screener, name="screener"),
]
