from decimal import Decimal

from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import Customer, Invoice, Order, VPSPlan


def home(request):
    return render(request, "billing/home.html")


def register(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)

        if form.is_valid():
            user = form.save()

            Customer.objects.create(
                user=user,
                full_name=user.username,
            )

            login(request, user)
            return redirect("plans")
    else:
        form = UserCreationForm()

    return render(request, "billing/register.html", {"form": form})


@login_required
def plans(request):
    plans = VPSPlan.objects.filter(is_active=True)

    return render(
        request,
        "billing/plans.html",
        {"plans": plans},
    )


@login_required
def select_plan(request, plan_id):
    plan = get_object_or_404(
        VPSPlan,
        id=plan_id,
        is_active=True,
    )

    if request.method == "POST":
        billing_cycle = request.POST.get("billing_cycle")

        if billing_cycle not in (
            Order.BillingCycle.MONTHLY,
            Order.BillingCycle.YEARLY,
        ):
            return render(
                request,
                "billing/select_plan.html",
                {
                    "plan": plan,
                    "error": "Invalid billing cycle.",
                },
            )

        if billing_cycle == Order.BillingCycle.MONTHLY:
            amount = plan.monthly_price
        else:
            amount = plan.yearly_price

        customer = request.user.customer_profile

        order = Order.objects.create(
            customer=customer,
            plan=plan,
            billing_cycle=billing_cycle,
            amount=amount,
        )

        Invoice.objects.create(
            order=order,
            invoice_number=f"INV-{order.id:08d}",
            amount=amount,
            due_date=timezone.now(),
        )

        return redirect("invoice", order_id=order.id)

    return render(
        request,
        "billing/select_plan.html",
        {"plan": plan},
    )


@login_required
def invoice(request, order_id):
    customer = request.user.customer_profile

    order = get_object_or_404(
        Order,
        id=order_id,
        customer=customer,
    )

    invoice = order.invoice

    return render(
        request,
        "billing/invoice.html",
        {
            "order": order,
            "invoice": invoice,
        },
    )
