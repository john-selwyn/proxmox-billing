"""Small read-only client for the Hestia hosting package API."""

from decimal import Decimal

import requests
from django.conf import settings


PACKAGE_PRICES = {
    "starter": (Decimal("149.00"), Decimal("1490.00")),
    "business": (Decimal("299.00"), Decimal("2990.00")),
    "pro": (Decimal("499.00"), Decimal("4990.00")),
}

PACKAGE_FALLBACKS = {
    "starter": {"DISK_QUOTA": "2048", "BANDWIDTH": "20480", "WEB_DOMAINS": "1", "DATABASES": "2", "CRON_JOBS": "5", "MAIL_ACCOUNTS": "0"},
    "business": {"DISK_QUOTA": "10240", "BANDWIDTH": "102400", "WEB_DOMAINS": "5", "DATABASES": "10", "CRON_JOBS": "10", "MAIL_ACCOUNTS": "0"},
    "pro": {"DISK_QUOTA": "25600", "BANDWIDTH": "256000", "WEB_DOMAINS": "10", "DATABASES": "25", "CRON_JOBS": "20", "MAIL_ACCOUNTS": "0"},
}


def _gb(value):
    if str(value).lower() == "unlimited":
        return "Unlimited"
    try:
        number = Decimal(str(value)) / Decimal("1024")
        return f"{number.normalize():f} GB"
    except (ArithmeticError, ValueError):
        return str(value)


def _package(name, values, connected):
    monthly, yearly = PACKAGE_PRICES[name]
    return {
        "name": name.title(), "slug": name,
        "monthly_price": monthly, "yearly_price": yearly,
        "disk": _gb(values.get("DISK_QUOTA", "0")),
        "bandwidth": _gb(values.get("BANDWIDTH", "0")),
        "domains": values.get("WEB_DOMAINS", "0"),
        "databases": values.get("DATABASES", "0"),
        "cron_jobs": values.get("CRON_JOBS", "0"),
        "mail_accounts": values.get("MAIL_ACCOUNTS", "0"),
        "connected": connected,
    }


def list_packages():
    """Return sellable packages, refreshing limits from Hestia when configured."""
    packages = None
    if settings.HESTIA_API_URL and settings.HESTIA_API_USER and settings.HESTIA_API_PASSWORD:
        try:
            response = requests.post(
                settings.HESTIA_API_URL,
                data={
                    "user": settings.HESTIA_API_USER,
                    "password": settings.HESTIA_API_PASSWORD,
                    "returncode": "no",
                    "cmd": "v-list-user-packages",
                    "arg1": "json",
                },
                verify=settings.HESTIA_API_VERIFY_SSL,
                timeout=settings.HESTIA_API_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            packages = {key.lower(): value for key, value in payload.items() if key.lower() in PACKAGE_PRICES}
        except (requests.RequestException, ValueError, TypeError):
            packages = None

    source = packages or PACKAGE_FALLBACKS
    connected = packages is not None
    return [_package(name, source[name], connected) for name in PACKAGE_PRICES if name in source]
