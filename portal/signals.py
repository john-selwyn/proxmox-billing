from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Customer

@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_customer(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        Customer.objects.get_or_create(user=instance, defaults={
            'full_name': instance.get_full_name() or instance.get_username(),
        })
