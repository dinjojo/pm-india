"""Fund-analytics routes (owned by the fund-analytics worker)."""
from django.urls import path

from core import views_funds

urlpatterns = [
    # Analytics deep-dive for one scheme (metrics, MA signals, Monte Carlo, CAPM).
    path("<int:code>/analytics/", views_funds.scheme_analytics, name="scheme_analytics"),
    # Returns calculator (lumpsum / SIP with inflation adjustment).
    path("calculator/", views_funds.calculator, name="returns_calculator"),
]
