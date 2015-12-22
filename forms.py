from django import forms
from django.forms.extras.widgets import Select
from portal.plugins.TapelessIngest.models import MetadataMapping, Settings

def get_provider_metadatas():
    
    from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider
    provider = BaseProvider()
    provider_metadatas = provider.getAvailableMetadatas()
    
    return provider_metadatas


def get_system_fields():
    from portal.vidispine.iitem import ItemHelper

    itemhelper = ItemHelper()
    system_metadatas_fields = itemhelper.getAllMetadataFields(onlySortables=False, includeSystemFields=False, onlyReusables=False, onlyXMP=False)
    
    # get all portal metadatas
    system_metadatas = ()
    for system_metadatas_field in system_metadatas_fields:
        system_metadatas = system_metadatas + ((system_metadatas_field.getName(),system_metadatas_field.getLabel()),)
    return system_metadatas

def get_storagelist():
    from portal.vidispine.istorage import StorageHelper
    sth = StorageHelper()
    
    choices = ()
    
    storages = sth.getAllStorages()
    
    for storage in storages:
        choices = choices + ((storage.getId(), storage.getMetadataStorageName()),)
        
    return choices


class MetadataMappingForm(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super(MetadataMappingForm, self).__init__(*args, **kwargs)
        self.fields['metadata_provider'] = forms.ChoiceField(choices=[('','Select a provider field')] + get_provider_metadatas())
        self.fields['metadata_portal'] = forms.ChoiceField(choices=(('','Select a Portal field'),) + get_system_fields())
    
    class Meta:
        model = MetadataMapping
        fields = ('metadata_provider', 'metadata_portal')

class SettingsForm(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super(SettingsForm, self).__init__(*args, **kwargs)
        self.fields['storage_id'] = forms.ChoiceField(choices=get_storagelist())
    
    class Meta:
        model = Settings