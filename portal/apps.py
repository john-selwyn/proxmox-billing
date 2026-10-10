from django.apps import AppConfig


class PortalConfig(AppConfig):
    name = 'portal'
    label = 'portal'
    verbose_name = 'Cloud Portal'

    def ready(self):
        from . import checks, signals  # noqa: F401
