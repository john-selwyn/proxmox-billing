from django.conf import settings
from django.core.checks import Error, register
from .xendit import configured


@register()
def xendit_configuration(app_configs, **kwargs):
    values = (settings.XENDIT_SECRET_API_KEY, settings.XENDIT_WEBHOOK_TOKEN,
              settings.XENDIT_BUSINESS_ID, settings.XENDIT_PUBLIC_BASE_URL)
    if any(values) and not configured():
        return [Error('Xendit requires all four XENDIT settings and an HTTPS public origin.', id='billing.E001')]
    return []
