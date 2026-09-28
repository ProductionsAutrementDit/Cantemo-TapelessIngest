import json

from django.db import models

from portal.plugins.TapelessIngest.wrapped import verdicts


class AsciiJSONField(models.TextField):
    """JSON stored as an ASCII-escaped ``text`` column, never ``jsonb``.

    Prod's PostgreSQL database is SQL_ASCII. ``jsonb`` decodes every
    ``\\uXXXX`` escape back into a real character on input, which needs a
    server-encoding conversion that SQL_ASCII cannot perform for any code
    point above 0x7F — so even an already-ASCII-escaped string fails
    against a ``jsonb`` column (``unsupported Unicode escape sequence``).
    A plain ``text`` column does no such decoding, so this field is a
    ``TextField`` under the hood (``get_internal_type`` says so, and the
    migration is an ``AlterField`` to this type — PostgreSQL casts
    ``jsonb`` to ``text`` for free). ``ensure_ascii=True`` keeps the
    stored bytes themselves ASCII too, belt and braces.
    """

    def get_internal_type(self):
        return "TextField"

    def from_db_value(self, value, expression, connection):
        if value is None:
            return self.get_default()
        return json.loads(value)

    def to_python(self, value):
        if isinstance(value, (dict, list)) or value is None:
            return value if value is not None else self.get_default()
        return json.loads(value)

    def get_prep_value(self, value):
        if value is None:
            value = self.get_default()
        return json.dumps(value, ensure_ascii=True, sort_keys=True)


class WrappedMigration(models.Model):
    """One legacy wrapped item, its plan, and how far ``apply`` has taken it.

    ``plan`` is written by ``plan`` and then only COMPLETED by ``apply``
    (file ids, the new shape id, sha1s) — never re-derived. ``phase`` is
    the last phase that finished; a non-empty phase freezes the row
    against ``plan``. ``rollback`` holds everything needed to put the
    wrapped shape back, including its ``Default-Archive#...`` handle.
    """

    item_id = models.CharField(max_length=32, unique=True)
    clip_umid = models.CharField(max_length=100)
    verdict = models.CharField(max_length=32, choices=[(v, v) for v in verdicts.ALL])
    reason = models.TextField(blank=True, default="")
    phase = models.CharField(max_length=32, blank=True, default="")
    plan = AsciiJSONField(default=dict)
    rollback = AsciiJSONField(default=dict)
    error = models.TextField(blank=True, default="")
    planned_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)
