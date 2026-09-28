from django.db import migrations, models


class Migration(migrations.Migration):
    """The per-item state of the wrapped-items migration."""

    dependencies = [
        ("TapelessIngest", "0018_clip_status_shape_posted"),
    ]

    operations = [
        migrations.CreateModel(
            name="WrappedMigration",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("item_id", models.CharField(max_length=32, unique=True)),
                ("clip_umid", models.CharField(max_length=100)),
                (
                    "verdict",
                    models.CharField(
                        max_length=32,
                        choices=[
                            ("ready", "ready"),
                            ("already-migrated", "already-migrated"),
                            ("originals-missing", "originals-missing"),
                            ("spanned", "spanned"),
                            ("unexpected", "unexpected"),
                            ("error", "error"),
                        ],
                    ),
                ),
                ("reason", models.TextField(blank=True, default="")),
                ("phase", models.CharField(blank=True, default="", max_length=32)),
                ("plan", models.JSONField(default=dict)),
                ("rollback", models.JSONField(default=dict)),
                ("error", models.TextField(blank=True, default="")),
                ("planned_on", models.DateTimeField(auto_now_add=True)),
                ("updated_on", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
