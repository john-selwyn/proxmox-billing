from django.urls import path

from . import views

urlpatterns = [
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
