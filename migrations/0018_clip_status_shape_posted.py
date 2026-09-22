from django.db import migrations, models


class Migration(migrations.Migration):
    """``Clip.status`` gains state 5, "Shape posted".

    The route that states a whole ``ShapeDocument`` itself starts no
    import job, so the row it leaves has an ``item_id``, a real
    ``original`` shape and no ``job_id``. That is exactly the cell
    ``scan.ingestion.is_incomplete_import`` calls an unfinished import,
    and left at ``PLACEHOLDER_CREATED`` every scan would re-enter the
    route and stack another shape on the item. The new state is what
    tells the resume ladder the difference.

    Column-wise this is an ``AlterField`` that only declares the choices:
    an ``IntegerField`` stores 5 exactly as it stored 4, so no data is
    touched and nothing has to be backfilled — no existing row can
    already be in the new state.
    """

    dependencies = [
        ("TapelessIngest", "0017_settings_redline_path"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clip",
            name="status",
            field=models.IntegerField(
                blank=True,
                choices=[
                    (0, "Not imported"),
                    (1, "Wrapped"),
                    (2, "Registered"),
                    (3, "Placeholder created"),
                    (4, "Imported"),
                    (5, "Shape posted"),
                ],
                default=0,
            ),
        ),
    ]
