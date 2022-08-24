import logging
from portal.vidispine.signals import vidispine_post_delete

log = logging.getLogger(__name__)

from portal.plugins.TapelessIngest.utilities import ClipResetWithItemDeletion


def item_post_delete_handler(instance, method, **kwargs):
    if method == "removeItem":
        ClipResetWithItemDeletion(instance)


vidispine_post_delete.connect(item_post_delete_handler)
