"""Retrieve credentials on demand; never persist or log decrypted passwords."""
from django.views.decorators.debug import sensitive_variables

from .rdp_endpoint import parse_rdp_endpoint
from .vps_control import VPSControlError, _request


@sensitive_variables()
def get_windows_credentials(order_id):
    _, data = _request("GET", f"/api/internal/v1/vps/{int(order_id)}/windows-credentials/")
    if (not isinstance(data, dict) or set(data) != {"billing_order_id", "rdp_access"}
            or type(data.get("billing_order_id")) is not int
            or data["billing_order_id"] != int(order_id)):
        raise VPSControlError("INVALID_RESPONSE")
    access = data["rdp_access"]
    if not isinstance(access, dict) or set(access) != {"host", "port", "username", "password"}:
        raise VPSControlError("INVALID_RESPONSE")
    password = access["password"]
    if (not isinstance(password, str) or not 1 <= len(password) <= 256
            or any(ord(c) < 32 or ord(c) == 127 for c in password)):
        raise VPSControlError("INVALID_RESPONSE")
    try:
        parse_rdp_endpoint({"rdp_access": {k: v for k, v in access.items() if k != "password"}})
    except ValueError:
        raise VPSControlError("INVALID_RESPONSE") from None
    return access
