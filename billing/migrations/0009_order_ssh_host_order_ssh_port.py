from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("billing", "0008_order_operating_system")]

    operations = [
        migrations.AddField(
            model_name="order", name="ssh_host",
            field=models.CharField(blank=True, default="", editable=False, max_length=255),
        ),
        migrations.AddField(
            model_name="order", name="ssh_port",
            field=models.PositiveIntegerField(default=22, editable=False),
        ),
    ]
