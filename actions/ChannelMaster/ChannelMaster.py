import os
import time
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from ..WaveControllerBaseAction import WaveControllerBaseAction

class ChannelMaster(WaveControllerBaseAction):
    """
    Channel Master Fader Action.
    Controls global volume, mute, and live stereo VU meter for a specific audio channel.
    """
    action_description = "Master volume fader, mute toggle, and real-time VU peak metering for an input channel."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channels_list = []
        self._updating_channel_dropdown = False

    def initial_load_status(self):
        if time.time() - getattr(self, "_last_volume_adjust_time", 0.0) < 0.40:
            return
        settings = self.get_settings() or {}
        ch_id = self.get_configured_channel_id()
        if not settings.get("channel_id") and ch_id:
            settings["channel_id"] = ch_id
            self.set_settings(settings)
        vol, muted = self.client.get_channel_volume(ch_id)
        self.current_volume = int(vol) if vol is not None else 80
        self.last_mute = bool(muted) if muted is not None else False

    def get_configured_channel_id(self) -> str:
        settings = self.get_settings() or {}
        ch = settings.get("channel_id")
        if not ch:
            data = self.client.get_channels_and_mixes()
            channels = data.get("channels", [])
            if channels:
                ch = channels[0]["id"]
            else:
                ch = "mic"
        return ch

    def _match_channel(self, ch_id: str, channels: list) -> dict:
        if not channels:
            return {}
        if not ch_id:
            return channels[0]
        ch_id_low = ch_id.lower().strip()
        for c in channels:
            if c.get("id") == ch_id or c.get("name", "").lower() == ch_id_low:
                return c
        for c in channels:
            cand_id = c.get("id", "").lower()
            cand_name = c.get("name", "").lower()
            if ch_id_low in cand_id or cand_id in ch_id_low:
                return c
            if ch_id_low in cand_name or cand_name in ch_id_low:
                return c
        return channels[0]

    def get_target_title_and_subtitle(self) -> tuple:
        ch_id = self.get_configured_channel_id()
        data = self.client.get_channels_and_mixes()
        channels = data.get("channels", [])
        c = self._match_channel(ch_id, channels)
        name = c.get("name", ch_id.capitalize())
        if name.startswith("Elgato "):
            name = name[len("Elgato "):]
        return name, "Master"

    def get_target_icon_path(self) -> str:
        ch_id = self.get_configured_channel_id()
        data = self.client.get_channels_and_mixes()
        channels = data.get("channels", [])
        c = self._match_channel(ch_id, channels)
        if c.get("icon") and c.get("icon") not in ("network-offline-symbolic",):
            # If it's a mic channel with generic icon, verify if Wave XLR is attached
            if ch_id.lower() in ("mic", "microphone") and c.get("icon") == "audio-input-microphone-symbolic":
                hw_status = self.client.get_hardware_status()
                dev_name = hw_status.get("device_name", "").lower()
                if "xlr" in dev_name or "wave" in dev_name:
                    return "elgato-wave-xlr-symbolic"
            return c.get("icon")
        
        assigned_map = data.get("assigned_apps", {})
        apps = assigned_map.get(ch_id, [])
        if apps:
            app_low = apps[0].lower()
            if "spotify" in app_low:
                return "spotify"
            elif "discord" in app_low:
                return "discord"
            elif "steam" in app_low or "game" in app_low:
                return "steam"
            elif "firefox" in app_low:
                return "firefox"
            elif "chrome" in app_low or "chromium" in app_low:
                return "chromium"
            elif "vlc" in app_low:
                return "vlc"

        if ch_id.lower() in ("mic", "microphone", "fefine", "fifine", "wave"):
            hw_status = self.client.get_hardware_status()
            dev_name = hw_status.get("device_name", "").lower()
            if "xlr" in dev_name or "wave" in dev_name:
                return "elgato-wave-xlr-symbolic"
            return "audio-input-microphone-symbolic"
        return "audio-volume-high-symbolic"

    def handle_volume_change(self, delta: int):
        ch_id = self.get_configured_channel_id()
        curr = self.current_volume if self.current_volume is not None else 80
        self.current_volume = max(0, min(100, curr + delta))
        self._last_volume_adjust_time = time.time()
        self.client.set_channel_volume(ch_id, self.current_volume)
        self.update_ui_rendering(force=True)

    def handle_mute_toggle(self):
        ch_id = self.get_configured_channel_id()
        new_mute = self.client.toggle_channel_mute(ch_id)
        self.last_mute = new_mute
        self.update_ui_rendering(force=True)

    def get_hardware_telemetry_info(self):
        ch_id = self.get_configured_channel_id()
        data = self.client.get_channels_and_mixes()
        channels = data.get("channels", [])
        c = self._match_channel(ch_id, channels)
        icon = c.get("icon", "")

        is_wave = (
            ch_id.lower() in ("mic", "microphone", "wave_xlr", "elgato_wave_xlr") or
            "xlr" in ch_id.lower() or
            "wave" in ch_id.lower() or
            icon == "elgato-wave-xlr-symbolic"
        )
        if not is_wave:
            return None

        hw = self.client.get_hardware_status() or {}
        dev_name = hw.get("device_name", "").lower()
        elgato_info = hw.get("elgato_info", {})

        is_online = hw.get("is_connected")
        if is_online is None or is_online is False:
            if elgato_info and elgato_info.get("connected"):
                is_online = True
            elif ("xlr" in dev_name or "wave" in dev_name) and hw.get("gain_db") is not None:
                is_online = True
            else:
                is_online = False
        else:
            is_online = bool(is_online)

        phantom_48v = hw.get("phantom_48v", False)
        if not phantom_48v and elgato_info:
            phantom_48v = elgato_info.get("phantom_power", False)

        return {
            "is_hardware": True,
            "is_online": bool(is_online),
            "phantom_48v": bool(phantom_48v),
            "gain_db": hw.get("gain_db", 0)
        }

    def handle_touch_tap(self, data: dict) -> bool:
        """Handles touchscreen taps. Tapping the 48V badge toggles phantom power."""
        telemetry = self.get_hardware_telemetry_info()
        if not telemetry or not isinstance(data, dict):
            return False

        x = data.get("x")
        y = data.get("y")
        if x is None or y is None:
            return False

        # Stream Deck + dial screen slice width is 200px (4 dials x 200 = 800)
        rel_x = x % 200
        rel_y = y
        style = self.get_ui_style()

        if style == "wave":
            # Wave style: 48V badge is at top right: rel_x in [130, 195], rel_y in [10, 40]
            if rel_x >= 130 and 10 <= rel_y <= 40:
                self.client.toggle_phantom_power()
                self.update_ui_rendering(force=True)
                return True
        else:
            # Slot 2 (48V badge) touch bounding box in DX style: rel_x in [130, 200], rel_y in [40, 75]
            if rel_x >= 130 and 40 <= rel_y <= 75:
                self.client.toggle_phantom_power()
                self.update_ui_rendering(force=True)
                return True

        return False

    def get_current_peak_val(self) -> float:
        ch_id = self.get_configured_channel_id()
        peaks = self.client.get_peaks()
        val = peaks.get(ch_id)
        if val is None:
            for k, v in peaks.items():
                if k.lower() == ch_id.lower() or k.lower() in ch_id.lower() or ch_id.lower() in k.lower():
                    val = v
                    break
        return self._extract_peak_value(val)

    def get_current_peaks_stereo(self) -> tuple:
        ch_id = self.get_configured_channel_id()
        peaks = self.client.get_peaks()
        val = peaks.get(ch_id)
        if val is None:
            for k, v in peaks.items():
                if k.lower() == ch_id.lower() or k.lower() in ch_id.lower() or ch_id.lower() in k.lower():
                    val = v
                    break
        return self._extract_stereo_peak_values(val)

    def update_channel_dropdown(self):
        if not hasattr(self, "channel_selector"):
            return
        self._updating_channel_dropdown = True
        try:
            settings = self.get_settings() or {}
            data = self.client.get_channels_and_mixes(force=True)
            channels = data.get("channels", [])

            self.channels_list = []
            if channels:
                for c in channels:
                    name = c.get("name", c["id"].capitalize())
                    clean_name = name[len("Elgato "):] if name.startswith("Elgato ") else name
                    self.channels_list.append((c["id"], clean_name))
            else:
                self.channels_list = [("mic", "Microphone"), ("spotify", "Spotify")]

            self.channel_model = Gtk.StringList()
            for _, display_name in self.channels_list:
                self.channel_model.append(display_name)

            self.channel_selector.set_model(self.channel_model)

            current_ch = settings.get("channel_id")
            selected_idx = 0
            if current_ch:
                for idx, (cid, _) in enumerate(self.channels_list):
                    if cid == current_ch:
                        selected_idx = idx
                        break
            else:
                if self.channels_list:
                    settings["channel_id"] = self.channels_list[0][0]
                    settings["channel_name"] = self.channels_list[0][1]
                    self.set_settings(settings)

            self.channel_selector.set_selected(selected_idx)
        finally:
            self._updating_channel_dropdown = False

    def _on_channel_selected(self, combo, *args):
        if self._updating_channel_dropdown:
            return
        selected_idx = combo.get_selected()
        if 0 <= selected_idx < len(self.channels_list):
            ch_id, ch_name = self.channels_list[selected_idx]
            settings = self.get_settings() or {}
            settings["channel_id"] = ch_id
            settings["channel_name"] = ch_name
            self.set_settings(settings)
            self._cached_midground = None
            self.initial_load_status()
            self.update_ui_rendering(force=True)

    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        # 1. Channel Selector (Queries active WaveController channels)
        self.channel_model = Gtk.StringList()
        self.channel_selector = Adw.ComboRow(
            model=self.channel_model,
            title="Audio Channel"
        )
        self.channel_selector.connect("notify::selected", self._on_channel_selected)
        self.update_channel_dropdown()

        return [self.channel_selector] + self.get_base_config_rows()
