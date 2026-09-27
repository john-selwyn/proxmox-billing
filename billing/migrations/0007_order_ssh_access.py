# Generated for customer SSH access metadata
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0006_vpspoweroperation"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="ssh_public_key",
            field=models.TextField(blank=True, default="", editable=False),
        ),
        migrations.AddField(
            model_name="order",
            name="ssh_username",
            field=models.CharField(blank=True, default="", editable=False, max_length=32),
        ),
    ]
