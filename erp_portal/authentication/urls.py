from django.urls import path
from . import views
from .views import energy_points, get_employees
from django.views.generic import RedirectView

urlpatterns = [
    path("login/", views.frappe_login, name="frappe_login"),
    path("logout/", views.logout_view, name="logout"),
    path("", views.punch, name="punch"),
    path("punch/", views.punch, name="punch"),
    path("punch/select_customer/", views.select_customer, name="select_customer"),
    path("api/get_opportunities/", views.get_opportunities, name="get_opportunities"),
    path("api/get_opportunity_items/", views.get_opportunity_items, name="get_opportunity_items"),
    path("api/nearby_customers/", views.nearby_customers, name="nearby_customers"),
    path("api/update_customer_location/", views.update_customer_location, name="update_customer_location"),
    path("api/admin/push_customer_location/", views.admin_push_customer_location, name="admin_push_customer_location"),
    path("energy/", energy_points, name="energy_points"),
    path('accounts/login/', RedirectView.as_view(pattern_name='frappe_login', permanent=False)),
    path("employees", get_employees, name="get_employees"),
]
