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
import struct
import fcntl
import select
from PIL import Image, ImageDraw, ImageFont

# Import gtk modules - used for the config rows
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
import globals as gl

class VolumePeakMonitor:
    def __init__(self):
        self.device_id = "@DEFAULT_AUDIO_SINK@"
        self.peak = 0.0
        self.running = False
        self.proc = None
        self.thread = None
        self.lock = threading.Lock()

    def start(self, device_id: str):
        if self.running and self.device_id == device_id:
            return
        self.stop()
        
        self.device_id = device_id
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        cmd = [
            'parecord',
            '--raw',
            '--format=s16le',
            '--channels=2',
            '--rate=44100',
            '--latency-msec=30',
            '--process-time-msec=10',
            '--device=' + self.device_id
        ]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            try:
                cmd[0] = 'parec'
                self.proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )
            except Exception:
                self.running = False
                return

        fd = self.proc.stdout.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        chunk_bytes = 512 * 4
        buf = bytearray()
        smooth_val = 0.0
        decay = 0.85

        try:
            while self.running:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    continue
                try:
                    data = self.proc.stdout.read(chunk_bytes)
                except (OSError, IOError):
                    continue
                if not data:
                    break
                buf.extend(data)
                while len(buf) >= chunk_bytes:
                    chunk = bytes(buf[:chunk_bytes])
                    del buf[:chunk_bytes]

                    num_samples = len(chunk) // 2
                    if num_samples > 0:
                        samples = struct.unpack(f"<{num_samples}h", chunk)
                        peak_val = 0.0
                        for val in samples:
                            abs_val = abs(val) / 32768.0
                            if abs_val > peak_val:
                                peak_val = abs_val
                        smooth_val = max(peak_val, smooth_val * decay)
                        with self.lock:
                            self.peak = smooth_val
        except Exception:
            pass
        finally:
            self.stop_proc()

    def get_peak(self) -> float:
        with self.lock:
            return self.peak

    def stop_proc(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=0.1)
            except Exception:
                pass
            self.proc = None

    def stop(self):
        self.running = False
        self.stop_proc()
        if self.thread:
            self.thread.join(timeout=0.2)
            self.thread = None
        with self.lock:
            self.peak = 0.0

