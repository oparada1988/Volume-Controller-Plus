# Import StreamController modules
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
from src.backend.DeckManagement.InputIdentifier import Input

# Import python & gtk modules
import os
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

# Import actions
from .actions.VolumeControl.VolumeControl import VolumeControl

class PluginTemplate(PluginBase):
    def __init__(self):
        super().__init__()

        ## Register actions
        self.volume_control_holder = ActionHolder(
            plugin_base = self,
            action_base = VolumeControl,
            action_id = "com_oparada_VolumeControllerPlus::VolumeControl",
            action_name = "Volume Controller Plus",
            icon = Gtk.Image(file=os.path.join(self.PATH, "assets", "Action_icon.png")),
            action_support = {
                Input.Key: ActionInputSupport.UNSUPPORTED,
                Input.Dial: ActionInputSupport.SUPPORTED,
                Input.Touchscreen: ActionInputSupport.SUPPORTED
            }
        )
        self.add_action_holder(self.volume_control_holder)

        # Register plugin
        self.register(
            plugin_name = "Volume Control for Stream Deck Plus",
            github_repo = "https://github.com/oparada1988/Volume-Controller-Plus",
            plugin_version = "1.0.0",
            app_version = "1.0.0-alpha"
        )

        # Apply robust Gtk/StreamController bug workarounds
        try:
            from src.windows.mainWindow.elements.Sidebar.elements.ActionConfigurator import CommentGroup
            
            # 1. Prevent TypeError: nothing connected
            original_disconnect = CommentGroup.disconnect_signals
            def safe_disconnect(self):
                try:
                    original_disconnect(self)
                except TypeError:
                    pass
            CommentGroup.disconnect_signals = safe_disconnect
            
            # 2. Prevent IndexError: list index out of range on corrupted dials
            original_get_comment = CommentGroup.get_comment
            def safe_get_comment(self):
                try:
                    return original_get_comment(self)
                except IndexError:
                    return ""
                except Exception:
                    return ""
            CommentGroup.get_comment = safe_get_comment
            
        except Exception:
            pass

    def get_selector_icon(self) -> Gtk.Widget:
        icon_path = os.path.join(self.PATH, "assets", "tune.png")
        return Gtk.Image(file=icon_path)