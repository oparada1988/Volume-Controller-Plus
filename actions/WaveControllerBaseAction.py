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
from PIL import Image, ImageDraw, ImageFont

# Import gtk modules
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, Adw, GLib

from .WaveControllerClient import WaveControllerClient

RENDER_SCALE = 2

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
        self._current_peak = 0.0
        self._peak_hold_val = 0.0
        self._peak_hold_time = 0.0
        self._last_volume_adjust_time = 0.0
        self._last_drawn_adjusting = False

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

    def event_callback(self, event: InputEvent, data: dict = None):
        if event == Input.Dial.Events.TURN_CW:
            step_val = self.get_step_size()
            self.handle_volume_change(step_val)
        elif event == Input.Dial.Events.TURN_CCW:
            step_val = self.get_step_size()
            self.handle_volume_change(-step_val)
        elif event in (Input.Dial.Events.DOWN, Input.Dial.Events.SHORT_UP, Input.Dial.Events.SHORT_TOUCH_PRESS):
            self.handle_mute_toggle()
        elif hasattr(Input, "Touchscreen") and hasattr(Input.Touchscreen, "Events") and event in (
            getattr(Input.Touchscreen.Events, "SHORT_PRESS", None),
            getattr(Input.Touchscreen.Events, "TAP", None)
        ):
            self.handle_mute_toggle()

    def on_tick_update(self) -> bool:
        if not self.running:
            return False

        now = time.time()
        # Fast 80ms poll from WaveController for 1:1 fader responsiveness
        if now - self.last_poll_time > 0.08:
            self.last_poll_time = now
            self.initial_load_status()

        # Update live VU peak
        if self.get_live_meter() and not self.last_mute:
            raw_peak = self.get_current_peak_val()
            
            # Smooth responsive attack + graceful exponential fade-to-zero
            if raw_peak > self._current_peak:
                self._current_peak = min(1.0, self._current_peak + (raw_peak - self._current_peak) * 0.75)
            else:
                self._current_peak = max(0.0, self._current_peak * 0.92 - 0.002)
                if self._current_peak < 0.002:
                    self._current_peak = 0.0

            # Peak hold marker decay
            if self._current_peak >= self._peak_hold_val:
                self._peak_hold_val = self._current_peak
                self._peak_hold_time = now
            elif (now - self._peak_hold_time) > 1.2:
                self._peak_hold_val = max(self._current_peak, self._peak_hold_val - 0.03)

            self.update_ui_rendering(peak=self._current_peak)
        else:
            # Gracefully fade out any residual peak when muted or stopped
            if self._current_peak > 0.0:
                self._current_peak = max(0.0, self._current_peak * 0.88 - 0.004)
                if self._current_peak < 0.002:
                    self._current_peak = 0.0
                self.update_ui_rendering(peak=self._current_peak)
            else:
                self.update_ui_rendering(peak=0.0)

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

    def resolve_icon_as_pil(self, icon_identifier: str, target_size: int = 54) -> Image.Image:
        """Resolves an icon name or file path to a crisp PIL Image using GTK IconTheme or local assets."""
        if not icon_identifier:
            return None

        # 1. Direct valid image file on disk
        if os.path.exists(icon_identifier):
            try:
                if icon_identifier.endswith(".svg"):
                    pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_identifier, target_size, target_size, True)
                    ok, buf = pix.save_to_bufferv("png")
                    return Image.open(io.BytesIO(buf)).convert("RGBA")
                else:
                    return Image.open(icon_identifier).convert("RGBA")
            except Exception:
                pass

        # 2. Check plugin bundled assets
        if hasattr(self, "plugin_base") and hasattr(self.plugin_base, "PATH"):
            for candidate in [
                icon_identifier,
                f"{icon_identifier}.png",
                f"{icon_identifier}.svg",
                os.path.join("assets", icon_identifier),
                os.path.join("assets", f"{icon_identifier}.png")
            ]:
                asset_file = os.path.join(self.plugin_base.PATH, candidate)
                if os.path.exists(asset_file):
                    try:
                        if asset_file.endswith(".svg"):
                            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(asset_file, target_size, target_size, True)
                            ok, buf = pix.save_to_bufferv("png")
                            return Image.open(io.BytesIO(buf)).convert("RGBA")
                        else:
                            return Image.open(asset_file).convert("RGBA")
                    except Exception:
                        pass

        # 3. Look up via Freedesktop GTK IconTheme
        try:
            display = Gdk.Display.get_default()
            if display:
                theme = Gtk.IconTheme.get_for_display(display)
                if theme.has_icon(icon_identifier):
                    paintable = theme.lookup_icon(icon_identifier, None, target_size, 1, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.NONE)
                    if paintable and paintable.get_file():
                        path = paintable.get_file().get_path()
                        if path and os.path.exists(path):
                            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, target_size, target_size, True)
                            ok, buf = pix.save_to_bufferv("png")
                            return Image.open(io.BytesIO(buf)).convert("RGBA")
        except Exception:
            pass

        return None

    def generate_volume_image(self, volume: int, is_muted: bool, peak: float = 0.0) -> Image.Image:
        width, height = 200 * RENDER_SCALE, 100 * RENDER_SCALE
        
        # 1. Base Background with Ticks & Gauge Track
        if self._cached_base_bg is None:
            bg_path = os.path.join(self.plugin_base.PATH, "assets", "background-volume.png")
            try:
                self.bg_image = Image.open(bg_path).convert("RGBA")
            except Exception:
                pass
                    
            if self.bg_image is not None:
                bg = self.bg_image.resize((width, height), Image.Resampling.LANCZOS)
            else:
                bg = Image.new("RGBA", (width, height), (30, 30, 32, 255))
                
            bg_draw = ImageDraw.Draw(bg)
            
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
                    tick_color = (150, 152, 160, 255)
                else:
                    r_start = r_minor_start
                    r_end = r_minor_end
                    tick_w = int(1.5 * RENDER_SCALE)
                    tick_color = (105, 107, 115, 255)

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

            track_color = (20, 20, 24, 255)
            bg_draw.arc(bbox_bg, start=210, end=330, fill=track_color, width=arc_w)
            for cap_angle in (210, 330):
                rad_cap = math.radians(cap_angle)
                xc = cx_bg + r_arc_center * math.cos(rad_cap)
                yc = cy_bg + r_arc_center * math.sin(rad_cap)
                bg_draw.ellipse([(xc - cap_r, yc - cap_r), (xc + cap_r, yc + cap_r)], fill=track_color)
            
            self._cached_base_bg = bg

        # 2. Resolve Labels, Fonts & Cache Keys
        settings = self.get_settings() or {}
        volume_format = settings.get("volume_format", "percent")
        custom_name = settings.get("custom_name", "")
        custom_icon_path = settings.get("custom_icon", "")
        
        target_title, target_subtitle = self.get_target_title_and_subtitle()
        title_text = custom_name if custom_name else target_title
        
        effective_icon_identifier = custom_icon_path if custom_icon_path else self.get_target_icon_path()
        font_name = settings.get("font_name", "DejaVu Sans Bold 15")
        font_path = settings.get("font_path", "")

        midground_key = (
            volume,
            is_muted,
            title_text,
            effective_icon_identifier,
            volume_format,
            font_name,
            font_path
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
                else:
                    self._cached_font_title = ImageFont.load_default()
                    self._cached_font_vol = ImageFont.load_default()
            except Exception:
                self._cached_font_title = ImageFont.load_default()
                self._cached_font_vol = ImageFont.load_default()

            font_title = self._cached_font_title
            font_vol = self._cached_font_vol

            try:
                mid_draw.text((165 * RENDER_SCALE, 64 * RENDER_SCALE), vol_text, font=font_vol, fill=vol_color, anchor="mm")
            except TypeError:
                mid_draw.text((int((165 - 20) * RENDER_SCALE), int((64 - 10) * RENDER_SCALE)), vol_text, font=font_vol, fill=vol_color)

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
            right_bound = 195
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
            is_adjusting = (now - self._last_volume_adjust_time) < 1.2
            
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
                    if peak > 0.04:
                        scaled_peak = peak * (volume / 100.0)
                        peak_angle = int(210 + 120 * min(1.0, scaled_peak))
                        if peak_angle > 210:
                            rad_e = math.radians(min(330, peak_angle))
                            xe = cx + r_arc_center * math.cos(rad_e)
                            ye = cy + r_arc_center * math.sin(rad_e)
                            
                            if peak >= 0.99 or scaled_peak >= 0.99:
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

                    # Peak Hold Marker
                    if self._peak_hold_val > 0.04:
                        scaled_hold = self._peak_hold_val * (volume / 100.0)
                        hold_angle = int(210 + 120 * min(1.0, scaled_hold))
                        if hold_angle > 210:
                            draw.arc(bbox, start=max(210, hold_angle - 1), end=min(330, hold_angle + 1), fill=(255, 75, 75, 255), width=arc_w)
                else:
                    vol_angle = int(210 + 120 * (volume / 100.0))
                    if vol_angle > 210:
                        draw.arc(bbox, start=210, end=vol_angle, fill=(0, 168, 255, 255), width=arc_w)
                        rad_e = math.radians(vol_angle)
                        xe = cx + r_arc_center * math.cos(rad_e)
                        ye = cy + r_arc_center * math.sin(rad_e)
                        draw.ellipse([(xe - cap_r, ye - cap_r), (xe + cap_r, ye + cap_r)], fill=(0, 168, 255, 255))

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

    def update_ui_rendering(self, peak: float = 0.0, force: bool = False):
        if not force and hasattr(self, "get_is_present") and not self.get_is_present():
            return
            
        now = time.time()
        # Cap frame rate to ~30 FPS per dial to prevent USB HID pipe saturation
        if not force and (now - getattr(self, "_last_render_time", 0.0) < 0.033):
            return

        is_adjusting = (now - self._last_volume_adjust_time) < 1.2
        adjust_changed = (is_adjusting != getattr(self, "_last_drawn_adjusting", False))
        
        vol_changed = (self.current_volume != self.last_drawn_volume)
        mute_changed = (self.last_mute != self.last_drawn_mute)
        peak_changed = (abs(peak - self.last_drawn_peak) > 0.012) or (abs(self._peak_hold_val - self.last_drawn_hold) > 0.02)
        
        if force or vol_changed or mute_changed or adjust_changed or (peak_changed and not is_adjusting):
            with self._render_lock:
                self._last_render_time = now
                self.last_drawn_volume = self.current_volume
                self.last_drawn_mute = self.last_mute
                self.last_drawn_peak = peak
                self.last_drawn_hold = self._peak_hold_val
                self._last_drawn_adjusting = is_adjusting
                
                img = self.generate_volume_image(self.current_volume, self.last_mute, peak=peak)
                try:
                    self.set_media(image=img)
                except Exception:
                    try:
                        self.set_media(img)
                    except Exception:
                        GLib.idle_add(self.set_media, img)
