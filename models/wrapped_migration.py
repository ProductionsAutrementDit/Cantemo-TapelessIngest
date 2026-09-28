from django.db import models

from portal.plugins.TapelessIngest.wrapped import verdicts


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
    plan = models.JSONField(default=dict)
    rollback = models.JSONField(default=dict)
    error = models.TextField(blank=True, default="")
    planned_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)
