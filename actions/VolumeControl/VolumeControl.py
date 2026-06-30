# Import StreamController modules
from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport

# Import python modules
import os
import subprocess
import threading
import time
import math
from PIL import Image, ImageDraw, ImageFont

# Import gtk modules - used for the config rows
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
import globals as gl

class VolumeControl(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.running = False
        self.last_volume = -1
        self.last_mute = None
        self.poll_thread = None
        self.bg_image = None
        self.knob_image = None

    def on_ready(self) -> None:
        self.running = True
        self.poll_thread = threading.Thread(target=self.poll_volume_loop, daemon=True)
        self.poll_thread.start()

    def on_remove(self) -> None:
        self.running = False

    def on_disconnect(self) -> None:
        self.running = False

    def event_callback(self, event: InputEvent, data: dict = None):
        if event == Input.Dial.Events.TURN_CW:
            self.change_volume(self.get_step_size())
        elif event == Input.Dial.Events.TURN_CCW:
            self.change_volume(-self.get_step_size())
        elif event in [Input.Dial.Events.DOWN, Input.Dial.Events.SHORT_TOUCH_PRESS]:
            self.toggle_mute()
        else:
            super().event_callback(event, data)

    def get_mixer_name(self) -> str:
        settings = self.get_settings()
        if settings is not None:
            return settings.get("mixer_name", "Master")
        return "Master"

    def get_step_size(self) -> int:
        settings = self.get_settings()
        if settings is not None:
            val = settings.get("step_size", "5%")
            try:
                return int(val.replace("%", ""))
            except ValueError:
                return 5
        return 5

    def get_system_volume_status(self):
        mixer = self.get_mixer_name()
        try:
            output = subprocess.check_output(["amixer", "sget", mixer], text=True)
            volume = 0
            mute = False
            for line in output.splitlines():
                if "Playback" in line and "[" in line:
                    # Extract volume percentage
                    start_vol = line.find("[")
                    end_vol = line.find("]", start_vol)
                    if start_vol != -1 and end_vol != -1:
                        vol_str = line[start_vol+1:end_vol].replace("%", "")
                        if vol_str.isdigit():
                            volume = int(vol_str)
                    
                    # Extract mute status, look for [on] or [off]
                    start_mute = line.find("[", end_vol+1)
                    end_mute = line.find("]", start_mute)
                    if start_mute != -1 and end_mute != -1:
                        mute_str = line[start_mute+1:end_mute]
                        mute = (mute_str == "off")
                    break
            return volume, mute
        except Exception:
            return 50, False

    def change_volume(self, delta: int) -> None:
        mixer = self.get_mixer_name()
        sign = "+" if delta >= 0 else "-"
        cmd = f"{abs(delta)}%{sign}"
        try:
            subprocess.run(["amixer", "sset", mixer, cmd], check=True)
            self.update_volume_status()
        except Exception:
            pass

    def toggle_mute(self) -> None:
        mixer = self.get_mixer_name()
        try:
            subprocess.run(["amixer", "sset", mixer, "toggle"], check=True)
            self.update_volume_status()
        except Exception:
            pass

    def poll_volume_loop(self) -> None:
        while self.running:
            try:
                self.update_volume_status()
            except Exception:
                pass
            time.sleep(0.3)

    def update_volume_status(self) -> None:
        if not self.get_is_present():
            return
        
        volume, mute = self.get_system_volume_status()
        if volume != self.last_volume or mute != self.last_mute:
            self.last_volume = volume
            self.last_mute = mute
            
            # Generate the volume display image
            img = self.generate_volume_image(volume, mute)
            
            # Use GLib.idle_add to update UI thread-safely in GTK
            GLib.idle_add(self.set_media, img)

    def load_icon_image(self, path: str) -> Image.Image | None:
        if not path or not os.path.exists(path):
            return None
        try:
            if path.endswith(".svg"):
                return gl.media_manager.generate_svg_thumbnail(path)
            else:
                return Image.open(path)
        except Exception:
            return None

    def generate_volume_image(self, volume: int, is_muted: bool) -> Image.Image:
        width, height = 200, 100
        
        # 1. Load Background Image (lazily cached)
        if self.bg_image is None:
            bg_path = os.path.join(self.plugin_base.PATH, "assets", "background-volume.png")
            try:
                self.bg_image = Image.open(bg_path).convert("RGBA")
            except Exception:
                pass
                
        if self.bg_image is not None:
            img = self.bg_image.copy()
        else:
            img = Image.new("RGBA", (width, height), (28, 28, 28, 255))
            
        draw = ImageDraw.Draw(img)
        
        # 2. Header
        settings = self.get_settings() or {}
        custom_icon_path = settings.get("custom_icon", "")
        icon_scale = settings.get("icon_scale", 1.0)
        
        icon_drawn = False
        if custom_icon_path:
            icon_img = self.load_icon_image(custom_icon_path)
            if icon_img is not None:
                icon_img = icon_img.convert("RGBA")
                base_size = 20  # Speaker container base size
                scaled_size = int(base_size * icon_scale)
                scaled_size = max(4, min(scaled_size, 60))
                icon_img = icon_img.resize((scaled_size, scaled_size))
                
                # Center around the speaker area (spk_x=22, spk_y=17)
                cx_icon, cy_icon = 22, 17
                x_start = cx_icon - scaled_size // 2
                y_start = cy_icon - scaled_size // 2
                
                if is_muted:
                    r, g, b, a = icon_img.split()
                    a = a.point(lambda i: int(i * 0.4))
                    icon_img = Image.merge("RGBA", (r, g, b, a))
                
                img.paste(icon_img, (x_start, y_start), icon_img)
                
                if is_muted:
                    # Red diagonal mute line over the icon
                    draw.line([(x_start - 2, y_start - 2), (x_start + scaled_size + 2, y_start + scaled_size + 2)], fill=(239, 68, 68, 255), width=2)
                icon_drawn = True

        if not icon_drawn:
            # Speaker Icon (custom slate-blue speaker with cyan/blue waves)
            spk_x, spk_y = 12, 10
            spk_color = (90, 105, 120, 255) if is_muted else (110, 130, 150, 255)
            
            # Speaker body
            draw.rectangle([(spk_x, spk_y + 4), (spk_x + 5, spk_y + 10)], fill=spk_color)
            # Speaker cone
            draw.polygon([(spk_x + 5, spk_y + 4), (spk_x + 10, spk_y + 0), (spk_x + 10, spk_y + 14), (spk_x + 5, spk_y + 10)], fill=spk_color)
            
            if is_muted:
                # Red diagonal mute line
                draw.line([(spk_x - 2, spk_y + 2), (spk_x + 16, spk_y + 12)], fill=(239, 68, 68, 255), width=2)
            else:
                # Waves in bright blue/cyan (3 waves matching g19.png)
                wave_color = (0, 168, 255, 255)
                # Wave 1 (small)
                draw.arc([(spk_x + 3, spk_y + 2), (spk_x + 13, spk_y + 12)], start=-45, end=45, fill=wave_color, width=2)
                # Wave 2 (medium)
                draw.arc([(spk_x, spk_y - 1), (spk_x + 18, spk_y + 15)], start=-45, end=45, fill=wave_color, width=2)
                # Wave 3 (large)
                draw.arc([(spk_x - 3, spk_y - 4), (spk_x + 23, spk_y + 18)], start=-45, end=45, fill=wave_color, width=2)
            
        # Fonts
        font_path_regular = "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf"
        font_path_bold = "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"
        try:
            font_title = ImageFont.truetype(font_path_bold, 13)
            font_vol = ImageFont.truetype(font_path_bold, 18)
        except Exception:
            font_title = ImageFont.load_default()
            font_vol = ImageFont.load_default()
            
        # Title Text (light silver, bold)
        title_text = self.get_mixer_name()
        if len(title_text) > 12:
            title_text = title_text[:10] + ".."
        draw.text((38, 9), title_text, font=font_title, fill=(220, 222, 230, 255))
        
        # Volume Text (bold white)
        vol_text = "MUTE" if is_muted else f"{volume}%"
        vol_color = (239, 68, 68, 255) if is_muted else (255, 255, 255, 255)
        try:
            text_w = font_vol.getlength(vol_text)
        except Exception:
            text_w = 40
        draw.text((188 - text_w, 5), vol_text, font=font_vol, fill=vol_color)
        
        # 3. Dial Geometry (Restored to manual knob core layout)
        cx, cy = 100, 92
        r_tick_start, r_tick_end = 55, 63
        r_arc = 46
        r_core_outer = 37
        r_core_inner = 34
        r_pt_start, r_pt_end = 16, 29
        
        # Draw Ticks (arranged semi-circularly from 180 to 360 degrees)
        for tick_angle in range(180, 361, 18):
            rad = math.radians(tick_angle)
            x1 = cx + r_tick_start * math.cos(rad)
            y1 = cy + r_tick_start * math.sin(rad)
            x2 = cx + r_tick_end * math.cos(rad)
            y2 = cy + r_tick_end * math.sin(rad)
            draw.line([(x1, y1), (x2, y2)], fill=(130, 132, 140, 255), width=2)
            
        # Draw Gauge Track (inactive - dark background arc)
        bbox = [(cx - r_arc, cy - r_arc), (cx + r_arc, cy + r_arc)]
        draw.arc(bbox, start=180, end=360, fill=(38, 38, 42, 255), width=7)
        
        # Draw Active Gauge Segment
        end_angle = 180 + 180 * (volume / 100.0)
        
        if not is_muted and volume > 0:
            # Segment-by-segment gradient drawing
            for angle in range(180, int(end_angle)):
                pct = (angle - 180) / 180.0
                # Green (0, 180, 0) to Yellow (235, 220, 0) to Orange/Red (255, 60, 0)
                if pct < 0.5:
                    t = pct / 0.5
                    r_col = int(0 + 235 * t)
                    g_col = int(180 + 40 * t)
                    b_col = 0
                else:
                    t = (pct - 0.5) / 0.5
                    r_col = int(235 + 20 * t)
                    g_col = int(220 - 160 * t)
                    b_col = 0
                    
                draw.arc(bbox, start=angle, end=angle+2, fill=(r_col, g_col, b_col, 255), width=7)
                
        # 4. Draw Inner Knob Core (Outer shadow/border for 3D bevel look)
        draw.ellipse([(cx - r_core_outer, cy - r_core_outer), (cx + r_core_outer, cy + r_core_outer)], fill=(18, 18, 20, 255))
        # Inner circle of the core
        draw.ellipse([(cx - r_core_inner, cy - r_core_inner), (cx + r_core_inner, cy + r_core_inner)], fill=(28, 28, 32, 255), outline=(60, 62, 72, 255), width=1)
        
        # 5. Draw Pointer line on top of the knob
        pointer_angle = end_angle
        rad_pt = math.radians(pointer_angle)
        xp1 = cx + r_pt_start * math.cos(rad_pt)
        yp1 = cy + r_pt_start * math.sin(rad_pt)
        xp2 = cx + r_pt_end * math.cos(rad_pt)
        yp2 = cy + r_pt_end * math.sin(rad_pt)
        pointer_color = (239, 68, 68, 255) if is_muted else (240, 242, 250, 255)
        draw.line([(xp1, yp1), (xp2, yp2)], fill=pointer_color, width=3)
        
        return img

    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        # 1. Mixer name selector
        self.mixer_row = Adw.EntryRow(
            title="Mixer Device Name",
            text=self.get_mixer_name()
        )
        
        # 2. Step size selector
        self.step_model = Gtk.StringList()
        step_sizes = ["1%", "2%", "5%", "10%"]
        for size in step_sizes:
            self.step_model.append(size)
            
        self.step_selector = Adw.ComboRow(
            model=self.step_model,
            title="Volume Step Size",
            subtitle="Volume change per dial tick"
        )
        
        # Set default selection
        current_step = f"{self.get_step_size()}%"
        if current_step in step_sizes:
            self.step_selector.set_selected(step_sizes.index(current_step))
        else:
            self.step_selector.set_selected(2) # Default to 5%
            
        # 3. Custom Icon selection
        self.icon_row = Adw.ActionRow(
            title="Custom Icon",
            subtitle="Select a custom icon to display"
        )
        
        self.choose_icon_button = Gtk.Button.new_from_icon_name("document-open-symbolic")
        self.choose_icon_button.set_valign(Gtk.Align.CENTER)
        self.icon_row.add_suffix(self.choose_icon_button)
        
        self.clear_icon_button = Gtk.Button.new_from_icon_name("edit-clear-symbolic")
        self.clear_icon_button.set_valign(Gtk.Align.CENTER)
        self.icon_row.add_suffix(self.clear_icon_button)
        
        # 4. Icon Scale slider
        self.scale_row = Adw.ActionRow(
            title="Icon Scale",
            subtitle="Change the custom icon's display size"
        )
        
        settings = self.get_settings() or {}
        current_scale = settings.get("icon_scale", 1.0)
        
        self.scale_adj = Gtk.Adjustment.new(current_scale, 0.4, 2.0, 0.05, 0.1, 0.0)
        self.scale_slider = Gtk.Scale.new(Gtk.Orientation.HORIZONTAL, self.scale_adj)
        self.scale_slider.set_draw_value(True)
        self.scale_slider.set_hexpand(True)
        self.scale_slider.set_valign(Gtk.Align.CENTER)
        self.scale_slider.set_size_request(150, -1)
        self.scale_row.add_suffix(self.scale_slider)
        
        # Connect changes to save settings
        self.mixer_row.connect("notify::text", self.on_mixer_changed)
        self.step_selector.connect("notify::selected-item", self.on_step_changed)
        self.choose_icon_button.connect("clicked", self.on_choose_icon_clicked)
        self.clear_icon_button.connect("clicked", self.on_clear_icon_clicked)
        self.scale_slider.connect("value-changed", self.on_scale_changed)
        
        # Update clear button sensitivity
        icon_path = settings.get("custom_icon", "")
        self.clear_icon_button.set_sensitive(bool(icon_path))
        
        return [self.mixer_row, self.step_selector, self.icon_row, self.scale_row]

    def on_mixer_changed(self, entry, *args):
        settings = self.get_settings()
        if settings is None:
            settings = {}
        settings["mixer_name"] = entry.get_text()
        self.set_settings(settings)
        # Clear cache to force refresh on next check
        self.last_volume = -1
        self.last_mute = None

    def on_step_changed(self, combo, *args):
        settings = self.get_settings()
        if settings is None:
            settings = {}
        selected_item = combo.get_selected_item()
        if selected_item is not None:
            settings["step_size"] = selected_item.get_string()
            self.set_settings(settings)

    def on_choose_icon_clicked(self, button):
        settings = self.get_settings() or {}
        current_val = settings.get("custom_icon", "")
        
        def on_select_callback(path):
            if not path:
                return
            settings = self.get_settings() or {}
            settings["custom_icon"] = path
            self.set_settings(settings)
            
            # Update UI on select
            self.clear_icon_button.set_sensitive(True)
            self.last_volume = -1  # force redraw
            self.last_mute = None
            self.update_volume_status()
            
        GLib.idle_add(gl.app.let_user_select_asset, current_val, on_select_callback)

    def on_clear_icon_clicked(self, button):
        settings = self.get_settings() or {}
        settings["custom_icon"] = ""
        self.set_settings(settings)
        
        self.clear_icon_button.set_sensitive(False)
        self.last_volume = -1  # force redraw
        self.last_mute = None
        self.update_volume_status()

    def on_scale_changed(self, slider):
        settings = self.get_settings() or {}
        settings["icon_scale"] = slider.get_value()
        self.set_settings(settings)
        
        self.last_volume = -1  # force redraw
        self.last_mute = None
        self.update_volume_status()
