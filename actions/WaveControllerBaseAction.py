# Import StreamController modules
from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport

# Import python modules
import os
import io
import time
import math
import threading
from PIL import Image, ImageDraw, ImageFont, ImageChops

# Import gtk modules
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, Adw, GLib, Gio

from .WaveControllerClient import WaveControllerClient

RENDER_SCALE = 2
ADJUSTING_TIMEOUT = 2.0  # 1:1 synchronization with WaveController's 2.0s hardware LED peek

class WaveControllerBaseAction(ActionBase):
    """
    Base Action maintaining the exact intact visual widget rendering engine:
    9-tick radial knob, volume arcs, rotating pointer notch, peak hold markers,
    mute border, and dual-meter styling.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = WaveControllerClient.get_instance()
        self.running = False
        self.current_volume = 80
        self.last_mute = False
        self.bg_image = None
        self.tick_timer_id = 0
        self.last_poll_time = 0.0
        self.last_drawn_volume = -1
        self.last_drawn_mute = None
        self.last_drawn_peak = -1.0
        self.last_drawn_hold = -1.0
        self._gauge_gradient_img = None
        self._gauge_gradient_img_sub = None
        self._render_lock = threading.RLock()
        
        # Cached resources for performance
        self._cached_font_title = None
        self._cached_font_vol = None
        self._cached_font_name = None
        self._cached_font_path = None
        self._cached_icon_path = None
        self._cached_icon_img = None
        self._cached_font_file = None
        self._cached_title_font_size = 14
        self._cached_base_bg = None
        self._cached_midground = None
        self._cached_midground_key = None
        self._cached_wave_midground = None
        self._cached_wave_midground_key = None
        self._current_peak = 0.0
        self._current_peak_l = 0.0
        self._current_peak_r = 0.0
        self.last_drawn_peak_l = -1.0
        self.last_drawn_peak_r = -1.0
        self._peak_hold_val = 0.0
        self._peak_hold_time = 0.0
        self._last_volume_adjust_time = 0.0
        self._last_drawn_adjusting = False
        self.last_drawn_title = None
        self.last_drawn_subtitle = None
        self.last_drawn_icon = None
        self.last_drawn_ui_style = None

        # Pre-computed geometry constants
        self._cx = 70 * RENDER_SCALE
        self._cy = 104 * RENDER_SCALE
        self._r_outer = 44 * RENDER_SCALE
        self._r_inner = 39 * RENDER_SCALE
        self._r_arc_box = 51 * RENDER_SCALE
        self._arc_w = 7 * RENDER_SCALE
        self._r_arc_center = self._r_arc_box - self._arc_w / 2.0
        self._cap_r = self._arc_w / 2.0
        self._bbox = [(self._cx - self._r_arc_box, self._cy - self._r_arc_box), (self._cx + self._r_arc_box, self._cy + self._r_arc_box)]
        self._bbox_outer = [(self._cx - self._r_outer, self._cy - self._r_outer), (self._cx + self._r_outer, self._cy + self._r_outer)]
        self._bbox_inner = [(self._cx - self._r_inner, self._cy - self._r_inner), (self._cx + self._r_inner, self._cy + self._r_inner)]

        # Fixed 210-degree start cap coordinates
        rad_start = math.radians(210)
        self._cos_210 = math.cos(rad_start)
        self._sin_210 = math.sin(rad_start)
        self._start_cap_x = self._cx + self._r_arc_center * self._cos_210
        self._start_cap_y = self._cy + self._r_arc_center * self._sin_210

        self._gx1 = self._cx - self._r_arc_box - 8 * RENDER_SCALE
        self._gy1 = self._cy - self._r_arc_box - 8 * RENDER_SCALE
        self._gx2 = self._cx + self._r_arc_box + 8 * RENDER_SCALE
        self._gy2 = self._cy + 8 * RENDER_SCALE

        self._sub_width = self._gx2 - self._gx1
        self._sub_height = self._gy2 - self._gy1
        self._sub_cx = 8 * RENDER_SCALE + self._r_arc_box
        self._sub_cy = 8 * RENDER_SCALE + self._r_arc_box
        self._sub_bbox = [(self._sub_cx - self._r_arc_box, self._sub_cy - self._r_arc_box), (self._sub_cx + self._r_arc_box, self._sub_cy + self._r_arc_box)]
        self._sub_start_cap_x = self._sub_cx + self._r_arc_center * self._cos_210
        self._sub_start_cap_y = self._sub_cy + self._r_arc_center * self._sin_210

        self._peak_mask_sub = Image.new("L", (self._sub_width, self._sub_height), 0)
        self._peak_mask_sub_draw = ImageDraw.Draw(self._peak_mask_sub)

    def on_ready(self) -> None:
        self.running = True
        self.initial_load_status()
        self.update_ui_rendering(force=True)

        if self.tick_timer_id == 0:
            self.tick_timer_id = GLib.timeout_add(30, self.on_tick_update)

    def on_update(self) -> None:
        self.initial_load_status()
        self.update_ui_rendering(force=True)

    def on_remove(self) -> None:
        self.running = False
        if self.tick_timer_id > 0:
            try:
                GLib.source_remove(self.tick_timer_id)
            except Exception:
                pass
            self.tick_timer_id = 0

    def on_disconnect(self) -> None:
        self.on_remove()

    def on_removed_from_cache(self) -> None:
        self.on_remove()

    def initial_load_status(self):
        pass

    def get_target_title_and_subtitle(self) -> tuple:
        return "WaveController", ""

    def get_target_icon_path(self) -> str:
        return ""

    def handle_volume_change(self, delta: int):
        pass

    def handle_mute_toggle(self):
        pass

    def get_current_peak_val(self) -> float:
        return 0.0

    def get_current_peaks_stereo(self) -> tuple:
        val = self.get_current_peak_val()
        return float(val), float(val)

    def get_step_size(self) -> int:
        settings = self.get_settings() or {}
        step_str = settings.get("step_size", "5%")
        try:
            return int(step_str.replace("%", "").strip())
        except (ValueError, AttributeError):
            return 5

    def get_live_meter(self) -> bool:
        settings = self.get_settings() or {}
        return settings.get("live_meter", True)

    def get_ui_style(self) -> str:
        settings = self.get_settings() or {}
        return settings.get("ui_style", "wave").lower()

    def get_appearance(self) -> str:
        settings = self.get_settings() or {}
        return settings.get("appearance", "midnight").lower()

    def get_accent_color(self) -> str:
        settings = self.get_settings() or {}
        return settings.get("accent_color", "system").lower()

    @staticmethod
    def get_system_theme_preference() -> str:
        try:
            settings = Gio.Settings.new("org.gnome.desktop.interface")
            val = settings.get_string("color-scheme")
            if "dark" in val.lower():
                return "dark"
            elif "light" in val.lower():
                return "light"
            gtk_theme = settings.get_string("gtk-theme").lower()
            if "dark" in gtk_theme:
                return "dark"
        except Exception:
            pass
        return "dark"

    @staticmethod
    def get_system_accent_name() -> str:
        try:
            settings = Gio.Settings.new("org.gnome.desktop.interface")
            return settings.get_string("accent-color").lower()
        except Exception:
            pass
        return "purple"

    ACCENT_PALETTE = {
        "blue": (53, 132, 228),
        "purple": (145, 70, 255),
        "teal": (26, 177, 167),
        "green": (46, 194, 126),
        "yellow": (246, 211, 45),
        "orange": (255, 120, 0),
        "red": (237, 51, 59),
        "pink": (220, 138, 174),
        "slate": (154, 153, 150),
    }

    def resolve_accent_rgb(self) -> tuple:
        choice = self.get_accent_color()
        if choice == "system":
            sys_accent = self.get_system_accent_name()
            return self.ACCENT_PALETTE.get(sys_accent, (145, 70, 255))
        return self.ACCENT_PALETTE.get(choice, (145, 70, 255))

    def get_theme_palette(self) -> dict:
        appearance = self.get_appearance()
        accent_rgb = self.resolve_accent_rgb()

        is_dark = True
        if appearance == "system":
            sys_theme = self.get_system_theme_preference()
            is_dark = (sys_theme == "dark")
        elif appearance == "midnight":
            is_dark = True

        def blend_rgb(fg, bg, alpha):
            return (
                int(fg[0] * alpha + bg[0] * (1.0 - alpha)),
                int(fg[1] * alpha + bg[1] * (1.0 - alpha)),
                int(fg[2] * alpha + bg[2] * (1.0 - alpha)),
                255
            )

        if appearance == "midnight":
            card_col = (29, 29, 36, 255)
            return {
                "is_dark": True,
                "bg_outer": (17, 17, 20, 255),
                "card_bg": card_col,
                "card_border": (255, 255, 255, 18),
                "text_primary": (228, 228, 231, 255),
                "text_secondary": (161, 161, 170, 255),
                "track_recessed": (18, 18, 24, 255),
                "track_guide": (45, 45, 58, 255),
                "knob_body": (accent_rgb[0], accent_rgb[1], accent_rgb[2], 255),
                "knob_border": (255, 255, 255, 220),
                "badge_bg": blend_rgb(accent_rgb, card_col, 0.28),
                "badge_border": blend_rgb(accent_rgb, card_col, 0.85),
                "badge_text": (255, 255, 255, 255),
                "accent": accent_rgb,
            }
        elif not is_dark: # System Light
            card_col = (255, 255, 255, 255)
            return {
                "is_dark": False,
                "bg_outer": (240, 240, 244, 255),
                "card_bg": card_col,
                "card_border": (0, 0, 0, 26),
                "text_primary": (30, 30, 35, 255),
                "text_secondary": (115, 115, 125, 255),
                "track_recessed": (225, 225, 230, 255),
                "track_guide": (195, 195, 205, 255),
                "knob_body": (accent_rgb[0], accent_rgb[1], accent_rgb[2], 255),
                "knob_border": (255, 255, 255, 230),
                "badge_bg": blend_rgb(accent_rgb, card_col, 0.18),
                "badge_border": blend_rgb(accent_rgb, card_col, 0.70),
                "badge_text": (max(0, accent_rgb[0] - 40), max(0, accent_rgb[1] - 40), max(0, accent_rgb[2] - 40), 255),
                "accent": accent_rgb,
            }
        else: # System Dark
            card_col = (40, 40, 48, 255)
            return {
                "is_dark": True,
                "bg_outer": (26, 26, 30, 255),
                "card_bg": card_col,
                "card_border": (255, 255, 255, 24),
                "text_primary": (240, 240, 245, 255),
                "text_secondary": (170, 170, 180, 255),
                "track_recessed": (20, 20, 26, 255),
                "track_guide": (55, 55, 68, 255),
                "knob_body": (accent_rgb[0], accent_rgb[1], accent_rgb[2], 255),
                "knob_border": (255, 255, 255, 220),
                "badge_bg": blend_rgb(accent_rgb, card_col, 0.28),
                "badge_border": blend_rgb(accent_rgb, card_col, 0.85),
                "badge_text": (255, 255, 255, 255),
                "accent": accent_rgb,
            }

    def _extract_peak_value(self, val) -> float:
        if isinstance(val, dict):
            l = float(val.get("left", val.get("l", 0.0)))
            r = float(val.get("right", val.get("r", 0.0)))
            p = float(val.get("peak", max(l, r)))
            return max(l, r, p)
        elif isinstance(val, (list, tuple)):
            if len(val) >= 2:
                return max(float(val[0]), float(val[1]))
            elif len(val) == 1:
                return float(val[0])
        elif isinstance(val, (int, float)):
            return float(val)
        return 0.0

    def _extract_stereo_peak_values(self, val) -> tuple:
        if isinstance(val, dict):
            l = float(val.get("left", val.get("l", 0.0)))
            r = float(val.get("right", val.get("r", 0.0)))
            return l, r
        elif isinstance(val, (list, tuple)):
            if len(val) >= 2:
                return float(val[0]), float(val[1])
            elif len(val) == 1:
                return float(val[0]), float(val[0])
        elif isinstance(val, (int, float)):
            f = float(val)
            return f, f
        return 0.0, 0.0

    def get_hardware_telemetry_info(self):
        """Override in subclasses to provide hardware telemetry (e.g. Wave XLR)."""
        return None

    def handle_touch_tap(self, data: dict) -> bool:
        """Override in subclasses to intercept touchscreen events (e.g. 48V badge toggle)."""
        return False

    def event_callback(self, event: InputEvent, data: dict = None):
        if event == Input.Dial.Events.TURN_CW:
            step_val = self.get_step_size()
            self.handle_volume_change(step_val)
        elif event == Input.Dial.Events.TURN_CCW:
            step_val = self.get_step_size()
            self.handle_volume_change(-step_val)
        elif event in (Input.Dial.Events.DOWN, Input.Dial.Events.SHORT_TOUCH_PRESS):
            if event == Input.Dial.Events.SHORT_TOUCH_PRESS and data and isinstance(data, dict):
                if self.handle_touch_tap(data):
                    return
            self.handle_mute_toggle()
        elif hasattr(Input, "Touchscreen") and hasattr(Input.Touchscreen, "Events") and event in (
            getattr(Input.Touchscreen.Events, "SHORT_PRESS", None),
            getattr(Input.Touchscreen.Events, "TAP", None)
        ):
            if data and isinstance(data, dict) and self.handle_touch_tap(data):
                return
            self.handle_mute_toggle()

    def on_tick_update(self) -> bool:
        if not self.running:
            return False

        now = time.time()
        # High-speed in-memory state sync on every frame for instantaneous 1:1 fader responsiveness
        self.initial_load_status()
        self.last_poll_time = now

        # Update live VU peak
        if self.get_live_meter() and not self.last_mute:
            raw_l, raw_r = self.get_current_peaks_stereo()
            raw_peak = max(raw_l, raw_r)
            
            # Critically damped broadcast ballistics: smooth rise on beats, natural acoustic gravity release
            if raw_l > self._current_peak_l:
                self._current_peak_l = min(1.0, self._current_peak_l + (raw_l - self._current_peak_l) * 0.45)
            else:
                self._current_peak_l = max(0.0, self._current_peak_l + (raw_l - self._current_peak_l) * 0.20)
                if self._current_peak_l < 0.005:
                    self._current_peak_l = 0.0

            if raw_r > self._current_peak_r:
                self._current_peak_r = min(1.0, self._current_peak_r + (raw_r - self._current_peak_r) * 0.45)
            else:
                self._current_peak_r = max(0.0, self._current_peak_r + (raw_r - self._current_peak_r) * 0.20)
                if self._current_peak_r < 0.005:
                    self._current_peak_r = 0.0

            self._current_peak = max(self._current_peak_l, self._current_peak_r)

            # Peak hold marker decay
            if self._current_peak >= self._peak_hold_val:
                self._peak_hold_val = self._current_peak
                self._peak_hold_time = now
            elif (now - self._peak_hold_time) > 1.2:
                self._peak_hold_val = max(self._current_peak, self._peak_hold_val - 0.03)

            self.update_ui_rendering(peak=self._current_peak, peak_l=self._current_peak_l, peak_r=self._current_peak_r)
        else:
            # Gracefully fade out any residual peak when muted or stopped
            if self._current_peak > 0.0 or self._current_peak_l > 0.0 or self._current_peak_r > 0.0:
                self._current_peak_l = max(0.0, self._current_peak_l * 0.88 - 0.004)
                self._current_peak_r = max(0.0, self._current_peak_r * 0.88 - 0.004)
                self._current_peak = max(self._current_peak_l, self._current_peak_r)
                if self._current_peak < 0.002:
                    self._current_peak = 0.0
                    self._current_peak_l = 0.0
                    self._current_peak_r = 0.0
                self.update_ui_rendering(peak=self._current_peak, peak_l=self._current_peak_l, peak_r=self._current_peak_r)
            else:
                self.update_ui_rendering(peak=0.0, peak_l=0.0, peak_r=0.0)

        return True

    def _get_gauge_gradient_image(self, width: int, height: int, bbox: list) -> Image.Image:
        with self._render_lock:
            if self._gauge_gradient_img is not None:
                return self._gauge_gradient_img
                
            grad_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            grad_draw = ImageDraw.Draw(grad_img)
            arc_w = int(7 * RENDER_SCALE)
            
            for angle in range(210, 330):
                pct = (angle - 210) / 120.0
                if pct < 0.65:
                    r_col, g_col, b_col = 61, 179, 86
                elif pct < 0.85:
                    t = (pct - 0.65) / 0.20
                    r_col = int(61 + (235 - 61) * t)
                    g_col = int(179 + (210 - 179) * t)
                    b_col = int(86 * (1 - t))
                else:
                    t = (pct - 0.85) / 0.15
                    r_col = int(235 + (255 - 235) * t)
                    g_col = int(210 - (210 - 50) * t)
                    b_col = 0
                    
                grad_draw.arc(bbox, start=angle, end=angle+2, fill=(r_col, g_col, b_col, 255), width=arc_w)
                
            # Seamless rounded caps on gradient
            r_arc_box = (bbox[1][0] - bbox[0][0]) / 2.0
            r_arc_center = r_arc_box - arc_w / 2.0
            cap_r = arc_w / 2.0
            cx_arc = (bbox[0][0] + bbox[1][0]) / 2.0
            cy_arc = (bbox[0][1] + bbox[1][1]) / 2.0
            
            rad_start = math.radians(210)
            xs = cx_arc + r_arc_center * math.cos(rad_start)
            ys = cy_arc + r_arc_center * math.sin(rad_start)
            grad_draw.ellipse([(xs - cap_r, ys - cap_r), (xs + cap_r, ys + cap_r)], fill=(61, 179, 86, 255))
            
            rad_end = math.radians(330)
            xe = cx_arc + r_arc_center * math.cos(rad_end)
            ye = cy_arc + r_arc_center * math.sin(rad_end)
            grad_draw.ellipse([(xe - cap_r, ye - cap_r), (xe + cap_r, ye + cap_r)], fill=(255, 50, 0, 255))
                
            self._gauge_gradient_img = grad_img
            return self._gauge_gradient_img

    def _get_gauge_gradient_image_sub(self, width: int, height: int, bbox: list) -> Image.Image:
        with self._render_lock:
            if self._gauge_gradient_img_sub is not None:
                return self._gauge_gradient_img_sub
            grad_img = self._get_gauge_gradient_image(width, height, bbox)
            self._gauge_gradient_img_sub = grad_img.crop((self._gx1, self._gy1, self._gx2, self._gy2))
            return self._gauge_gradient_img_sub

    def resolve_icon_as_pil(self, icon_identifier: str, target_size: int = 56) -> Image.Image:
        """Resolves an icon name or file path to a crisp, high-contrast PIL Image using local assets or GTK IconTheme."""
        if not icon_identifier:
            return None

        resolved_img = None

        # 1. Direct valid image file on disk
        if os.path.exists(icon_identifier):
            try:
                if icon_identifier.endswith(".svg"):
                    pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_identifier, target_size, target_size, True)
                    ok, buf = pix.save_to_bufferv("png")
                    resolved_img = Image.open(io.BytesIO(buf)).convert("RGBA")
                else:
                    resolved_img = Image.open(icon_identifier).convert("RGBA")
            except Exception:
                pass

        # 2. Check plugin bundled assets and assets/icons
        if resolved_img is None and hasattr(self, "plugin_base") and hasattr(self.plugin_base, "PATH"):
            clean_name = icon_identifier.replace("-symbolic", "")
            candidates = [
                icon_identifier,
                f"{icon_identifier}.png",
                f"{icon_identifier}.svg",
                os.path.join("assets", icon_identifier),
                os.path.join("assets", f"{icon_identifier}.png"),
                os.path.join("assets", f"{icon_identifier}.svg"),
                os.path.join("assets", f"{clean_name}.png"),
                os.path.join("assets", "icons", f"{icon_identifier}.png"),
                os.path.join("assets", "icons", f"{icon_identifier}.svg"),
                os.path.join("assets", "icons", f"{clean_name}.png"),
                os.path.join("assets", "icons", f"{clean_name}.svg"),
            ]
            for candidate in candidates:
                asset_file = os.path.join(self.plugin_base.PATH, candidate)
                if os.path.exists(asset_file):
                    try:
                        if asset_file.endswith(".svg"):
                            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(asset_file, target_size, target_size, True)
                            ok, buf = pix.save_to_bufferv("png")
                            resolved_img = Image.open(io.BytesIO(buf)).convert("RGBA")
                            break
                        else:
                            resolved_img = Image.open(asset_file).convert("RGBA")
                            break
                    except Exception:
                        pass

        # 3. Look up via Freedesktop GTK IconTheme
        if resolved_img is None:
            try:
                display = Gdk.Display.get_default()
                if display:
                    theme = Gtk.IconTheme.get_for_display(display)
                    for lookup_name in [icon_identifier, f"{icon_identifier}-symbolic", icon_identifier.replace("-symbolic", "")]:
                        if theme.has_icon(lookup_name):
                            paintable = theme.lookup_icon(lookup_name, None, target_size, 1, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.NONE)
                            if paintable and paintable.get_file():
                                path = paintable.get_file().get_path()
                                if path and os.path.exists(path):
                                    pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, target_size, target_size, True)
                                    ok, buf = pix.save_to_bufferv("png")
                                    resolved_img = Image.open(io.BytesIO(buf)).convert("RGBA")
                                    break
            except Exception:
                pass

        if resolved_img is None:
            return None

        # 4. Ensure high contrast: If the icon is monochrome/grayscale, make it crisp bright white (#ffffff)
        try:
            r, g, b, a = resolved_img.split()
            diff_rg = ImageChops.difference(r, g)
            diff_gb = ImageChops.difference(g, b)
            max_d_rg = diff_rg.getextrema()[1]
            max_d_gb = diff_gb.getextrema()[1]
            
            # If color difference across channels is low (grayscale/symbolic), recolor to crisp pure white
            if max_d_rg < 22 and max_d_gb < 22:
                white_img = Image.new("RGBA", resolved_img.size, (255, 255, 255, 255))
                white_img.putalpha(a)
                return white_img
        except Exception:
            pass

        return resolved_img

    def get_badge_style(self) -> str:
        settings = self.get_settings() or {}
        return settings.get("badge_style", "icon_text")

    def resolve_badge_image(self, title: str, subtitle: str, target_size: int = 22):
        """Resolves a category or mix badge image (136x136 PNG) based on target title and subtitle."""
        settings = self.get_settings() or {}
        custom_badge = settings.get("custom_badge", "auto")
        badge_name = None

        if custom_badge and custom_badge != "auto":
            badge_name = custom_badge
            if not badge_name.endswith(".png"):
                badge_name += ".png"
        else:
            sub_low = (subtitle or "").lower()
            title_low = (title or "").lower()

            # 1. Match subtitle (mix / role)
            if "personal" in sub_low or "user" in sub_low:
                badge_name = "badge-personal.png"
            elif "stream" in sub_low or "record" in sub_low or "obs" in sub_low:
                badge_name = "badge-stream.png"
            elif "chat" in sub_low or "voice" in sub_low:
                badge_name = "badge-chat.png"
            elif "headphone" in sub_low or "monitor" in sub_low or "ear" in sub_low:
                badge_name = "badge-headphone.png"
            elif "game" in sub_low or "gaming" in sub_low:
                badge_name = "badge-game.png"
            elif "music" in sub_low or "song" in sub_low:
                badge_name = "badge-music.png"
            elif "browser" in sub_low or "web" in sub_low:
                badge_name = "badge-browser.png"
            elif "mic" in sub_low or "capture" in sub_low:
                badge_name = "badge-mic.png"
            elif "system" in sub_low or "alert" in sub_low:
                badge_name = "badge-system.png"
            elif "master" in sub_low:
                badge_name = "badge-master.png"
            else:
                # 2. Match channel / app context
                if any(k in title_low for k in ("spotify", "music", "amberol", "rhythmbox", "tidal", "vlc", "clementine")):
                    badge_name = "badge-music.png"
                elif any(k in title_low for k in ("discord", "teams", "slack", "zoom", "skype", "telegram")):
                    badge_name = "badge-chat.png"
                elif any(k in title_low for k in ("steam", "heroic", "lutris", "wine", "game", "proton")):
                    badge_name = "badge-game.png"
                elif any(k in title_low for k in ("firefox", "chrome", "chromium", "brave", "edge", "browser")):
                    badge_name = "badge-browser.png"
                elif any(k in title_low for k in ("mic", "microphone", "wave xlr", "input", "fifine")):
                    badge_name = "badge-mic.png"
                elif any(k in title_low for k in ("system", "alert", "bell", "notification")):
                    badge_name = "badge-system.png"
                elif "personal" in title_low:
                    badge_name = "badge-personal.png"
                elif "stream" in title_low:
                    badge_name = "badge-stream.png"
                elif "chat" in title_low:
                    badge_name = "badge-chat.png"
                else:
                    badge_name = "badge-master.png"

        if not badge_name:
            return None

        if not hasattr(self, "_cached_badge_images"):
            self._cached_badge_images = {}

        cache_key = (badge_name, target_size)
        if cache_key in self._cached_badge_images:
            return self._cached_badge_images[cache_key]

        plugin_path = self.plugin_base.PATH if hasattr(self, "plugin_base") and hasattr(self.plugin_base, "PATH") else ""
        candidate_paths = [
            os.path.join(plugin_path, "assets", "badges", badge_name),
            os.path.join("/home/oscar/Pictures/badges", badge_name)
        ]
        badge_path = None
        for p in candidate_paths:
            if os.path.exists(p):
                badge_path = p
                break

        if not badge_path:
            return None

        try:
            with Image.open(badge_path) as b_img:
                res = b_img.convert("RGBA").resize((target_size, target_size), Image.Resampling.LANCZOS)
                self._cached_badge_images[cache_key] = res
                return res
        except Exception:
            return None

    def _generate_volume_image_dx(self, volume: int, is_muted: bool, peak: float = 0.0) -> Image.Image:
        width, height = 200 * RENDER_SCALE, 100 * RENDER_SCALE
        palette = self.get_theme_palette()
        
        # 1. Base Background with Wave-matching Card Container, Ticks & Gauge Track
        base_bg_key = (self.get_appearance(), self.get_accent_color())
        if self._cached_base_bg is None or getattr(self, "_cached_base_bg_key", None) != base_bg_key:
            bg = Image.new("RGBA", (width, height), palette["bg_outer"])
            bg_draw = ImageDraw.Draw(bg)

            # Wave-matching Card Container
            card_x1 = int(1 * RENDER_SCALE)
            card_y1 = int(1 * RENDER_SCALE)
            card_x2 = int(width - 1 * RENDER_SCALE)
            card_y2 = int(height - 1 * RENDER_SCALE)
            card_r = int(10 * RENDER_SCALE)
            bg_draw.rounded_rectangle(
                [(card_x1, card_y1), (card_x2, card_y2)],
                radius=card_r,
                fill=palette["card_bg"],
                outline=palette["card_border"],
                width=max(1, int(1.2 * RENDER_SCALE))
            )
                
            # Pre-render Ticks (5 Major tall ticks, 4 Minor short ticks)
            cx_bg, cy_bg = 70 * RENDER_SCALE, 104 * RENDER_SCALE
            tick_angles = [210, 225, 240, 255, 270, 285, 300, 315, 330]
            r_major_start = 54 * RENDER_SCALE
            r_major_end = 63 * RENDER_SCALE
            r_minor_start = 56 * RENDER_SCALE
            r_minor_end = 61 * RENDER_SCALE

            for i, t_angle in enumerate(tick_angles):
                rad = math.radians(t_angle)
                if i % 2 == 0:
                    r_start = r_major_start
                    r_end = r_major_end
                    tick_w = int(2 * RENDER_SCALE)
                    tick_color = (150, 154, 165, 255) if palette["is_dark"] else (120, 124, 135, 255)
                else:
                    r_start = r_minor_start
                    r_end = r_minor_end
                    tick_w = int(1.5 * RENDER_SCALE)
                    tick_color = (100, 104, 115, 255) if palette["is_dark"] else (170, 174, 185, 255)

                x1 = cx_bg + r_start * math.cos(rad)
                y1 = cy_bg + r_start * math.sin(rad)
                x2 = cx_bg + r_end * math.cos(rad)
                y2 = cy_bg + r_end * math.sin(rad)
                bg_draw.line([(x1, y1), (x2, y2)], fill=tick_color, width=tick_w)
                
            # Pre-render Gauge Track
            r_arc_box = 51 * RENDER_SCALE
            arc_w = 7 * RENDER_SCALE
            r_arc_center = r_arc_box - arc_w / 2.0
            cap_r = arc_w / 2.0
            bbox_bg = [(cx_bg - r_arc_box, cy_bg - r_arc_box), (cx_bg + r_arc_box, cy_bg + r_arc_box)]

            track_color = (35, 38, 48, 255) if palette["is_dark"] else (220, 222, 230, 255)
            bg_draw.arc(bbox_bg, start=210, end=330, fill=track_color, width=arc_w)
            for cap_angle in (210, 330):
                rad_cap = math.radians(cap_angle)
                xc = cx_bg + r_arc_center * math.cos(rad_cap)
                yc = cy_bg + r_arc_center * math.sin(rad_cap)
                bg_draw.ellipse([(xc - cap_r, yc - cap_r), (xc + cap_r, yc + cap_r)], fill=track_color)
            
            self._cached_base_bg = bg
            self._cached_base_bg_key = base_bg_key

        # 2. Resolve Labels, Fonts & Cache Keys
        settings = self.get_settings() or {}
        volume_format = settings.get("volume_format", "percent")
        custom_icon_path = settings.get("custom_icon", "")
        
        target_title, target_subtitle = self.get_target_title_and_subtitle()
        title_text = target_title
        if title_text.startswith("Elgato "):
            title_text = title_text[len("Elgato "):]
        
        effective_icon_identifier = custom_icon_path if custom_icon_path else self.get_target_icon_path()
        font_name = settings.get("font_name", "DejaVu Sans Bold 15")
        font_path = settings.get("font_path", "")

        telemetry_info = self.get_hardware_telemetry_info()
        midground_key = (
            volume,
            is_muted,
            title_text,
            target_subtitle,
            effective_icon_identifier,
            volume_format,
            font_name,
            font_path,
            self.get_appearance(),
            self.get_accent_color(),
            tuple(sorted(telemetry_info.items())) if isinstance(telemetry_info, dict) else None
        )

        cx, cy = 70 * RENDER_SCALE, 104 * RENDER_SCALE
        r_outer = 44 * RENDER_SCALE
        r_inner = 39 * RENDER_SCALE
        r_arc_box = 51 * RENDER_SCALE
        arc_w = 7 * RENDER_SCALE
        r_arc_center = r_arc_box - arc_w / 2.0
        cap_r = arc_w / 2.0
        bbox = [(cx - r_arc_box, cy - r_arc_box), (cx + r_arc_box, cy + r_arc_box)]
        bbox_outer = [(cx - r_outer, cy - r_outer), (cx + r_outer, cy + r_outer)]
        bbox_inner = [(cx - r_inner, cy - r_inner), (cx + r_inner, cy + r_inner)]
        start_cap_x = self._start_cap_x
        start_cap_y = self._start_cap_y

        sub_cx = self._sub_cx
        sub_cy = self._sub_cy
        sub_start_cap_x = self._sub_start_cap_x
        sub_start_cap_y = self._sub_start_cap_y

        # Build Midground Card (Text, Icon, Inner Knob)
        if self._cached_midground is None or self._cached_midground_key != midground_key:
            mid_img = self._cached_base_bg.copy()
            if is_muted:
                red_tint = Image.new("RGBA", (width, height), (239, 68, 68, 48))
                mid_img = Image.alpha_composite(mid_img, red_tint)
            mid_draw = ImageDraw.Draw(mid_img)

            # Draw Volume Text
            if is_muted:
                vol_text = "MUTE"
                vol_color = (239, 68, 68, 255)
            elif volume_format == "db":
                if volume <= 0:
                    vol_text = "-inf dB"
                elif volume >= 100:
                    vol_text = "0.0 dB"
                else:
                    db_val = 20.0 * math.log10(max(1, volume) / 100.0)
                    vol_text = f"{db_val:.1f} dB"
                vol_color = (255, 255, 255, 255)
            else:
                vol_text = f"{volume}%"
                vol_color = (255, 255, 255, 255)
            
            # Resolve bold TrueType font with bundled plugin fonts priority
            font_file = None
            bundled_bold = os.path.join(self.plugin_base.PATH, "assets", "fonts", "DejaVuSans-Bold.ttf")
            bundled_regular = os.path.join(self.plugin_base.PATH, "assets", "fonts", "DejaVuSans.ttf")
            
            if os.path.exists(bundled_bold):
                font_file = bundled_bold
            elif font_path and os.path.exists(font_path):
                font_file = font_path
            else:
                for path in [
                    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
                    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
                ]:
                    if os.path.exists(path):
                        font_file = path
                        break

            title_font_size = 14
            vol_font_size = 15 if volume_format == "db" else 19

            try:
                if font_file:
                    self._cached_font_title = ImageFont.truetype(font_file, int(title_font_size * RENDER_SCALE))
                    self._cached_font_vol = ImageFont.truetype(font_file, int(vol_font_size * RENDER_SCALE))
                    self._cached_font_badge_sm = ImageFont.truetype(font_file, int(11 * RENDER_SCALE))
                    self._cached_font_badge_md = ImageFont.truetype(font_file, int(12.5 * RENDER_SCALE))
                    self._cached_font_vol_wave = ImageFont.truetype(font_file, int(18 * RENDER_SCALE))
                else:
                    self._cached_font_title = ImageFont.load_default()
                    self._cached_font_vol = ImageFont.load_default()
                    self._cached_font_badge_sm = ImageFont.load_default()
                    self._cached_font_badge_md = ImageFont.load_default()
                    self._cached_font_vol_wave = ImageFont.load_default()
            except Exception:
                self._cached_font_title = ImageFont.load_default()
                self._cached_font_vol = ImageFont.load_default()
                self._cached_font_badge_sm = ImageFont.load_default()
                self._cached_font_badge_md = ImageFont.load_default()
                self._cached_font_vol_wave = ImageFont.load_default()

            font_title = self._cached_font_title
            font_vol = self._cached_font_vol
            font_badge_sm = getattr(self, "_cached_font_badge_sm", font_title)
            font_badge_md = getattr(self, "_cached_font_badge_md", font_title)
            font_vol_wave = getattr(self, "_cached_font_vol_wave", font_vol)

            # Draw Right-Column Elements (3-Slot Telemetry vs Standard)
            if telemetry_info and isinstance(telemetry_info, dict) and telemetry_info.get("is_hardware"):
                # --- Extended 3-Slot Telemetry Layout (Wave XLR / Hardware Only) ---
                is_online = telemetry_info.get("is_online", True)
                phantom_48v = telemetry_info.get("phantom_48v", False)

                # Slot 1: Connection Status Badge (Center: 165, 33)
                s1_w, s1_h = int(54 * RENDER_SCALE), int(20 * RENDER_SCALE)
                s1_x, s1_y = int(165 * RENDER_SCALE), int(33 * RENDER_SCALE)
                s1_bbox = [
                    (s1_x - s1_w // 2, s1_y - s1_h // 2),
                    (s1_x + s1_w // 2, s1_y + s1_h // 2)
                ]
                if is_online:
                    mid_draw.rounded_rectangle(s1_bbox, radius=5 * RENDER_SCALE, fill=(24, 52, 34, 255), outline=(52, 128, 68, 200), width=max(1, int(1.2 * RENDER_SCALE)))
                    mid_draw.text((s1_x, s1_y), "Online", font=font_badge_sm, fill=(74, 222, 128, 255), anchor="mm")
                else:
                    mid_draw.rounded_rectangle(s1_bbox, radius=5 * RENDER_SCALE, fill=(50, 36, 18, 255), outline=(180, 120, 20, 200), width=max(1, int(1.2 * RENDER_SCALE)))
                    mid_draw.text((s1_x, s1_y), "Offline", font=font_badge_sm, fill=(251, 191, 36, 255), anchor="mm")

                # Slot 2: 48V Phantom Power Badge (Center: 165, 59)
                s2_w, s2_h = int(54 * RENDER_SCALE), int(23 * RENDER_SCALE)
                s2_x, s2_y = int(165 * RENDER_SCALE), int(59 * RENDER_SCALE)
                s2_bbox = [
                    (s2_x - s2_w // 2, s2_y - s2_h // 2),
                    (s2_x + s2_w // 2, s2_y + s2_h // 2)
                ]
                if phantom_48v:
                    mid_draw.rounded_rectangle(s2_bbox, radius=6 * RENDER_SCALE, fill=(45, 33, 16, 255), outline=(245, 158, 11, 230), width=max(1, int(1.5 * RENDER_SCALE)))
                    # Sharp vector lightning bolt
                    lx = s2_x - 13 * RENDER_SCALE
                    ly = s2_y
                    s = RENDER_SCALE
                    poly = [
                        (lx + 1.2*s, ly - 6.5*s),
                        (lx - 4.5*s, ly + 0.5*s),
                        (lx - 0.5*s, ly + 0.5*s),
                        (lx - 2.2*s, ly + 6.5*s),
                        (lx + 4.5*s, ly - 0.5*s),
                        (lx + 0.5*s, ly - 0.5*s)
                    ]
                    mid_draw.polygon(poly, fill=(251, 191, 36, 255))
                    mid_draw.text((s2_x + 5 * RENDER_SCALE, s2_y), "48V", font=font_badge_md, fill=(251, 191, 36, 255), anchor="mm")
                else:
                    mid_draw.rounded_rectangle(s2_bbox, radius=6 * RENDER_SCALE, fill=(35, 37, 42, 255), outline=(80, 85, 95, 180), width=max(1, int(1 * RENDER_SCALE)))
                    mid_draw.text((s2_x, s2_y), "48V", font=font_badge_md, fill=(160, 165, 175, 255), anchor="mm")

                # Slot 3: Volume / Gain Readout (Center: 165, 85)
                s3_x, s3_y = int(165 * RENDER_SCALE), int(85 * RENDER_SCALE)
                mid_draw.text((s3_x, s3_y), vol_text, font=font_vol_wave, fill=vol_color, anchor="mm")
            else:
                # --- Standard Layout (Generic Channels: Spotify, Discord, Sub-Mix, etc.) ---
                s3_x = int(164 * RENDER_SCALE)
                s3_y = int(80 * RENDER_SCALE)
                try:
                    mid_draw.text((s3_x, s3_y), vol_text, font=font_vol, fill=vol_color, anchor="mm")
                except TypeError:
                    mid_draw.text((int((164 - 20) * RENDER_SCALE), int((80 - 10) * RENDER_SCALE)), vol_text, font=font_vol, fill=vol_color)

                # Badge icon positioned directly above volume readout (32px unscaled / 64px at 2x)
                badge_target_size = int(32 * RENDER_SCALE)
                badge_img = self.resolve_badge_image(title_text, target_subtitle, target_size=badge_target_size)
                if badge_img is not None:
                    bx = s3_x - badge_img.width // 2
                    by = int(25 * RENDER_SCALE)
                    mid_img.paste(badge_img, (bx, by), badge_img)

            # Icon Placement & Rendering
            icon_drawn = False
            icon_w = 26
            
            if effective_icon_identifier:
                if effective_icon_identifier != self._cached_icon_path or self._cached_icon_img is None:
                    target_max = int(27 * RENDER_SCALE)
                    resolved_pil = self.resolve_icon_as_pil(effective_icon_identifier, target_size=target_max)
                    if resolved_pil is not None:
                        orig_w, orig_h = resolved_pil.size
                        if orig_w > orig_h:
                            new_w = target_max
                            new_h = max(1, int(orig_h * target_max / orig_w))
                        else:
                            new_h = target_max
                            new_w = max(1, int(orig_w * target_max / orig_h))
                        self._cached_icon_img = resolved_pil.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    else:
                        self._cached_icon_img = None
                    self._cached_icon_path = effective_icon_identifier
                    
                if self._cached_icon_img is not None:
                    icon_img = self._cached_icon_img.copy()
                    icon_w_unscaled = icon_img.width // RENDER_SCALE
                    icon_h_unscaled = icon_img.height // RENDER_SCALE
                    x_start = 12
                    y_start = 16 - icon_h_unscaled // 2
                    y_start = max(3, min(y_start, 38 - icon_h_unscaled))
                    mid_img.paste(icon_img, (x_start * RENDER_SCALE, y_start * RENDER_SCALE), icon_img)
                    icon_drawn = True
                    icon_w = icon_w_unscaled

            if not icon_drawn:
                spk_x, spk_y = 12, 9
                spk_color = (90, 105, 120, 255) if is_muted else (110, 130, 150, 255)
                mid_draw.rectangle([
                    (spk_x * RENDER_SCALE, (spk_y + 4) * RENDER_SCALE), 
                    ((spk_x + 5) * RENDER_SCALE, (spk_y + 10) * RENDER_SCALE)
                ], fill=spk_color)
                mid_draw.polygon([
                    ((spk_x + 5) * RENDER_SCALE, (spk_y + 4) * RENDER_SCALE), 
                    ((spk_x + 10) * RENDER_SCALE, (spk_y + 0) * RENDER_SCALE), 
                    ((spk_x + 10) * RENDER_SCALE, (spk_y + 14) * RENDER_SCALE), 
                    ((spk_x + 5) * RENDER_SCALE, (spk_y + 10) * RENDER_SCALE)
                ], fill=spk_color)
                
                if is_muted:
                    mid_draw.line([
                        ((spk_x - 2) * RENDER_SCALE, (spk_y + 2) * RENDER_SCALE), 
                        ((spk_x + 16) * RENDER_SCALE, (spk_y + 12) * RENDER_SCALE)
                    ], fill=(239, 68, 68, 255), width=2 * RENDER_SCALE)
                else:
                    wave_color = (0, 168, 255, 255)
                    mid_draw.arc([
                        ((spk_x + 3) * RENDER_SCALE, (spk_y + 2) * RENDER_SCALE), 
                        ((spk_x + 13) * RENDER_SCALE, (spk_y + 12) * RENDER_SCALE)
                    ], start=-45, end=45, fill=wave_color, width=2 * RENDER_SCALE)

            # Title Text
            left_bound = 12 + icon_w + 6
            right_bound = 144 if (locals().get("badge_img") is not None or locals().get("s1_x") is not None) else 195
            max_width = right_bound - left_bound - 4

            max_width_scaled = max_width * RENDER_SCALE
            title_text_to_draw = title_text

            try:
                text_w = font_title.getlength(title_text_to_draw)
            except Exception:
                text_w = len(title_text_to_draw) * (title_font_size * RENDER_SCALE * 0.6)

            current_size = title_font_size
            font_title_to_draw = font_title
            while text_w > max_width_scaled and current_size > 9:
                current_size -= 1
                try:
                    if font_file:
                        temp_font = ImageFont.truetype(font_file, current_size * RENDER_SCALE)
                    else:
                        temp_font = ImageFont.load_default()
                    text_w = temp_font.getlength(title_text_to_draw)
                    font_title_to_draw = temp_font
                except Exception:
                    break

            while text_w > max_width_scaled and len(title_text_to_draw) > 3:
                title_text_to_draw = title_text_to_draw[:-3] + ".."
                try:
                    text_w = font_title_to_draw.getlength(title_text_to_draw)
                except Exception:
                    break
            
            try:
                mid_draw.text((left_bound * RENDER_SCALE, 16 * RENDER_SCALE), title_text_to_draw, font=font_title_to_draw, fill=(220, 222, 230, 255), anchor="lm")
            except TypeError:
                mid_draw.text((left_bound * RENDER_SCALE, (16 - 8) * RENDER_SCALE), title_text_to_draw, font=font_title_to_draw, fill=(220, 222, 230, 255))

            # Inner Knob Core
            mid_draw.chord(bbox_outer, start=180, end=360, fill=(35, 35, 38, 255))
            mid_draw.chord(bbox_inner, start=180, end=360, fill=(66, 66, 70, 255))
            mid_draw.arc(bbox_inner, start=180, end=360, fill=(85, 85, 92, 255), width=1 * RENDER_SCALE)

            self._cached_midground = mid_img
            self._cached_midground_key = midground_key

        # 3. Dynamic Frame Rendering
        img = self._cached_midground.copy()
        draw = ImageDraw.Draw(img)
        
        if not is_muted:
            now = time.time()
            is_adjusting = (now - self._last_volume_adjust_time) < ADJUSTING_TIMEOUT
            
            if is_adjusting:
                vol_angle = int(210 + 120 * (volume / 100.0))
                if vol_angle > 210:
                    draw.arc(bbox, start=210, end=vol_angle, fill=(255, 255, 255, 255), width=arc_w)
                    draw.ellipse([(start_cap_x - cap_r, start_cap_y - cap_r), (start_cap_x + cap_r, start_cap_y + cap_r)], fill=(255, 255, 255, 255))
                    rad_e = math.radians(vol_angle)
                    xe = cx + r_arc_center * math.cos(rad_e)
                    ye = cy + r_arc_center * math.sin(rad_e)
                    draw.ellipse([(xe - cap_r, ye - cap_r), (xe + cap_r, ye + cap_r)], fill=(255, 255, 255, 255))
            else:
                is_live_enabled = settings.get("live_meter", True)
                if is_live_enabled:
                    vol_pct = max(0.0, min(1.0, volume / 100.0))
                    if peak > 0.02 and vol_pct > 0.0:
                        scaled_peak = min(vol_pct, peak)
                        peak_angle = int(210 + 120 * scaled_peak)
                        if peak_angle > 210:
                            rad_e = math.radians(min(330, peak_angle))
                            xe = cx + r_arc_center * math.cos(rad_e)
                            ye = cy + r_arc_center * math.sin(rad_e)
                            
                            if scaled_peak >= 0.98:
                                draw.arc(bbox, start=210, end=min(330, peak_angle), fill=(255, 30, 30, 255), width=arc_w)
                                draw.ellipse([(start_cap_x - cap_r, start_cap_y - cap_r), (start_cap_x + cap_r, start_cap_y + cap_r)], fill=(255, 30, 30, 255))
                                draw.ellipse([(xe - cap_r, ye - cap_r), (xe + cap_r, ye + cap_r)], fill=(255, 30, 30, 255))
                            else:
                                self._peak_mask_sub_draw.rectangle([(0, 0), (self._sub_width, self._sub_height)], fill=0)
                                self._peak_mask_sub_draw.arc(self._sub_bbox, start=210, end=peak_angle, fill=255, width=arc_w)
                                sub_xe = sub_cx + r_arc_center * math.cos(rad_e)
                                sub_ye = sub_cy + r_arc_center * math.sin(rad_e)
                                self._peak_mask_sub_draw.ellipse([(sub_start_cap_x - cap_r, sub_start_cap_y - cap_r), (sub_start_cap_x + cap_r, sub_start_cap_y + cap_r)], fill=255)
                                self._peak_mask_sub_draw.ellipse([(sub_xe - cap_r, sub_ye - cap_r), (sub_xe + cap_r, sub_ye + cap_r)], fill=255)
                                
                                grad_img_sub = self._get_gauge_gradient_image_sub(width, height, bbox)
                                img.paste(grad_img_sub, (self._gx1, self._gy1), self._peak_mask_sub)

                    # Peak Hold Marker (bounded by volume)
                    if self._peak_hold_val > 0.04 and vol_pct > 0.0:
                        scaled_hold = min(vol_pct, max(0.0, self._peak_hold_val))
                        hold_angle = int(210 + 120 * scaled_hold)
                        if hold_angle > 210:
                            draw.arc(bbox, start=max(210, hold_angle - 1), end=min(330, hold_angle + 1), fill=(255, 75, 75, 255), width=arc_w)
                else:
                    vol_angle = int(210 + 120 * (volume / 100.0))
                    if vol_angle > 210:
                        arc_fill = (120, 120, 120, 160) if (telemetry_info and not telemetry_info.get("is_online", True)) else (0, 168, 255, 255)
                        draw.arc(bbox, start=210, end=vol_angle, fill=arc_fill, width=arc_w)
                        rad_e = math.radians(vol_angle)
                        xe = cx + r_arc_center * math.cos(rad_e)
                        ye = cy + r_arc_center * math.sin(rad_e)
                        draw.ellipse([(xe - cap_r, ye - cap_r), (xe + cap_r, ye + cap_r)], fill=arc_fill)

        # 4. Draw Rotating Pointer Notch on Inner Knob
        pointer_angle = 210 + 120 * (volume / 100.0)
        rad_pt = math.radians(pointer_angle)
        r_notch_in = 26 * RENDER_SCALE
        r_notch_out = 36 * RENDER_SCALE
        xp1 = cx + r_notch_in * math.cos(rad_pt)
        yp1 = cy + r_notch_in * math.sin(rad_pt)
        xp2 = cx + r_notch_out * math.cos(rad_pt)
        yp2 = cy + r_notch_out * math.sin(rad_pt)
        pointer_color = (255, 255, 255, 255)
        notch_w = int(2.5 * RENDER_SCALE)
        draw.line([(xp1, yp1), (xp2, yp2)], fill=pointer_color, width=notch_w)

        # 5. Red Perimeter Border when Muted
        if is_muted:
            border_w = int(2 * RENDER_SCALE)
            draw.rounded_rectangle(
                [(1 * RENDER_SCALE, 1 * RENDER_SCALE), (width - 1 - 1 * RENDER_SCALE, height - 1 - 1 * RENDER_SCALE)],
                radius=12 * RENDER_SCALE,
                outline=(255, 59, 48, 255),
                width=border_w
            )

        # 6. Downsample back to Stream Deck + LCD resolution (200x100)
        if RENDER_SCALE > 1:
            return img.resize((200, 100), Image.Resampling.BILINEAR)
        return img

    def _generate_volume_image_wave(self, volume: int, is_muted: bool, peak: float = 0.0, peak_l: float = 0.0, peak_r: float = 0.0) -> Image.Image:
        width, height = 200 * RENDER_SCALE, 100 * RENDER_SCALE
        palette = self.get_theme_palette()
        settings = self.get_settings() or {}
        volume_format = settings.get("volume_format", "percent")
        custom_icon_path = settings.get("custom_icon", "")

        target_title, target_subtitle = self.get_target_title_and_subtitle()
        title_text = target_title
        if title_text.startswith("Elgato "):
            title_text = title_text[len("Elgato "):]

        subtitle_text = target_subtitle or ""
        effective_icon_identifier = custom_icon_path if custom_icon_path else self.get_target_icon_path()
        telemetry_info = self.get_hardware_telemetry_info()

        # Resolve fonts
        bundled_bold = os.path.join(self.plugin_base.PATH, "assets", "fonts", "DejaVuSans-Bold.ttf") if hasattr(self, "plugin_base") and hasattr(self.plugin_base, "PATH") else ""
        font_file = None
        if bundled_bold and os.path.exists(bundled_bold):
            font_file = bundled_bold
        else:
            for p in [
                "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            ]:
                if os.path.exists(p):
                    font_file = p
                    break

        if font_file:
            try:
                f_title = ImageFont.truetype(font_file, int(14 * RENDER_SCALE))
                f_badge = ImageFont.truetype(font_file, int(12 * RENDER_SCALE))
                f_badge_md = ImageFont.truetype(font_file, int(12 * RENDER_SCALE))
                f_vol = ImageFont.truetype(font_file, int(14.5 * RENDER_SCALE))
            except Exception:
                f_title = f_badge = f_badge_md = f_vol = ImageFont.load_default()
        else:
            f_title = f_badge = f_badge_md = f_vol = ImageFont.load_default()

        card_x1 = int(1 * RENDER_SCALE)
        card_y1 = int(1 * RENDER_SCALE)
        card_x2 = int(width - 1 * RENDER_SCALE)
        card_y2 = int(height - 1 * RENDER_SCALE)
        card_r = int(10 * RENDER_SCALE)

        wave_midground_key = (
            volume,
            is_muted,
            title_text,
            subtitle_text,
            effective_icon_identifier,
            volume_format,
            self.get_appearance(),
            self.get_accent_color(),
            self.get_badge_style(),
            tuple(sorted(telemetry_info.items())) if isinstance(telemetry_info, dict) else None
        )

        if self._cached_wave_midground is None or self._cached_wave_midground_key != wave_midground_key:
            mid_img = Image.new("RGBA", (width, height), palette["bg_outer"])
            mid_draw = ImageDraw.Draw(mid_img)

            # 1. Card container (matching DX style 1px border margin)
            mid_draw.rounded_rectangle(
                [(card_x1, card_y1), (card_x2, card_y2)],
                radius=card_r,
                fill=palette["card_bg"],
                outline=(239, 68, 68, 230) if is_muted else palette["card_border"],
                width=int(1.8 * RENDER_SCALE) if is_muted else max(1, int(1.2 * RENDER_SCALE))
            )

            # 2. Header Row: Icon
            icon_size = int(24 * RENDER_SCALE)
            icon_x = card_x1 + int(9 * RENDER_SCALE)
            icon_y = card_y1 + int(8 * RENDER_SCALE)
            icon_drawn = False

            if effective_icon_identifier:
                resolved_pil = self.resolve_icon_as_pil(effective_icon_identifier, target_size=icon_size)
                if resolved_pil is not None:
                    orig_w, orig_h = resolved_pil.size
                    if orig_w > orig_h:
                        new_w = icon_size
                        new_h = max(1, int(orig_h * icon_size / orig_w))
                    else:
                        new_h = icon_size
                        new_w = max(1, int(orig_w * icon_size / orig_h))
                    scaled_ico = resolved_pil.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    ix = icon_x + (icon_size - new_w) // 2
                    iy = icon_y + (icon_size - new_h) // 2
                    mid_img.paste(scaled_ico, (ix, iy), scaled_ico)
                    icon_drawn = True

            if not icon_drawn:
                mid_draw.ellipse([(icon_x, icon_y), (icon_x + icon_size, icon_y + icon_size)], fill=(40, 45, 55, 255))
                cx_i = icon_x + icon_size // 2
                cy_i = icon_y + icon_size // 2
                is_mic = ("mic" in title_text.lower() or "wave" in title_text.lower() or (telemetry_info and telemetry_info.get("is_hardware")))
                if is_mic:
                    mid_draw.rounded_rectangle([(cx_i - 3*RENDER_SCALE, cy_i - 7*RENDER_SCALE), (cx_i + 3*RENDER_SCALE, cy_i + 2*RENDER_SCALE)], radius=3*RENDER_SCALE, fill=(220, 220, 230, 255))
                    mid_draw.arc([(cx_i - 6*RENDER_SCALE, cy_i - 4*RENDER_SCALE), (cx_i + 6*RENDER_SCALE, cy_i + 5*RENDER_SCALE)], start=0, end=180, fill=(220, 220, 230, 255), width=2*RENDER_SCALE)
                    mid_draw.line([(cx_i, cy_i + 5*RENDER_SCALE), (cx_i, cy_i + 8*RENDER_SCALE)], fill=(220, 220, 230, 255), width=2*RENDER_SCALE)
                else:
                    mid_draw.arc([(cx_i - 7*RENDER_SCALE, cy_i - 7*RENDER_SCALE), (cx_i + 7*RENDER_SCALE, cy_i + 4*RENDER_SCALE)], start=180, end=360, fill=(220, 220, 230, 255), width=2*RENDER_SCALE)
                    mid_draw.rounded_rectangle([(cx_i - 8*RENDER_SCALE, cy_i - 2*RENDER_SCALE), (cx_i - 5*RENDER_SCALE, cy_i + 5*RENDER_SCALE)], radius=2*RENDER_SCALE, fill=(220, 220, 230, 255))
                    mid_draw.rounded_rectangle([(cx_i + 5*RENDER_SCALE, cy_i - 2*RENDER_SCALE), (cx_i + 8*RENDER_SCALE, cy_i + 5*RENDER_SCALE)], radius=2*RENDER_SCALE, fill=(220, 220, 230, 255))

            # Header Row: Right Badge (Hardware 48V only in header)
            badge_w = 0
            if telemetry_info and telemetry_info.get("is_hardware"):
                phantom_48v = telemetry_info.get("phantom_48v", False)
                bh = int(18 * RENDER_SCALE)
                bw = int(48 * RENDER_SCALE)
                bx2 = card_x2 - int(10 * RENDER_SCALE)
                bx1 = bx2 - bw
                by1 = icon_y + (icon_size - bh) // 2
                by2 = by1 + bh
                badge_w = bw + int(8 * RENDER_SCALE)

                if phantom_48v:
                    mid_draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=int(5 * RENDER_SCALE), fill=(45, 33, 16, 255), outline=(245, 158, 11, 230), width=max(1, int(1.2 * RENDER_SCALE)))
                    lx = bx1 + int(11 * RENDER_SCALE)
                    ly = (by1 + by2) // 2
                    s = RENDER_SCALE
                    poly = [
                        (lx + 1.0*s, ly - 5.0*s),
                        (lx - 3.5*s, ly + 0.5*s),
                        (lx - 0.5*s, ly + 0.5*s),
                        (lx - 1.8*s, ly + 5.0*s),
                        (lx + 3.5*s, ly - 0.5*s),
                        (lx + 0.5*s, ly - 0.5*s)
                    ]
                    mid_draw.polygon(poly, fill=(251, 191, 36, 255))
                    mid_draw.text((bx1 + int(28 * RENDER_SCALE), ly), "48V", font=f_badge_md, fill=(251, 191, 36, 255), anchor="mm")
                else:
                    mid_draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=int(5 * RENDER_SCALE), fill=(35, 37, 42, 255), outline=(80, 85, 95, 180), width=max(1, int(1 * RENDER_SCALE)))
                    mid_draw.text(((bx1 + bx2) // 2, (by1 + by2) // 2), "48V", font=f_badge_md, fill=(160, 165, 175, 255), anchor="mm")

            # Header Row: Title with full available width
            title_x = icon_x + icon_size + int(8 * RENDER_SCALE)
            max_title_w = (card_x2 - int(10 * RENDER_SCALE) - badge_w) - title_x
            title_to_draw = title_text
            font_curr = f_title
            try:
                tw = font_curr.getlength(title_to_draw)
            except Exception:
                tw = len(title_to_draw) * 14

            if tw > max_title_w:
                try:
                    f_smaller = ImageFont.truetype(font_file, int(12 * RENDER_SCALE)) if font_file else f_title
                    if f_smaller.getlength(title_to_draw) <= max_title_w:
                        font_curr = f_smaller
                        tw = font_curr.getlength(title_to_draw)
                    else:
                        while len(title_to_draw) > 3 and f_smaller.getlength(title_to_draw + "..") > max_title_w:
                            title_to_draw = title_to_draw[:-1]
                        title_to_draw += ".."
                        font_curr = f_smaller
                except Exception:
                    pass

            mid_draw.text((title_x, icon_y + icon_size // 2), title_to_draw, font=font_curr, fill=palette["text_primary"], anchor="lm")

            # Middle Tier: Badge above Fader Track
            badge_style = self.get_badge_style()
            if subtitle_text and not (telemetry_info and telemetry_info.get("is_hardware")):
                bx2 = card_x2 - int(10 * RENDER_SCALE)

                if badge_style == "icon_only":
                    badge_img = self.resolve_badge_image(title_text, subtitle_text, target_size=int(32 * RENDER_SCALE))
                    if badge_img is not None:
                        bh = badge_img.height
                        bx1 = bx2 - badge_img.width
                        by1 = card_y1 + int(31 * RENDER_SCALE)
                        mid_img.paste(badge_img, (bx1, by1), badge_img)
                elif badge_style == "text_only":
                    bh = int(22 * RENDER_SCALE)
                    sub_font = ImageFont.truetype(font_file, int(11 * RENDER_SCALE)) if font_file else f_badge
                    text_w = int(sub_font.getlength(subtitle_text)) if hasattr(sub_font, "getlength") else len(subtitle_text) * 9
                    pill_w = text_w + int(16 * RENDER_SCALE)
                    bx1 = bx2 - pill_w
                    by1 = card_y1 + int(38 * RENDER_SCALE)
                    by2 = by1 + bh
                    mid_draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=int(5 * RENDER_SCALE), fill=palette["badge_bg"], outline=palette["badge_border"], width=max(1, int(1 * RENDER_SCALE)))
                    mid_draw.text(((bx1 + bx2) // 2, (by1 + by2) // 2), subtitle_text, font=sub_font, fill=palette["badge_text"], anchor="mm")
                else:
                    # Default: "icon_text" (Pill with icon and text)
                    bh = int(24 * RENDER_SCALE)
                    ico_s = int(18 * RENDER_SCALE)
                    sub_font = ImageFont.truetype(font_file, int(11 * RENDER_SCALE)) if font_file else f_badge
                    text_w = int(sub_font.getlength(subtitle_text)) if hasattr(sub_font, "getlength") else len(subtitle_text) * 9
                    pill_w = text_w + int(28 * RENDER_SCALE)
                    bx1 = bx2 - pill_w
                    by1 = card_y1 + int(37 * RENDER_SCALE)
                    by2 = by1 + bh
                    mid_draw.rounded_rectangle([(bx1, by1), (bx2, by2)], radius=int(5 * RENDER_SCALE), fill=palette["badge_bg"], outline=palette["badge_border"], width=max(1, int(1 * RENDER_SCALE)))
                    badge_img = self.resolve_badge_image(title_text, subtitle_text, target_size=ico_s)
                    if badge_img is not None:
                        mid_img.paste(badge_img, (bx1 + int(4 * RENDER_SCALE), by1 + int(3 * RENDER_SCALE)), badge_img)
                    mid_draw.text((bx1 + int(25 * RENDER_SCALE), (by1 + by2) // 2), subtitle_text, font=sub_font, fill=palette["badge_text"], anchor="lm")

            # 3. Bottom Row: Speaker / Mute icon & Lowered Fader Track
            slider_y = card_y1 + int(74 * RENDER_SCALE)
            spk_cx = card_x1 + int(16 * RENDER_SCALE)
            spk_cy = slider_y
            spk_color = (239, 68, 68, 255) if is_muted else palette["text_secondary"]
            s = RENDER_SCALE
            mid_draw.rectangle([(spk_cx - 7*s, spk_cy - 3.5*s), (spk_cx - 3.5*s, spk_cy + 3.5*s)], fill=spk_color)
            poly = [(spk_cx - 3.5*s, spk_cy - 3.5*s), (spk_cx + 1*s, spk_cy - 7*s), (spk_cx + 1*s, spk_cy + 7*s), (spk_cx - 3.5*s, spk_cy + 3.5*s)]
            mid_draw.polygon(poly, fill=spk_color)
            if not is_muted:
                mid_draw.arc([(spk_cx - 3*s, spk_cy - 5.5*s), (spk_cx + 5.5*s, spk_cy + 5.5*s)], start=305, end=55, fill=spk_color, width=int(1.5*s))
                mid_draw.arc([(spk_cx - 3*s, spk_cy - 8.5*s), (spk_cx + 8.5*s, spk_cy + 8.5*s)], start=315, end=45, fill=spk_color, width=int(1.5*s))
            else:
                mid_draw.line([(spk_cx - 8*s, spk_cy - 8*s), (spk_cx + 8*s, spk_cy + 8*s)], fill=(239, 68, 68, 255), width=int(2*s))

            # Bottom Row: Volume Readout Text
            pct_x = card_x2 - int(10 * RENDER_SCALE)
            if is_muted:
                vol_text = "MUTE"
                vol_color = (239, 68, 68, 255)
            elif volume_format == "db":
                if volume <= 0:
                    vol_text = "-inf dB"
                elif volume >= 100:
                    vol_text = "0.0 dB"
                else:
                    db_val = 20.0 * math.log10(max(1, volume) / 100.0)
                    vol_text = f"{db_val:.1f} dB"
                vol_color = palette["text_primary"]
            else:
                vol_text = f"{volume}%"
                vol_color = palette["text_primary"]

            mid_draw.text((pct_x, slider_y), vol_text, font=f_vol, fill=vol_color, anchor="rm")
            try:
                readout_w = int(f_vol.getlength(vol_text))
            except Exception:
                readout_w = len(vol_text) * 12

            # Bottom Row: Recessed and Active Volume Tracks
            track_x1 = spk_cx + int(16 * RENDER_SCALE)
            track_x2 = pct_x - readout_w - int(10 * RENDER_SCALE)
            track_w = max(20, track_x2 - track_x1)

            bar_h = max(2, int(3 * RENDER_SCALE))
            bar_gap = max(2, int(3 * RENDER_SCALE))
            top_y1 = slider_y - bar_h - bar_gap // 2
            top_y2 = top_y1 + bar_h
            bot_y1 = top_y2 + bar_gap
            bot_y2 = bot_y1 + bar_h
            bar_r = max(1, int(1.5 * RENDER_SCALE))

            mid_draw.rounded_rectangle([(track_x1, top_y1), (track_x2, top_y2)], radius=bar_r, fill=palette["track_recessed"])
            mid_draw.rounded_rectangle([(track_x1, bot_y1), (track_x2, bot_y2)], radius=bar_r, fill=palette["track_recessed"])

            vol_pct = max(0.0, min(1.0, volume / 100.0))
            vol_x = int(track_x1 + track_w * vol_pct)
            if vol_x > track_x1:
                mid_draw.rounded_rectangle([(track_x1, top_y1), (vol_x, top_y2)], radius=bar_r, fill=palette["track_guide"])
                mid_draw.rounded_rectangle([(track_x1, bot_y1), (vol_x, bot_y2)], radius=bar_r, fill=palette["track_guide"])

            # Store pre-calculated layout in midground cache
            self._cached_wave_midground = mid_img
            self._cached_wave_midground_key = wave_midground_key
            self._wave_track_params = (track_x1, track_x2, track_w, top_y1, top_y2, bot_y1, bot_y2, bar_r, slider_y, vol_x, vol_pct)

        # Dynamic live frame overlay (VU meter & fader knob)
        img = self._cached_wave_midground.copy()
        draw = ImageDraw.Draw(img)

        track_x1, track_x2, track_w, top_y1, top_y2, bot_y1, bot_y2, bar_r, slider_y, vol_x, vol_pct = self._wave_track_params
        s = RENDER_SCALE

        # Live VU meter bars bounded strictly by volume thumb position (zero bleed contract)
        if not is_muted and self.get_live_meter() and (peak_l > 0.01 or peak_r > 0.01 or peak > 0.01) and vol_pct > 0.0:
            pl = max(peak_l, peak if peak_l <= 0.0 else 0.0)
            pr = max(peak_r, peak if peak_r <= 0.0 else 0.0)
            eff_l = min(vol_pct, pl)
            eff_r = min(vol_pct, pr)

            p65 = int(track_x1 + track_w * 0.65)
            p85 = int(track_x1 + track_w * 0.85)

            def draw_meter_channel(y1, y2, eff_val):
                m_x = min(vol_x, int(track_x1 + track_w * eff_val))
                if m_x <= track_x1:
                    return
                # Green zone (0% - 65%)
                gx2 = min(m_x, p65)
                if gx2 > track_x1:
                    draw.rounded_rectangle([(track_x1, y1), (gx2, y2)], radius=bar_r, fill=(61, 179, 86, 255))
                # Yellow zone (65% - 85%)
                if m_x > p65:
                    yx2 = min(m_x, p85)
                    draw.rectangle([(p65, y1), (yx2, y2)], fill=(242, 191, 51, 255))
                # Red zone (85% - 100%)
                if m_x > p85:
                    draw.rounded_rectangle([(p85, y1), (m_x, y2)], radius=bar_r, fill=(242, 76, 64, 255))

            draw_meter_channel(top_y1, top_y2, eff_l)
            draw_meter_channel(bot_y1, bot_y2, eff_r)

        # Draw Draggable Fader Knob on top
        thumb_r = int(6.5 * RENDER_SCALE)
        draw.ellipse([(vol_x - thumb_r, slider_y - thumb_r + int(1.5*s)), (vol_x + thumb_r, slider_y + thumb_r + int(1.5*s))], fill=(0, 0, 0, 90))
        knob_color = (75, 75, 85, 255) if is_muted else palette["knob_body"]
        draw.ellipse([(vol_x - thumb_r, slider_y - thumb_r), (vol_x + thumb_r, slider_y + thumb_r)], fill=knob_color, outline=palette["knob_border"], width=max(1, int(1.2 * RENDER_SCALE)))

        if RENDER_SCALE > 1:
            return img.resize((200, 100), Image.Resampling.BILINEAR)
        return img

    def generate_volume_image(self, volume: int, is_muted: bool, peak: float = 0.0, peak_l: float = 0.0, peak_r: float = 0.0) -> Image.Image:
        style = self.get_ui_style()
        if style == "dx":
            return self._generate_volume_image_dx(volume, is_muted, peak=peak)
        return self._generate_volume_image_wave(volume, is_muted, peak=peak, peak_l=peak_l, peak_r=peak_r)

    def _invalidate_all_caches(self):
        self._cached_base_bg = None
        self._cached_base_bg_key = None
        self._cached_midground = None
        self._cached_midground_key = None
        self._cached_wave_midground = None
        self._cached_wave_midground_key = None
        self._cached_badge_images = {}
        self._gauge_gradient_img = None
        self._gauge_gradient_img_sub = None

    def get_base_config_rows(self) -> list:
        settings = self.get_settings() or {}
        rows = []

        # 1. UI Style (Wave / DX)
        self.ui_style_model = Gtk.StringList()
        self.ui_style_model.append("Wave (Default)")
        self.ui_style_model.append("DX (Dial)")
        self.ui_style_selector = Adw.ComboRow(
            model=self.ui_style_model,
            title="UI Style",
            subtitle="Display style matching WaveController app or DX dial"
        )
        current_style = self.get_ui_style()
        self.ui_style_selector.set_selected(0 if current_style == "wave" else 1)
        def on_style_changed(combo, *args):
            s = self.get_settings() or {}
            s["ui_style"] = "wave" if combo.get_selected() == 0 else "dx"
            self.set_settings(s)
            self._invalidate_all_caches()
            self.update_ui_rendering(force=True)
        self.ui_style_selector.connect("notify::selected", on_style_changed)
        rows.append(self.ui_style_selector)

        # 2. Appearance / Theme (Midnight / Follow System)
        self.appearance_model = Gtk.StringList()
        self.appearance_model.append("Midnight Dark (Default)")
        self.appearance_model.append("Follow System Theme")
        self.appearance_selector = Adw.ComboRow(
            model=self.appearance_model,
            title="Appearance",
            subtitle="Theme palette for card background and surfaces"
        )
        current_app = self.get_appearance()
        self.appearance_selector.set_selected(0 if current_app == "midnight" else 1)
        def on_appearance_changed(combo, *args):
            s = self.get_settings() or {}
            s["appearance"] = "midnight" if combo.get_selected() == 0 else "system"
            self.set_settings(s)
            self._invalidate_all_caches()
            self.update_ui_rendering(force=True)
        self.appearance_selector.connect("notify::selected", on_appearance_changed)
        rows.append(self.appearance_selector)

        # 3. Accent Color
        accents = [
            ("system", "Follow System (Auto)"),
            ("blue", "Blue"),
            ("purple", "Purple"),
            ("teal", "Teal"),
            ("green", "Green"),
            ("yellow", "Yellow"),
            ("orange", "Orange"),
            ("red", "Red"),
            ("pink", "Pink"),
            ("slate", "Slate"),
        ]
        self.accent_model = Gtk.StringList()
        for _, name in accents:
            self.accent_model.append(name)
        self.accent_selector = Adw.ComboRow(
            model=self.accent_model,
            title="Accent Color",
            subtitle="Color for fader knob, badges, and active accents"
        )
        curr_accent = self.get_accent_color()
        acc_idx = 0
        for i, (k, _) in enumerate(accents):
            if k == curr_accent:
                acc_idx = i
                break
        self.accent_selector.set_selected(acc_idx)
        def on_accent_changed(combo, *args):
            s = self.get_settings() or {}
            idx = combo.get_selected()
            if 0 <= idx < len(accents):
                s["accent_color"] = accents[idx][0]
                self.set_settings(s)
                self._invalidate_all_caches()
                self.update_ui_rendering(force=True)
        self.accent_selector.connect("notify::selected", on_accent_changed)
        rows.append(self.accent_selector)

        # 4. Volume Step Size
        step_sizes = ["1%", "2%", "5%", "10%"]
        self.step_model = Gtk.StringList()
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
        rows.append(self.step_selector)

        # 5. Volume Display Format
        self.vol_format_model = Gtk.StringList()
        self.vol_format_model.append("Percentage (%)")
        self.vol_format_model.append("Decibels (dB)")
        self.vol_format_selector = Adw.ComboRow(
            model=self.vol_format_model,
            title="Volume Display Format"
        )
        vol_format = settings.get("volume_format", "percent")
        self.vol_format_selector.set_selected(0 if vol_format == "percent" else 1)
        def on_format_changed(combo, *args):
            s = self.get_settings() or {}
            s["volume_format"] = "percent" if combo.get_selected() == 0 else "db"
            self.set_settings(s)
            self._invalidate_all_caches()
            self.update_ui_rendering(force=True)
        self.vol_format_selector.connect("notify::selected", on_format_changed)
        rows.append(self.vol_format_selector)

        # 6. Live Peak Meter Toggle
        self.live_meter_row = Adw.SwitchRow(
            title="Live Peak Meter"
        )
        self.live_meter_row.set_active(self.get_live_meter())
        def on_meter_toggled(switch, *args):
            s = self.get_settings() or {}
            s["live_meter"] = switch.get_active()
            self.set_settings(s)
            self._invalidate_all_caches()
            self.update_ui_rendering(force=True)
        self.live_meter_row.connect("notify::active", on_meter_toggled)
        rows.append(self.live_meter_row)

        # 7. Badge Display Style (Icon & Text, Badge Only, Text Only)
        self.badge_style_model = Gtk.StringList()
        self.badge_style_model.append("Icon & Text (Pill)")
        self.badge_style_model.append("Badge Only (Icon)")
        self.badge_style_model.append("Text Only")
        self.badge_style_selector = Adw.ComboRow(
            model=self.badge_style_model,
            title="Badge Display Style",
            subtitle="Display badge as pill with text, compact icon only, or text only"
        )
        curr_b_style = settings.get("badge_style", "icon_text")
        b_idx = 0 if curr_b_style == "icon_text" else (1 if curr_b_style == "icon_only" else 2)
        self.badge_style_selector.set_selected(b_idx)
        def on_badge_style_changed(combo, *args):
            s = self.get_settings() or {}
            sel = combo.get_selected()
            s["badge_style"] = "icon_text" if sel == 0 else ("icon_only" if sel == 1 else "text_only")
            self.set_settings(s)
            self._invalidate_all_caches()
            self.update_ui_rendering(force=True)
        self.badge_style_selector.connect("notify::selected", on_badge_style_changed)
        rows.append(self.badge_style_selector)

        return rows

    def update_ui_rendering(self, peak: float = 0.0, peak_l: float = 0.0, peak_r: float = 0.0, force: bool = False):
        if not force and hasattr(self, "get_is_present") and not self.get_is_present():
            return
            
        now = time.time()
        # Cap frame rate to ~30 FPS per dial to prevent USB HID pipe saturation
        if not force and (now - getattr(self, "_last_render_time", 0.0) < 0.033):
            return

        is_adjusting = (now - self._last_volume_adjust_time) < ADJUSTING_TIMEOUT
        adjust_changed = (is_adjusting != getattr(self, "_last_drawn_adjusting", False))
        
        vol_changed = (self.current_volume != self.last_drawn_volume)
        mute_changed = (self.last_mute != self.last_drawn_mute)
        peak_changed = (abs(peak - self.last_drawn_peak) > 0.012) or (abs(self._peak_hold_val - self.last_drawn_hold) > 0.02) or (abs(peak_l - getattr(self, "last_drawn_peak_l", 0.0)) > 0.012) or (abs(peak_r - getattr(self, "last_drawn_peak_r", 0.0)) > 0.012)
        
        settings = self.get_settings() or {}
        custom_icon = settings.get("custom_icon", "")
        target_title, target_subtitle = self.get_target_title_and_subtitle()
        title_text = target_title
        if title_text.startswith("Elgato "):
            title_text = title_text[len("Elgato "):]
        effective_icon = custom_icon if custom_icon else self.get_target_icon_path()
        
        title_changed = (title_text != getattr(self, "last_drawn_title", None))
        subtitle_changed = (target_subtitle != getattr(self, "last_drawn_subtitle", None))
        icon_changed = (effective_icon != getattr(self, "last_drawn_icon", None))
        
        telemetry_info = self.get_hardware_telemetry_info()
        telemetry_changed = (telemetry_info != getattr(self, "last_drawn_telemetry", None))
        
        ui_style = self.get_ui_style()
        style_changed = (ui_style != getattr(self, "last_drawn_ui_style", None))
        
        badge_style = self.get_badge_style()
        badge_style_changed = (badge_style != getattr(self, "last_drawn_badge_style", None))
        
        if force or vol_changed or mute_changed or adjust_changed or title_changed or subtitle_changed or icon_changed or telemetry_changed or style_changed or badge_style_changed or (peak_changed and not is_adjusting):
            with self._render_lock:
                self._last_render_time = now
                self.last_drawn_volume = self.current_volume
                self.last_drawn_mute = self.last_mute
                self.last_drawn_peak = peak
                self.last_drawn_peak_l = peak_l
                self.last_drawn_peak_r = peak_r
                self.last_drawn_hold = self._peak_hold_val
                self._last_drawn_adjusting = is_adjusting
                self.last_drawn_title = title_text
                self.last_drawn_subtitle = target_subtitle
                self.last_drawn_icon = effective_icon
                self.last_drawn_telemetry = telemetry_info
                self.last_drawn_ui_style = ui_style
                self.last_drawn_badge_style = badge_style
                
                img = self.generate_volume_image(self.current_volume, self.last_mute, peak=peak, peak_l=peak_l, peak_r=peak_r)
                try:
                    self.set_media(image=img)
                except Exception:
                    try:
                        self.set_media(img)
                    except Exception:
                        GLib.idle_add(self.set_media, img)

