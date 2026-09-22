import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from . import xendit
from .checkouts import process_event, start_checkout
from .models import Invoice, Order


@transaction.non_atomic_requests
@login_required
@require_POST
def pay_now(request, order_id):
    order = get_object_or_404(Order, pk=order_id, customer__user=request.user)
    get_object_or_404(Invoice, order=order)
    if order.status != 'PENDING':
        return redirect('invoice', order_id=order.pk)
    try:
        checkout = start_checkout(order.pk)
    except (xendit.XenditError, IntegrityError):
        return render(request, 'billing/payment_return.html', {
            'order': order, 'payment_unavailable': True,
        }, status=503)
    if checkout.status != 'ACTIVE':
        return redirect('xendit_return', order_id=order.pk)
    return redirect(xendit.checkout_url(checkout.payment_link_url))


@login_required
@require_GET
def payment_return(request, order_id):
    # No API request and no state change. A browser is never evidence of payment.
    order = get_object_or_404(Order.objects.select_related('invoice'), pk=order_id, customer__user=request.user)
    return render(request, 'billing/payment_return.html', {'order': order})


@transaction.non_atomic_requests
@csrf_exempt
@require_POST
@sensitive_variables()
def webhook(request):
    expected = settings.XENDIT_WEBHOOK_TOKEN
    supplied = request.headers.get('x-callback-token', '')
    if not expected or not supplied or not constant_time_compare(supplied, expected):
        return JsonResponse({'error': 'UNAUTHORIZED'}, status=401)
    if not xendit.configured():
        return JsonResponse({'error': 'UNAVAILABLE'}, status=503)
    if len(request.body) > 65536:
        return JsonResponse({'error': 'INVALID_EVENT'}, status=400)
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'INVALID_EVENT'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'error': 'INVALID_EVENT'}, status=400)
    event = payload.get('event')
    if event not in ('payment_session.completed', 'payment_session.expired'):
        return HttpResponse(status=200)
    if payload.get('business_id') != settings.XENDIT_BUSINESS_ID:
        return JsonResponse({'error': 'VERIFICATION_REJECTED'}, status=400)
    try:
        process_event(event, payload.get('data'))
    except xendit.XenditError as exc:
        return JsonResponse({'error': 'VERIFICATION_UNAVAILABLE' if exc.retryable else 'VERIFICATION_REJECTED'},
                            status=503 if exc.retryable else 400)
    return HttpResponse(status=200)
