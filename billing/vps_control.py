"""Authenticated VM100 adapter for customer VPS runtime and durable power operations."""
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit
import ipaddress
import uuid

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from .models import VPSPowerOperation


class VPSControlError(Exception):
    def __init__(self, code, *, http_status=None, active_operation_id=None):
        self.code = code
        self.http_status = http_status
        self.active_operation_id = active_operation_id
        super().__init__(code)


@dataclass(frozen=True)
class VPSRuntime:
    state: str
    observed_at: object
    ip_address: str = ""


@dataclass(frozen=True)
class RemotePowerOperation:
    operation_id: uuid.UUID
    action: str
    status: str
    result: str | None
    observed_state: str
    observed_at: object
    error_code: str


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

    if not valid or len(secret or "") < 32 or not secret.isascii() or any(
        ord(char) <= 32 or ord(char) == 127 for char in secret
    ):
        raise VPSControlError("NOT_CONFIGURED")
    if parsed.scheme != "https" and not (settings.DEBUG or settings.PROVISIONING_ALLOW_HTTP):
        raise VPSControlError("HTTPS_REQUIRED")
    return base, secret


def _parse_timestamp(value, *, nullable):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise VPSControlError("INVALID_RESPONSE")
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        raise VPSControlError("INVALID_RESPONSE")
    return parsed


def _parse_error_response(data):
    if not isinstance(data, dict):
        return None, None
    error = data.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    active = data.get("active_operation_id")
    if active is not None:
        try:
            active = uuid.UUID(str(active))
        except (ValueError, TypeError, AttributeError):
            active = None
    return code if isinstance(code, str) else None, active


@sensitive_variables()
def _request(method, path, *, payload=None, extra_headers=None):
    base, secret = _base_and_secret()
    headers = {
        "Authorization": f"Bearer {secret}",
        "Accept": "application/json",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)

    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(
                method,
                base + path,
                json=payload,
                headers=headers,
                timeout=(5, 20),
                allow_redirects=False,
            )
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

    if not 200 <= response.status_code < 300:
        code, active = _parse_error_response(data)
        raise VPSControlError(
            code or f"HTTP_{response.status_code}",
            http_status=response.status_code,
            active_operation_id=active,
        )
    return response.status_code, data


def _parse_runtime(data, order_id):
    required = {"version", "billing_order_id", "state", "observed_at", "ip_address"}
    if not isinstance(data, dict) or set(data) != required or data.get("version") != 1:
        raise VPSControlError("INVALID_RESPONSE")
    if data.get("billing_order_id") != int(order_id):
        raise VPSControlError("ORDER_MISMATCH")

    state = data.get("state")
    if state not in {"running", "stopped", "paused", "suspended", "unknown"}:
        raise VPSControlError("INVALID_RESPONSE")

    observed_at = _parse_timestamp(data.get("observed_at"), nullable=False)
    ip_address = data.get("ip_address")
    if ip_address in ("", None, "0.0.0.0"):
        ip_address = ""
    elif not isinstance(ip_address, str):
        raise VPSControlError("INVALID_RESPONSE")
    else:
        try:
            ipaddress.ip_address(ip_address)
        except ValueError:
            raise VPSControlError("INVALID_RESPONSE") from None

    return VPSRuntime(state=state, observed_at=observed_at, ip_address=ip_address)


def _parse_power(data, order_id, action):
    required = {
        "version", "billing_order_id", "operation_id", "action", "status",
        "result", "observed_state", "observed_at", "error",
    }
    if not isinstance(data, dict) or set(data) != required or data.get("version") != 1:
        raise VPSControlError("INVALID_RESPONSE")
    if data.get("billing_order_id") != int(order_id) or data.get("action") != action:
        raise VPSControlError("ORDER_MISMATCH")

    try:
        operation_id = uuid.UUID(str(data.get("operation_id")))
    except (ValueError, TypeError, AttributeError):
        raise VPSControlError("INVALID_RESPONSE") from None
    if str(operation_id) != str(data.get("operation_id")).lower():
        raise VPSControlError("INVALID_RESPONSE")

    status = data.get("status")
    if status not in {"pending", "running", "succeeded", "failed", "unknown"}:
        raise VPSControlError("INVALID_RESPONSE")
    result = data.get("result")
    if status == "succeeded":
        if result not in {"executed", "noop"}:
            raise VPSControlError("INVALID_RESPONSE")
    elif result is not None:
        raise VPSControlError("INVALID_RESPONSE")

    observed_state = data.get("observed_state")
    if observed_state not in {"running", "stopped", "paused", "suspended", "unknown"}:
        raise VPSControlError("INVALID_RESPONSE")
    observed_at = _parse_timestamp(data.get("observed_at"), nullable=True)

    error = data.get("error")
    if error is None:
        error_code = ""
    elif (
        isinstance(error, dict)
        and set(error) == {"code", "message"}
        and isinstance(error.get("code"), str)
        and isinstance(error.get("message"), str)
    ):
        error_code = error["code"]
    else:
        raise VPSControlError("INVALID_RESPONSE")
    if status == "succeeded" and error_code:
        raise VPSControlError("INVALID_RESPONSE")

    return RemotePowerOperation(
        operation_id=operation_id,
        action=action,
        status=status,
        result=result,
        observed_state=observed_state,
        observed_at=observed_at,
        error_code=error_code,
    )


