from django.urls import path

from . import views

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("account/", views.account, name="account"),
    path("orders/", views.orders, name="orders"),
    path("orders/confirm/", views.order_confirm, name="order_confirm"),
    path("orders/<int:order_id>/", views.order_detail, name="order_detail"),
    path("invoices/", views.invoices, name="invoices"),
    path("", views.home, name="home"),
    path("register/", views.register, name="register"),
    path("plans/", views.plans, name="plans"),
    path(
        "plans/<int:plan_id>/",
        views.select_plan,
        name="select_plan",
    ),
    path(
        "invoice/<int:order_id>/",
        views.invoice,
        name="invoice",
    ),
]
