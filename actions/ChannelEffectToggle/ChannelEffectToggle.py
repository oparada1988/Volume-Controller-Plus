import os
import re
import time
import math
import threading
from typing import Optional, List, Tuple
from PIL import Image, ImageDraw, ImageFont

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk

from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent

from ..WaveControllerClient import WaveControllerClient


class ChannelEffectToggle(ActionBase):
    """
    Channel Effect Enable/Disable Action for StreamController.
    Allows creators to independently toggle a specific microphone DSP processor
    (Noise Gate, AI Denoise, Equalizer, Compressor, De-Esser, Limiter, High-Pass, or Reverb)
    with studio-accurate dual-state glowing visuals and bidirectional live synchronization.
    """
    action_description = "Toggle a specific microphone audio effect (Noise Gate, AI Denoise, EQ, Compressor, Reverb) on or off."

    EFFECT_METADATA = {
        "dsp_highpass": ("High-Pass", "HPF", "Filter"),
        "dsp_noise_suppression": ("AI Denoise", "AI", "Noise Reduction"),
        "dsp_noise_gate": ("Noise Gate", "GATE", "Dynamics"),
        "dsp_equalizer": ("Vocal EQ", "EQ", "Equalizer"),
        "dsp_compressor": ("Compressor", "COMP", "Dynamics"),
        "dsp_deesser": ("De-Esser", "DE-ESS", "Equalizer"),
        "dsp_limiter": ("Peak Limiter", "LIMIT", "Dynamics"),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = WaveControllerClient.get_instance()
        self.is_effect_enabled = True
        self.channels_list: List[Tuple[str, str]] = []
        self.effects_list: List[Tuple[str, str, str, bool]] = []
        self._updating_dropdowns = False
        self._poll_timer_id = 0
        self._last_state_check = 0.0

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
        self._poll_timer_id = GLib.timeout_add(800, self._periodic_sync)

    def _stop_poll_timer(self):
        if self._poll_timer_id:
            try:
                GLib.source_remove(self._poll_timer_id)
            except Exception:
                pass
            self._poll_timer_id = 0

    def _periodic_sync(self) -> bool:
        now = time.time()
        if now - self._last_state_check > 0.7:
            self._last_state_check = now
            ch_id = self.get_configured_channel_id()
            fx_id = self.get_configured_effect_id()
            live_status = self.client.get_effect_status(ch_id, fx_id)
            if live_status != self.is_effect_enabled:
                self.is_effect_enabled = live_status
                self.render_key_image()
        return True

    def initial_load_status(self):
        ch_id = self.get_configured_channel_id()
        fx_id = self.get_configured_effect_id()
        self.is_effect_enabled = self.client.get_effect_status(ch_id, fx_id)
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

    DEFAULT_GLOW_COLOR = "#9146FF"

    def get_configured_effect_id(self) -> str:
        settings = self.get_settings() or {}
        return settings.get("effect_id", "dsp_noise_suppression")

    def get_configured_style(self) -> str:
        settings = self.get_settings() or {}
        return settings.get("badge_style", "badge")  # "badge" or "hybrid"

    def get_configured_glow_color(self) -> str:
        settings = self.get_settings() or {}
        return settings.get("glow_color", self.DEFAULT_GLOW_COLOR)

    def _set_color_button_rgba(self, button: Gtk.ColorButton, hex_str: str):
        try:
            rgba = Gdk.RGBA()
            rgba.parse(hex_str)
            button.set_rgba(rgba)
        except Exception:
            pass

    def _on_glow_color_set(self, btn):
        rgba = btn.get_rgba()
        r = int(rgba.red * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue * 255)
        hex_color = f"#{r:02X}{g:02X}{b:02X}"
        settings = self.get_settings() or {}
        settings["glow_color"] = hex_color
        self.set_settings(settings)
        self.render_key_image()

    def _on_reset_glow_color(self, _btn):
        settings = self.get_settings() or {}
        settings["glow_color"] = self.DEFAULT_GLOW_COLOR
        self.set_settings(settings)
        if hasattr(self, "glow_color_btn") and self.glow_color_btn:
            self._set_color_button_rgba(self.glow_color_btn, self.DEFAULT_GLOW_COLOR)
        self.render_key_image()

    @staticmethod
    def smart_shorten_name(name: str, fx_id: str = "") -> str:
        s = name.strip()
        s_lower = s.lower()
        id_lower = fx_id.lower()

        # 1. High priority URI / ID keywords matching known plugin suites
        if "dragonfly" in id_lower or "dragonfly" in s_lower:
            if "hall" in id_lower or "hall" in s_lower:
                return "Hall Reverb"
            if "plate" in id_lower or "plate" in s_lower:
                return "Plate Reverb"
            if "room" in id_lower or "room" in s_lower:
                return "Room Reverb"
            if "early" in id_lower or "early" in s_lower:
                return "Early Reflect"
            return "Reverb"

        if "rnnoise" in id_lower or "rnnoise" in s_lower:
            return "AI Denoise"

        # 2. If name is a URI or path, extract the meaningful tail
        if "http" in s_lower or "github" in s_lower or "urn:" in s_lower or "/" in s:
            s = s.split("/")[-1].split(":")[-1].replace("_", " ").replace("-", " ")
            s = re.sub(r"^(lv2|vst3|ladspa)\s+", "", s, flags=re.I).strip()

        # 3. Strip trailing categories in parentheses like (Reverb & Delay), (Equalizer)
        s = re.sub(r"\s*\([^)]*\)", "", s).strip()

        # 4. Strip manufacturer / suite prefixes if followed by actual name
        prefixes = [
            "Dragonfly ", "LSP ", "Calf ", "MDA ", "TAP ", "Steve Harris ", "x42 ", "Bespoke ",
            "Broadcast ", "Parametric "
        ]
        for p in prefixes:
            if s.lower().startswith(p.lower()) and len(s) > len(p):
                remainder = s[len(p):].strip()
                if len(remainder) >= 3:
                    s = remainder
                    break

        return s

    def get_effect_info(self, effect_id: str) -> Tuple[str, str]:
        """Returns (smart_short_title, badge_code) for an effect ID."""
        if effect_id in self.EFFECT_METADATA:
            title, code, _ = self.EFFECT_METADATA[effect_id]
            return title, code

        # Check if friendly name is cached in settings or available effects
        settings = self.get_settings() or {}
        raw_name = settings.get("effect_name", "")
        if not raw_name and self.effects_list:
            for item_id, label, _, _ in self.effects_list:
                if item_id == effect_id:
                    raw_name = label
                    break

        if not raw_name:
            try:
                ch = self.get_configured_channel_id()
                effs = self.client.get_available_effects(ch)
                for item_id, label, _, _ in effs:
                    if item_id == effect_id:
                        raw_name = label
                        break
            except Exception:
                pass

        if not raw_name:
            raw_name = effect_id

        clean_name = self.smart_shorten_name(raw_name, effect_id)

        # Derive 3-4 char badge code
        lower_ref = f"{effect_id} {raw_name} {clean_name}".lower()
        if "reverb" in lower_ref or "hall" in lower_ref or "plate" in lower_ref or "room" in lower_ref:
            code = "REV"
        elif "delay" in lower_ref or "echo" in lower_ref:
            code = "DLY"
        elif "gate" in lower_ref:
            code = "GATE"
        elif "denoise" in lower_ref or "rnnoise" in lower_ref or "suppress" in lower_ref:
            code = "AI"
        elif "equal" in lower_ref or "eq" in lower_ref:
            code = "EQ"
        elif "comp" in lower_ref:
            code = "COMP"
        elif "limit" in lower_ref:
            code = "LIMIT"
        elif "deess" in lower_ref:
            code = "DE-ESS"
        else:
            words = clean_name.split()
            if len(words) >= 2:
                code = "".join(w[0] for w in words[:3]).upper()
            else:
                code = clean_name[:4].upper()

        return clean_name, code

    def get_display_title(self) -> str:
        settings = self.get_settings() or {}
        custom_label = settings.get("custom_title", "").strip()
        if custom_label:
            return custom_label

        fx_id = self.get_configured_effect_id()
        title, _ = self.get_effect_info(fx_id)
        return title

    def event_callback(self, event: InputEvent, data: dict = None):
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
        fx_id = self.get_configured_effect_id()
        new_val = self.client.toggle_channel_effect(ch_id, fx_id)
        self.is_effect_enabled = new_val
        self.render_key_image()

    def on_key_up(self) -> None:
        pass

    def _get_font(self, size: int, bold: bool = False, scale: int = 1):
        try:
            font_filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
            font_path = os.path.join(self.plugin_base.PATH, "assets", "fonts", font_filename)
            if os.path.exists(font_path):
                return ImageFont.truetype(font_path, size * scale)
        except Exception:
            pass
        try:
            return ImageFont.load_default()
        except Exception:
            return None

    def _draw_dsp_glyph(self, draw: ImageDraw.Draw, fx_key: str, cx: int, cy: int, active: bool, scale: int, accent_rgb: tuple = (145, 70, 255)):
        gr, gg, gb = accent_rgb
        col = (min(255, int(gr * 0.45 + 140)), min(255, int(gg * 0.45 + 140)), min(255, int(gb * 0.45 + 140)), 255) if active else (135, 135, 148, 240)
        dim_col = (gr, gg, gb, 130) if active else (75, 75, 85, 130)
        w = 2 * scale

        if fx_key == "dsp_highpass":
            pts = [
                (cx - 16 * scale, cy + 6 * scale),
                (cx - 7 * scale, cy + 6 * scale),
                (cx - 1 * scale, cy + 2 * scale),
                (cx + 6 * scale, cy - 6 * scale),
                (cx + 16 * scale, cy - 6 * scale)
            ]
            draw.line(pts, fill=col, width=w, joint="curve")
            draw.ellipse([(cx + 4 * scale, cy - 8 * scale), (cx + 8 * scale, cy - 4 * scale)], fill=col)

        elif fx_key == "dsp_noise_suppression":
            pts = []
            for i in range(-15 * scale, 16 * scale):
                x = cx + i
                y = cy + int(math.sin(i * 0.32 / scale) * 4 * scale)
                pts.append((x, y))
            draw.line(pts, fill=col, width=w)
            draw.ellipse([(cx - 9 * scale, cy - 7 * scale), (cx - 6 * scale, cy - 4 * scale)], fill=col)
            draw.ellipse([(cx + 8 * scale, cy + 4 * scale), (cx + 11 * scale, cy + 7 * scale)], fill=col)
            draw.ellipse([(cx + 10 * scale, cy - 6 * scale), (cx + 13 * scale, cy - 3 * scale)], fill=col)

        elif fx_key == "dsp_noise_gate":
            pts = [
                (cx - 16 * scale, cy + 6 * scale),
                (cx - 2 * scale, cy + 6 * scale),
                (cx - 2 * scale, cy - 6 * scale),
                (cx + 16 * scale, cy - 6 * scale)
            ]
            draw.line(pts, fill=col, width=w)
            draw.line([(cx - 16 * scale, cy), (cx + 16 * scale, cy)], fill=dim_col, width=1 * scale)

        elif fx_key == "dsp_equalizer":
            pts = []
            for i in range(-16 * scale, 17 * scale):
                x = cx + i
                norm_i = i / float(scale)
                y_mid = math.exp(-((norm_i / 4.8) ** 2)) * 7 * scale
                y_shelf = (norm_i / 16.0) * 2 * scale
                y = cy - int(y_mid + y_shelf) + 2 * scale
                pts.append((x, y))
            draw.line(pts, fill=col, width=w)
            draw.ellipse([(cx - 2 * scale, cy - 7 * scale), (cx + 2 * scale, cy - 3 * scale)], fill=col)

        elif fx_key == "dsp_compressor":
            pts = [
                (cx - 15 * scale, cy + 6 * scale),
                (cx - 1 * scale, cy - 1 * scale),
                (cx + 15 * scale, cy - 5 * scale)
            ]
            draw.line(pts, fill=col, width=w)
            draw.line([(cx - 1 * scale, cy - 1 * scale), (cx + 10 * scale, cy - 10 * scale)], fill=dim_col, width=1 * scale)
            draw.ellipse([(cx - 3 * scale, cy - 3 * scale), (cx + 1 * scale, cy + 1 * scale)], fill=col)

        elif fx_key == "dsp_deesser":
            pts = []
            for i in range(-16 * scale, 17 * scale):
                x = cx + i
                norm_i = i / float(scale)
                amp = 2.5 * scale if norm_i > 0 else 5.5 * scale
                y = cy - int(math.sin(norm_i * 0.42) * amp)
                pts.append((x, y))
            draw.line(pts, fill=col, width=w)
            draw.line([(cx, cy - 4 * scale), (cx + 15 * scale, cy - 4 * scale)], fill=dim_col, width=1 * scale)

        elif fx_key == "dsp_limiter":
            draw.line([(cx - 16 * scale, cy - 6 * scale), (cx + 16 * scale, cy - 6 * scale)], fill=col, width=w + 1 * scale)
            pts = [
                (cx - 15 * scale, cy + 6 * scale),
                (cx - 7 * scale, cy - 6 * scale),
                (cx + 7 * scale, cy - 6 * scale),
                (cx + 15 * scale, cy + 6 * scale)
            ]
            draw.line(pts, fill=col, width=w)

        else:
            # Reverb / spatial concentric arcs and focal sound point
            for r in [4, 8, 12]:
                bbox = [(cx - r * scale, cy - (r - 2) * scale), (cx + r * scale, cy + (r + 2) * scale)]
                draw.arc(bbox, start=215, end=325, fill=col, width=w)
            draw.ellipse([(cx - 2 * scale, cy + 3 * scale), (cx + 2 * scale, cy + 7 * scale)], fill=col)

    def render_key_image(self):
        """Renders 144x144 anti-aliased dual-state key graphic with authentic studio violet glow."""
        scale = 2  # 2x supersampling for high-performance crisp rendering
        base_size = 144
        size = base_size * scale
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        is_connected = self.client.is_connected()
        bg_color = (18, 18, 22, 255)
        border_color = (36, 36, 44, 255)
        pad = 4 * scale
        draw.rounded_rectangle([(pad, pad), (size - pad, size - pad)], radius=18 * scale, fill=bg_color, outline=border_color, width=2 * scale)

        # 2. Top Title: Smart-shortened Effect Name with width auto-scaling
        title_text = self.get_display_title()
        max_title_w = size - 26 * scale
        chosen_font = None
        for pt in [12, 11, 10, 9]:
            f = self._get_font(pt, bold=True, scale=scale)
            if f:
                bbox = f.getbbox(title_text)
                if (bbox[2] - bbox[0]) <= max_title_w:
                    chosen_font = f
                    break
        if not chosen_font:
            chosen_font = self._get_font(9, bold=True, scale=scale)
            if chosen_font:
                while len(title_text) > 3:
                    bbox = chosen_font.getbbox(title_text + "…")
                    if (bbox[2] - bbox[0]) <= max_title_w:
                        title_text = title_text + "…"
                        break
                    title_text = title_text[:-1]

        if chosen_font:
            t_bbox = chosen_font.getbbox(title_text)
            tw = t_bbox[2] - t_bbox[0]
            draw.text(((size - tw) // 2, 13 * scale), title_text, fill=(225, 228, 235, 255), font=chosen_font)

        # 3. Center Badge Plate
        cx, cy = size // 2, (size // 2) + (1 * scale)
        badge_w, badge_h = 70 * scale, 44 * scale
        bx1 = cx - badge_w // 2
        by1 = cy - badge_h // 2
        bx2 = cx + badge_w // 2
        by2 = cy + badge_h // 2

        fx_id = self.get_configured_effect_id()
        _, badge_code = self.get_effect_info(fx_id)
        style = self.get_configured_style()

        glow_hex = self.get_configured_glow_color()
        try:
            clean_hex = glow_hex.lstrip("#")
            if len(clean_hex) == 6:
                gr, gg, gb = int(clean_hex[0:2], 16), int(clean_hex[2:4], 16), int(clean_hex[4:6], 16)
            else:
                gr, gg, gb = 145, 70, 255
        except Exception:
            gr, gg, gb = 145, 70, 255

        if not is_connected:
            b_fill = (26, 26, 30, 255)
            b_border = (50, 50, 60, 255)
            text_color = (100, 100, 110, 255)
            draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=7 * scale, fill=b_fill, outline=b_border, width=1 * scale)
        elif self.is_effect_enabled:
            # Multi-layer neon glow in custom accent
            glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            gdraw = ImageDraw.Draw(glow)
            gdraw.rounded_rectangle([(bx1 - 6 * scale, by1 - 6 * scale), (bx2 + 6 * scale, by2 + 6 * scale)], radius=13 * scale, fill=(gr, gg, gb, 30))
            gdraw.rounded_rectangle([(bx1 - 3 * scale, by1 - 3 * scale), (bx2 + 3 * scale, by2 + 3 * scale)], radius=10 * scale, fill=(gr, gg, gb, 60))
            img = Image.alpha_composite(img, glow)
            draw = ImageDraw.Draw(img)

            b_fill = (max(16, int(gr * 0.35)), max(16, int(gg * 0.35)), max(18, int(gb * 0.35)), 255)
            b_border = (min(255, int(gr * 1.15)), min(255, int(gg * 1.15)), min(255, int(gb * 1.15)), 255)
            text_color = (min(255, int(gr * 0.45 + 140)), min(255, int(gg * 0.45 + 140)), min(255, int(gb * 0.45 + 140)), 255)
            draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=7 * scale, fill=b_fill, outline=b_border, width=2 * scale)
        else:
            b_fill = (34, 34, 40, 255)
            b_border = (60, 60, 70, 255)
            text_color = (130, 130, 142, 255)
            draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=7 * scale, fill=b_fill, outline=b_border, width=1 * scale)

        # Center Content (Badge vs Technical Glyph)
        if style == "hybrid":
            self._draw_dsp_glyph(draw, fx_id, cx, cy - 7 * scale, self.is_effect_enabled, scale, accent_rgb=(gr, gg, gb))
            font_sub = self._get_font(10, bold=True, scale=scale)
            if font_sub:
                b_bbox = font_sub.getbbox(badge_code)
                bw = b_bbox[2] - b_bbox[0]
                draw.text((cx - bw // 2, cy + 6 * scale), badge_code, fill=text_color, font=font_sub)
        else:
            font_size = 21 if len(badge_code) <= 3 else (17 if len(badge_code) <= 4 else 14)
            font_badge = self._get_font(font_size, bold=True, scale=scale)
            if font_badge:
                b_bbox = font_badge.getbbox(badge_code)
                bw = b_bbox[2] - b_bbox[0]
                bh = b_bbox[3] - b_bbox[1]
                draw.text((cx - bw // 2, cy - bh // 2 - 2 * scale), badge_code, fill=text_color, font=font_badge)

        # 4. Bottom Status Pill
        font_pill = self._get_font(11, bold=True, scale=scale)
        if not is_connected:
            pill_text = "OFFLINE"
            p_bg = (45, 30, 15, 255)
            p_border = (255, 149, 0, 255)
            p_text_col = (255, 175, 50, 255)
        elif self.is_effect_enabled:
            pill_text = "ON"
            p_bg = (max(16, int(gr * 0.28)), max(16, int(gg * 0.28)), max(18, int(gb * 0.28)), 255)
            p_border = (min(255, int(gr * 1.15)), min(255, int(gg * 1.15)), min(255, int(gb * 1.15)), 255)
            p_text_col = (min(255, int(gr * 0.45 + 140)), min(255, int(gg * 0.45 + 140)), min(255, int(gb * 0.45 + 140)), 255)
        else:
            pill_text = "BYPASS"
            p_bg = (28, 28, 33, 255)
            p_border = (65, 65, 75, 255)
            p_text_col = (140, 140, 150, 255)

        if font_pill:
            p_bbox = font_pill.getbbox(pill_text)
            pw = p_bbox[2] - p_bbox[0] + 18 * scale
            ph = 20 * scale
            px1 = (size - pw) // 2
            py1 = size - 31 * scale
            draw.rounded_rectangle([(px1, py1), (px1 + pw, py1 + ph)], radius=10 * scale, fill=p_bg, outline=p_border, width=1 * scale)
            draw.text((px1 + 9 * scale, py1 + 3 * scale), pill_text, fill=p_text_col, font=font_pill)

        final_img = img.resize((base_size, base_size), Image.Resampling.LANCZOS)
        try:
            self.set_media(image=final_img)
        except Exception:
            try:
                GLib.idle_add(self.set_media, final_img)
            except Exception:
                pass

    def update_dropdowns(self):
        if not hasattr(self, "channel_selector") or not hasattr(self, "effect_selector"):
            return
        self._updating_dropdowns = True
        try:
            settings = self.get_settings() or {}
            
            # 1. Update Channels
            data = self.client.get_channels_and_mixes(force=True)
            channels = data.get("channels", [])
            self.channels_list = []
            source_channels = []
            other_channels = []

            if channels:
                for c in channels:
                    c_id = c.get("id", "")
                    name = c.get("name", c_id.capitalize())
                    clean = name[len("Elgato "):] if name.startswith("Elgato ") else name
                    is_source = (c.get("type") == "source" or c_id in ("mic", "microphone") or any(k in c_id.lower() for k in ("mic", "wave_xlr", "input", "capture")))
                    if is_source:
                        source_channels.append((c_id, clean))
                self.channels_list = source_channels if source_channels else [("mic", "Microphone")]
            else:
                self.channels_list = [("mic", "Microphone")]

            self.channel_model = Gtk.StringList()
            for _, display_name in self.channels_list:
                self.channel_model.append(display_name)
            self.channel_selector.set_model(self.channel_model)

            cur_ch = settings.get("channel_id")
            ch_idx = 0
            if cur_ch:
                for i, (cid, _) in enumerate(self.channels_list):
                    if cid == cur_ch:
                        ch_idx = i
                        break
            elif self.channels_list:
                settings["channel_id"] = self.channels_list[0][0]
                self.set_settings(settings)
            self.channel_selector.set_selected(ch_idx)

            # 2. Update Effects for selected channel
            selected_ch = settings.get("channel_id", "mic")
            self.effects_list = self.client.get_available_effects(selected_ch)
            self.effect_model = Gtk.StringList()
            for _, label, _, _ in self.effects_list:
                self.effect_model.append(label)
            self.effect_selector.set_model(self.effect_model)

            cur_fx = settings.get("effect_id", "dsp_noise_suppression")
            fx_idx = 0
            for i, (fid, flabel, _, _) in enumerate(self.effects_list):
                if fid == cur_fx:
                    fx_idx = i
                    if not settings.get("effect_name"):
                        settings["effect_name"] = flabel
                        self.set_settings(settings)
                    break
            self.effect_selector.set_selected(fx_idx)

        finally:
            self._updating_dropdowns = False

    def _on_channel_selected(self, combo, *args):
        if self._updating_dropdowns:
            return
        idx = combo.get_selected()
        if 0 <= idx < len(self.channels_list):
            ch_id, _ = self.channels_list[idx]
            settings = self.get_settings() or {}
            settings["channel_id"] = ch_id
            self.set_settings(settings)
            # Refresh available effects for this channel
            self.update_dropdowns()
            self.initial_load_status()

    def _on_effect_selected(self, combo, *args):
        if self._updating_dropdowns:
            return
        idx = combo.get_selected()
        if 0 <= idx < len(self.effects_list):
            fx_id, flabel, _, _ = self.effects_list[idx]
            settings = self.get_settings() or {}
            settings["effect_id"] = fx_id
            settings["effect_name"] = flabel
            self.set_settings(settings)
            self.initial_load_status()

    def _on_style_selected(self, combo, *args):
        idx = combo.get_selected()
        style_key = "hybrid" if idx == 1 else "badge"
        settings = self.get_settings() or {}
        settings["badge_style"] = style_key
        self.set_settings(settings)
        self.render_key_image()

    def _on_custom_title_changed(self, entry):
        settings = self.get_settings() or {}
        settings["custom_title"] = entry.get_text()
        self.set_settings(settings)
        self.render_key_image()

    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        settings = self.get_settings() or {}

        # 1. Target Channel
        self.channel_model = Gtk.StringList()
        self.channel_selector = Adw.ComboRow(
            model=self.channel_model,
            title="Target Microphone / Channel"
        )
        self.channel_selector.connect("notify::selected", self._on_channel_selected)

        # 2. Target Processor
        self.effect_model = Gtk.StringList()
        self.effect_selector = Adw.ComboRow(
            model=self.effect_model,
            title="Target Audio Effect"
        )
        self.effect_selector.connect("notify::selected", self._on_effect_selected)

        # 3. Badge Aesthetic Style Selector
        style_model = Gtk.StringList()
        style_model.append("Studio Badge (Style A)")
        style_model.append("Technical Glyph (Style B)")
        self.style_selector = Adw.ComboRow(
            model=style_model,
            title="Key Visual Style"
        )
        current_style = settings.get("badge_style", "badge")
        self.style_selector.set_selected(1 if current_style == "hybrid" else 0)
        self.style_selector.connect("notify::selected", self._on_style_selected)

        # 4. Custom Title Override Entry
        title_entry = Adw.EntryRow(
            title="Custom Button Title (Optional)"
        )
        title_entry.set_text(settings.get("custom_title", ""))
        title_entry.connect("changed", self._on_custom_title_changed)

        # 5. Glow Accent Color
        glow_row = Adw.ActionRow(
            title="Glow Accent Color",
            subtitle="Custom color for active neon glow and badge styling"
        )
        self.glow_color_btn = Gtk.ColorButton()
        self.glow_color_btn.set_valign(Gtk.Align.CENTER)
        self._set_color_button_rgba(self.glow_color_btn, self.get_configured_glow_color())
        self.glow_color_btn.connect("color-set", self._on_glow_color_set)
        glow_row.add_suffix(self.glow_color_btn)

        reset_btn = Gtk.Button()
        reset_btn.set_icon_name("edit-undo-symbolic")
        reset_btn.set_tooltip_text("Reset to WaveController Accent (#9146FF)")
        reset_btn.set_valign(Gtk.Align.CENTER)
        reset_btn.connect("clicked", self._on_reset_glow_color)
        glow_row.add_suffix(reset_btn)

        self.update_dropdowns()
        return [self.channel_selector, self.effect_selector, self.style_selector, glow_row, title_entry]