def get_vps_runtime(order_id):
    _, data = _request("GET", f"/api/internal/v1/vps/{int(order_id)}/")
    return _parse_runtime(data, order_id)


def submit_power_operation(order_id, action, idempotency_key):
    if action not in {"start", "shutdown", "reboot"}:
        raise VPSControlError("INVALID_ACTION")
    key = uuid.UUID(str(idempotency_key))
    _, data = _request(
        "POST",
        f"/api/internal/v1/vps/{int(order_id)}/power/",
        payload={"action": action},
        extra_headers={"Idempotency-Key": str(key)},
    )
    return _parse_power(data, order_id, action)


def get_power_operation(order_id, operation_id, action):
    operation_id = uuid.UUID(str(operation_id))
    _, data = _request(
        "GET",
        f"/api/internal/v1/vps/{int(order_id)}/power-operations/{operation_id}/",
    )
    remote = _parse_power(data, order_id, action)
    if remote.operation_id != operation_id:
        raise VPSControlError("OPERATION_MISMATCH")
    return remote


def _schedule_retry(operation):
    operation.sync_attempts = min(operation.sync_attempts + 1, 10)
    delay = min(60, 2 ** min(operation.sync_attempts, 5))
    operation.next_sync_at = timezone.now() + timedelta(seconds=delay)
    operation.save(update_fields=["sync_attempts", "next_sync_at", "updated_at"])


def sync_power_operation(operation, *, force=False):
    if operation.status in {VPSPowerOperation.Status.SUCCEEDED, VPSPowerOperation.Status.FAILED}:
        return operation
    if not force and operation.next_sync_at and operation.next_sync_at > timezone.now():
        return operation

    try:
        if operation.remote_operation_id:
            remote = get_power_operation(operation.order_id, operation.remote_operation_id, operation.action)
        else:
            remote = submit_power_operation(operation.order_id, operation.action, operation.idempotency_key)
    except VPSControlError as exc:
        if exc.code == "OPERATION_IN_PROGRESS":
            operation.status = VPSPowerOperation.Status.UNKNOWN
            operation.error_code = "REMOTE_OPERATION_IN_PROGRESS"
            operation.next_sync_at = None
            operation.save(update_fields=["status", "error_code", "next_sync_at", "updated_at"])
        elif exc.code in {
            "INVALID_REQUEST", "UNAUTHORIZED", "NOT_FOUND", "VPS_NOT_READY",
            "IDEMPOTENCY_CONFLICT", "INVALID_ACTION",
        }:
            operation.status = VPSPowerOperation.Status.FAILED
            operation.error_code = exc.code
            operation.next_sync_at = None
            operation.save(update_fields=["status", "error_code", "next_sync_at", "updated_at"])
        else:
            _schedule_retry(operation)
        raise

    if operation.remote_operation_id and operation.remote_operation_id != remote.operation_id:
        operation.status = VPSPowerOperation.Status.UNKNOWN
        operation.error_code = "OPERATION_MISMATCH"
        operation.next_sync_at = None
        operation.save(update_fields=["status", "error_code", "next_sync_at", "updated_at"])
        raise VPSControlError("OPERATION_MISMATCH")

    operation.remote_operation_id = remote.operation_id
    operation.status = remote.status
    operation.result = remote.result
    operation.observed_state = remote.observed_state
    operation.observed_at = remote.observed_at
    operation.error_code = remote.error_code
    operation.sync_attempts = 0
    operation.next_sync_at = (
        timezone.now() + timedelta(seconds=2)
        if remote.status in {"pending", "running"}
        else None
    )
    operation.save(update_fields=[
        "remote_operation_id", "status", "result", "observed_state",
        "observed_at", "error_code", "sync_attempts", "next_sync_at", "updated_at",
    ])
    return operation
