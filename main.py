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
from .actions.ChannelMaster.ChannelMaster import ChannelMaster
from .actions.SubMix.SubMix import SubMix
from .actions.MixMaster.MixMaster import MixMaster
from .actions.VolumeControl.VolumeControl import VolumeControl

class PluginTemplate(PluginBase):
    def __init__(self):
        super().__init__()

        icon_img = Gtk.Image(file=os.path.join(self.PATH, "assets", "Action_icon.png"))
        supported_inputs = {
            Input.Key: ActionInputSupport.UNSUPPORTED,
            Input.Dial: ActionInputSupport.SUPPORTED,
            Input.Touchscreen: ActionInputSupport.SUPPORTED
        }

        # 1. Channel Master Action
        self.channel_master_holder = ActionHolder(
            plugin_base = self,
            action_base = ChannelMaster,
            action_id = "com_oparada_WaveControllerPlugin::ChannelMaster",
            action_name = "Channel",
            icon = icon_img,
            action_support = supported_inputs
        )
        self.add_action_holder(self.channel_master_holder)

        # 2. Sub-Mix Fader Action
        self.sub_mix_holder = ActionHolder(
            plugin_base = self,
            action_base = SubMix,
            action_id = "com_oparada_WaveControllerPlugin::SubMix",
            action_name = "Sub-Mix",
            icon = icon_img,
            action_support = supported_inputs
        )
        self.add_action_holder(self.sub_mix_holder)

        # 3. Mix Master Output Action
        self.mix_master_holder = ActionHolder(
            plugin_base = self,
            action_base = MixMaster,
            action_id = "com_oparada_WaveControllerPlugin::MixMaster",
            action_name = "Master Mix",
            icon = icon_img,
            action_support = supported_inputs
        )
        self.add_action_holder(self.mix_master_holder)

        # Register plugin
        self.register(
            plugin_name = "WaveController",
            github_repo = "https://github.com/oparada1988/WaveController",
            plugin_version = "1.2.0",
            app_version = "1.0.0-alpha"
        )

        # Apply robust Gtk/StreamController bug workarounds & enhancements
        try:
            from src.windows.mainWindow.elements.Sidebar.elements.ActionConfigurator import CommentGroup, ConfigGroup
            
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

            # 3. Dynamic Action Description in Configuration Header
            original_load_for_action = ConfigGroup.load_for_action
            def safe_load_for_action(self, action):
                original_load_for_action(self, action)
                if hasattr(action, "action_description") and action.action_description:
                    self.set_description(action.action_description)
            ConfigGroup.load_for_action = safe_load_for_action
            
        except Exception:
            pass

    def get_selector_icon(self) -> Gtk.Widget:
        icon_path = os.path.join(self.PATH, "assets", "tune.png")
        return Gtk.Image(file=icon_path)