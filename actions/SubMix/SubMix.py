import os
import time
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from ..WaveControllerBaseAction import WaveControllerBaseAction

class SubMix(WaveControllerBaseAction):
    """
    Sub-Mix Fader Action.
    Controls a specific audio channel's independent send level & mute state
    within a designated virtual mix bus (1:1 with WaveController Matrix Cells).
    """
    action_description = "Independent send level of a channel into a specific mix matrix (1:1 with WaveController matrix cells)."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mixes_list = []
        self.channels_list = []
        self._updating_dropdowns = False

    def get_configured_mix_id(self) -> str:
        settings = self.get_settings() or {}
        m = settings.get("mix_id")
        if not m:
            data = self.client.get_channels_and_mixes()
            mixes = data.get("mixes", [])
            if mixes:
                m = mixes[0]["id"]
            else:
                m = "personal_mix"
        return m

    def get_configured_channel_id(self) -> str:
        settings = self.get_settings() or {}
        ch = settings.get("channel_id")
        if not ch:
            data = self.client.get_channels_and_mixes()
            channels = data.get("channels", [])
            if channels:
                ch = channels[0]["id"]
            else:
                ch = "spotify"
        return ch

    def initial_load_status(self):
        ch_id = self.get_configured_channel_id()
        m_id = self.get_configured_mix_id()
        vol, muted = self.client.get_channel_volume(ch_id, mix_id=m_id)
        self.current_volume = int(vol) if vol is not None else 80
        self.last_mute = bool(muted) if muted is not None else False

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

    def _match_mix(self, m_id: str, mixes: list) -> dict:
        if not mixes:
            return {}
        if not m_id:
            return mixes[0]
        m_id_low = m_id.lower().strip()
        for m in mixes:
            if m.get("id") == m_id or m.get("name", "").lower() == m_id_low:
                return m
        for m in mixes:
            cand_id = m.get("id", "").lower()
            cand_name = m.get("name", "").lower()
            if m_id_low in cand_id or cand_id in m_id_low:
                return m
            if m_id_low in cand_name or cand_name in m_id_low:
                return m
        return mixes[0]

    def get_target_title_and_subtitle(self) -> tuple:
        ch_id = self.get_configured_channel_id()
        m_id = self.get_configured_mix_id()
        data = self.client.get_channels_and_mixes()
        channels = data.get("channels", [])
        mixes = data.get("mixes", [])
        
        c = self._match_channel(ch_id, channels)
        m = self._match_mix(m_id, mixes)

        ch_name = c.get("name", ch_id.capitalize())
        mix_name = m.get("name", m_id.capitalize())

        return ch_name, mix_name

    def get_target_icon_path(self) -> str:
        ch_id = self.get_configured_channel_id()
        data = self.client.get_channels_and_mixes()
        channels = data.get("channels", [])
        c = self._match_channel(ch_id, channels)
        if c.get("icon") and c.get("icon") not in ("network-offline-symbolic",):
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
        m_id = self.get_configured_mix_id()
        curr = self.current_volume if self.current_volume is not None else 80
        self.current_volume = max(0, min(100, curr + delta))
        self._last_volume_adjust_time = time.time()
        self.client.set_channel_volume(ch_id, self.current_volume, mix_id=m_id)
        self.update_ui_rendering(force=True)

    def handle_mute_toggle(self):
        ch_id = self.get_configured_channel_id()
        m_id = self.get_configured_mix_id()
        new_mute = self.client.toggle_channel_mute(ch_id, mix_id=m_id)
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

    def update_dropdowns(self):
        if not hasattr(self, "mix_selector") or not hasattr(self, "channel_selector"):
            return
        self._updating_dropdowns = True
        try:
            settings = self.get_settings() or {}
            data = self.client.get_channels_and_mixes(force=True)
            channels = data.get("channels", [])
            mixes = data.get("mixes", [])

            # 1. Populate Mixes
            self.mixes_list = []
            if mixes:
                for m in mixes:
                    self.mixes_list.append((m["id"], m.get("name", m["id"].capitalize())))
            else:
                self.mixes_list = [("personal_mix", "Personal Mix"), ("chat_mix", "Chat Mix")]

            self.mix_model = Gtk.StringList()
            for _, display_name in self.mixes_list:
                self.mix_model.append(display_name)
            self.mix_selector.set_model(self.mix_model)

            current_mix = settings.get("mix_id")
            mix_idx = 0
            if current_mix:
                for idx, (mid, _) in enumerate(self.mixes_list):
                    if mid == current_mix:
                        mix_idx = idx
                        break
            else:
                if self.mixes_list:
                    settings["mix_id"] = self.mixes_list[0][0]
                    settings["mix_name"] = self.mixes_list[0][1]

            self.mix_selector.set_selected(mix_idx)

            # 2. Populate Channels
            self.channels_list = []
            if channels:
                for c in channels:
                    self.channels_list.append((c["id"], c.get("name", c["id"].capitalize())))
            else:
                self.channels_list = [("spotify", "Spotify"), ("fefine", "Fefine")]

            self.channel_model = Gtk.StringList()
            for _, display_name in self.channels_list:
                self.channel_model.append(display_name)
            self.channel_selector.set_model(self.channel_model)

            current_ch = settings.get("channel_id")
            ch_idx = 0
            if current_ch:
                for idx, (cid, _) in enumerate(self.channels_list):
                    if cid == current_ch:
                        ch_idx = idx
                        break
            else:
                if self.channels_list:
                    settings["channel_id"] = self.channels_list[0][0]
                    settings["channel_name"] = self.channels_list[0][1]

            self.channel_selector.set_selected(ch_idx)
            self.set_settings(settings)
        finally:
            self._updating_dropdowns = False

    def _on_mix_selected(self, combo, *args):
        if self._updating_dropdowns:
            return
        selected_idx = combo.get_selected()
        if 0 <= selected_idx < len(self.mixes_list):
            m_id, m_name = self.mixes_list[selected_idx]
            settings = self.get_settings() or {}
            settings["mix_id"] = m_id
            settings["mix_name"] = m_name
            self.set_settings(settings)
            self._cached_midground = None
            self.initial_load_status()
            self.update_ui_rendering(force=True)

    def _on_channel_selected(self, combo, *args):
        if self._updating_dropdowns:
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

        # 1. Mix Selector (Personal Mix, Stream Mix, etc.)
        self.mix_model = Gtk.StringList()
        self.mix_selector = Adw.ComboRow(
            model=self.mix_model,
            title="Target Mix Bus"
        )
        self.mix_selector.connect("notify::selected", self._on_mix_selected)

        # 2. Channel Selector (Spotify, Discord, Microphone, etc.)
        self.channel_model = Gtk.StringList()
        self.channel_selector = Adw.ComboRow(
            model=self.channel_model,
            title="Audio Channel"
        )
        self.channel_selector.connect("notify::selected", self._on_channel_selected)
        self.update_dropdowns()

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
            self.mix_selector,
            self.channel_selector,
            self.step_selector,
            self.vol_format_selector,
            self.live_meter_row
        ]
