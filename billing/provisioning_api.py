"""VM100 HTTP adapter. No Proxmox code and no raw remote errors leave this module."""
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.views.decorators.debug import sensitive_variables


class ProvisioningAPIError(Exception):
    """Only fixed, credential-free error codes may be propagated or stored."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProvisioningResult:
    status: str
    vps_id: str = ''
    vmid: str = ''
    error: str = ''


def parse_response(data, *, order_id):
    """The one place to adjust if VM100 changes its response contract."""
    if not isinstance(data, dict):
        raise ProvisioningAPIError('INVALID_RESPONSE')
    if data.get('success') is False:
        raise ProvisioningAPIError('REMOTE_REJECTED')
    if 'billing_order_id' in data and str(data['billing_order_id']) != str(order_id):
        raise ProvisioningAPIError('ORDER_MISMATCH')
    status = data.get('status')
    if not isinstance(status, str):
        raise ProvisioningAPIError('INVALID_RESPONSE')
    mapped = {
        'provisioning': 'PROVISIONING', 'in-progress': 'PROVISIONING',
        'in_progress': 'PROVISIONING', 'running': 'ACTIVE', 'ready': 'ACTIVE',
        'failed': 'FAILED',
    }.get(status.strip().lower())
    if mapped is None:
        raise ProvisioningAPIError('UNKNOWN_STATUS')
    ids = []
    for field in ('vps_id', 'vmid'):
        value = data.get(field)
        if value is None:
            ids.append('')
        elif isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).isascii() and str(value).isdigit() and len(str(value)) <= 20:
            ids.append(str(value))
        else:
            raise ProvisioningAPIError('INVALID_RESPONSE')
    # Never persist raw error_message, current_step, IPs, or response bodies.
    # Operators correlate this safe category with VM100 using the order ID.
    return ProvisioningResult(mapped, *ids, error='REMOTE_FAILED' if mapped == 'FAILED' else '')


@sensitive_variables()
def api_request(method, order_id, *, payload=None):
    base = settings.PROVISIONING_API_URL.strip().rstrip('/')
    secret = settings.BILLING_API_SECRET
    try:
        parsed = urlsplit(base)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname and
                 not parsed.username and not parsed.password and
                 not parsed.query and not parsed.fragment and parsed.path in ('', '/'))
    except ValueError:
        valid = False
    if not valid or not secret or not secret.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in secret):
        raise ProvisioningAPIError('NOT_CONFIGURED')
    if parsed.scheme != 'https' and not (settings.DEBUG or settings.PROVISIONING_ALLOW_HTTP):
        raise ProvisioningAPIError('HTTPS_REQUIRED')
    path = '/api/internal/provision/'
    if method == 'GET':
        path += f'{int(order_id)}/status/'
    try:
        with requests.Session() as session:
            # Do not allow netrc/proxy environment settings to replace or forward
            # internal authentication. TLS verification remains enabled.
            session.trust_env = False
            response = session.request(
                method, base + path, json=payload,
                headers={'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json'},
                timeout=(5, 20), allow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                raise ProvisioningAPIError(f'HTTP_{response.status_code}')
            try:
                data = response.json()
            except ValueError:
                raise ProvisioningAPIError('INVALID_JSON') from None
    except requests.Timeout:
        raise ProvisioningAPIError('TIMEOUT') from None
    except requests.ConnectionError:
        raise ProvisioningAPIError('CONNECTION_FAILED') from None
    except requests.RequestException:
        raise ProvisioningAPIError('REQUEST_FAILED') from None
    return parse_response(data, order_id=order_id)
