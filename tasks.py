# Embedded file name: /var/lib/jenkins/workspace/Portal_20/label/buildslave-co64.cantemo.com/rpm/BUILD/Portal-2.0.2/opt/cantemo/portal/portal/search/tasks.py
import logging
from django.conf import settings

try:
    from celery.decorators import task
    from celery.registry import tasks
except ImportError:
    raise ImproperlyConfigured("Please install 'celery' library to use the task syntax.")


from portal.plugins.TapelessIngest.models import Clip, AggregateIngestJob, IngestJob

log = logging.getLogger(__name__)

"""
@task(name="portal.plugins.TapelessIngest.tasks.ingest_clip")
def ingest_clip(clip, user):
    clip.status = clip.provider.getClipStatus(clip)
    if clip.status is 0:
        clip = clip.provider.exportClip(clip)
    if clip.status is 1:
        clip = clip.provider.registerOutputFile(clip)
    if clip.status is 2:
        clip = clip.provider.createPlaceHolder(clip, user)
    if clip.status is 3:
        clip = clip.provider.importClipToPlaceholder(clip)
    if clip.status is 4:
        pass
    clip.save()
"""

@task(name="portal.plugins.TapelessIngest.tasks.ingest_task")
def ingest_task(clips, aggregate_task_id, user):

    log.info("Start ingest for %s clips" % str(len(clips)))
    
    aggregate_job = AggregateIngestJob.objects.get(id=aggregate_task_id)

    for clip in clips:
        job, created = IngestJob.objects.get_or_create(clip=clip, defaults={'job': aggregate_job,
         'status': 'START_INGEST'})

        try:
            job.status = 'START_WRAPPING'
            job.save()
            clip = clip.provider.exportClip(clip)
            clip.save()
        except Exception as x:
            log.error("%s: %s" % (type(x), x))
            job.status = 'WRAPPING_FAILED'
            job.error = x
            job.save()
        try:
            job.status = 'START_REGISTER_FILE'
            job.save()
            clip = clip.provider.registerOutputFile(clip)
            clip.save()
        except Exception as x:
            log.error("%s: %s" % (type(x), x))
            job.status = 'REGISTER_FILE_FAILED'
            job.error = x
            job.save()
        try:
            job.status = 'START_CREATING_PLACEHOLDER'
            job.save()
            clip = clip.provider.createPlaceHolder(clip, user)
            clip.save()
        except Exception as x:
            log.error("%s: %s" % (type(x), x))
            job.status = 'PLACEHOLDER_CREATION_FAILED'
            job.error = x
            job.save()
        try:
            job.status = 'START_IMPORTING_TO_PLACEHOLDER'
            job.save()
            clip = clip.provider.importClipToPlaceholder(clip)
            clip.save()
        except Exception as x:
            log.error("%s: %s" % (type(x), x))
            job.status = 'IMPORTING_TO_PLACEHOLDER_FAILED'
            job.error = x
            job.save()
        if clip.status is 4:
            job.status = 'FINISHED'
            job.save()

    return