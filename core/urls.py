from django.urls import path

from core import views
from core.views_protection import protection

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("holdings/", views.holdings, name="holdings"),
    path("holdings/<int:pk>/value/", views.update_value, name="update_value"),
    path("holdings/<int:pk>/priority/", views.toggle_priority, name="toggle_priority"),
    path("tax/", views.tax, name="tax"),
    path("import/", views.import_cas, name="import_cas"),
    path("import/broker/", views.import_broker, name="import_broker"),
    path("import/bank/", views.import_bank, name="import_bank"),
    path("add/", views.add_investment, name="add_investment"),
    path("scan/", views.scan, name="scan"),
    path("funds/", views.funds, name="funds"),
    path("funds/<int:code>/", views.fund_detail, name="fund_detail"),
    path("goals/", views.goals, name="goals"),
    path("protection/", protection, name="protection"),
    path("goals/new/", views.goal_create, name="goal_create"),
    path("goals/settings/", views.goal_settings, name="goal_settings"),
    path("goals/<int:pk>/", views.goal_detail, name="goal_detail"),
    path("goals/<int:pk>/delete/", views.goal_delete, name="goal_delete"),
    path("signup/", views.signup, name="signup"),
]
