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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channels_list = []
        self._updating_channel_dropdown = False

    def initial_load_status(self):
        settings = self.get_settings() or {}
        ch_id = self.get_configured_channel_id()
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
        return c.get("name", ch_id.capitalize()), "Master"

    def get_target_icon_path(self) -> str:
        ch_id = self.get_configured_channel_id()
        data = self.client.get_channels_and_mixes()
        channels = data.get("channels", [])
        c = self._match_channel(ch_id, channels)
        if c.get("icon"):
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

        if ch_id.lower() in ("mic", "microphone", "fefine", "fifine"):
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

    def get_current_peak_val(self) -> float:
        ch_id = self.get_configured_channel_id()
        peaks = self.client.get_peaks()
        val = peaks.get(ch_id)
        if val is None:
            for k, v in peaks.items():
                if k.lower() == ch_id.lower() or k.lower() in ch_id.lower() or ch_id.lower() in k.lower():
                    val = v
                    break
        if val is None and ch_id.lower() not in ("mic", "microphone", "input"):
            val = peaks.get("system", peaks.get("spotify", peaks.get("music")))
        return self._extract_peak_value(val)

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
                    self.channels_list.append((c["id"], c.get("name", c["id"].capitalize())))
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
        settings = self.get_settings() or {}
        vol_format = settings.get("volume_format", "percent")

        # 1. Channel Selector (Queries active WaveController channels)
        self.channel_model = Gtk.StringList()
        self.channel_selector = Adw.ComboRow(
            model=self.channel_model,
            title="Audio Channel"
        )
        self.channel_selector.connect("notify::selected", self._on_channel_selected)
        self.update_channel_dropdown()

        # 2. Custom Label
        self.custom_name_row = Adw.EntryRow(
            title="Custom Label",
            text=settings.get("custom_name", "")
        )
        def on_name_changed(entry, *args):
            s = self.get_settings() or {}
            s["custom_name"] = entry.get_text().strip()
            self.set_settings(s)
            self._cached_midground = None
            self.update_ui_rendering(force=True)
        self.custom_name_row.connect("notify::text", on_name_changed)

        # 3. Volume Step Size
        self.step_model = Gtk.StringList()
        step_sizes = ["1%", "2%", "5%", "10%"]
        for size in step_sizes:
            self.step_model.append(size)
        self.step_selector = Adw.ComboRow(
            model=self.step_model,
            title="Volume Step Size"
        )
        curr_step = f"{self.get_step_size()}%"
        self.step_selector.set_selected(step_sizes.index(curr_step) if curr_step in step_sizes else 2)
        def on_step_changed(combo, *args):
            s = self.get_settings() or {}
            idx = combo.get_selected()
            if 0 <= idx < len(step_sizes):
                s["step_size"] = step_sizes[idx]
                self.set_settings(s)
        self.step_selector.connect("notify::selected", on_step_changed)

        # 4. Volume Format
        self.vol_format_model = Gtk.StringList()
        self.vol_format_model.append("Percentage (%)")
        self.vol_format_model.append("Decibels (dB)")
        self.vol_format_selector = Adw.ComboRow(
            model=self.vol_format_model,
            title="Volume Display Format"
        )
        self.vol_format_selector.set_selected(0 if vol_format == "percent" else 1)
        def on_format_changed(combo, *args):
            s = self.get_settings() or {}
            s["volume_format"] = "percent" if combo.get_selected() == 0 else "db"
            self.set_settings(s)
            self._cached_midground = None
            self.update_ui_rendering(force=True)
        self.vol_format_selector.connect("notify::selected", on_format_changed)

        # 5. Live Peak Meter Toggle
        self.live_meter_row = Adw.SwitchRow(
            title="Live Peak Meter"
        )
        self.live_meter_row.set_active(settings.get("live_meter", True))
        def on_meter_toggled(switch, *args):
            s = self.get_settings() or {}
            s["live_meter"] = switch.get_active()
            self.set_settings(s)
            self._cached_midground = None
            self.update_ui_rendering(force=True)
        self.live_meter_row.connect("notify::active", on_meter_toggled)

        return [
            self.channel_selector,
            self.custom_name_row,
            self.step_selector,
            self.vol_format_selector,
            self.live_meter_row
        ]
