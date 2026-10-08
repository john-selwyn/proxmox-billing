from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("billing", "0009_order_ssh_host_order_ssh_port")]
    operations = [migrations.AlterField(
        model_name="order", name="operating_system",
        field=models.CharField(
            choices=[("Ubuntu 26.04", "Ubuntu 26.04 LTS"), ("Debian 13", "Debian 13"),
                     ("Windows 11", "Windows 11")], default="Ubuntu 26.04", max_length=50,
        ),
    )]
