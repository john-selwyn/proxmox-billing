"""Xendit protocol boundary. Only fixed safe errors leave the HTTP client."""
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.urls import reverse
from django.views.decorators.debug import sensitive_variables

API_BASE = 'https://api.xendit.co'
PAYMENTS_API_VERSION = '2024-11-11'
CHECKOUT_HOSTS = {'checkout.xendit.co', 'checkout-staging.xendit.co', 'xen.to', 'dev.xen.to'}


class XenditError(Exception):
    def __init__(self, code, *, retryable=False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def https_origin(value):
    try:
        url = urlsplit(value)
        return bool(url.scheme == 'https' and url.hostname and not url.username and
                    not url.password and not url.query and not url.fragment and
                    url.path in ('', '/') and url.port in (None, 443))
    except (ValueError, TypeError):
        return False


def configured():
    return bool(settings.XENDIT_SECRET_API_KEY and settings.XENDIT_WEBHOOK_TOKEN and
                settings.XENDIT_BUSINESS_ID and https_origin(settings.XENDIT_PUBLIC_BASE_URL))


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise XenditError('INVALID_IDENTIFIER')
    return value


def checkout_url(value):
    try:
        url = urlsplit(value)
        valid = (isinstance(value, str) and len(value) <= 2048 and url.scheme == 'https' and
                 url.hostname in CHECKOUT_HOSTS and not url.username and not url.password and
                 url.port in (None, 443) and not any(ord(c) < 32 for c in value))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise XenditError('INVALID_CHECKOUT_URL')
    return value


def amount(value):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise XenditError('AMOUNT_MISMATCH') from None
    if not parsed.is_finite() or parsed <= 0:
        raise XenditError('AMOUNT_MISMATCH')
    return parsed


@sensitive_variables()
def api_request(method, path, *, payload=None, payment_api=False):
    if not configured():
        raise XenditError('NOT_CONFIGURED', retryable=True)
    headers = {'Content-Type': 'application/json'}
    if payment_api:
        headers['api-version'] = PAYMENTS_API_VERSION
    try:
        with requests.Session() as client:
            client.trust_env = False
            response = client.request(method, API_BASE + path, json=payload,
                auth=(settings.XENDIT_SECRET_API_KEY, ''), headers=headers,
                timeout=(5, 20), allow_redirects=False, verify=True)
            if not 200 <= response.status_code < 300:
                raise XenditError('API_HTTP_ERROR', retryable=True)
            try:
                data = response.json(parse_float=Decimal)
            except ValueError:
                raise XenditError('INVALID_JSON', retryable=True) from None
    except requests.Timeout:
        raise XenditError('TIMEOUT', retryable=True) from None
    except requests.RequestException:
        raise XenditError('CONNECTION_ERROR', retryable=True) from None
    if not isinstance(data, dict):
        raise XenditError('INVALID_RESPONSE', retryable=True)
    return data


def create_session(checkout):
    base = settings.XENDIT_PUBLIC_BASE_URL.rstrip('/')
    payload = {
        'reference_id': checkout.reference_id, 'session_type': 'PAY', 'mode': 'PAYMENT_LINK',
        'currency': 'PHP', 'country': 'PH', 'amount': float(checkout.amount),
        'capture_method': 'AUTOMATIC', 'allow_save_payment_method': 'DISABLED',
        'description': f'VPS order #{checkout.order_id}',
        'success_return_url': base + reverse('xendit_return', args=[checkout.order_id]),
        'cancel_return_url': base + reverse('xendit_cancel', args=[checkout.order_id]),
    }
    # Customer is optional for PAY + DISABLED. Hosted checkout collects any
    # channel-specific details; do not manufacture names or duplicate customers.
    return api_request('POST', '/sessions', payload=payload)


def retrieve_session(session_id):
    return api_request('GET', '/sessions/' + identifier(session_id))


def retrieve_payment(payment_id):
    return api_request('GET', '/v3/payments/' + identifier(payment_id), payment_api=True)


def verify_session(data, checkout, *, session_id, completed=False):
    expected = {'payment_session_id': session_id, 'reference_id': checkout.reference_id,
                'business_id': settings.XENDIT_BUSINESS_ID, 'session_type': 'PAY',
                'currency': 'PHP', 'country': 'PH'}
    if any(data.get(key) != value for key, value in expected.items()):
        raise XenditError('SESSION_MISMATCH')
    if amount(data.get('amount')) != checkout.amount:
        raise XenditError('AMOUNT_MISMATCH')
    if completed and data.get('status') != 'COMPLETED':
        raise XenditError('SESSION_NOT_COMPLETED')
    if data.get('status') not in ('ACTIVE', 'COMPLETED', 'EXPIRED', 'CANCELED'):
        raise XenditError('INVALID_SESSION_STATUS')


def verify_payment(data, checkout, payment_id):
    expected = {'payment_id': payment_id, 'reference_id': checkout.reference_id,
                'business_id': settings.XENDIT_BUSINESS_ID, 'currency': 'PHP', 'status': 'SUCCEEDED'}
    if any(data.get(key) != value for key, value in expected.items()):
        raise XenditError('PAYMENT_MISMATCH')
    verified_amount = amount(data.get('request_amount'))
    if verified_amount != checkout.amount:
        raise XenditError('AMOUNT_MISMATCH')
    return verified_amount
