from django.urls import path

from . import payment_views, views

urlpatterns = [
    path("orders/<int:order_id>/pay/", payment_views.pay_now, name="pay_now"),
    path("payments/xendit/<int:order_id>/return/", payment_views.payment_return, name="xendit_return"),
    path("payments/xendit/<int:order_id>/cancel/", payment_views.payment_return, name="xendit_cancel"),
    path("payments/xendit/webhook/", payment_views.webhook, name="xendit_webhook"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("vps/", views.my_vps, name="my_vps"),
    path("vps/<int:order_id>/", views.vps_detail, name="vps_detail"),
    path("vps/<int:order_id>/runtime/", views.vps_runtime_status, name="vps_runtime_status"),
    path("vps/<int:order_id>/power/<str:action>/", views.vps_power, name="vps_power"),
    path("account/", views.account, name="account"),
    path("orders/", views.orders, name="orders"),
    path("orders/confirm/", views.order_confirm, name="order_confirm"),
    path("orders/<int:order_id>/", views.order_detail, name="order_detail"),
    path("orders/<int:order_id>/provisioning-status/", views.provisioning_status, name="customer_provisioning_status"),
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
