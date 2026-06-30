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

    def _raw_event_callback(self, event: InputEvent, data: dict = None):
        # Directly intercept physical dial turning and press events
        if event == Input.Dial.Events.TURN_CW:
            self.change_volume(self.get_step_size())
        elif event == Input.Dial.Events.TURN_CCW:
            self.change_volume(-self.get_step_size())
        elif event in [Input.Dial.Events.DOWN, Input.Dial.Events.SHORT_TOUCH_PRESS]:
            self.toggle_mute()
        else:
            super()._raw_event_callback(event, data)

    def event_callback(self, event: InputEvent, data: dict = None):
        # Intercept legacy callback in case EventManager configuration calls it
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

    def get_pipewire_devices(self) -> "tuple[list, list]":
        sinks = []
        sources = []
        try:
            output = subprocess.check_output(["wpctl", "status"], text=True)
            current_section = None
            for line in output.splitlines():
                line_strip = line.strip()
                if not line_strip:
                    continue
                if "Sinks:" in line:
                    current_section = "sinks"
                    continue
                elif "Sources:" in line:
                    current_section = "sources"
                    continue
                elif ":" in line and not any(s in line for s in ["Sinks", "Sources"]):
                    if current_section in ["sinks", "sources"]:
                        current_section = None
                        
                if current_section == "sinks":
                    parts = line_strip.split(".")
                    if len(parts) >= 2:
                        id_part = parts[0].replace("*", "").strip()
                        name_part = parts[1].split("[")[0].strip()
                        if id_part.isdigit():
                            sinks.append((id_part, name_part))
                elif current_section == "sources":
                    parts = line_strip.split(".")
                    if len(parts) >= 2:
                        id_part = parts[0].replace("*", "").strip()
                        name_part = parts[1].split("[")[0].strip()
                        if id_part.isdigit():
                            sources.append((id_part, name_part))
        except Exception:
            pass
        return sinks, sources

    def get_alsa_status(self) -> "tuple[int, bool]":
        mixer = self.get_mixer_name()
        try:
            output = subprocess.check_output(["amixer", "sget", mixer], text=True)
            volume = 0
            mute = False
            for line in output.splitlines():
                if "Playback" in line and "[" in line:
                    start_vol = line.find("[")
                    end_vol = line.find("]", start_vol)
                    if start_vol != -1 and end_vol != -1:
                        vol_str = line[start_vol+1:end_vol].replace("%", "")
                        if vol_str.isdigit():
                            volume = int(vol_str)
                    
                    start_mute = line.find("[", end_vol+1)
                    end_mute = line.find("]", start_mute)
                    if start_mute != -1 and end_mute != -1:
                        mute_str = line[start_mute+1:end_mute]
                        mute = (mute_str == "off")
                    break
            return volume, mute
        except Exception:
            return 50, False

    def get_pipewire_status(self, device_id: str) -> "tuple[int, bool]":
        try:
            output = subprocess.check_output(["wpctl", "get-volume", device_id], text=True).strip()
            parts = output.split()
            volume = 0
            mute = False
            if len(parts) >= 2:
                vol_str = parts[1]
                try:
                    volume = int(float(vol_str) * 100)
                except ValueError:
                    volume = 0
            if "[MUTED]" in output:
                mute = True
            return volume, mute
        except Exception:
            return 50, False

    def get_system_volume_status(self) -> "tuple[int, bool]":
        settings = self.get_settings() or {}
        backend = settings.get("audio_backend", "pipewire")
        if backend == "pipewire":
            device_id = settings.get("pipewire_device_id", "@DEFAULT_AUDIO_SINK@")
            return self.get_pipewire_status(device_id)
        else:
            return self.get_alsa_status()

    def change_alsa_volume(self, delta: int) -> None:
        mixer = self.get_mixer_name()
        sign = "+" if delta >= 0 else "-"
        cmd = f"{abs(delta)}%{sign}"
        try:
            subprocess.run(["amixer", "sset", mixer, cmd], check=True)
        except Exception:
            pass

    def change_pipewire_volume(self, device_id: str, delta: int) -> None:
        sign = "+" if delta >= 0 else "-"
        val = f"{abs(delta) / 100.0:.2f}{sign}"
        try:
            subprocess.run(["wpctl", "set-volume", "-l", "1.0", device_id, val], check=True)
        except Exception:
            pass

    def change_volume(self, delta: int) -> None:
        settings = self.get_settings() or {}
        backend = settings.get("audio_backend", "pipewire")
        if backend == "pipewire":
            device_id = settings.get("pipewire_device_id", "@DEFAULT_AUDIO_SINK@")
            self.change_pipewire_volume(device_id, delta)
        else:
            self.change_alsa_volume(delta)
            
        self.last_volume = -1
        self.last_mute = None
        self.update_volume_status()

    def toggle_alsa_mute(self) -> None:
        mixer = self.get_mixer_name()
        try:
            subprocess.run(["amixer", "sset", mixer, "toggle"], check=True)
        except Exception:
            pass

    def toggle_pipewire_mute(self, device_id: str) -> None:
        try:
            subprocess.run(["wpctl", "set-mute", device_id, "toggle"], check=True)
        except Exception:
            pass

    def toggle_mute(self) -> None:
        settings = self.get_settings() or {}
        backend = settings.get("audio_backend", "pipewire")
        if backend == "pipewire":
            device_id = settings.get("pipewire_device_id", "@DEFAULT_AUDIO_SINK@")
            self.toggle_pipewire_mute(device_id)
        else:
            self.toggle_alsa_mute()
            
        self.last_volume = -1
        self.last_mute = None
        self.update_volume_status()

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

        # Fonts
        font_path = settings.get("font_path", "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf")
        title_font_size = int(settings.get("title_font_size", 13))
        vol_font_size = 24  # Static size of 24px (made bigger as requested)
        try:
            if font_path and os.path.exists(font_path):
                font_title = ImageFont.truetype(font_path, title_font_size)
                font_vol = ImageFont.truetype(font_path, vol_font_size)
            else:
                fallback_path = "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"
                if os.path.exists(fallback_path):
                    font_title = ImageFont.truetype(fallback_path, title_font_size)
                    font_vol = ImageFont.truetype(fallback_path, vol_font_size)
                else:
                    font_title = ImageFont.load_default()
                    font_vol = ImageFont.load_default()
        except Exception:
            font_title = ImageFont.load_default()
            font_vol = ImageFont.load_default()

        # Calculate volume text width to determine boundaries
        vol_text = "MUTE" if is_muted else f"{volume}%"
        vol_color = (239, 68, 68, 255) if is_muted else (255, 255, 255, 255)
        
        try:
            vol_w = font_vol.getlength(vol_text)
        except Exception:
            vol_w = 48
            
        # Draw Volume Text (right-aligned, vertically centered at y=18)
        try:
            draw.text((188, 18), vol_text, font=font_vol, fill=vol_color, anchor="rm")
        except TypeError:
            draw.text((188 - vol_w, 18 - 12), vol_text, font=font_vol, fill=vol_color)
        
        # Icon placement area
        icon_drawn = False
        icon_w = 20  # Base width of the icon area
        if custom_icon_path:
            icon_img = self.load_icon_image(custom_icon_path)
            if icon_img is not None:
                icon_img = icon_img.convert("RGBA")
                base_size = 20  # Base size
                scaled_size = int(base_size * icon_scale)
                scaled_size = max(4, min(scaled_size, 60))
                icon_img = icon_img.resize((scaled_size, scaled_size))
                
                # Center vertically at y=18, left-aligned at x=12
                x_start = 12
                y_start = 18 - scaled_size // 2
                
                if is_muted:
                    r, g, b, a = icon_img.split()
                    a = a.point(lambda i: int(i * 0.4))
                    icon_img = Image.merge("RGBA", (r, g, b, a))
                
                img.paste(icon_img, (x_start, y_start), icon_img)
                
                if is_muted:
                    # Red diagonal mute line over the icon
                    draw.line([(x_start - 2, y_start - 2), (x_start + scaled_size + 2, y_start + scaled_size + 2)], fill=(239, 68, 68, 255), width=2)
                
                icon_drawn = True
                icon_w = scaled_size

        if not icon_drawn:
            # Default Speaker Icon (slate-blue speaker with cyan/blue waves)
            spk_x, spk_y = 12, 11
            spk_color = (90, 105, 120, 255) if is_muted else (110, 130, 150, 255)
            
            # Speaker body (centered vertically at y=18)
            draw.rectangle([(spk_x, spk_y + 4), (spk_x + 5, spk_y + 10)], fill=spk_color)
            # Speaker cone
            draw.polygon([(spk_x + 5, spk_y + 4), (spk_x + 10, spk_y + 0), (spk_x + 10, spk_y + 14), (spk_x + 5, spk_y + 10)], fill=spk_color)
            
            if is_muted:
                # Red diagonal mute line
                draw.line([(spk_x - 2, spk_y + 2), (spk_x + 16, spk_y + 12)], fill=(239, 68, 68, 255), width=2)
            else:
                # Waves in bright blue/cyan (3 waves matching g2.png)
                wave_color = (0, 168, 255, 255)
                # Wave 1 (small)
                draw.arc([(spk_x + 3, spk_y + 2), (spk_x + 13, spk_y + 12)], start=-45, end=45, fill=wave_color, width=2)
                # Wave 2 (medium)
                draw.arc([(spk_x, spk_y - 1), (spk_x + 18, spk_y + 15)], start=-45, end=45, fill=wave_color, width=2)
                # Wave 3 (large)
                draw.arc([(spk_x - 3, spk_y - 4), (spk_x + 23, spk_y + 18)], start=-45, end=45, fill=wave_color, width=2)
            icon_w = 26  # default speaker width including waves

        # Draw Title Text (centered horizontally in the remaining header space between icon and volume text)
        backend = settings.get("audio_backend", "pipewire")
        if backend == "pipewire":
            title_text = settings.get("pipewire_device_name", "Default Sink")
        else:
            title_text = self.get_mixer_name()
            
        if len(title_text) > 16:
            title_text = title_text[:14] + ".."
            
        left_bound = 12 + icon_w + 6
        right_bound = 188 - vol_w - 6
        center_x = left_bound + (right_bound - left_bound) // 2
        
        try:
            draw.text((center_x, 18), title_text, font=font_title, fill=(220, 222, 230, 255), anchor="mm")
        except TypeError:
            try:
                title_w = font_title.getlength(title_text)
            except Exception:
                title_w = len(title_text) * 7
            draw.text((center_x - title_w // 2, 18 - 7), title_text, font=font_title, fill=(220, 222, 230, 255))
        
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

    def get_font_path(self) -> str:
        settings = self.get_settings()
        if settings is not None:
            return settings.get("font_path", "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf")
        return "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"

    def get_title_font_size(self) -> int:
        settings = self.get_settings()
        if settings is not None:
            return int(settings.get("title_font_size", 13))
        return 13

    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        settings = self.get_settings() or {}
        backend = settings.get("audio_backend", "pipewire")

        # 1. Backend Selector
        self.backend_model = Gtk.StringList()
        self.backend_model.append("PipeWire (wpctl)")
        self.backend_model.append("ALSA (amixer)")
        self.backend_selector = Adw.ComboRow(
            model=self.backend_model,
            title="Audio Backend",
            subtitle="Choose PipeWire or ALSA control"
        )
        self.backend_selector.set_selected(0 if backend == "pipewire" else 1)

        # 2. PipeWire Devices Selector
        self.pw_devices_map = [
            ("@DEFAULT_AUDIO_SINK@", "Default Sink"),
            ("@DEFAULT_AUDIO_SOURCE@", "Default Source")
        ]
        sinks, sources = self.get_pipewire_devices()
        for s_id, s_name in sinks:
            self.pw_devices_map.append((s_id, f"Sink: {s_name}"))
        for s_id, s_name in sources:
            self.pw_devices_map.append((s_id, f"Source: {s_name}"))

        self.pw_device_model = Gtk.StringList()
        for pw_id, display_name in self.pw_devices_map:
            self.pw_device_model.append(display_name)
            
        self.pw_device_selector = Adw.ComboRow(
            model=self.pw_device_model,
            title="PipeWire Device",
            subtitle="Select the output (sink) or input (source) device"
        )
        current_pw_id = settings.get("pipewire_device_id", "@DEFAULT_AUDIO_SINK@")
        selected_index = 0
        for idx, (pw_id, display_name) in enumerate(self.pw_devices_map):
            if pw_id == current_pw_id:
                selected_index = idx
                break
        self.pw_device_selector.set_selected(selected_index)

        # 3. Mixer name selector (ALSA)
        self.mixer_row = Adw.EntryRow(
            title="Mixer Device Name",
            text=self.get_mixer_name()
        )
        
        # Set visibility based on backend
        is_pw = (backend == "pipewire")
        self.pw_device_selector.set_visible(is_pw)
        self.mixer_row.set_visible(not is_pw)
        
        # 4. Step size selector
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
            
        # 5. Custom Icon selection
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
        
        # 6. Icon Scale slider (Wrapped in Gtk.Box and PreferencesRow to allow dragging)
        self.scale_row = Adw.PreferencesRow(activatable=False)
        scale_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scale_box.set_margin_start(12)
        scale_box.set_margin_end(12)
        scale_box.set_margin_top(8)
        scale_box.set_margin_bottom(8)
        scale_label = Gtk.Label(text="Icon Scale", xalign=0.0)
        scale_box.append(scale_label)
        
        current_scale = settings.get("icon_scale", 1.0)
        self.scale_adj = Gtk.Adjustment.new(current_scale, 0.4, 2.0, 0.05, 0.1, 0.0)
        self.scale_slider = Gtk.Scale.new(Gtk.Orientation.HORIZONTAL, self.scale_adj)
        self.scale_slider.set_draw_value(True)
        self.scale_slider.set_hexpand(True)
        self.scale_slider.set_valign(Gtk.Align.CENTER)
        scale_box.append(self.scale_slider)
        self.scale_row.set_child(scale_box)

        # 7. Font Path entry row showing basename, non-editable
        self.font_row = Adw.EntryRow(
            title="Custom Font File (*.ttf)",
            text=os.path.basename(self.get_font_path())
        )
        self.font_row.set_editable(False)
        self.choose_font_button = Gtk.Button.new_from_icon_name("document-open-symbolic")
        self.choose_font_button.set_valign(Gtk.Align.CENTER)
        self.font_row.add_suffix(self.choose_font_button)

        # 8. Title Text Size slider (Wrapped in Gtk.Box and PreferencesRow to allow dragging)
        self.title_size_row = Adw.PreferencesRow(activatable=False)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_box.set_margin_start(12)
        title_box.set_margin_end(12)
        title_box.set_margin_top(8)
        title_box.set_margin_bottom(8)
        title_label = Gtk.Label(text="Title Text Size", xalign=0.0)
        title_box.append(title_label)
        
        current_title_size = float(self.get_title_font_size())
        self.title_size_adj = Gtk.Adjustment.new(current_title_size, 8.0, 24.0, 1.0, 2.0, 0.0)
        self.title_size_slider = Gtk.Scale.new(Gtk.Orientation.HORIZONTAL, self.title_size_adj)
        self.title_size_slider.set_draw_value(True)
        self.title_size_slider.set_hexpand(True)
        self.title_size_slider.set_valign(Gtk.Align.CENTER)
        title_box.append(self.title_size_slider)
        self.title_size_row.set_child(title_box)
        
        # Connect changes to save settings
        self.backend_selector.connect("notify::selected-item", self.on_backend_changed)
        self.pw_device_selector.connect("notify::selected-item", self.on_pw_device_changed)
        self.mixer_row.connect("notify::text", self.on_mixer_changed)
        self.step_selector.connect("notify::selected-item", self.on_step_changed)
        self.choose_icon_button.connect("clicked", self.on_choose_icon_clicked)
        self.clear_icon_button.connect("clicked", self.on_clear_icon_clicked)
        self.scale_slider.connect("value-changed", self.on_scale_changed)
        self.font_row.connect("notify::text", self.on_font_path_changed)
        self.choose_font_button.connect("clicked", self.on_choose_font_clicked)
        self.title_size_slider.connect("value-changed", self.on_title_size_changed)
        
        # Update clear button sensitivity
        icon_path = settings.get("custom_icon", "")
        self.clear_icon_button.set_sensitive(bool(icon_path))
        
        return [
            self.backend_selector,
            self.pw_device_selector,
            self.mixer_row,
            self.step_selector,
            self.icon_row,
            self.scale_row,
            self.font_row,
            self.title_size_row
        ]

    def on_backend_changed(self, combo, *args):
        selected_index = combo.get_selected()
        is_pw = (selected_index == 0)
        settings = self.get_settings() or {}
        settings["audio_backend"] = "pipewire" if is_pw else "alsa"
        self.set_settings(settings)
        
        # Toggle visibility
        self.pw_device_selector.set_visible(is_pw)
        self.mixer_row.set_visible(not is_pw)
        
        self.last_volume = -1
        self.last_mute = None
        self.update_volume_status()

    def on_pw_device_changed(self, combo, *args):
        selected_index = combo.get_selected()
        if 0 <= selected_index < len(self.pw_devices_map):
            pw_id, display_name = self.pw_devices_map[selected_index]
            settings = self.get_settings() or {}
            settings["pipewire_device_id"] = pw_id
            settings["pipewire_device_name"] = display_name
            self.set_settings(settings)
            
            self.last_volume = -1
            self.last_mute = None
            self.update_volume_status()

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

    def on_font_path_changed(self, entry, *args):
        settings = self.get_settings()
        if settings is None:
            settings = {}
        settings["font_path"] = entry.get_text()
        self.set_settings(settings)
        self.last_volume = -1
        self.last_mute = None
        self.update_volume_status()

    def update_font_setting(self, path):
        self.font_row.set_text(os.path.basename(path))
        settings = self.get_settings() or {}
        settings["font_path"] = path
        self.set_settings(settings)
        self.last_volume = -1
        self.last_mute = None
        self.update_volume_status()

    def on_choose_font_clicked(self, button):
        dialog = Gtk.FileChooserNative.new(
            title="Select Font File",
            parent=None,
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Open",
            cancel_label="Cancel"
        )
        filter_ttf = Gtk.FileFilter.new()
        filter_ttf.set_name("Font files (*.ttf, *.otf)")
        filter_ttf.add_pattern("*.ttf")
        filter_ttf.add_pattern("*.otf")
        dialog.add_filter(filter_ttf)
        
        def on_response(dialog, response_id):
            if response_id == Gtk.ResponseType.ACCEPT:
                file_path = dialog.get_file().get_path()
                GLib.idle_add(self.update_font_setting, file_path)
            dialog.destroy()
            
        dialog.connect("response", on_response)
        dialog.show()

    def on_title_size_changed(self, slider):
        settings = self.get_settings() or {}
        settings["title_font_size"] = int(slider.get_value())
        self.set_settings(settings)
        self.last_volume = -1
        self.last_mute = None
        self.update_volume_status()
