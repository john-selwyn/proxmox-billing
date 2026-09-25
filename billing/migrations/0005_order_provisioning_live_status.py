# Generated for live provisioning status and guest IP tracking
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0004_xendit_checkout"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="provisioning_progress",
            field=models.PositiveSmallIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="order",
            name="provisioning_step",
            field=models.CharField(blank=True, editable=False, max_length=255),
        ),
        migrations.AddField(
            model_name="order",
            name="provisioning_ip_address",
            field=models.GenericIPAddressField(blank=True, editable=False, null=True),
        ),
    ]
