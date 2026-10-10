from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portal", "0010_alter_order_operating_system")]
    operations = [
        migrations.AddField(model_name="order", name="rdp_host",
            field=models.CharField(blank=True, default="", editable=False, max_length=253)),
        migrations.AddField(model_name="order", name="rdp_port",
            field=models.PositiveIntegerField(default=3389, editable=False)),
    ]
