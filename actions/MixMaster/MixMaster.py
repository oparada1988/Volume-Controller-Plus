import os
import time
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent

from ..WaveControllerBaseAction import WaveControllerBaseAction

class MixMaster(WaveControllerBaseAction):
    """
    Mix Master Output Action.
    Controls overall mix bus master output volume, master mute, and destination output device routing
    (1:1 with WaveController top Mix Header Cards).
    """
    action_description = "Master output bus volume, mute toggle, and physical audio output device routing."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mixes_list = []
        self.devices_list = []
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

    def initial_load_status(self):
        m_id = self.get_configured_mix_id()
        vol, muted = self.client.get_mix_master_volume(m_id)
        self.current_volume = int(vol) if vol is not None else 100
        self.last_mute = bool(muted) if muted is not None else False

    def _match_mix(self, m_id: str, mixes: list) -> dict:
        if not mixes:
            return {}
        if not m_id:
            return mixes[0]
        m_id_low = m_id.lower().strip()
        # 1. Exact match by id or name
        for m in mixes:
            if m.get("id") == m_id or m.get("name", "").lower() == m_id_low:
                return m
        # 2. Fuzzy / prefix / suffix match (e.g. "personal_mix" matches "personal", "personal" matches "personal_mix")
        for m in mixes:
            cand_id = m.get("id", "").lower()
            cand_name = m.get("name", "").lower()
            if m_id_low in cand_id or cand_id in m_id_low:
                return m
            if m_id_low in cand_name or cand_name in m_id_low:
                return m
        return mixes[0]

    def get_target_title_and_subtitle(self) -> tuple:
        m_id = self.get_configured_mix_id()
        data = self.client.get_channels_and_mixes()
        mixes = data.get("mixes", [])
        m = self._match_mix(m_id, mixes)
        
        mix_name = m.get("name", m_id.capitalize())
        target_dev = m.get("target_device", "none")

        # Resolve target device display name
        dev_display = "Output"
        if target_dev and target_dev != "none":
            devices = self.client.get_output_devices()
            for d in devices:
                if d.get("name") == target_dev:
                    dev_display = d.get("display_name", d.get("name", "Output"))
                    break

        return mix_name, dev_display

    def get_target_icon_path(self) -> str:
        m_id = self.get_configured_mix_id()
        data = self.client.get_channels_and_mixes()
        mixes = data.get("mixes", [])
        m = self._match_mix(m_id, mixes)
        if m.get("icon"):
            return m.get("icon")
        return "audio-headphones-symbolic"

    def handle_volume_change(self, delta: int):
        m_id = self.get_configured_mix_id()
        curr = self.current_volume if self.current_volume is not None else 100
        self.current_volume = max(0, min(100, curr + delta))
        self._last_volume_adjust_time = time.time()
        self.client.set_mix_master_volume(m_id, self.current_volume)
        self.update_ui_rendering(force=True)

    def handle_mute_toggle(self):
        m_id = self.get_configured_mix_id()
        new_mute = self.client.toggle_mix_master_mute(m_id)
        self.last_mute = new_mute
        self.update_ui_rendering(force=True)

    def handle_cycle_device(self):
        m_id = self.get_configured_mix_id()
        new_dev = self.client.cycle_mix_target_device(m_id)
        settings = self.get_settings() or {}
        settings["target_device"] = new_dev
        self.set_settings(settings)
        self._cached_midground = None
        self.update_ui_rendering(force=True)

    def event_callback(self, event: InputEvent, data: dict = None):
        if event == Input.Dial.Events.TURN_CW:
            step_val = self.get_step_size()
            self.handle_volume_change(step_val)
        elif event == Input.Dial.Events.TURN_CCW:
            step_val = self.get_step_size()
            self.handle_volume_change(-step_val)
        elif event in (Input.Dial.Events.DOWN, Input.Dial.Events.SHORT_TOUCH_PRESS):
            self.handle_mute_toggle()
        elif event == Input.Dial.Events.LONG_TOUCH_PRESS:
            self.handle_cycle_device()
        elif hasattr(Input, "Touchscreen") and hasattr(Input.Touchscreen, "Events") and event in (
            getattr(Input.Touchscreen.Events, "SHORT_PRESS", None),
            getattr(Input.Touchscreen.Events, "TAP", None)
        ):
            self.handle_mute_toggle()
        elif hasattr(Input, "Touchscreen") and hasattr(Input.Touchscreen, "Events") and event in (
            getattr(Input.Touchscreen.Events, "DRAG_LEFT", None),
            getattr(Input.Touchscreen.Events, "DRAG_RIGHT", None),
            getattr(Input.Touchscreen.Events, "LONG_PRESS", None),
            getattr(Input.Touchscreen.Events, "DRAG", None)
        ):
            self.handle_cycle_device()

    def get_current_peak_val(self) -> float:
        m_id = self.get_configured_mix_id()
        peaks = self.client.get_peaks()
        val = peaks.get(f"{m_id}_mix", peaks.get(m_id, peaks.get("master")))
        if val is None:
            val = peaks.get("system", peaks.get("spotify", peaks.get("music")))
        return self._extract_peak_value(val)

    def update_dropdowns(self):
        if not hasattr(self, "mix_selector") or not hasattr(self, "device_selector"):
            return
        self._updating_dropdowns = True
        try:
            settings = self.get_settings() or {}
            data = self.client.get_channels_and_mixes(force=True)
            mixes = data.get("mixes", [])
            devices = self.client.get_output_devices()

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

            # 2. Populate Output Devices
            self.devices_list = [("none", "None / Default")]
            if devices:
                for d in devices:
                    dev_id = d.get("name")
                    display_name = d.get("display_name", dev_id)
                    if dev_id:
                        self.devices_list.append((dev_id, display_name))

            self.device_model = Gtk.StringList()
            for _, display_name in self.devices_list:
                self.device_model.append(display_name)
            self.device_selector.set_model(self.device_model)

            # Resolve current device from mix
            current_target = settings.get("target_device", "none")
            for m in mixes:
                if m["id"] == current_mix:
                    current_target = m.get("target_device", current_target)
                    break

            dev_idx = 0
            for idx, (did, _) in enumerate(self.devices_list):
                if did == current_target:
                    dev_idx = idx
                    break
            self.device_selector.set_selected(dev_idx)
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
            
            # Safely sync target device selection without rebuilding models
            if hasattr(self, "device_selector") and hasattr(self, "devices_list"):
                data = self.client.get_channels_and_mixes()
                mixes = data.get("mixes", [])
                current_target = "none"
                for m in mixes:
                    if m["id"] == m_id:
                        current_target = m.get("target_device", "none")
                        break
                dev_idx = 0
                for idx, (did, _) in enumerate(self.devices_list):
                    if did == current_target:
                        dev_idx = idx
                        break
                self._updating_dropdowns = True
                try:
                    self.device_selector.set_selected(dev_idx)
                finally:
                    self._updating_dropdowns = False

            self.update_ui_rendering(force=True)

    def _on_device_selected(self, combo, *args):
        if self._updating_dropdowns:
            return
        selected_idx = combo.get_selected()
        if 0 <= selected_idx < len(self.devices_list):
            dev_id, _ = self.devices_list[selected_idx]
            settings = self.get_settings() or {}
            settings["target_device"] = dev_id
            self.set_settings(settings)
            m_id = self.get_configured_mix_id()
            self.client.set_mix_target_device(m_id, dev_id)
            self._cached_midground = None
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

        # 2. Output Device Selector (Headphones, Speakers, etc.)
        self.device_model = Gtk.StringList()
        self.device_selector = Adw.ComboRow(
            model=self.device_model,
            title="Physical Output Device"
        )
        self.device_selector.connect("notify::selected", self._on_device_selected)
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
            self.device_selector,
            self.step_selector,
            self.vol_format_selector,
            self.live_meter_row
        ]
