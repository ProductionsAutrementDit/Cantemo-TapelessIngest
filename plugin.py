"""
This is your new plugin handler code.

Put your plugin handling code in here. remember to update the __init__.py file with 
you app version number. We have automatically generated a GUID for you, namespace, and a url 
that serves up index.html file
"""
import logging

from portal.pluginbase.core import Plugin, implements
from portal.generic.plugin_interfaces import (IPluginURL, IPluginBlock, IAppRegister, IContextProcessor)

log = logging.getLogger(__name__)

# Register the URL file
class TapelessingestPluginURL(Plugin):
    """ Adds a plugin handler which creates url handler for the index page """
    implements(IPluginURL)

    def __init__(self):
        self.name = "Tapelessingest App"
        self.urls = 'portal.plugins.TapelessIngest.urls'
        self.urlpattern = r'^tapelessingest/'
        self.namespace = r'tapelessingest'
        self.plugin_guid = '7c4fcd24-3a86-4530-b42b-f81cbaa03b6d'
        log.debug("Initiated Tapelessingest App")

pluginurls = TapelessingestPluginURL()

# Create a menu item for settings in the admin section in the navbar
class TapelessingestAdminNavigationPlugin(Plugin):
    # This adds your app to the navigation bar
    # Please update the information below with the author etc..
    implements(IPluginBlock)

    def __init__(self):
        self.name = "NavigationAdminPlugin"
        self.plugin_guid = '105de45f-c59e-479f-9108-546bf4b4185d'
        log.debug('Initiated navigation plugin')

    # Returns the template file navigation.html
    # Change navigation.html to the string that you want to use
    def return_string(self, tagname, *args):
        return {'guid': self.plugin_guid, 'template': 'TapelessIngest/navigation.html'}

navbaradminplug = TapelessingestAdminNavigationPlugin()

class TapelessingestAdminMenuPlugin(Plugin):
    u""" adds a menu item to the admin screen
    """
    implements(IPluginBlock)

    def __init__(self):
        self.name = u'AdminLeftPanelBottomPanePlugin'
        self.plugin_guid = u'57fd8d82-cc72-4883-878f-971bf7ec198d'

    def return_string(self, tagname, *args):
        from portal.plugins.TapelessIngest.utilities import getProvidersList
        providers = getProvidersList()
        ctx = {"providers": providers}
        return {u'guid': self.plugin_guid, u'template': u'TapelessIngest/admin/admin_leftpanel_pane.html', u'context': ctx}

pluginblock = TapelessingestAdminMenuPlugin()

# Create a menu item for ingest in the ingest section in the navbar
class TapelessingestMenuNavigationPlugin(Plugin):
    implements(IPluginBlock)

    def __init__(self):
        self.name = "NavigationIngestPlugin"
        self.plugin_guid = "897c2112-6d41-4c94-b9bb-4bcbf0cae483"

    def return_string(self, tagname, *args):
        return {'guid': self.plugin_guid, 'template': 'TapelessIngest/menuitem.html'}

navbarplugin = TapelessingestMenuNavigationPlugin()

class TapelessIngestMenuItemPlugin(Plugin):
    implements(IPluginBlock)

    def __init__(self):
        self.name = u'MediaViewLeftPanelMenu'
        self.plugin_guid = u'a603a4f3-4d0e-4667-998e-76985e94d572'

    def return_string(self, tagname, *args):
        from models import Clip
        _context = args[1]
        cur_item = _context[u'item']
        try:
            clip = Clip.objects.get(item_id=cur_item.getId())
        except Clip.DoesNotExist:
            clip = False
        return {u'guid': self.plugin_guid,
         u'template': u'TapelessIngest/ti_menuitem.html', u'context': {u'item_id': cur_item.getId(), u'clip': clip}}


ti_menu_item = TapelessIngestMenuItemPlugin()


class TapelessIngestInlineJSPlugin(Plugin):
    implements(IPluginBlock)

    def __init__(self):
        self.name = u'MediaViewInLineJS'
        self.plugin_guid = u'13fdcff9-9d73-4449-8585-68a2c62fd4b8'

    def return_string(self, tagname, *args):
        _context = args[1]
        vsitem = _context[u'item']
        return {u'guid': self.plugin_guid,
         u'template': u'TapelessIngest/ti_inlinejs.html'}

ti_inline_js = TapelessIngestInlineJSPlugin()

class TapelessIngestPanelPlugin(Plugin):
    implements(IPluginBlock)

    def __init__(self):
        self.name = u'MediaViewPanelRow1'
        self.plugin_guid = u'98182183-7950-4b27-a442-eba65221d854'

    def return_string(self, tagname, *args):
        from portal.plugins.TapelessIngest.models import Clip
        _context = args[1]
        cur_item = _context[u'item']
        try:
            clip = Clip.objects.get(item_id=cur_item.getId())
        except Clip.DoesNotExist:
            clip = False
        return {u'guid': self.plugin_guid,
         u'template': u'TapelessIngest/ti_viewpanel.html',
         u'context': {u'item_id': cur_item.getId(), u'clip': clip}}


ti_block = TapelessIngestPanelPlugin()


# Register the app
class TapelessingestRegister(Plugin):
    # This adds it to the list of installed Apps
    # Please update the information below with the author etc..
    implements(IAppRegister)

    def __init__(self):
        self.name = "Tapelessingest"
        self.plugin_guid = 'd2fdc1ea-68e8-42b4-a723-e262f524c12e'
        log.debug('Register the App')

    def __call__(self):
        _app_dict = {
                'name': 'Tapelessingest',
                'version': '0.0.1',
                'author': 'Camille Darley - Productions Autrement Dit',
                'author_url': 'www.studiopad.fr',
                'notes': 'Copyright 2015. All Rights Reserved'}
        return _app_dict

tapelessingestplugin = TapelessingestRegister()


