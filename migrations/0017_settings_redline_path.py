from django.db import migrations, models


class Migration(migrations.Migration):
    """Operator override for the REDline binary path.

    Left empty, providers/red.py discovers REDline on PATH and then at the
    known install locations — which is what the nightly cron needs, since
    /etc/crontab's PATH does not carry /usr/local/bin.
    """

    dependencies = [
        ("TapelessIngest", "0016_auto_20211006_1749"),
    ]

    operations = [
        migrations.AddField(
            model_name="settings",
            name="redline_path",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
