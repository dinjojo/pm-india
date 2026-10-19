"""Portfolio-tools routes (owned by the portfolio-tools worker)."""
from django.urls import path

from core import views_portfolio

urlpatterns = [
    # Upload holdings CSV / pick tracked funds → MPT + CAPM optimisation, overlap, baskets.
    path("portfolio-tools/", views_portfolio.portfolio_tools, name="portfolio_tools"),
]
