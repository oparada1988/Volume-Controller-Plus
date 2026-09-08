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
        if time.time() - getattr(self, "_last_volume_adjust_time", 0.0) < 0.40:
            return
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
        mix_type = m.get("type", "sink").lower()

        if mix_type in ("source", "input", "mic") or any(k in m_id.lower() for k in ("source", "chat", "record", "mic", "input")):
            dev_display = "Input"
        else:
            dev_display = "Output"

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
        data = self.client.get_channels_and_mixes()
        mixes = data.get("mixes", [])
        m = self._match_mix(m_id, mixes)
        if m.get("type", "sink").lower() not in ("sink", "output"):
            return
        new_dev = self.client.cycle_mix_target_device(m_id)
        settings = self.get_settings() or {}
        settings["target_device"] = new_dev
        self.set_settings(settings)
        self._cached_midground = None
        self.update_ui_rendering(force=True)

    def _raw_event_callback(self, event: InputEvent, data: dict = None):
        ev_str = str(event)
        if event == Input.Dial.Events.TURN_CW or ev_str == "Dial Turn CW":
            self.handle_volume_change(self.get_step_size())
        elif event == Input.Dial.Events.TURN_CCW or ev_str == "Dial Turn CCW":
            self.handle_volume_change(-self.get_step_size())
        elif event in (Input.Dial.Events.DOWN, Input.Dial.Events.SHORT_TOUCH_PRESS) or ev_str in ("Dial Down", "Dial Touchscreen Short Press"):
            self.handle_mute_toggle()
        elif event == Input.Dial.Events.LONG_TOUCH_PRESS or ev_str == "Dial Touchscreen Long Press":
            self.handle_cycle_device()
        else:
            super()._raw_event_callback(event, data)

    def event_callback(self, event: InputEvent, data: dict = None):
        ev_str = str(event)
        if event == Input.Dial.Events.TURN_CW or ev_str == "Dial Turn CW":
            step_val = self.get_step_size()
            self.handle_volume_change(step_val)
        elif event == Input.Dial.Events.TURN_CCW or ev_str == "Dial Turn CCW":
            step_val = self.get_step_size()
            self.handle_volume_change(-step_val)
        elif event in (Input.Dial.Events.DOWN, Input.Dial.Events.SHORT_TOUCH_PRESS) or ev_str in ("Dial Down", "Dial Touchscreen Short Press"):
            self.handle_mute_toggle()
        elif event == Input.Dial.Events.LONG_TOUCH_PRESS or ev_str == "Dial Touchscreen Long Press":
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
        return self._extract_peak_value(val)

    def get_current_peaks_stereo(self) -> tuple:
        m_id = self.get_configured_mix_id()
        peaks = self.client.get_peaks()
        val = peaks.get(f"{m_id}_mix", peaks.get(m_id, peaks.get("master")))
        return self._extract_stereo_peak_values(val)

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
            self._sync_device_visibility(current_mix, mixes)
            self.set_settings(settings)
        finally:
            self._updating_dropdowns = False

    def _sync_device_visibility(self, mix_id: str, mixes: list):
        """Physical Output Device row should only appear when the mix selected is a sink/output, not a source/input/microphone."""
        if not hasattr(self, "device_selector"):
            return
        m = self._match_mix(mix_id, mixes) if mixes else {}
        m_type = str(m.get("type", "") if m else "").lower()
        if m_type:
            is_sink = (m_type in ("sink", "output"))
        else:
            mid_low = str(mix_id or "").lower()
            is_sink = not any(k in mid_low for k in ("source", "chat", "mic", "input"))
        self.device_selector.set_visible(is_sink)

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
            
            data = self.client.get_channels_and_mixes()
            mixes = data.get("mixes", [])
            self._sync_device_visibility(m_id, mixes)

            # Safely sync target device selection without rebuilding models
            if hasattr(self, "device_selector") and hasattr(self, "devices_list"):
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

        return [self.mix_selector, self.device_selector] + self.get_base_config_rows()
