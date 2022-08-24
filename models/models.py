import logging

import uuid

from django.db import models
from django.contrib.auth.models import User

from django.utils.translation import ugettext_lazy as _

log = logging.getLogger(__name__)

from portal.plugins.TapelessIngest.models.clip import Clip


class AggregateIngestJob(models.Model):
    user = models.ForeignKey(User, null=False, editable=False, on_delete=models.CASCADE)
    task_id = models.CharField(null=True, editable=False, max_length=48)


class IngestClipJob(models.Model):
    clip = models.OneToOneField(
        Clip,
        related_name="jobs",
        primary_key=True,
        max_length=100,
        on_delete=models.CASCADE,
    )
    status = models.CharField(null=False, max_length=48)
    progress = models.IntegerField(_("Ingest progress"), null=True, default=None)
    error = models.CharField(
        _("Error message"), null=True, default=None, max_length=255
    )
    exception = models.CharField(
        _("Exception message"), null=True, default=None, max_length=65535
    )
    created_date = models.DateTimeField(auto_now_add=True)
    modified_date = models.DateTimeField(auto_now=True)

    @property
    def state(self):
        if not hasattr(self, "_state"):
            self._state = "Finished"
        return self._state

    def get_progress(self):
        tasks_progress = 0
        for task in self.tasks:
            tasks_progress += task.progress
        progress = tasks_progress / len(self.tasks)
        return progress

    def get_state(self):
        tasks_status = []
        for task in self.tasks:
            tasks_status.append(task.status)
        if "Pending" in tasks_status:
            return "Started"
        if "Started" in tasks_status:
            return "Started"
        return self.tasks.latest("modified_date").status


class IngestTaskJob(models.Model):
    job = models.ForeignKey(
        IngestClipJob,
        related_name="tasks",
        null=False,
        editable=False,
        on_delete=models.CASCADE,
    )
    task_name = models.CharField(
        _("Task name"), null=True, default=None, max_length=255
    )
    status = models.CharField(null=False, max_length=48)
    progress = models.IntegerField(_("Ingest progress"), null=True, default=None)
    error = models.CharField(
        _("Error message"), null=True, default=None, max_length=255
    )
    exception = models.CharField(
        _("Exception message"), null=True, default=None, max_length=65535
    )
    created_date = models.DateTimeField(auto_now_add=True)
    modified_date = models.DateTimeField(auto_now=True)


def start_task(task_name, job):
    task = IngestTaskJob.objects.create(
        job=job, task_name=task_name, status="Pending", progress=0
    )
    return task
