# coding=utf-8
from __future__ import absolute_import

import octoprint.plugin
import octoprint.printer

__plugin_name__ = "flyingOctobear"
__plugin_version__ = "0.1.0"
__plugin_description__ = "Plugin for WIFI control FlyingBear Ghost4"

class OctoBear(octoprint.plugin.OctoPrintPlugin):
  def __init__(self):
    self.printer = None

  def printer_factory_hook(self, components, *args, **kwargs):
    return self.printer

def __plugin_load__():
    plugin = OctoBear()

    global __plugin_implementation__
    __plugin_implementation__ = plugin

    global __plugin_hooks__
    __plugin_hooks__ = {"octoprint.printer.factory": plugin.printer_factory_hook}
