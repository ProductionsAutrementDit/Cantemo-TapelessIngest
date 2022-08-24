from django.core.cache import cache
from urllib.parse import quote_plus

import logging

log = logging.getLogger(__name__)

PROVIDERS_LIST = ["panasonicP2", "xdcam", "jvcprohd", "video_file"]

TAGS_FIELD = "portal_mf245404"
COLLECTION_TAGS_FIELD = "portal_mf423577"


def build_nested_helper(path, text, container, value):
    segs = path.split(":")
    head = segs[0]
    tail = segs[1:]
    if not tail:
        container["fields"][head] = {"type": "text", "value": value}
    else:
        if "groups" not in container:
            container["groups"] = {}
        if head not in container["groups"]:
            container["groups"][head] = [{"fields": {}}]
        build_nested_helper(":".join(tail), text, container["groups"][head][0], value)

def build_nested(paths):
    container = {"fields": {}, "groups": {}}
    for path, value in paths.items():
        build_nested_helper(path, path, container, value)
    return container

def ClipResetWithItemDeletion(_deleted_resource_name):
    from portal.plugins.TapelessIngest.models.clip import Clip

    """
    When deleting an item in Vidispine, check the corresponding item in Clips and delete it.
    """
    log.info("Updating all Clip associated with ID %s" % _deleted_resource_name)
    try:
        clips = Clip.objects.filter(item_id=_deleted_resource_name)
        for clip in clips:
            clip.file_id = None
            clip.item_id = ""
            clip.job_id = ""
            clip.output_file = None
            clip.save()
            log.info(f"Put clip {clip.umid} status as Not Imported")
    except Exception as e:
        log.info(
            f"Automatic Clip item deletion based upon Media deletion failed because {e}"
        )
