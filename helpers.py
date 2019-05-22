# coding: utf-8

import logging
log = logging.getLogger(__name__)

from portal.plugins.TapelessIngest.models import Settings

class BMXTransWrapHelper():
    def __init__(self):
        settings = Settings.objects.get(pk=1)
        self.bmxtranswrap_bin = settings.bmxtranswrap
        self.input_streams = []
        self.errors = []
        self.output_stream = ""