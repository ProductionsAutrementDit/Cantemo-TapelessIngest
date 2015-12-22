# Embedded file name: /var/lib/jenkins/workspace/Portal_21/label/buildslave-co64.cantemo.com/rpm/BUILD/Portal-2.1.0/opt/cantemo/portal/portal/plugins/TapelessIngest/ingest.py
"""
Archive Framework models.

Copyright 2014 Cantemo AB. All Rights Reserved
"""
import logging
from portal.plugins.TapelessIngest.models import AggregateIngestJob
from portal.plugins.TapelessIngest.tasks import ingest_task

log = logging.getLogger(__name__)

def ingest_clip(clips, user):
    """
    Queue one or many items for ingest.
    This operation will return immediately.
    Use the returned AggregateIngestJob model object to query ingest
    progress and status. Files associated with the ingested items will
    be deleted from storage after successful ingest.
    
    :param clips: list of tapeless ingest clip ids
    :param user: user
    :return: AggregateIngestJob
    """
    return call_task(clips, user, ingest_task)

def call_task(clips, user, celery_task):
    aggregate_job = AggregateIngestJob.objects.create(user=user)
    aggregate_job.save()
    result = celery_task.delay(clips, aggregate_job.id, user)
    log.info("Task %s (id:%s) %s, for %s clips, (aggregator id is %s and user is %s)" % (celery_task.name, result.id, result.status, len(clips), aggregate_job.id, user.username))
    aggregate_job.task_id = result.id
    aggregate_job.save()
    return aggregate_job