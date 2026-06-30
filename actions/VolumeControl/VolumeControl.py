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
        self.is_source = False
        self.peak = 0.0
        self.running = False
        self.proc = None
        self.thread = None
        self.lock = threading.Lock()

    def start(self, device_id: str, is_source: bool = False):
        if self.running and self.device_id == device_id and self.is_source == is_source:
            return
        self.stop()
        
        self.device_id = device_id
        self.is_source = is_source
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        target_device = self.device_id
        if not self.is_source:
            target_device = self.device_id + ".monitor"
            
        cmd = [
            'parecord',
            '--raw',
            '--format=s16le',
            '--channels=2',
            '--rate=44100',
            '--latency-msec=30',
            '--process-time-msec=10',
            '--device=' + target_device
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
        self.last_poll_time = 0.0
        self.last_drawn_volume = -1
        self.last_drawn_mute = None
        self.last_drawn_peak = -1.0

    def on_ready(self) -> None:
        self.running = True
        
        # Load initial status once (in a background thread to avoid GTK block)
        threading.Thread(target=self._initial_load_status, daemon=True).start()
        
        # Start GLib tick timer based on configured FPS for real-time peak meter
        fps = self.get_meter_fps()
        interval = int(1000 / fps)
        self.tick_timer_id = GLib.timeout_add(interval, self.on_tick_update)

    def _initial_load_status(self):
        settings = self.get_settings() or {}
        if not settings.get("device_type"):
            settings["device_type"] = "sink"
            
        dtype = settings.get("device_type", "sink")
        sinks, sources = self.get_pipewire_devices()
        devices = sinks if dtype == "sink" else sources
        
        if not settings.get("pipewire_device_id") and devices:
            settings["pipewire_device_id"] = devices[0][0]
            settings["pipewire_device_name"] = devices[0][1]
            self.set_settings(settings)
            
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

    def get_meter_fps(self) -> int:
        settings = self.get_settings()
        if settings is not None:
            val = settings.get("meter_fps", "25 FPS")
            try:
                return int(val.split()[0])
            except ValueError:
                return 25
        return 25

    def get_configured_device_id(self) -> str:
        settings = self.get_settings() or {}
        dev_id = settings.get("pipewire_device_id", "@DEFAULT_AUDIO_SINK@")
        if dev_id == "@DEFAULT_AUDIO_SINK@":
            return "@DEFAULT_SINK@"
        elif dev_id == "@DEFAULT_AUDIO_SOURCE@":
            return "@DEFAULT_SOURCE@"
        return dev_id

    def restart_peak_monitor(self):
        device_id = self.get_configured_device_id()
        settings = self.get_settings() or {}
        dtype = settings.get("device_type", "sink")
        is_source = (dtype == "source")
        self.peak_monitor.start(device_id, is_source)

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
            
        # Poll system volume changes at a fixed 500ms interval
        import time
        now = time.time()
        if now - self.last_poll_time >= 0.5:
            self.last_poll_time = now
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
        try:
            return subprocess.check_output(cmd, text=True)
        except Exception:
            return ""

    def execute_cmd(self, cmd: list) -> None:
        try:
            subprocess.run(cmd, check=True)
        except Exception:
            pass

    def parse_pactl_list(self, output: str) -> list:
        devices = []
        current_name = None
        current_desc = None
        for line in output.splitlines():
            line_strip = line.strip()
            if line_strip.startswith("Name:"):
                current_name = line_strip.split("Name:", 1)[1].strip()
            elif line_strip.startswith("Description:"):
                current_desc = line_strip.split("Description:", 1)[1].strip()
                if current_name and current_desc:
                    devices.append((current_name, current_desc))
                    current_name = None
                    current_desc = None
        return devices

    def get_pipewire_devices(self) -> "tuple[list, list]":
        sinks = []
        sources = []
        try:
            sinks_out = self.run_cmd(["pactl", "list", "sinks"])
            sinks = self.parse_pactl_list(sinks_out)
        except Exception:
            pass
        try:
            sources_out = self.run_cmd(["pactl", "list", "sources"])
            sources = self.parse_pactl_list(sources_out)
            # Filter out monitors (which are internal loopbacks of outputs)
            sources = [(n, d) for n, d in sources if not n.endswith(".monitor")]
        except Exception:
            pass
        return sinks, sources

    def get_pipewire_status(self, device_id: str) -> "tuple[int, bool]":
        settings = self.get_settings() or {}
        dtype = settings.get("device_type", "sink")
        cmd_type = "sink" if dtype == "sink" else "source"
        
        volume = self.current_volume
        mute = self.last_mute
        
        try:
            # Query mute status
            mute_out = self.run_cmd(["pactl", f"get-{cmd_type}-mute", device_id]).strip()
            if "Mute: yes" in mute_out:
                mute = True
            elif "Mute: no" in mute_out:
                mute = False
                
            # Query volume status
            vol_out = self.run_cmd(["pactl", f"get-{cmd_type}-volume", device_id]).strip()
            import re
            match = re.search(r'/\s*(\d+)%', vol_out)
            if match:
                volume = int(match.group(1))
        except Exception:
            pass
            
        return volume, mute

    def get_system_volume_status(self) -> "tuple[int, bool]":
        device_id = self.get_configured_device_id()
        return self.get_pipewire_status(device_id)

    def change_pipewire_volume(self, device_id: str, target_vol: int) -> None:
        settings = self.get_settings() or {}
        dtype = settings.get("device_type", "sink")
        cmd_type = "sink" if dtype == "sink" else "source"
        try:
            self.execute_cmd(["pactl", f"set-{cmd_type}-volume", device_id, f"{target_vol}%"])
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
        settings = self.get_settings() or {}
        dtype = settings.get("device_type", "sink")
        cmd_type = "sink" if dtype == "sink" else "source"
        try:
            self.execute_cmd(["pactl", f"set-{cmd_type}-mute", device_id, "toggle"])
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
        font_path = settings.get("font_path", "")
        font_name = settings.get("font_name", "")
        title_font_size = 14
        font_file = None
        
        if font_name:
            import re
            match = re.search(r'\s+(\d+)$', font_name.strip())
            if match:
                title_font_size = int(match.group(1))
            
            # Resolve dynamically on the fly in the backend's environment
            resolved_path = self.font_name_to_path(font_name)
            if resolved_path and os.path.exists(resolved_path):
                font_file = resolved_path
                
        # If not resolved from font_name, check saved font_path
        if not font_file and font_path and os.path.exists(font_path):
            font_file = font_path
            
        # Fallback to standard system paths if still not found
        if not font_file:
            for path in [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                "/usr/share/fonts/ubuntu/Ubuntu-B.ttf",
                "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/dejavu/DejaVuSans.ttf"
            ]:
                if os.path.exists(path):
                    font_file = path
                    break
                    
        vol_font_size = 28
                    
        try:
            if font_file:
                font_title = ImageFont.truetype(font_file, title_font_size)
                font_vol = ImageFont.truetype(font_file, vol_font_size)
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
        
        # Draw Active Gauge Segment (now functions as the live audio playback meter)
        if not is_muted and peak > 0.04:
            scaled_peak = peak * (volume / 100.0)
            end_angle = 180 + 180 * scaled_peak
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

        # 4. Draw Inner Knob Core (Outer shadow/border for 3D bevel look)
        draw.ellipse([(cx - rx_outer, cy - ry_outer), (cx + rx_outer, cy + ry_outer)], fill=(18, 18, 20, 255))
        # Inner circle of the core
        draw.ellipse([(cx - rx_inner, cy - ry_inner), (cx + rx_inner, cy + ry_inner)], fill=(28, 28, 32, 255), outline=(60, 62, 72, 255), width=1)
        
        # 5. Draw Pointer line on top of the knob (still represents static volume level)
        pointer_angle = 180 + 180 * (volume / 100.0)
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


    def update_device_dropdown(self):
        settings = self.get_settings() or {}
        dtype = settings.get("device_type", "sink")
        
        self.pw_devices_map = []
        if dtype == "sink":
            sinks, _ = self.get_pipewire_devices()
            for s_id, s_name in sinks:
                self.pw_devices_map.append((s_id, s_name))
        else:
            _, sources = self.get_pipewire_devices()
            for s_id, s_name in sources:
                self.pw_devices_map.append((s_id, s_name))
                
        self.pw_device_model = Gtk.StringList()
        for pw_id, display_name in self.pw_devices_map:
            self.pw_device_model.append(display_name)
            
        self.pw_device_selector.set_model(self.pw_device_model)
        
        current_pw_id = settings.get("pipewire_device_id")
        if not current_pw_id or not any(pw_id == current_pw_id for pw_id, _ in self.pw_devices_map):
            if self.pw_devices_map:
                current_pw_id = self.pw_devices_map[0][0]
                settings["pipewire_device_id"] = current_pw_id
                settings["pipewire_device_name"] = self.pw_devices_map[0][1]
                self.set_settings(settings)
            else:
                current_pw_id = ""
                settings["pipewire_device_id"] = ""
                settings["pipewire_device_name"] = ""
                self.set_settings(settings)
            
        selected_index = 0
        for idx, (pw_id, display_name) in enumerate(self.pw_devices_map):
            if pw_id == current_pw_id:
                selected_index = idx
                break
                
        self._updating_dropdown = True
        self.pw_device_selector.set_selected(selected_index)
        self._updating_dropdown = False

    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        settings = self.get_settings() or {}
        dtype = settings.get("device_type", "sink")

        # 1. Custom Name Row
        self.custom_name_row = Adw.EntryRow(
            title="Custom Display Name",
            text=settings.get("custom_name", "")
        )

        # 2. Device Type Selector
        self.type_model = Gtk.StringList()
        self.type_model.append("Output (sink)")
        self.type_model.append("Input (source)")
        self.type_selector = Adw.ComboRow(
            model=self.type_model,
            title="Device Type",
            subtitle="Select Output (speaker/headphones) or Input (mic)"
        )
        self.type_selector.set_selected(0 if dtype == "sink" else 1)

        # 3. PipeWire Device Selector ComboRow
        self.pw_device_selector = Adw.ComboRow(
            title="PipeWire Device",
            subtitle="Select the specific audio device"
        )
        
        # Populate initial list
        self.update_device_dropdown()
        
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
        
        current_step = f"{self.get_step_size()}%"
        if current_step in step_sizes:
            self.step_selector.set_selected(step_sizes.index(current_step))
        else:
            self.step_selector.set_selected(2) # Default to 5%
            
        # 5. FPS selection combo row
        self.fps_model = Gtk.StringList()
        fps_options = ["1 FPS", "5 FPS", "10 FPS", "15 FPS", "20 FPS", "25 FPS"]
        for opt in fps_options:
            self.fps_model.append(opt)
            
        self.fps_selector = Adw.ComboRow(
            model=self.fps_model,
            title="Meter Update Rate (FPS)",
            subtitle="Higher FPS yields smoother animations but increases CPU usage"
        )
        current_fps = f"{self.get_meter_fps()} FPS"
        if current_fps in fps_options:
            self.fps_selector.set_selected(fps_options.index(current_fps))
        else:
            self.fps_selector.set_selected(5) # Default to 25 FPS

        # 6. Custom Icon selection
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
        
        # 7. Icon Scale slider (Wrapped in Gtk.Box and PreferencesRow to allow dragging)
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

        # 8. Custom Font Row (using FontChooserDialog)
        friendly_font_name = settings.get("font_name")
        if not friendly_font_name:
            font_path_val = settings.get("font_path", "Ubuntu-B.ttf")
            friendly_font_name = os.path.basename(font_path_val).replace(".ttf", "").replace(".otf", "").replace("-", " ")
        self.font_row = Adw.ActionRow(
            title="Font",
            subtitle=friendly_font_name,
            activatable=True
        )
        self.choose_font_button = Gtk.Button.new_from_icon_name("document-open-symbolic")
        self.choose_font_button.set_valign(Gtk.Align.CENTER)
        self.font_row.add_suffix(self.choose_font_button)

        # Connect changes to save settings
        self.custom_name_row.connect("notify::text", self.on_custom_name_changed)
        self.type_selector.connect("notify::selected-item", self.on_device_type_changed)
        self.pw_device_selector.connect("notify::selected-item", self.on_pw_device_changed)
        self.step_selector.connect("notify::selected-item", self.on_step_changed)
        self.fps_selector.connect("notify::selected-item", self.on_fps_changed)
        self.choose_icon_button.connect("clicked", self.on_choose_icon_clicked)
        self.clear_icon_button.connect("clicked", self.on_clear_icon_clicked)
        self.scale_slider.connect("value-changed", self.on_scale_changed)
        self.font_row.connect("activated", self.on_choose_font_clicked)
        self.choose_font_button.connect("clicked", self.on_choose_font_clicked)
        
        # Update clear button sensitivity
        icon_path = settings.get("custom_icon", "")
        self.clear_icon_button.set_sensitive(bool(icon_path))
        
        # Create Text (Custom Display Name) Expander Row
        self.text_expander = Adw.ExpanderRow(
            title="Custom Display Name",
            subtitle="Configure display name and font"
        )
        self.text_expander.add_row(self.custom_name_row)
        self.text_expander.add_row(self.font_row)

        # Create Icon Expander Row
        self.icon_expander = Adw.ExpanderRow(
            title="Icon Configuration",
            subtitle="Configure icon and scaling"
        )
        self.icon_expander.add_row(self.icon_row)
        self.icon_expander.add_row(self.scale_row)
        
        return [
            self.text_expander,
            self.type_selector,
            self.pw_device_selector,
            self.step_selector,
            self.fps_selector,
            self.icon_expander
        ]

    def on_custom_name_changed(self, entry, *args):
        settings = self.get_settings() or {}
        settings["custom_name"] = entry.get_text()
        self.set_settings(settings)
        self.update_ui_rendering()

    def on_device_type_changed(self, combo, *args):
        selected_index = combo.get_selected()
        new_type = "sink" if selected_index == 0 else "source"
        
        settings = self.get_settings() or {}
        settings["device_type"] = new_type
        
        # Select first available device for the new type
        sinks, sources = self.get_pipewire_devices()
        devices = sinks if new_type == "sink" else sources
        if devices:
            settings["pipewire_device_id"] = devices[0][0]
            settings["pipewire_device_name"] = devices[0][1]
        else:
            settings["pipewire_device_id"] = ""
            settings["pipewire_device_name"] = ""
        self.set_settings(settings)
        
        # Rebuild the Device Selection dropdown items
        self.update_device_dropdown()
        
        # Restart monitor and draw
        self.restart_peak_monitor()
        self.update_ui_rendering()

    def on_pw_device_changed(self, combo, *args):
        if getattr(self, "_updating_dropdown", False):
            return
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

    def on_fps_changed(self, combo, *args):
        settings = self.get_settings() or {}
        selected_item = combo.get_selected_item()
        if selected_item is not None:
            settings["meter_fps"] = selected_item.get_string()
            self.set_settings(settings)
            
            # Restart GLib timer with the new update interval
            if self.tick_timer_id:
                GLib.source_remove(self.tick_timer_id)
                self.tick_timer_id = 0
            
            if self.running:
                fps = self.get_meter_fps()
                interval = int(1000 / fps)
                self.tick_timer_id = GLib.timeout_add(interval, self.on_tick_update)

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

    def font_name_to_path(self, font_name: str) -> str:
        import re
        import subprocess
        # font_name is e.g. "DejaVu Sans Bold 15" or "DejaVu Sans 15"
        # Remove trailing digits (size)
        match = re.match(r'^(.*?)\s+\d+$', font_name.strip())
        if match:
            font_desc = match.group(1)
        else:
            font_desc = font_name.strip()
        
        # Try to find common styles: "Bold", "Italic", "Oblique", "Condensed", "Medium", "Light", "Semibold"
        styles = []
        family = font_desc
        for style in ["Bold", "Italic", "Oblique", "Condensed", "Medium", "Light", "Semibold", "Regular", "Book"]:
            pattern = re.compile(rf'\b{style}\b', re.IGNORECASE)
            if pattern.search(family):
                styles.append(style.lower())
                family = pattern.sub("", family).strip()
                
        family = " ".join(family.split())
        pattern_str = family
        if styles:
            pattern_str += ":" + ":".join(styles)
            
        try:
            path = subprocess.check_output(
                ["fc-match", "-f", "%{file}", pattern_str],
                text=True
            ).strip()
            if path and os.path.exists(path):
                return path
        except Exception:
            pass
            
        try:
            path = subprocess.check_output(
                ["fc-match", "-f", "%{file}", font_desc],
                text=True
            ).strip()
            if path and os.path.exists(path):
                return path
        except Exception:
            pass
            
        return "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"

    def on_font_path_changed(self, entry, *args):
        # Kept for backward compatibility but no longer used
        pass

    def update_font_setting(self, font_name: str):
        settings = self.get_settings() or {}
        settings["font_name"] = font_name
        
        # We clear font_path so that the backend resolves it dynamically in its own environment
        if "font_path" in settings:
            del settings["font_path"]
            
        self.set_settings(settings)
        self.font_row.set_subtitle(font_name)
        self.update_ui_rendering()

    def on_choose_font_clicked(self, *args):
        parent_window = None
        if args and hasattr(args[0], "get_root"):
            root = args[0].get_root()
            if isinstance(root, Gtk.Window):
                parent_window = root
                
        dialog = Gtk.FontChooserDialog(
            title="Pick a Font",
            transient_for=parent_window,
            modal=True
        )
        
        # Set the currently selected font if available
        settings = self.get_settings() or {}
        current_font = settings.get("font_name", "")
        if current_font:
            dialog.set_font(current_font)
            
        def on_response(dialog, response_id):
            if response_id == Gtk.ResponseType.ACCEPT:
                font_name = dialog.get_font()
                GLib.idle_add(self.update_font_setting, font_name)
            dialog.destroy()
            
        dialog.connect("response", on_response)
        dialog.show()

