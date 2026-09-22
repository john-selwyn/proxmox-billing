import uuid
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from .forms import AccountForm, BillingCycleForm, RegistrationForm
from .models import Customer, Invoice, Order, Subscription, VPSPlan
from .services import confirm_order, plan_amount

CHECKOUT_SALT = 'billing.order-review'


def customer_for(user):
    # Support accounts that predate automatic customer profiles.
    return Customer.objects.get_or_create(user=user, defaults={
        'full_name': user.get_full_name() or user.get_username(),
    })[0]


@require_GET
def home(request):
    return render(request, 'billing/home.html')


@require_http_methods(['GET', 'POST'])
def register(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = RegistrationForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            user = form.save(commit=False)
            user.email = form.cleaned_data['email']
            user.save()
            customer = customer_for(user)
            customer.full_name = form.cleaned_data['full_name']
            customer.save(update_fields=['full_name'])
        login(request, user)
        return redirect('dashboard')
    return render(request, 'billing/register.html', {'form': form})


@login_required
@require_GET
def dashboard(request):
    customer = customer_for(request.user)
    records = customer.orders.select_related('plan', 'invoice').order_by('-created_at', '-pk')
    return render(request, 'billing/dashboard.html', {
        'customer': customer, 'order_count': records.count(),
        'active_count': customer.subscriptions.filter(status=Subscription.Status.ACTIVE).count(),
        'pending_count': Invoice.objects.filter(order__customer=customer, status=Invoice.Status.PENDING).count(),
        'orders': records[:5],
    })


@login_required
@require_GET
def plans(request):
    return render(request, 'billing/plans.html', {
        'plans': VPSPlan.objects.filter(is_active=True).order_by('monthly_price', 'pk'),
    })


@login_required
@require_http_methods(['GET', 'POST'])
def select_plan(request, plan_id):
    plan = get_object_or_404(VPSPlan, pk=plan_id, is_active=True)
    form = BillingCycleForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        cycle = form.cleaned_data['billing_cycle']
        try:
            amount = plan_amount(plan, cycle)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            token = signing.dumps({'user': request.user.pk, 'plan': plan.pk,
                'cycle': cycle, 'amount': str(amount), 'key': str(uuid.uuid4())}, salt=CHECKOUT_SALT)
            return render(request, 'billing/review_order.html', {
                'plan': plan, 'cycle': cycle, 'amount': amount, 'checkout_token': token,
            })
    return render(request, 'billing/select_plan.html', {'plan': plan, 'form': form},
                  status=400 if request.method == 'POST' else 200)


@login_required
@require_POST
def order_confirm(request):
    try:
        data = signing.loads(request.POST.get('checkout_token', ''), salt=CHECKOUT_SALT, max_age=3600)
        if data['user'] != request.user.pk:
            raise signing.BadSignature('Wrong account')
    except (signing.BadSignature, KeyError, TypeError):
        return render(request, 'billing/checkout_error.html', {
            'error': 'This order review is invalid or expired. Please select your plan again.',
        }, status=400)
    try:
        order = confirm_order(customer=customer_for(request.user), plan_id=data['plan'],
            cycle=data['cycle'], checkout_token=data['key'], reviewed_amount=data['amount'])
    except (ValidationError, VPSPlan.DoesNotExist) as exc:
        error = 'This plan is no longer available. Please choose another plan.'
        if isinstance(exc, ValidationError):
            error = ' '.join(exc.messages)
        return render(request, 'billing/checkout_error.html', {'error': error}, status=400)
    return redirect('invoice', order_id=order.pk)


@login_required
@require_GET
def orders(request):
    records = Order.objects.filter(customer__user=request.user).select_related('plan', 'invoice').order_by('-created_at', '-pk')
    return render(request, 'billing/orders.html', {'page_obj': Paginator(records, 20).get_page(request.GET.get('page'))})


@login_required
@require_GET
def order_detail(request, order_id):
    order = get_object_or_404(Order.objects.select_related('plan', 'invoice'), pk=order_id, customer__user=request.user)
    return render(request, 'billing/order_detail.html', {'order': order})


@login_required
@require_GET
def invoices(request):
    records = Invoice.objects.filter(order__customer__user=request.user).select_related('order').order_by('-created_at', '-pk')
    return render(request, 'billing/invoices.html', {'page_obj': Paginator(records, 20).get_page(request.GET.get('page'))})


@login_required
@require_GET
def invoice(request, order_id):
    record = get_object_or_404(Invoice.objects.select_related('order__plan', 'order__customer'),
                              order_id=order_id, order__customer__user=request.user)
    return render(request, 'billing/invoice.html', {'invoice': record, 'order': record.order})


@login_required
@require_http_methods(['GET', 'POST'])
def account(request):
    form = AccountForm(request.POST if request.method == 'POST' else None, instance=customer_for(request.user))
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Your account details have been updated.')
        return redirect('account')
    return render(request, 'billing/account.html', {'form': form})
