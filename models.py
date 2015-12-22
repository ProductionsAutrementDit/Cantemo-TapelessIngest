import logging

from django.db import models
from django.contrib.auth.models import User
from os.path import isdir, basename

from django.core.exceptions import ObjectDoesNotExist

from django.utils.translation import ugettext_lazy as _

log = logging.getLogger(__name__)

from portal.plugins.TapelessIngest.utilities import generate_folder_clips, getProvidersByName

class Settings(models.Model):
    storage_id = models.CharField(max_length=255, blank=True, default='', db_column='storage')
    tmp_storage = models.CharField(max_length=255, blank=True, default='')
    bmxtranswrap = models.CharField(max_length=255, blank=True, default='')
    mxf2raw = models.CharField(max_length=255, blank=True, default='')
    base_folder = models.CharField(max_length=255, blank=True, default='')
    
    @property
    def storage(self):
        if not hasattr(self, '_storage'):
            from portal.vidispine.istorage import StorageHelper
            sth = StorageHelper()
            self._storage = sth.getStorage(self.storage_id)
        return self._storage
    
    @storage.setter
    def storage(self, storage_id):
        from portal.vidispine.istorage import StorageHelper
        sth = StorageHelper()
        self._storage = sth.getStorage(storage_id)
        self.storage_id = storage_id


class Clip(models.Model):

    umid = models.CharField(primary_key=True, max_length=100)
    created_on = models.DateTimeField(auto_now=True)
    imported_on = models.DateTimeField(null = True)
    user = models.ForeignKey(User, null = True)
    folder_path = models.TextField()
    output_file = models.TextField(null = True)
    file_id = models.CharField(max_length=255, null = True)
    status = models.IntegerField(blank = True, default=0)
    progress = models.CharField(max_length=255, null = True)
    spanned = models.BooleanField()
    master_clip = models.BooleanField(default=False)
    provider_name = models.CharField(max_length=100, db_column='provider')
    collection_id = models.TextField(null = True)
    item_id = models.CharField(max_length=10)
    clip_xml = models.TextField()
    media_xml = models.TextField()
    
    @property
    def provider(self):
        if not hasattr(self, '_provider'):
            self._provider = getProvidersByName(self.provider_name)
            self._provider.MetadataMappingModel = MetadataMapping
        return self._provider
    
    @provider.setter
    def provider(self, Provider):
        self._provider = Provider
        self.provider_name = Provider.machine_name
    
    def get_thumbnail_url(self):
        from django.core.urlresolvers import reverse
        return reverse('clip_thumbnail', None, [str(self.umid)])
    
    def get_state(self):
        try:
            if self.job.status == "FINISHED":
                return "IMPORTED"
            elif self.job.status == "INGEST_FAILED":
                return "FAILED"
            else:
                return "PROCESSING"
        except ObjectDoesNotExist:
            if self.status is 4:
                return "IMPORTED"
            else:
                return "NOTIMPORTED"
    
    def get_error(self):
        try:
            if self.job.status == "INGEST_FAILED":
                return self.job.error
            else:
                return ""
        except ObjectDoesNotExist:
            return ""
        
    def get_readable_duration(self):
        from .templatetags.tapelessingest_extras import frame_to_time
        total_duration = 0
        clips_ids = []
        if self.spanned and self.master_clip:
            spanned_clips = self.spanned_clips.all()
            for spanned_clip in spanned_clips:
                clips_ids.append(spanned_clip.clip.umid)
        else:
            clips_ids.append(self.umid)
        durations = ClipMetadata.objects.filter(clip__in=clips_ids, name='duration')
        for duration in durations:
            total_duration += int(duration.value)
        return frame_to_time(total_duration)
    
    def get_readable_status(self):
        if self.status is 0:
            return "NOT IMPORTED"
        if self.status is 1:
            return "WRAPPED"
        if self.status is 2:
            return "REGISTERED"
        if self.status is 3:
            return "PLACEHOLDER CREATED"
        if self.status is 4:
            return "IMPORTED"
   
    def get_absolute_url(self):
        from django.core.urlresolvers import reverse
        return reverse('clips-detail', args=[str(self.umid)])
    
    def get_resource_uri(self):
        from django.core.urlresolvers import reverse
        return reverse('clips-detail', args=[str(self.umid)])
    
    def get_metadata_set(self):
        metadata_set = {}
        metadatas = ClipMetadata.objects.filter(clip=self.umid)
        for metadata in metadatas:
            metadata_set[metadata.name] = metadata.value
        return metadata_set
    
    def __unicode__(self):
        return '%s' % self.umid

class ClipMetadata(models.Model):
    clip = models.ForeignKey(Clip, related_name = "metadatas", max_length=100)
    name = models.CharField(max_length=200)
    value = models.CharField(max_length=200, blank = True, null = True, default = "")
    
    class Meta:
        unique_together = (("clip", "name"),)
    
    def __unicode__(self):
        return '%s' % self.value

class ClipFile(models.Model):
    clip = models.ForeignKey(Clip, related_name = "input_files", max_length=100)
    path = models.TextField()
    filetype = models.CharField(max_length=100)
    order = models.IntegerField(blank = True, default=0)
    
    class Meta:
        unique_together = (("clip", "path"),)

class SpannedClips(models.Model):
    master_clip = models.ForeignKey(Clip, related_name="spanned_clips", max_length=100, on_delete=models.CASCADE)
    clip = models.ForeignKey(Clip, max_length=100, on_delete=models.CASCADE)
    order = models.IntegerField(blank = True, default=0)
    
    class Meta:
        unique_together = (("master_clip", "order"),)
        ordering = ['order']

class Folder(models.Model):
    user = models.ForeignKey(User)
    created_on = models.DateTimeField(auto_now=True)
    basename = models.CharField(max_length=200)
    path = models.TextField()
    clips = models.ManyToManyField(Clip, related_name = "folders")
    
    class Meta:
        unique_together = (("user", "path"),)

models.signals.post_save.connect(generate_folder_clips, sender=Folder)

class MetadataMapping(models.Model):
    metadata_provider = models.CharField(max_length=200)
    metadata_portal = models.CharField(max_length=100, blank = True, null = True, default = "")
    

class AggregateIngestJob(models.Model):
    user = models.ForeignKey(User, null=False, editable=False)
    task_id = models.CharField(null=True, editable=False, max_length=48)

class IngestJob(models.Model):
    clip = models.OneToOneField(Clip, related_name = "job", primary_key=True, max_length=100)
    job = models.ForeignKey(AggregateIngestJob, related_name='jobs', null=False, editable=False)
    status = models.CharField(null=False, max_length=48)
    progress = models.IntegerField(_('Ingest progress'), null=True, default=None)
    error = models.CharField(_('Error message'), null=True, default=None, max_length=255)
    exception = models.CharField(_('Exception message'), null=True, default=None, max_length=65535)

from portal.plugins.TapelessIngest.plistner import register_tapelessingest_signal_listeners
register_tapelessingest_signal_listeners()
