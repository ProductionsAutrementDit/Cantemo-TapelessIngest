# Importing `plistner` is what REGISTERS the item-deletion listener: the
# module's `vidispine_post_delete.connect(...)` runs at import time and
# nowhere else. Without this line the module is never imported — the plugin
# shipped that way, and prod's __pycache__ proved it: `plugin.pyc` and
# `__init__.pyc` were byte-compiled under both Python 3.11 and 3.14, and no
# `plistner.pyc` ever appeared under either.
#
# The consequence was silent. Deleting an item in Portal left the Clip row
# pointing at a Vidispine item that no longer existed, so the clip could
# never be re-ingested. It stayed hidden while the scan re-created such
# items by accident (`Clip.item` returns None on NotFoundError, so
# `create_item` made a fresh placeholder); story 2.5's FR-23 rung then
# began skipping on stored item_id alone, without asking Vidispine, and the
# accident that had been covering for this stopped happening.
#
# This mirrors how the sibling collection_to_folder_mapper plugin wires its
# own listener, from its models/__init__.py.
from portal.plugins.TapelessIngest import plistner  # noqa: F401
