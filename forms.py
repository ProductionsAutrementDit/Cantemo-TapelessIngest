from django import forms
from django.forms.widgets import Select
from portal.plugins.TapelessIngest.models.settings import Settings, MetadataMapping


def get_provider_metadatas():
    """Every mappable provider key: the base 13 first, then each registry
    provider's own (``PROVIDER_NAMES`` order), duplicates dropped.

    A provider that cannot be loaded or listed costs its own keys only —
    the mapping page must still open."""
    import logging

    from portal.plugins.TapelessIngest.models.clip import Clip
    from portal.plugins.TapelessIngest.providers import PROVIDER_NAMES
    from portal.plugins.TapelessIngest.providers.providers import (
        Provider as BaseProvider,
    )

    provider_metadatas = []
    seen = set()

    def add(entries):
        for key, label in entries:
            if key not in seen:
                seen.add(key)
                provider_metadatas.append((key, label))

    add(BaseProvider().getAvailableMetadatas())
    for name in PROVIDER_NAMES:
        try:
            add(Clip.get_provider_by_name(name).getAvailableMetadatas())
        except Exception:
            logging.getLogger(__name__).exception(
                "provider %s: its mappable metadatas could not be listed", name
            )

    return tuple(provider_metadatas)


# The mapping formset builds one MetadataMappingForm per mapping row; the
# union is computed once per form build and reused for this long.
PROVIDER_METADATAS_TTL_SECONDS = 10
_provider_metadatas_cache = None  # (expires at, choices)


def provider_metadata_choices():
    """``get_provider_metadatas()``, memoized for a form build."""
    import time

    global _provider_metadatas_cache
    now = time.monotonic()
    cached = _provider_metadatas_cache
    if cached is not None and cached[0] > now:
        return cached[1]
    choices = get_provider_metadatas()
    _provider_metadatas_cache = (now + PROVIDER_METADATAS_TTL_SECONDS, choices)
    return choices


def get_system_fields():
    from portal.vidispine.iitem import ItemHelper

    system_metadatas = ()
    fields_in_groups = []

    itemhelper = ItemHelper()
    # get all portal metadatas

    vs_groups = itemhelper.getMetadataFieldGroups(content=True)
    for group in vs_groups.getGroups():
        for field in group.getFields():
            field_name = "%s:%s" % (group.getName(), field.getName())
            field_label = "%s:%s" % (group.getName(), field.getLabel())
            system_metadatas = system_metadatas + ((field_name, field_label),)

    system_metadatas_fields = itemhelper.getAllMetadataFields(
        onlySortables=False, includeSystemFields=True, onlyXMP=False
    )
    for system_metadatas_field in system_metadatas_fields:
        system_metadatas += (
            (system_metadatas_field.getName(), system_metadatas_field.getLabel()),
        )

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
    __doc__ = "\n    Metadata Mapping form\n    "

    def __init__(self, *args, **kwargs):
        super(MetadataMappingForm, self).__init__(*args, **kwargs)
        self.fields["metadata_provider"] = forms.ChoiceField(
            choices=(("", "Select a provider field"),) + provider_metadata_choices(),
            required=False,
        )
        self.fields["metadata_portal"] = forms.ChoiceField(
            choices=(("", "Select a Portal field"),) + get_system_fields(),
            required=False,
        )

    class Meta:
        model = MetadataMapping
        fields = ("metadata_provider", "metadata_portal")


class SettingsForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super(SettingsForm, self).__init__(*args, **kwargs)
        self.fields["storage_id"] = forms.ChoiceField(choices=get_storagelist())

    class Meta:
        model = Settings
        fields = "__all__"
