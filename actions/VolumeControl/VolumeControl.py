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

class VolumeControl(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.running = False
        self.last_volume = -1
        self.last_mute = None
        self.poll_thread = None

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

    def generate_volume_image(self, volume: int, is_muted: bool) -> Image.Image:
        # Stream Deck + screen segments are 200x100 pixels
        width, height = 200, 100
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # 1. Background (solid premium dark charcoal, matching g19.png)
        draw.rectangle([(0, 0), (width, height)], fill=(28, 28, 28, 255))
        
        # 2. Header
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
        
        # 3. Dial Geometry
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
                
        # Draw Inner Knob Core (Outer shadow/border for 3D bevel look)
        draw.ellipse([(cx - r_core_outer, cy - r_core_outer), (cx + r_core_outer, cy + r_core_outer)], fill=(18, 18, 20, 255))
        # Inner circle of the core
        draw.ellipse([(cx - r_core_inner, cy - r_core_inner), (cx + r_core_inner, cy + r_core_inner)], fill=(28, 28, 32, 255), outline=(60, 62, 72, 255), width=1)
        
        # Draw Pointer
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
            
        # Connect changes to save settings
        self.mixer_row.connect("notify::text", self.on_mixer_changed)
        self.step_selector.connect("notify::selected-item", self.on_step_changed)
        
        return [self.mixer_row, self.step_selector]

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
