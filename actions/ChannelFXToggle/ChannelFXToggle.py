import os
import time
import threading
from typing import Optional
from PIL import Image, ImageDraw, ImageFont

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent

from ..WaveControllerClient import WaveControllerClient


class ChannelFXToggle(ActionBase):
    """
    Channel FX Toggle Action for StreamController.
    Enables/disables (bypasses) real-time audio effects and DSP processing
    for a selected microphone or input channel with dynamic dual-state visuals.
    """
    action_description = "Toggle microphone audio effects (FX Rack / VST plugins) on or off."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = WaveControllerClient.get_instance()
        self.is_fx_enabled = True
        self.channels_list = []
        self._updating_channel_dropdown = False
        self._poll_timer_id = 0
        self._last_state_check = 0.0
        self._cached_font_bold = None
        self._cached_font_regular = None
        self._cached_font_small = None

    def on_ready(self) -> None:
        try:
            state = self.get_state()
            if state is not None:
                apm = getattr(state, "action_permission_manager", None)
                own_index = self.get_own_action_index()
                if apm and own_index is not None and own_index != -1:
                    if apm.get_image_control_index() is None or not self.get_is_multi_action():
                        if apm.get_image_control_index() != own_index:
                            apm.set_image_control_index(own_index, reload_pages=False, reload_self=False)
        except Exception:
            pass

        self.initial_load_status()
        self._start_poll_timer()

    def on_destroy(self) -> None:
        self._stop_poll_timer()

    def on_remove(self) -> None:
        self._stop_poll_timer()

    def _start_poll_timer(self):
        self._stop_poll_timer()
        self._poll_timer_id = GLib.timeout_add(1000, self._periodic_sync)

    def _stop_poll_timer(self):
        if self._poll_timer_id:
            try:
                GLib.source_remove(self._poll_timer_id)
            except Exception:
                pass
            self._poll_timer_id = 0

    def _periodic_sync(self) -> bool:
        now = time.time()
        if now - self._last_state_check > 0.8:
            self._last_state_check = now
            ch_id = self.get_configured_channel_id()
            live_status = self.client.get_fx_status(ch_id)
            if live_status != self.is_fx_enabled:
                self.is_fx_enabled = live_status
                self.render_key_image()
        return True

    def initial_load_status(self):
        ch_id = self.get_configured_channel_id()
        self.is_fx_enabled = self.client.get_fx_status(ch_id)
        self.render_key_image()

    def get_configured_channel_id(self) -> str:
        settings = self.get_settings() or {}
        ch = settings.get("channel_id")
        if not ch:
            data = self.client.get_channels_and_mixes()
            channels = data.get("channels", [])
            for c in channels:
                if c.get("type") == "source" or c.get("id") in ("mic", "microphone"):
                    ch = c["id"]
                    break
            if not ch and channels:
                ch = channels[0]["id"]
            if not ch:
                ch = "mic"
        return ch

    def get_display_title(self) -> str:
        settings = self.get_settings() or {}
        custom_label = settings.get("custom_title", "").strip()
        if custom_label:
            return custom_label
        
        ch_name = settings.get("channel_name", "")
        if ch_name:
            clean = ch_name[len("Elgato "):] if ch_name.startswith("Elgato ") else ch_name
            return clean

        ch_id = self.get_configured_channel_id()
        if ch_id in ("mic", "microphone"):
            return "Mic FX"
        return ch_id.capitalize()

    def event_callback(self, event: InputEvent, data: dict = None):
        # Support Key down, Dial down, or Touchscreen short press safely without assuming enum members exist
        down_events = [Input.Key.Events.DOWN]
        if hasattr(Input, "Dial") and hasattr(Input.Dial, "Events"):
            if hasattr(Input.Dial.Events, "DOWN"):
                down_events.append(Input.Dial.Events.DOWN)
            if hasattr(Input.Dial.Events, "SHORT_TOUCH_PRESS"):
                down_events.append(Input.Dial.Events.SHORT_TOUCH_PRESS)
        if hasattr(Input, "Touchscreen") and hasattr(Input.Touchscreen, "Events"):
            for attr in ("SHORT_TOUCH_PRESS", "TOUCH", "TAP", "DOWN"):
                if hasattr(Input.Touchscreen.Events, attr):
                    down_events.append(getattr(Input.Touchscreen.Events, attr))

        if event in down_events:
            self.on_key_down()
        elif event == Input.Key.Events.UP or (hasattr(Input, "Dial") and hasattr(Input.Dial, "Events") and hasattr(Input.Dial.Events, "UP") and event == Input.Dial.Events.UP):
            self.on_key_up()

    def on_key_down(self) -> None:
        ch_id = self.get_configured_channel_id()
        new_val = self.client.toggle_fx(ch_id)
        self.is_fx_enabled = new_val
        self.render_key_image()

    def on_key_up(self) -> None:
        pass

    def _get_font(self, size: int, bold: bool = False):
        try:
            font_filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
            font_path = os.path.join(self.plugin_base.PATH, "assets", "fonts", font_filename)
            if os.path.exists(font_path):
                return ImageFont.truetype(font_path, size)
        except Exception:
            pass
        try:
            return ImageFont.load_default()
        except Exception:
            return None

    def render_key_image(self):
        """Renders dynamic high-contrast 144x144 dual-state FX key graphic matching WaveController badge."""
        size = 144
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 1. Outer Studio card background
        is_connected = self.client.is_connected()
        bg_color = (18, 18, 22, 255)
        border_color = (36, 36, 44, 255)
        draw.rounded_rectangle([(4, 4), (size - 4, size - 4)], radius=18, fill=bg_color, outline=border_color, width=2)

        # 2. Top Title (Channel / Mic name)
        title_text = self.get_display_title()
        font_title = self._get_font(13, bold=True)
        if font_title:
            bbox = font_title.getbbox(title_text)
            tw = bbox[2] - bbox[0]
            tx = (size - tw) // 2
            draw.text((tx, 14), title_text, fill=(225, 228, 235, 255), font=font_title)

        # 3. Center Graphic: WaveController FX Badge Plate
        cx, cy = size // 2, size // 2 + 1
        badge_w, badge_h = 66, 42
        bx1 = cx - badge_w // 2
        by1 = cy - badge_h // 2
        bx2 = cx + badge_w // 2
        by2 = cy + badge_h // 2

        font_badge = self._get_font(26, bold=True)

        if not is_connected:
            # Offline state
            b_fill = (26, 26, 30, 255)
            b_border = (50, 50, 60, 255)
            text_color = (100, 100, 110, 255)
            draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=7, fill=b_fill, outline=b_border, width=1)
        elif self.is_fx_enabled:
            # Subtle accent violet glow behind badge
            glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            gdraw = ImageDraw.Draw(glow)
            gdraw.rounded_rectangle([(bx1 - 3, by1 - 3), (bx2 + 3, by2 + 3)], radius=10, fill=(145, 70, 255, 38))
            img = Image.alpha_composite(img, glow)
            draw = ImageDraw.Draw(img)

            # Active FX badge: WaveController purple accent
            b_fill = (52, 28, 85, 255)
            b_border = (169, 112, 255, 255)
            text_color = (212, 185, 255, 255)
            draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=7, fill=b_fill, outline=b_border, width=2)
        else:
            # Bypassed FX badge: dimmed plate matching WaveController dimmed-fx button
            b_fill = (34, 34, 40, 255)
            b_border = (60, 60, 70, 255)
            text_color = (130, 130, 142, 255)
            draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=7, fill=b_fill, outline=b_border, width=1)

        if font_badge:
            t_bbox = font_badge.getbbox("FX")
            tw = t_bbox[2] - t_bbox[0]
            th = t_bbox[3] - t_bbox[1]
            draw.text((cx - tw // 2, cy - th // 2 - 3), "FX", fill=text_color, font=font_badge)

        # 4. Bottom Status Pill
        font_pill = self._get_font(11, bold=True)
        if not is_connected:
            pill_text = "OFFLINE"
            p_bg = (45, 30, 15, 255)
            p_border = (255, 149, 0, 255)
            p_text_col = (255, 175, 50, 255)
        elif self.is_fx_enabled:
            pill_text = "FX ON"
            p_bg = (40, 20, 68, 255)
            p_border = (155, 95, 245, 255)
            p_text_col = (215, 185, 255, 255)
        else:
            pill_text = "BYPASS"
            p_bg = (28, 28, 33, 255)
            p_border = (65, 65, 75, 255)
            p_text_col = (140, 140, 150, 255)

        if font_pill:
            p_bbox = font_pill.getbbox(pill_text)
            pw = p_bbox[2] - p_bbox[0] + 18
            ph = 20
            px1 = (size - pw) // 2
            py1 = size - 31
            draw.rounded_rectangle([(px1, py1), (px1 + pw, py1 + ph)], radius=10, fill=p_bg, outline=p_border, width=1)
            draw.text((px1 + 9, py1 + 3), pill_text, fill=p_text_col, font=font_pill)

        # Deliver to deck via StreamController
        try:
            self.set_media(image=img)
        except Exception:
            try:
                GLib.idle_add(self.set_media, img)
            except Exception:
                pass

    def update_channel_dropdown(self):
        if not hasattr(self, "channel_selector"):
            return
        self._updating_channel_dropdown = True
        try:
            settings = self.get_settings() or {}
            data = self.client.get_channels_and_mixes(force=True)
            channels = data.get("channels", [])

            self.channels_list = []
            source_channels = []
            other_channels = []

            if channels:
                for c in channels:
                    c_id = c.get("id", "")
                    name = c.get("name", c_id.capitalize())
                    clean_name = name[len("Elgato "):] if name.startswith("Elgato ") else name
                    
                    is_source = (c.get("type") == "source" or c_id in ("mic", "microphone"))
                    if is_source:
                        source_channels.append((c_id, f"🎙 {clean_name}"))
                    else:
                        other_channels.append((c_id, clean_name))

                # Prioritize microphone/source channels at top
                self.channels_list = source_channels + other_channels
            else:
                self.channels_list = [("mic", "🎙 Microphone")]

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
            settings["channel_name"] = ch_name.replace("🎙 ", "")
            self.set_settings(settings)
            self.initial_load_status()

    def _on_custom_title_changed(self, entry):
        settings = self.get_settings() or {}
        settings["custom_title"] = entry.get_text()
        self.set_settings(settings)
        self.render_key_image()

    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        # 1. Microphone / Audio Channel Selector
        self.channel_model = Gtk.StringList()
        self.channel_selector = Adw.ComboRow(
            model=self.channel_model,
            title="Target Microphone / Channel"
        )
        self.channel_selector.connect("notify::selected", self._on_channel_selected)
        self.update_channel_dropdown()

        # 2. Custom Title Override Entry
        settings = self.get_settings() or {}
        title_entry = Adw.EntryRow(
            title="Custom Button Title (Optional)"
        )
        title_entry.set_text(settings.get("custom_title", ""))
        title_entry.connect("changed", self._on_custom_title_changed)

        return [self.channel_selector, title_entry]
