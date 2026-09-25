"""Authenticated VM100 adapter for customer VPS runtime status and power actions."""
from dataclasses import dataclass
from urllib.parse import urlsplit
import ipaddress

import requests
from django.conf import settings
from django.views.decorators.debug import sensitive_variables


class VPSControlError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class VPSRuntime:
    state: str
    vmid: str
    ip_address: str = ""


def _base_and_secret():
    base = settings.PROVISIONING_API_URL.strip().rstrip("/")
    secret = settings.BILLING_API_SECRET
    try:
        parsed = urlsplit(base)
        valid = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path in ("", "/")
        )
    except ValueError:
        valid = False

    if not valid or not secret or not secret.isascii() or any(
        ord(char) <= 32 or ord(char) == 127 for char in secret
    ):
        raise VPSControlError("NOT_CONFIGURED")
    if parsed.scheme != "https" and not (settings.DEBUG or settings.PROVISIONING_ALLOW_HTTP):
        raise VPSControlError("HTTPS_REQUIRED")
    return base, secret


def _parse_runtime(data, order_id):
    if not isinstance(data, dict):
        raise VPSControlError("INVALID_RESPONSE")
    if str(data.get("billing_order_id", "")) != str(order_id):
        raise VPSControlError("ORDER_MISMATCH")

    vmid = data.get("vmid")
    if not isinstance(vmid, (str, int)) or isinstance(vmid, bool):
        raise VPSControlError("INVALID_RESPONSE")
    vmid = str(vmid)
    if not vmid.isascii() or not vmid.isdigit() or len(vmid) > 20:
        raise VPSControlError("INVALID_RESPONSE")

    state = data.get("state")
    if state not in {"running", "stopped", "paused", "suspended", "unknown"}:
        raise VPSControlError("INVALID_RESPONSE")

    ip_address = data.get("ip_address", "")
    if ip_address in ("", None, "0.0.0.0"):
        ip_address = ""
    elif not isinstance(ip_address, str):
        raise VPSControlError("INVALID_RESPONSE")
    else:
        try:
            ipaddress.ip_address(ip_address)
        except ValueError:
            raise VPSControlError("INVALID_RESPONSE") from None

    return VPSRuntime(state=state, vmid=vmid, ip_address=ip_address)


@sensitive_variables()
def _request(method, order_id, *, action=None):
    base, secret = _base_and_secret()
    path = f"/api/internal/vps/{int(order_id)}/"
    payload = None
    if method == "POST":
        path += "power/"
        payload = {"action": action}

    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(
                method,
                base + path,
                json=payload,
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
                timeout=(5, 20),
                allow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                raise VPSControlError(f"HTTP_{response.status_code}")
            try:
                data = response.json()
            except ValueError:
                raise VPSControlError("INVALID_JSON") from None
    except requests.Timeout:
        raise VPSControlError("TIMEOUT") from None
    except requests.ConnectionError:
        raise VPSControlError("CONNECTION_FAILED") from None
    except requests.RequestException:
        raise VPSControlError("REQUEST_FAILED") from None

    return data


def get_vps_runtime(order_id):
    return _parse_runtime(_request("GET", order_id), order_id)


def power_vps(order_id, action):
    if action not in {"start", "shutdown", "reboot"}:
        raise VPSControlError("INVALID_ACTION")
    data = _request("POST", order_id, action=action)
    if not isinstance(data, dict) or data.get("accepted") is not True:
        raise VPSControlError("INVALID_RESPONSE")
    return data
