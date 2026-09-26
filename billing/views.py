import uuid
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from .xendit import configured as xendit_configured
from .forms import AccountForm, BillingCycleForm, RegistrationForm
from .models import Customer, Invoice, Order, Subscription, VPSPlan, VPSPowerOperation
from .provisioning import get_vps_provisioning_status
from .services import confirm_order, plan_amount
from .vps_control import VPSControlError, get_vps_runtime, sync_power_operation

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
        'services': records.filter(
            invoice__status=Invoice.Status.PAID,
            status__in=[Order.Status.PAID, Order.Status.PROVISIONING, Order.Status.ACTIVE, Order.Status.FAILED],
        )[:3],
    })


@login_required
@require_GET
def my_vps(request):
    services = (
        Order.objects.filter(customer__user=request.user, invoice__status=Invoice.Status.PAID)
        .filter(status__in=[Order.Status.PAID, Order.Status.PROVISIONING, Order.Status.ACTIVE, Order.Status.FAILED])
        .select_related('plan', 'invoice')
        .order_by('-created_at', '-pk')
    )
    return render(request, 'billing/my_vps.html', {'services': services})


@login_required
@require_GET
def vps_detail(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related('plan', 'invoice', 'subscription'),
        pk=order_id,
        customer__user=request.user,
        invoice__status=Invoice.Status.PAID,
    )
    return render(request, 'billing/vps_detail.html', {'order': order})


@login_required
@require_GET
def vps_runtime_status(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related('invoice'),
        pk=order_id,
        customer__user=request.user,
        invoice__status=Invoice.Status.PAID,
    )
    if order.status != Order.Status.ACTIVE:
        return JsonResponse({
            'available': False,
            'state': order.status.lower(),
            'vmid': order.provisioning_vmid,
            'ip_address': order.provisioning_ip_address or '',
            'operation': None,
        })

    operation = (
        order.power_operations
        .filter(status__in=[
            VPSPowerOperation.Status.PENDING,
            VPSPowerOperation.Status.RUNNING,
            VPSPowerOperation.Status.UNKNOWN,
        ])
        .order_by('-created_at', '-pk')
        .first()
    )
    if operation is not None:
        try:
            sync_power_operation(operation)
        except VPSControlError:
            operation.refresh_from_db()

    operation_data = None
    if operation is not None:
        operation_data = {
            'action': operation.action,
            'status': operation.status,
            'result': operation.result,
            'observed_state': operation.observed_state,
            'error': bool(operation.error_code),
        }

    try:
        runtime = get_vps_runtime(order.pk)
    except VPSControlError:
        return JsonResponse({
            'available': False,
            'state': 'unknown',
            'vmid': order.provisioning_vmid,
            'ip_address': order.provisioning_ip_address or '',
            'error': True,
            'operation': operation_data,
        })

    if runtime.ip_address and runtime.ip_address != order.provisioning_ip_address:
        Order.objects.filter(pk=order.pk).update(provisioning_ip_address=runtime.ip_address)

    return JsonResponse({
        'available': True,
        'state': runtime.state,
        'vmid': order.provisioning_vmid,
        'ip_address': runtime.ip_address or order.provisioning_ip_address or '',
        'operation': operation_data,
    })


@login_required
@require_POST
def vps_power(request, order_id, action):
    if action not in {'start', 'shutdown', 'reboot'}:
        return JsonResponse({'error': 'Invalid power action.'}, status=400)

    try:
        with transaction.atomic():
            order = get_object_or_404(
                Order.objects.select_for_update(),
                pk=order_id,
                customer__user=request.user,
                invoice__status=Invoice.Status.PAID,
                status=Order.Status.ACTIVE,
            )
            existing = order.power_operations.filter(status__in=[
                VPSPowerOperation.Status.PENDING,
                VPSPowerOperation.Status.RUNNING,
                VPSPowerOperation.Status.UNKNOWN,
            ]).order_by('-created_at', '-pk').first()
            if existing is not None:
                messages.error(request, 'A VPS power operation is already in progress or awaiting review.')
                return redirect('vps_detail', order_id=order.pk)
            operation = VPSPowerOperation.objects.create(order=order, action=action)
    except IntegrityError:
        messages.error(request, 'A VPS power operation is already in progress.')
        return redirect('vps_detail', order_id=order_id)

    try:
        sync_power_operation(operation, force=True)
    except VPSControlError:
        operation.refresh_from_db()
        if operation.status == VPSPowerOperation.Status.FAILED:
            messages.error(request, 'The VPS power command was rejected.')
        elif operation.status == VPSPowerOperation.Status.UNKNOWN:
            messages.error(request, 'The VPS power command status needs support review before another command can be sent.')
        else:
            messages.info(request, 'The VPS power command is awaiting confirmation. It will be checked again safely.')
    else:
        if operation.status == VPSPowerOperation.Status.SUCCEEDED:
            if operation.result == VPSPowerOperation.Result.NOOP:
                messages.success(request, 'Your VPS is already in the requested state.')
            else:
                messages.success(request, f'{action.title()} completed successfully.')
        elif operation.status in {VPSPowerOperation.Status.PENDING, VPSPowerOperation.Status.RUNNING}:
            messages.success(request, f'{action.title()} command accepted and is processing.')
        elif operation.status == VPSPowerOperation.Status.UNKNOWN:
            messages.error(request, 'The VPS power command status needs support review.')
        else:
            messages.error(request, 'The VPS power command failed.')
    return redirect('vps_detail', order_id=order.pk)


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
    return render(request, 'billing/order_detail.html', {'order': order, **payment_context(order)})


@login_required
@require_GET
def provisioning_status(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related('invoice'),
        pk=order_id,
        customer__user=request.user,
    )
    if order.invoice.status == Invoice.Status.PAID and order.status in (
        Order.Status.PAID, Order.Status.PROVISIONING, Order.Status.ACTIVE
    ):
        try:
            order = get_vps_provisioning_status(order)
        except ValidationError:
            order.refresh_from_db()

    return JsonResponse({
        'status': order.status,
        'progress': order.provisioning_progress,
        'step': order.provisioning_step,
        'vmid': order.provisioning_vmid,
        'ip_address': order.provisioning_ip_address or '',
        'error': bool(order.provisioning_error),
    })


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
    return render(request, 'billing/invoice.html', {'invoice': record, 'order': record.order, **payment_context(record.order)})


@login_required
@require_http_methods(['GET', 'POST'])
def account(request):
    form = AccountForm(request.POST if request.method == 'POST' else None, instance=customer_for(request.user))
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Your account details have been updated.')
        return redirect('account')
    return render(request, 'billing/account.html', {'form': form})


def payment_context(order):
    return {'xendit_enabled': xendit_configured(),
            'checkout': order.checkouts.order_by('-created_at', '-pk').first()}
