from django.db import migrations

import portal.plugins.TapelessIngest.models.wrapped_migration


class Migration(migrations.Migration):
    """``plan``/``rollback`` move off ``jsonb`` onto a plain ``text`` column.

    Prod's PostgreSQL database is SQL_ASCII. ``jsonb`` decodes every
    ``\\uXXXX`` escape back into a real character on input, which needs a
    server-encoding conversion SQL_ASCII cannot perform above 0x7F — so a
    rollback holding a BOM, or a path with an accented name, fails to
    save at all (``unsupported Unicode escape sequence``). PostgreSQL
    casts ``jsonb`` to ``text`` for free, so this is a same-data
    ``AlterField`` — nothing to backfill.
    """

    dependencies = [
        ("TapelessIngest", "0019_wrapped_migration"),
    ]

    operations = [
        migrations.AlterField(
            model_name="wrappedmigration",
            name="plan",
            field=portal.plugins.TapelessIngest.models.wrapped_migration.AsciiJSONField(
                default=dict
            ),
        ),
        migrations.AlterField(
            model_name="wrappedmigration",
            name="rollback",
            field=portal.plugins.TapelessIngest.models.wrapped_migration.AsciiJSONField(
                default=dict
            ),
        ),
    ]
