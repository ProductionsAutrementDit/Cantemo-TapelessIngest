from django.db import migrations, models


class Migration(migrations.Migration):
    """Operator override for the brawprobe binary path.

    Left empty, providers/braw.py discovers brawprobe on PATH and then at
    /usr/local/bin/brawprobe — which is what the nightly cron needs, since
    /etc/crontab's PATH does not carry /usr/local/bin.
    """

    dependencies = [
        ("TapelessIngest", "0020_wrapped_migration_ascii_json"),
    ]

    operations = [
        migrations.AddField(
            model_name="settings",
            name="brawprobe_path",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