class VolumeControl(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.running = False
        self.current_volume = 50
        self.last_mute = False
        self.bg_image = None
        self.knob_image = None
        self.peak_monitor = VolumePeakMonitor()
        self.tick_timer_id = 0
        self.tick_counter = 0
        
        self.last_drawn_volume = -1
        self.last_drawn_mute = None
        self.last_drawn_peak = -1.0

    def on_ready(self) -> None:
        self.running = True
        
        # Load initial status once (in a background thread to avoid GTK block)
        threading.Thread(target=self._initial_load_status, daemon=True).start()
        
        # Start GLib tick timer (40ms / 25 FPS) for real-time peak meter and polling
        self.tick_timer_id = GLib.timeout_add(40, self.on_tick_update)

    def _initial_load_status(self):
        vol, mute = self.get_system_volume_status()
        self.current_volume = vol
        self.last_mute = mute
        self.update_ui_rendering()
        self.restart_peak_monitor()

    def on_remove(self) -> None:
        self.running = False
        if self.tick_timer_id:
            GLib.source_remove(self.tick_timer_id)
            self.tick_timer_id = 0
        self.peak_monitor.stop()

    def on_disconnect(self) -> None:
        self.running = False
        if self.tick_timer_id:
            GLib.source_remove(self.tick_timer_id)
            self.tick_timer_id = 0
        self.peak_monitor.stop()

    def _raw_event_callback(self, event: InputEvent, data: dict = None):
        if event == Input.Dial.Events.TURN_CW:
            self.change_volume(self.get_step_size())
        elif event == Input.Dial.Events.TURN_CCW:
            self.change_volume(-self.get_step_size())
        elif event in [Input.Dial.Events.DOWN, Input.Dial.Events.SHORT_TOUCH_PRESS]:
            self.toggle_mute()
        else:
            super()._raw_event_callback(event, data)

    def event_callback(self, event: InputEvent, data: dict = None):
        if event == Input.Dial.Events.TURN_CW:
            self.change_volume(self.get_step_size())
        elif event == Input.Dial.Events.TURN_CCW:
            self.change_volume(-self.get_step_size())
        elif event in [Input.Dial.Events.DOWN, Input.Dial.Events.SHORT_TOUCH_PRESS]:
            self.toggle_mute()
        else:
            super().event_callback(event, data)



    def get_step_size(self) -> int:
        settings = self.get_settings()
        if settings is not None:
            val = settings.get("step_size", "5%")
            try:
                return int(val.replace("%", ""))
            except ValueError:
                return 5
        return 5

    def get_configured_device_id(self) -> str:
        settings = self.get_settings() or {}
        return settings.get("pipewire_device_id", "@DEFAULT_AUDIO_SINK@")

    def restart_peak_monitor(self):
        device_id = self.get_configured_device_id()
        self.peak_monitor.start(device_id)

    def on_tick_update(self) -> bool:
        if not self.running:
            return False
            
        peak = self.peak_monitor.get_peak()
        peak = max(0.0, min(1.0, peak))
        if peak < 0.04:
            peak = 0.0
        
        peak_diff = abs(peak - self.last_drawn_peak)
        if (self.current_volume != self.last_drawn_volume or 
            self.last_mute != self.last_drawn_mute or 
            (peak > 0.0 and peak_diff > 0.04) or 
            (peak == 0.0 and self.last_drawn_peak > 0.0)):
            
            self.update_ui_rendering(peak)
            
        self.tick_counter += 1
        if self.tick_counter >= 12:
            self.tick_counter = 0
            threading.Thread(target=self._poll_system_volume_bg, daemon=True).start()
            
        return True

    def _poll_system_volume_bg(self):
        if not self.running:
            return
        vol, mute = self.get_system_volume_status()
        if vol != self.current_volume or mute != self.last_mute:
            self.current_volume = vol
            self.last_mute = mute
            GLib.idle_add(self.update_ui_rendering)

    def update_ui_rendering(self, peak: float = 0.0):
        if not self.get_is_present():
            return
        
        self.last_drawn_volume = self.current_volume
        self.last_drawn_mute = self.last_mute
        self.last_drawn_peak = peak
        
        img = self.generate_volume_image(self.current_volume, self.last_mute, peak)
        GLib.idle_add(self.set_media, img)

    def run_cmd(self, cmd: list) -> str:
        if os.path.exists("/.flatpak-info"):
            full_cmd = ["flatpak-spawn", "--host"] + cmd
        else:
            full_cmd = cmd
        try:
            return subprocess.check_output(full_cmd, text=True)
        except Exception:
            try:
                return subprocess.check_output(cmd, text=True)
            except Exception:
                return ""

    def execute_cmd(self, cmd: list) -> None:
        if os.path.exists("/.flatpak-info"):
            full_cmd = ["flatpak-spawn", "--host"] + cmd
        else:
            full_cmd = cmd
        try:
            subprocess.run(full_cmd, check=True)
        except Exception:
            try:
                subprocess.run(cmd, check=True)
            except Exception:
                pass

    def get_pipewire_devices(self) -> "tuple[list, list]":
        sinks = []
        sources = []
        try:
            output = self.run_cmd(["wpctl", "status"])
            current_section = None
            import re
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
                        
                if current_section in ["sinks", "sources"]:
                    match = re.search(r'(\d+)\.\s*([^\[]+)', line_strip)
                    if match:
                        id_part = match.group(1)
                        name_part = match.group(2).strip()
                        if current_section == "sinks":
                            sinks.append((id_part, name_part))
                        else:
                            sources.append((id_part, name_part))
        except Exception:
            pass
        return sinks, sources

    def get_pipewire_status(self, device_id: str) -> "tuple[int, bool]":
        try:
            output = self.run_cmd(["wpctl", "get-volume", device_id])
            if not output:
                return self.current_volume, self.last_mute
            output = output.strip()
            parts = output.split()
            volume = self.current_volume
            mute = self.last_mute
            if len(parts) >= 2:
                vol_str = parts[1]
                try:
                    volume = int(float(vol_str) * 100)
                except ValueError:
                    pass
            if "[MUTED]" in output:
                mute = True
            else:
                mute = False
            return volume, mute
        except Exception:
            return self.current_volume, self.last_mute

    def get_system_volume_status(self) -> "tuple[int, bool]":
        device_id = self.get_configured_device_id()
        return self.get_pipewire_status(device_id)

    def change_pipewire_volume(self, device_id: str, target_vol: int) -> None:
        val = f"{target_vol / 100.0:.2f}"
        try:
            self.execute_cmd(["wpctl", "set-volume", "-l", "1.0", device_id, val])
        except Exception:
            pass

    def change_volume(self, delta: int) -> None:
        self.current_volume = max(0, min(100, self.current_volume + delta))
        self.update_ui_rendering()
        threading.Thread(target=self._change_volume_bg, args=(self.current_volume,), daemon=True).start()

    def _change_volume_bg(self, target_vol: int):
        device_id = self.get_configured_device_id()
        self.change_pipewire_volume(device_id, target_vol)

    def toggle_pipewire_mute(self, device_id: str) -> None:
        try:
            self.execute_cmd(["wpctl", "set-mute", device_id, "toggle"])
        except Exception:
            pass

    def toggle_mute(self) -> None:
        self.last_mute = not self.last_mute
        self.update_ui_rendering()
        threading.Thread(target=self._toggle_mute_bg, daemon=True).start()

    def _toggle_mute_bg(self):
        device_id = self.get_configured_device_id()
        self.toggle_pipewire_mute(device_id)

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

    def generate_volume_image(self, volume: int, is_muted: bool, peak: float = 0.0) -> Image.Image:
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
        title_font_size = int(settings.get("title_font_size", 14)) # Made bigger as requested
        vol_font_size = 28  # Static size of 28px (made bigger as requested)
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
            vol_w = 56
            
        # Draw Volume Text (right-aligned, vertically centered at y=20)
        try:
            draw.text((188, 20), vol_text, font=font_vol, fill=vol_color, anchor="rm")
        except TypeError:
            draw.text((188 - vol_w, 20 - 14), vol_text, font=font_vol, fill=vol_color)
        
        # Icon placement area (vertical center shifted to y=20, base size increased to 24)
        icon_drawn = False
        icon_w = 24  # Base width of the icon area
        if custom_icon_path:
            icon_img = self.load_icon_image(custom_icon_path)
            if icon_img is not None:
                icon_img = icon_img.convert("RGBA")
                base_size = 24  # Base size (made bigger)
                scaled_size = int(base_size * icon_scale)
                scaled_size = max(4, min(scaled_size, 60))
                icon_img = icon_img.resize((scaled_size, scaled_size))
                
                # Center vertically at y=20, left-aligned at x=12
                x_start = 12
                y_start = 20 - scaled_size // 2
                
                if is_muted:
                    r, g, b, a = icon_img.split()
                    a = a.point(lambda i: int(i * 0.4))
                    icon_img = Image.merge("RGBA", (r, g, b, a))
                
                img.paste(icon_img, (x_start, y_start), icon_img)
                
                if is_muted:
                    draw.line([(x_start - 2, y_start - 2), (x_start + scaled_size + 2, y_start + scaled_size + 2)], fill=(239, 68, 68, 255), width=2)
                
                icon_drawn = True
                icon_w = scaled_size

        if not icon_drawn:
            # Default Speaker Icon (slate-blue speaker with cyan/blue waves, shifted to y=20 center)
            spk_x, spk_y = 12, 13
            spk_color = (90, 105, 120, 255) if is_muted else (110, 130, 150, 255)
            
            # Speaker body (centered vertically at y=20)
            draw.rectangle([(spk_x, spk_y + 4), (spk_x + 5, spk_y + 10)], fill=spk_color)
            # Speaker cone
            draw.polygon([(spk_x + 5, spk_y + 4), (spk_x + 10, spk_y + 0), (spk_x + 10, spk_y + 14), (spk_x + 5, spk_y + 10)], fill=spk_color)
            
            if is_muted:
                draw.line([(spk_x - 2, spk_y + 2), (spk_x + 16, spk_y + 12)], fill=(239, 68, 68, 255), width=2)
            else:
                wave_color = (0, 168, 255, 255)
                draw.arc([(spk_x + 3, spk_y + 2), (spk_x + 13, spk_y + 12)], start=-45, end=45, fill=wave_color, width=2)
                draw.arc([(spk_x, spk_y - 1), (spk_x + 18, spk_y + 15)], start=-45, end=45, fill=wave_color, width=2)
                draw.arc([(spk_x - 3, spk_y - 4), (spk_x + 23, spk_y + 18)], start=-45, end=45, fill=wave_color, width=2)
            icon_w = 26

        # Draw Title Text (centered horizontally, using custom name if set)
        custom_name = settings.get("custom_name", "")
        if custom_name:
            title_text = custom_name
        else:
            title_text = settings.get("pipewire_device_name", "Default Sink")
            
        if len(title_text) > 16:
            title_text = title_text[:14] + ".."
            
        left_bound = 12 + icon_w + 6
        right_bound = 188 - vol_w - 6
        center_x = left_bound + (right_bound - left_bound) // 2
        
        try:
            draw.text((center_x, 20), title_text, font=font_title, fill=(220, 222, 230, 255), anchor="mm")
        except TypeError:
            try:
                title_w = font_title.getlength(title_text)
            except Exception:
                title_w = len(title_text) * 8
            draw.text((center_x - title_w // 2, 20 - 8), title_text, font=font_title, fill=(220, 222, 230, 255))
        
        # 3. Dial Geometry (Smaller knob core: Width = 80px, Height = 38px)
        cx, cy = 100, 96
        
        # Outer Knob Core dimensions (80px wide, 38px high)
        rx_outer, ry_outer = 40, 38
        # Inner Knob Core dimensions (Thickness = 3px)
        rx_inner, ry_inner = 37, 35
        
        # Gauge Arc dimensions (9px outside the knob core)
        rx_arc, ry_arc = 49, 47
        
        # Pointer dimensions
        r_pt_start, r_pt_end = 14, 32
        
        # Draw Ticks (broken into quarters - 17 ticks total, every 11.25 degrees)
        for i in range(17):
            tick_angle = 180 + i * 11.25
            rad = math.radians(tick_angle)
            if i % 4 == 0:
                # Quarters: longer, thicker lines
                rx_tick_start, ry_tick_start = 53, 51
                rx_tick_end, ry_tick_end = 65, 63
                w = 3
                color = (160, 162, 175, 255)
            else:
                # Smaller lines in between
                rx_tick_start, ry_tick_start = 58, 56
                rx_tick_end, ry_tick_end = 63, 61
                w = 1
                color = (110, 112, 120, 255)
                
            x1 = cx + rx_tick_start * math.cos(rad)
            y1 = cy + ry_tick_start * math.sin(rad)
            x2 = cx + rx_tick_end * math.cos(rad)
            y2 = cy + ry_tick_end * math.sin(rad)
            draw.line([(x1, y1), (x2, y2)], fill=color, width=w)
            
        # Draw Gauge Track (inactive - dark background arc)
        bbox = [(cx - rx_arc, cy - ry_arc), (cx + rx_arc, cy + ry_arc)]
        draw.arc(bbox, start=180, end=360, fill=(38, 38, 42, 255), width=7)
        
        # Draw Active Gauge Segment
        end_angle = 180 + 180 * (volume / 100.0)
        
        if not is_muted and volume > 0:
            for angle in range(180, int(end_angle)):
                pct = (angle - 180) / 180.0
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
                
        # Draw Real-time Audio Peak Meter (glowing cyan arc hugging outer knob)
        if not is_muted and peak > 0.04:
            rx_peak, ry_peak = 44, 42
            bbox_peak = [(cx - rx_peak, cy - ry_peak), (cx + rx_peak, cy + ry_peak)]
            peak_angle = 180 + 180 * peak
            draw.arc(bbox_peak, start=180, end=int(peak_angle), fill=(0, 210, 255, 255), width=3)

        # 4. Draw Inner Knob Core (Outer shadow/border for 3D bevel look)
        draw.ellipse([(cx - rx_outer, cy - ry_outer), (cx + rx_outer, cy + ry_outer)], fill=(18, 18, 20, 255))
        # Inner circle of the core
        draw.ellipse([(cx - rx_inner, cy - ry_inner), (cx + rx_inner, cy + ry_inner)], fill=(28, 28, 32, 255), outline=(60, 62, 72, 255), width=1)
        
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

        # 1. Custom Name Row
        self.custom_name_row = Adw.EntryRow(
            title="Custom Display Name",
            text=settings.get("custom_name", "")
        )

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
        
        # 3. Step size selector
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
            
        # 4. Custom Icon selection
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
        
        # 5. Icon Scale slider (Wrapped in Gtk.Box and PreferencesRow to allow dragging)
        self.scale_row = Adw.PreferencesRow()
        self.scale_row.set_activatable(False)
        scale_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scale_box.set_margin_start(12)
        scale_box.set_margin_end(12)
        scale_box.set_margin_top(8)
        scale_box.set_margin_bottom(8)
        scale_label = Gtk.Label.new("Icon Scale")
        scale_label.set_xalign(0.0)
        scale_box.append(scale_label)
        
        current_scale = settings.get("icon_scale", 1.0)
        self.scale_adj = Gtk.Adjustment.new(current_scale, 0.4, 2.0, 0.05, 0.1, 0.0)
        self.scale_slider = Gtk.Scale.new(Gtk.Orientation.HORIZONTAL, self.scale_adj)
        self.scale_slider.set_draw_value(True)
        self.scale_slider.set_hexpand(True)
        self.scale_slider.set_valign(Gtk.Align.CENTER)
        scale_box.append(self.scale_slider)
        self.scale_row.set_child(scale_box)

        # 6. Custom Font File (*.ttf) entry row showing basename, non-editable
        self.font_row = Adw.EntryRow(
            title="Custom Font File (*.ttf)",
            text=os.path.basename(self.get_font_path())
        )
        self.font_row.set_editable(False)
        self.choose_font_button = Gtk.Button.new_from_icon_name("document-open-symbolic")
        self.choose_font_button.set_valign(Gtk.Align.CENTER)
        self.font_row.add_suffix(self.choose_font_button)

        # 7. Title Text Size slider (Wrapped in Gtk.Box and PreferencesRow to allow dragging)
        self.title_size_row = Adw.PreferencesRow()
        self.title_size_row.set_activatable(False)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_box.set_margin_start(12)
        title_box.set_margin_end(12)
        title_box.set_margin_top(8)
        title_box.set_margin_bottom(8)
        title_label = Gtk.Label.new("Title Text Size")
        title_label.set_xalign(0.0)
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
        self.custom_name_row.connect("notify::text", self.on_custom_name_changed)
        self.pw_device_selector.connect("notify::selected-item", self.on_pw_device_changed)
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
            self.custom_name_row,
            self.pw_device_selector,
            self.step_selector,
            self.icon_row,
            self.scale_row,
            self.font_row,
            self.title_size_row
        ]

    def on_custom_name_changed(self, entry, *args):
        settings = self.get_settings() or {}
        settings["custom_name"] = entry.get_text()
        self.set_settings(settings)
        self.update_ui_rendering()

    def on_pw_device_changed(self, combo, *args):
        selected_index = combo.get_selected()
        if 0 <= selected_index < len(self.pw_devices_map):
            pw_id, display_name = self.pw_devices_map[selected_index]
            settings = self.get_settings() or {}
            settings["pipewire_device_id"] = pw_id
            settings["pipewire_device_name"] = display_name
            self.set_settings(settings)
            
            self.restart_peak_monitor()
            self.update_ui_rendering()

    def on_step_changed(self, combo, *args):
        settings = self.get_settings() or {}
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
            
            self.clear_icon_button.set_sensitive(True)
            self.update_ui_rendering()
            
        GLib.idle_add(gl.app.let_user_select_asset, current_val, on_select_callback)

    def on_clear_icon_clicked(self, button):
        settings = self.get_settings() or {}
        settings["custom_icon"] = ""
        self.set_settings(settings)
        
        self.clear_icon_button.set_sensitive(False)
        self.update_ui_rendering()

    def on_scale_changed(self, slider):
        settings = self.get_settings() or {}
        settings["icon_scale"] = slider.get_value()
        self.set_settings(settings)
        self.update_ui_rendering()

    def on_font_path_changed(self, entry, *args):
        settings = self.get_settings() or {}
        settings["font_path"] = entry.get_text()
        self.set_settings(settings)
        self.update_ui_rendering()

    def update_font_setting(self, path):
        self.font_row.set_text(os.path.basename(path))
        settings = self.get_settings() or {}
        settings["font_path"] = path
        self.set_settings(settings)
        self.update_ui_rendering()

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
        self.update_ui_rendering()
