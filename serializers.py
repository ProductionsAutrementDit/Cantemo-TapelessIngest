import os

from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist

from rest_framework import serializers
from portal.plugins.TapelessIngest.models.clip import Clip, SpannedClips, ClipMetadata
from portal.plugins.TapelessIngest.models.folder import Folder


class ClipMetadataSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClipMetadata
        fields = ("name", "value")


class SpannedClipsSerializer(serializers.ModelSerializer):
    clip = serializers.CharField()

    class Meta:
        model = SpannedClips
        fields = "__all__"


class MediaFileSerializer(serializers.Serializer):
    type = serializers.CharField()
    track = serializers.IntegerField()
    order = serializers.IntegerField()
    path = serializers.CharField()
    file_id = serializers.CharField()


class JobSerializer(serializers.Serializer):
    id = serializers.CharField(source="getId", read_only=True)
    progress = serializers.CharField(source="getProgress", read_only=True)
    status = serializers.CharField(source="getStatus", read_only=True)
    type = serializers.CharField(source="getType", read_only=True)


class FolderMetadatasField(serializers.Field):

    def to_representation(self, value):
        ret = {
            "clipname": os.path.basename(value.path),
            "shooting_date": value.created_on
        }
        return ret

    def to_internal_value(self, data):
        ret = {
            "clipname": data["clipname"],
            "shooting_date": data["shooting_date"],
        }
        return ret

class ProviderSerializer(serializers.Field):
    def to_representation(self, value):
        return value.machine_name
    def to_internal_value(self, data):
        return data

class FolderSerializer(serializers.ModelSerializer):
    umid = serializers.CharField(source='id')
    error = serializers.CharField()
    metadatas = FolderMetadatasField(source='*', read_only=True)
    type = serializers.ReadOnlyField(default='folder')
    providers = serializers.ListField(child=ProviderSerializer(), read_only=True)
    class Meta:
        model = Folder
        fields = ("umid", "type", "created_on", "scanned_on", "path", "storage_id", "clips_total", "collection_id", "provider_names", "providers", "metadatas", "error")


class ClipSerializer(serializers.ModelSerializer):
    type = serializers.ReadOnlyField(default='clip')
    absolute_url = serializers.CharField(source="get_absolute_url", read_only=True)
    resource_uri = serializers.CharField(source="get_resource_uri", read_only=True)
    thumbnail_url = serializers.CharField(source="get_thumbnail_url", read_only=True)
    metadatas = serializers.DictField(child=serializers.CharField(allow_null=True))
    media_files = MediaFileSerializer(many=True, read_only=True)
    spanned_clips = SpannedClipsSerializer(many=True, read_only=True)
    state = serializers.CharField(source="get_state", read_only=True)
    error = serializers.CharField(source="get_error", read_only=True)
    job = JobSerializer(read_only=True)
    status_readable = serializers.CharField(source="get_readable_status", read_only=True)
    duration_readable = serializers.CharField(source="get_readable_duration", read_only=True)
    # jobs = serializers.JSONField(source='get_related_jobs', read_only=True)

    class Meta:
        model = Clip
        fields = (
            "umid",
            "type",
            "created_on",
            "imported_on",
            "user",
            "path",
            "storage_id",
            "folder_path",
            "output_file",
            "file_id",
            "status",
            "progress",
            "spanned",
            "master_clip",
            "provider_name",
            "collection_id",
            "item_id",
            "job_id",
            "absolute_url",
            "resource_uri",
            "thumbnail_url",
            "metadatas",
            "media_files",
            "spanned_clips",
            "state",
            "error",
            "job",
            "absolute_url",
            "status_readable",
            "duration_readable",
            "reference_file",
        )

    def is_valid(self, raise_exception=False):
        print("Call clip serializer")
        # hack to add a "get_or_create" feature to serializer
        if hasattr(self, "initial_data"):
            obj_id = self.initial_data.get("umid")
            # If we are instantiating with data={something}
            try:
                # Try to get the object in question
                obj = self.Meta.model.objects.get(pk=obj_id)
            except (
                ObjectDoesNotExist,
                MultipleObjectsReturned,
            ):
                # Except not finding the object or the data being ambiguous
                # for defining it. Then validate the data as usual
                return super().is_valid(raise_exception)
            else:
                # If the object is found add it to the serializer. Then
                # validate the data as usual
                self.instance = obj
                return super().is_valid(raise_exception)
        else:
            # If the Serializer was instantiated with just an object, and no
            # data={something} proceed as usual
            return super().is_valid(raise_exception)