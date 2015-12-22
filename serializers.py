# Embedded file name: /var/lib/jenkins/workspace/Portal_20/label/buildslave-co64.cantemo.com/rpm/BUILD/Portal-2.0.2/opt/cantemo/portal/portal/mediabin/serializers.py
from rest_framework import serializers
from portal.plugins.TapelessIngest.models import Clip, SpannedClips, Folder, ClipMetadata
from os.path import isdir

class ClipMetadataSerializer(serializers.ModelSerializer): 
    class Meta:
        model = ClipMetadata
        fields = ('name', 'value')

class SpannedClipsSerializer(serializers.ModelSerializer):
    clip = serializers.CharField()
    class Meta:
        model = SpannedClips


class ClipSerializer(serializers.ModelSerializer):
    absolute_url = serializers.CharField(source='get_absolute_url', read_only=True)
    resource_uri = serializers.CharField(source='get_resource_uri', read_only=True)
    thumbnail_url = serializers.CharField(source='get_thumbnail_url', read_only=True)
    metadatas = ClipMetadataSerializer(many=True, read_only=True)
    metadatas_set = serializers.CharField(source='get_metadata_set', read_only=True)
    spanned_clips = SpannedClipsSerializer(many=True, read_only=True)
    state = serializers.CharField(source='get_state', read_only=True)
    error = serializers.CharField(source='get_error', read_only=True)
    status_readable = serializers.CharField(source='get_readable_status', read_only=True)
    duration_readable = serializers.CharField(source='get_readable_duration', read_only=True)

    class Meta:
        model = Clip

class FolderSerializer(serializers.ModelSerializer):

    class Meta:
        model = Folder
        fields = ('id', 'user', 'created_on', 'basename', 'path')
    
    def validate_path(self, attrs, source):
        """
        Check that the blog post is about Django.
        """
        value = attrs[source]
        if value.endswith('/'):
            value = value[:-1]
        if isdir(value) is False:
            raise serializers.ValidationError("This folder doesn't exists")
        
        attrs[source] = value
        return attrs