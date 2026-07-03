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
import array
import fcntl
import select
from PIL import Image, ImageDraw, ImageFont

# Import gtk modules - used for the config rows
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
import globals as gl

RENDER_SCALE = 2

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
            '--property=application.id=org.PulseAudio.pavucontrol',
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

        smooth_val = 0.0
        decay = 0.85

        try:
            while self.running:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    continue
                try:
                    # Drain the pipe to keep real-time sync (8192 bytes ≈ 46ms of audio)
                    data = self.proc.stdout.read(8192)
                except (OSError, IOError) as e:
                    import errno
                    if getattr(e, 'errno', None) in (errno.EAGAIN, errno.EWOULDBLOCK):
                        time.sleep(0.005)
                    continue
                if not data:
                    break
                
                # Ensure the length is even for 16-bit short samples
                if len(data) % 2 != 0:
                    data = data[:-1]
                
                if data:
                    samples = array.array('h', data)
                    if samples:
                        # High-performance absolute peak calculation using builtins in C
                        max_val = max(samples)
                        min_val = min(samples)
                        peak_val = max(max_val, -min_val) / 32768.0
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
        self.active_device_index = 1
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
        self._cached_vol_mask = None
        self._cached_midground = None
        self._cached_midground_key = None
        self._current_peak = 0.0
        self._is_polling = False

        # Reusable draw masks & title layout cache for peak performance
        cx = 70 * RENDER_SCALE
        cy = 94 * RENDER_SCALE
        r_arc = 46 * RENDER_SCALE
        self._gx1 = cx - r_arc
        self._gy1 = cy - r_arc
        self._gx2 = cx + r_arc
        self._gy2 = cy
        self._sub_width = self._gx2 - self._gx1
        self._sub_height = self._gy2 - self._gy1
        self._peak_mask_sub = Image.new("L", (self._sub_width, self._sub_height), 0)
        self._peak_mask_sub_draw = ImageDraw.Draw(self._peak_mask_sub)
        self._sub_bbox = [(0, 0), (self._sub_width, self._sub_width)]
        self._last_title_text = None
        self._last_font_file = None
        self._last_font_name = None
        self._last_font_path = None
        self._last_title_font_size = None
        self._last_max_width = None
        self._resolved_title_text = None
        self._resolved_font_title = None
        self._peak_hold_val = 0.0
        self._peak_hold_ticks = 0
        self.poll_timer_id = 0
        self._event_proc = None

    def on_ready(self) -> None:
        self.running = True
        
        # Load initial status once (in a background thread to avoid GTK block)
        threading.Thread(target=self._initial_load_status, daemon=True).start()
        
        # Start persistent event listener for instant volume updates without CPU polling overhead
        self._start_event_listener()
        
        # Start GLib tick timer if live peak meter is enabled (increased to 25ms / 40 FPS for premium animation)
        settings = self.get_settings() or {}
        if settings.get("live_meter", True):
            self.tick_timer_id = GLib.timeout_add(25, self.on_tick_update)

    def _start_event_listener(self):
        threading.Thread(target=self._listen_for_volume_events, daemon=True).start()

    def _listen_for_volume_events(self):
        use_fallback_polling = True
        try:
            proc = subprocess.Popen(
                ["pactl", "subscribe"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True
            )
            self._event_proc = proc
            use_fallback_polling = False
        except Exception:
            pass

        if use_fallback_polling:
            # Fall back to periodic polling timer
            GLib.idle_add(self._start_fallback_polling)
            return

        # Keep reading lines from pactl subscribe
        while self.running and proc.poll() is None:
            line = proc.stdout.readline()
            if not line:
                break
            if "change" in line and ("sink" in line or "source" in line):
                # Trigger a non-blocking read of the volume status
                if not self._is_polling:
                    self._is_polling = True
                    threading.Thread(target=self._poll_system_volume_bg, daemon=True).start()

        if proc.poll() is None:
            proc.kill()

    def _start_fallback_polling(self):
        if not self.poll_timer_id and self.running:
            self.poll_timer_id = GLib.timeout_add(500, self.on_poll_tick)

    def on_poll_tick(self) -> bool:
        if not self.running:
            return False
        if not self._is_polling:
            self._is_polling = True
            threading.Thread(target=self._poll_system_volume_bg, daemon=True).start()
        return True

    def _initial_load_status(self):
        settings = self.get_settings() or {}
        updated = False
        if not settings.get("device_type"):
            settings["device_type"] = "sink"
            updated = True
            
        dtype = settings.get("device_type", "sink")
        sinks, sources = self.get_pipewire_devices()
        devices = sinks if dtype == "sink" else sources
        
        if not settings.get("pipewire_device_id") and devices:
            settings["pipewire_device_id"] = devices[0][0]
            settings["pipewire_device_name"] = devices[0][1]
            updated = True

        if not settings.get("device_type_2"):
            settings["device_type_2"] = "sink"
            updated = True
            
        dtype_2 = settings.get("device_type_2", "sink")
        devices_2 = sinks if dtype_2 == "sink" else sources
        if not settings.get("pipewire_device_id_2") and devices_2:
            primary_id = settings.get("pipewire_device_id")
            selected_dev = devices_2[0]
            if len(devices_2) > 1 and selected_dev[0] == primary_id:
                selected_dev = devices_2[1]
            settings["pipewire_device_id_2"] = selected_dev[0]
            settings["pipewire_device_name_2"] = selected_dev[1]
            updated = True
            
        if updated:
            self.set_settings(settings)
            
        vol, mute = self.get_system_volume_status()
        self.current_volume = vol
        self.last_mute = mute
        self.update_ui_rendering()
        if settings.get("live_meter", True):
            self.restart_peak_monitor()

    def on_remove(self) -> None:
        self.running = False
        if self.tick_timer_id:
            GLib.source_remove(self.tick_timer_id)
            self.tick_timer_id = 0
        if self.poll_timer_id:
            GLib.source_remove(self.poll_timer_id)
            self.poll_timer_id = 0
        if self._event_proc:
            try:
                self._event_proc.kill()
            except OSError:
                pass
            self._event_proc = None
        self.peak_monitor.stop()

    def on_disconnect(self) -> None:
        self.running = False
        if self.tick_timer_id:
            GLib.source_remove(self.tick_timer_id)
            self.tick_timer_id = 0
        if self.poll_timer_id:
            GLib.source_remove(self.poll_timer_id)
            self.poll_timer_id = 0
        if self._event_proc:
            try:
                self._event_proc.kill()
            except OSError:
                pass
            self._event_proc = None
        self.peak_monitor.stop()

    def _raw_event_callback(self, event: InputEvent, data: dict = None):
        if event == Input.Dial.Events.TURN_CW:
            self.change_volume(self.get_step_size())
        elif event == Input.Dial.Events.TURN_CCW:
            self.change_volume(-self.get_step_size())
        elif event in [Input.Dial.Events.DOWN, Input.Dial.Events.SHORT_TOUCH_PRESS, Input.Touchscreen.Events.DRAG_LEFT, Input.Touchscreen.Events.DRAG_RIGHT]:
            settings = self.get_settings() or {}
            device_switch_enabled = settings.get("device_switch", False)
            if event == Input.Dial.Events.DOWN:
                self.toggle_mute()
            elif event == Input.Dial.Events.SHORT_TOUCH_PRESS:
                if device_switch_enabled:
                    self.switch_active_device()
        else:
            super()._raw_event_callback(event, data)

    def event_callback(self, event: InputEvent, data: dict = None):
        if event == Input.Dial.Events.TURN_CW:
            self.change_volume(self.get_step_size())
        elif event == Input.Dial.Events.TURN_CCW:
            self.change_volume(-self.get_step_size())
        elif event in [Input.Dial.Events.DOWN, Input.Dial.Events.SHORT_TOUCH_PRESS, Input.Touchscreen.Events.DRAG_LEFT, Input.Touchscreen.Events.DRAG_RIGHT]:
            settings = self.get_settings() or {}
            device_switch_enabled = settings.get("device_switch", False)
            if event == Input.Dial.Events.DOWN:
                self.toggle_mute()
            elif event == Input.Dial.Events.SHORT_TOUCH_PRESS:
                if device_switch_enabled:
                    self.switch_active_device()
        else:
            super().event_callback(event, data)

    def get_active_device_type(self) -> str:
        settings = self.get_settings() or {}
        active = getattr(self, "active_device_index", 1)
        if active == 2 and settings.get("device_switch", False):
            return settings.get("device_type_2", "sink")
        return settings.get("device_type", "sink")

    def switch_active_device(self):
        settings = self.get_settings() or {}
        if not settings.get("device_switch", False):
            return
            
        self.active_device_index = 2 if getattr(self, "active_device_index", 1) == 1 else 1
        
        # Load the volume and mute status of the new active device quickly
        vol, mute = self.get_system_volume_status()
        self.current_volume = vol
        self.last_mute = mute
        
        self.restart_peak_monitor()
        self.update_ui_rendering(force=True)

    def get_step_size(self) -> int:
        settings = self.get_settings()
        if settings is not None:
            val = settings.get("step_size", "5%")
            try:
                return int(val.replace("%", ""))
            except ValueError:
                return 5
        return 5

    def get_live_meter(self) -> bool:
        settings = self.get_settings()
        if settings is not None:
            return settings.get("live_meter", True)
        return True

    def get_configured_device_id(self) -> str:
        settings = self.get_settings() or {}
        active = getattr(self, "active_device_index", 1)
        if active == 2 and settings.get("device_switch", False):
            dtype = settings.get("device_type_2", "sink")
            default_id = "@DEFAULT_AUDIO_SOURCE@" if dtype == "source" else "@DEFAULT_AUDIO_SINK@"
            dev_id = settings.get("pipewire_device_id_2", default_id)
        else:
            dtype = settings.get("device_type", "sink")
            default_id = "@DEFAULT_AUDIO_SOURCE@" if dtype == "source" else "@DEFAULT_AUDIO_SINK@"
            dev_id = settings.get("pipewire_device_id", default_id)
            
        if dev_id in ["@DEFAULT_AUDIO_SINK@", "@DEFAULT_SINK@"]:
            return "@DEFAULT_SINK@"
        elif dev_id in ["@DEFAULT_AUDIO_SOURCE@", "@DEFAULT_SOURCE@"]:
            return "@DEFAULT_SOURCE@"
        return dev_id

    def restart_peak_monitor(self):
        device_id = self.get_configured_device_id()
        dtype = self.get_active_device_type()
        is_source = (dtype == "source")
        self.peak_monitor.start(device_id, is_source)

    def on_tick_update(self) -> bool:
        if not self.running:
            return False
            
        raw_peak = self.peak_monitor.get_peak()
        # Apply a 1.5x gain boost to ensure standard audio peaks reach the red/orange zone at 100% volume
        raw_peak = max(0.0, min(1.0, raw_peak * 1.5))
        if raw_peak < 0.04:
            raw_peak = 0.0
            
        # Fast attack, slow release exponential smoothing for premium hardware meter physics
        if raw_peak >= self._current_peak:
            self._current_peak = raw_peak
        else:
            self._current_peak = max(raw_peak, self._current_peak * 0.88 - 0.005)
            
        peak = self._current_peak
        if peak < 0.01:
            peak = 0.0

        # Peak hold logic
        if raw_peak >= self._peak_hold_val:
            self._peak_hold_val = raw_peak
            self._peak_hold_ticks = 12  # Hold peak for ~300ms at 25ms interval
        else:
            if self._peak_hold_ticks > 0:
                self._peak_hold_ticks -= 1
            else:
                self._peak_hold_val = max(raw_peak, self._peak_hold_val * 0.96 - 0.003)
        
        peak_diff = abs(peak - self.last_drawn_peak)
        hold_diff = abs(self._peak_hold_val - self.last_drawn_hold)
        if (self.current_volume != self.last_drawn_volume or 
            self.last_mute != self.last_drawn_mute or 
            (peak > 0.0 and peak_diff > 0.01) or 
            (self._peak_hold_val > 0.0 and hold_diff > 0.01) or
            (peak == 0.0 and self.last_drawn_peak > 0.0) or
            (self._peak_hold_val == 0.0 and self.last_drawn_hold > 0.0)):
            
            self.last_drawn_hold = self._peak_hold_val
            self.update_ui_rendering(peak)
            
        return True

    def _poll_system_volume_bg(self):
        try:
            if not self.running:
                return
            vol, mute = self.get_system_volume_status()
            if vol != self.current_volume or mute != self.last_mute:
                self.current_volume = vol
                self.last_mute = mute
                GLib.idle_add(self.update_ui_rendering)
        finally:
            self._is_polling = False

    def update_ui_rendering(self, peak: float = 0.0, force: bool = False):
        if not force and not self.get_is_present():
            return
        
        with self._render_lock:
            self.last_drawn_volume = self.current_volume
            self.last_drawn_mute = self.last_mute
            self.last_drawn_peak = peak
            
            img = self.generate_volume_image(self.current_volume, self.last_mute, peak)
            GLib.idle_add(self.set_media, img)

    def run_cmd(self, cmd: list) -> str:
        try:
            env = os.environ.copy()
            env["LC_ALL"] = "C"
            return subprocess.check_output(cmd, text=True, env=env)
        except Exception:
            return ""

    def execute_cmd(self, cmd: list) -> None:
        try:
            env = os.environ.copy()
            env["LC_ALL"] = "C"
            subprocess.run(cmd, check=True, env=env)
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
        
        # Try pulsectl first (native Python binding, extremely robust and locale-independent)
        try:
            import pulsectl
            with pulsectl.Pulse("volume-controller-plus") as pulse:
                for s in pulse.sink_list():
                    sinks.append((s.name, s.description))
                for s in pulse.source_list():
                    if not s.name.endswith(".monitor"):
                        sources.append((s.name, s.description))
        except Exception:
            pass
            
        # Fallback to pactl if pulsectl is not available or fails
        if not sinks and not sources:
            try:
                sinks_out = self.run_cmd(["pactl", "list", "sinks"])
                sinks = self.parse_pactl_list(sinks_out)
            except Exception:
                pass
            try:
                sources_out = self.run_cmd(["pactl", "list", "sources"])
                sources = self.parse_pactl_list(sources_out)
                sources = [(n, d) for n, d in sources if not n.endswith(".monitor")]
            except Exception:
                pass
                
        return sinks, sources

    def get_pipewire_status(self, device_id: str) -> "tuple[int, bool]":
        dtype = self.get_active_device_type()
        
        # Try pulsectl first
        try:
            import pulsectl
            with pulsectl.Pulse("volume-controller-plus-status") as pulse:
                devs = pulse.sink_list() if dtype == "sink" else pulse.source_list()
                for d in devs:
                    if d.name == device_id:
                        vol = int(round(pulse.volume_get_all_chans(d) * 100))
                        mute = bool(d.mute)
                        return vol, mute
        except Exception:
            pass
            
        # Fallback to pactl
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
        dtype = self.get_active_device_type()
        
        # Try pulsectl first
        try:
            import pulsectl
            with pulsectl.Pulse("volume-controller-plus-volume") as pulse:
                devs = pulse.sink_list() if dtype == "sink" else pulse.source_list()
                for d in devs:
                    if d.name == device_id:
                        pulse.volume_set_all_chans(d, target_vol / 100.0)
                        return
        except Exception:
            pass
            
        # Fallback to pactl
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
        dtype = self.get_active_device_type()
        
        # Try pulsectl first
        try:
            import pulsectl
            with pulsectl.Pulse("volume-controller-plus-mute") as pulse:
                devs = pulse.sink_list() if dtype == "sink" else pulse.source_list()
                for d in devs:
                    if d.name == device_id:
                        pulse.mute(d, not d.mute)
                        return
        except Exception:
            pass
            
        # Fallback to pactl
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

    def _get_gauge_gradient_image(self, width: int, height: int, bbox: list) -> Image.Image:
        with self._render_lock:
            if self._gauge_gradient_img is not None:
                return self._gauge_gradient_img
                
            grad_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            grad_draw = ImageDraw.Draw(grad_img)
            for angle in range(180, 360):
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
                    
                grad_draw.arc(bbox, start=angle, end=angle+2, fill=(r_col, g_col, b_col, 255), width=7 * RENDER_SCALE)
                
            self._gauge_gradient_img = grad_img
            return self._gauge_gradient_img

    def _get_gauge_gradient_image_sub(self, width: int, height: int, bbox: list) -> Image.Image:
        with self._render_lock:
            if self._gauge_gradient_img_sub is not None:
                return self._gauge_gradient_img_sub
            
            grad_img = self._get_gauge_gradient_image(width, height, bbox)
            self._gauge_gradient_img_sub = grad_img.crop((self._gx1, self._gy1, self._gx2, self._gy2))
            return self._gauge_gradient_img_sub

    def generate_volume_image(self, volume: int, is_muted: bool, peak: float = 0.0) -> Image.Image:
        width, height = 200 * RENDER_SCALE, 100 * RENDER_SCALE
        
        # 1. Load/Generate Base Background with Ticks & Gauge Track (cached to avoid drawing lines/arcs every frame)
        if self._cached_base_bg is None:
            if self.bg_image is None:
                bg_path = os.path.join(self.plugin_base.PATH, "assets", "background-volume.png")
                try:
                    self.bg_image = Image.open(bg_path).convert("RGBA")
                except Exception:
                    pass
                    
            if self.bg_image is not None:
                bg = self.bg_image.resize((width, height), Image.Resampling.LANCZOS)
            else:
                bg = Image.new("RGBA", (width, height), (28, 28, 28, 255))
                
            bg_draw = ImageDraw.Draw(bg)
            
            # Pre-render Ticks (broken into quarters - 17 ticks total, every 11.25 degrees)
            cx_bg, cy_bg = 70 * RENDER_SCALE, 94 * RENDER_SCALE
            r_tick_major_start = 49 * RENDER_SCALE
            r_tick_major_end = 58 * RENDER_SCALE
            r_tick_minor_start = 53 * RENDER_SCALE
            r_tick_minor_end = 57 * RENDER_SCALE
            
            for i in range(17):
                tick_angle = 180 + i * 11.25
                rad = math.radians(tick_angle)
                if i % 4 == 0:
                    r_tick_start = r_tick_major_start
                    r_tick_end = r_tick_major_end
                    w = 3 * RENDER_SCALE
                    color = (160, 162, 175, 255)
                else:
                    r_tick_start = r_tick_minor_start
                    r_tick_end = r_tick_minor_end
                    w = 1 * RENDER_SCALE
                    color = (110, 112, 120, 255)
                    
                x1 = cx_bg + r_tick_start * math.cos(rad)
                y1 = cy_bg + r_tick_start * math.sin(rad)
                x2 = cx_bg + r_tick_end * math.cos(rad)
                y2 = cy_bg + r_tick_end * math.sin(rad)
                bg_draw.line([(x1, y1), (x2, y2)], fill=color, width=w)
                
            # Pre-render Gauge Track (inactive - dark background arc)
            r_arc_bg = 46 * RENDER_SCALE
            bbox_bg = [(cx_bg - r_arc_bg, cy_bg - r_arc_bg), (cx_bg + r_arc_bg, cy_bg + r_arc_bg)]
            bg_draw.arc(bbox_bg, start=180, end=360, fill=(38, 38, 42, 255), width=7 * RENDER_SCALE)
            
            self._cached_base_bg = bg

        # 2. Get settings/labels that form the midground cache key
        settings = self.get_settings() or {}
        active = getattr(self, "active_device_index", 1)
        device_switch_enabled = settings.get("device_switch", False)
        
        if active == 2 and device_switch_enabled:
            custom_icon_path = settings.get("custom_icon_2", "")
            custom_name = settings.get("custom_name_2", "")
            pw_name = settings.get("pipewire_device_name_2", "Default Sink")
            dtype = settings.get("device_type_2", "sink")
        else:
            custom_icon_path = settings.get("custom_icon", "")
            custom_name = settings.get("custom_name", "")
            pw_name = settings.get("pipewire_device_name", "Default Sink")
            dtype = settings.get("device_type", "sink")
            
        title_text = custom_name if custom_name else pw_name
        font_name = settings.get("font_name", "DejaVu Sans Bold 15")
        font_path = settings.get("font_path", "")

        midground_key = (
            volume,
            is_muted,
            title_text,
            custom_icon_path,
            dtype,
            font_name,
            font_path,
            active,
            device_switch_enabled
        )

        cx, cy = 70 * RENDER_SCALE, 94 * RENDER_SCALE
        r_outer = 43 * RENDER_SCALE
        r_inner = 40 * RENDER_SCALE
        r_arc = 46 * RENDER_SCALE
        bbox = [(cx - r_arc, cy - r_arc), (cx + r_arc, cy + r_arc)]

        # If cache misses, rebuild static midground card (Text, Icon, Background tracks)
        if self._cached_midground is None or self._cached_midground_key != midground_key:
            mid_img = self._cached_base_bg.copy()
            mid_draw = ImageDraw.Draw(mid_img)

            # Draw Volume Text (right of the knob, centered vertically)
            vol_text = "MUTE" if is_muted else f"{volume}%"
            vol_color = (239, 68, 68, 255) if is_muted else (255, 255, 255, 255)
            
            # Resolve and cache fonts if they have changed or are not cached
            if (self._cached_font_title is None or 
                self._cached_font_vol is None or 
                font_name != self._cached_font_name or 
                font_path != self._cached_font_path):
                
                title_font_size = 14
                font_file = None
                
                if font_name:
                    import re
                    match = re.search(r'\s+(\d+)$', font_name.strip())
                    if match:
                        title_font_size = int(match.group(1))
                    
                    resolved_path = self.font_name_to_path(font_name)
                    if resolved_path and os.path.exists(resolved_path):
                        font_file = resolved_path
                        
                if not font_file and font_path and os.path.exists(font_path):
                    font_file = font_path
                    
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
                            
                vol_font_file = None
                for path in [
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "/usr/share/fonts/ubuntu/Ubuntu-B.ttf",
                    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
                    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
                    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"
                ]:
                    if os.path.exists(path):
                        vol_font_file = path
                        break
                        
                try:
                    if font_file:
                        self._cached_font_title = ImageFont.truetype(font_file, title_font_size * RENDER_SCALE)
                    else:
                        self._cached_font_title = ImageFont.load_default()
                except Exception:
                    self._cached_font_title = ImageFont.load_default()
                    
                try:
                    if vol_font_file:
                        self._cached_font_vol = ImageFont.truetype(vol_font_file, 19 * RENDER_SCALE)
                    else:
                        self._cached_font_vol = ImageFont.load_default()
                except Exception:
                    self._cached_font_vol = ImageFont.load_default()
                    
                self._cached_font_file = font_file
                self._cached_title_font_size = title_font_size
                self._cached_font_name = font_name
                self._cached_font_path = font_path

            font_title = self._cached_font_title
            font_vol = self._cached_font_vol
            font_file = self._cached_font_file
            title_font_size = self._cached_title_font_size

            try:
                vol_w = font_vol.getlength(vol_text)
            except Exception:
                vol_w = 40
                
            try:
                mid_draw.text((165 * RENDER_SCALE, 68 * RENDER_SCALE), vol_text, font=font_vol, fill=vol_color, anchor="mm")
            except TypeError:
                vol_w_unscaled = vol_w / RENDER_SCALE
                mid_draw.text((int((165 - vol_w_unscaled / 2) * RENDER_SCALE), int((68 - 10) * RENDER_SCALE)), vol_text, font=font_vol, fill=vol_color)

            # Icon Placement Area
            icon_drawn = False
            icon_w = 16
            if not custom_icon_path:
                icon_filename = "input.png" if dtype == "source" else "output.png"
                custom_icon_path = os.path.join(self.plugin_base.PATH, "assets", icon_filename)
            icon_scale = 2.0

            if custom_icon_path:
                if custom_icon_path != self._cached_icon_path or self._cached_icon_img is None:
                    loaded_img = self.load_icon_image(custom_icon_path)
                    if loaded_img is not None:
                        loaded_img = loaded_img.convert("RGBA")
                        orig_w, orig_h = loaded_img.size
                        base_size = 14
                        scaled_size = max(4, min(int(base_size * icon_scale), 28))
                        target_max = scaled_size * RENDER_SCALE
                        if orig_w > orig_h:
                            new_w = target_max
                            new_h = max(1, int(orig_h * target_max / orig_w))
                        else:
                            new_h = target_max
                            new_w = max(1, int(orig_w * target_max / orig_h))
                        self._cached_icon_img = loaded_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    else:
                        self._cached_icon_img = None
                    self._cached_icon_path = custom_icon_path
                    
                if self._cached_icon_img is not None:
                    icon_img = self._cached_icon_img.copy()
                    icon_w_unscaled = icon_img.width // RENDER_SCALE
                    icon_h_unscaled = icon_img.height // RENDER_SCALE
                    x_start = 12
                    y_start = 16 - icon_h_unscaled // 2
                    y_start = max(6, min(y_start, 38 - icon_h_unscaled))
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
                    mid_draw.arc([
                        (spk_x * RENDER_SCALE, (spk_y - 1) * RENDER_SCALE), 
                        ((spk_x + 18) * RENDER_SCALE, (spk_y + 15) * RENDER_SCALE)
                    ], start=-45, end=45, fill=wave_color, width=2 * RENDER_SCALE)
                    mid_draw.arc([
                        ((spk_x - 3) * RENDER_SCALE, (spk_y - 4) * RENDER_SCALE), 
                        ((spk_x + 23) * RENDER_SCALE, (spk_y + 18) * RENDER_SCALE)
                    ], start=-45, end=45, fill=wave_color, width=2 * RENDER_SCALE)
                icon_w = 26

            # Title Text (wrapping and size calculation)
            left_bound = 12 + icon_w + 6
            right_bound = 168 - 6
            max_width = right_bound - left_bound - 4

            if (self._resolved_title_text is not None and
                self._resolved_font_title is not None and
                title_text == self._last_title_text and
                font_file == self._last_font_file and
                font_name == self._last_font_name and
                font_path == self._last_font_path and
                title_font_size == self._last_title_font_size and
                max_width == self._last_max_width):
                
                title_text_to_draw = self._resolved_title_text
                font_title_to_draw = self._resolved_font_title
            else:
                self._last_title_text = title_text
                self._last_font_file = font_file
                self._last_font_name = font_name
                self._last_font_path = font_path
                self._last_title_font_size = title_font_size
                self._last_max_width = max_width

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
                        
                        try:
                            text_w = temp_font.getlength(title_text_to_draw)
                        except Exception:
                            text_w = len(title_text_to_draw) * (current_size * RENDER_SCALE * 0.6)
                        font_title_to_draw = temp_font
                    except Exception:
                        break

                while text_w > max_width_scaled and len(title_text_to_draw) > 3:
                    title_text_to_draw = title_text_to_draw[:-3] + ".."
                    try:
                        text_w = font_title_to_draw.getlength(title_text_to_draw)
                    except Exception:
                        text_w = len(title_text_to_draw) * (current_size * RENDER_SCALE * 0.6)
                
                self._resolved_title_text = title_text_to_draw
                self._resolved_font_title = font_title_to_draw
            
            try:
                mid_draw.text((left_bound * RENDER_SCALE, 16 * RENDER_SCALE), title_text_to_draw, font=font_title_to_draw, fill=(220, 222, 230, 255), anchor="lm")
            except TypeError:
                mid_draw.text((left_bound * RENDER_SCALE, (16 - 8) * RENDER_SCALE), title_text_to_draw, font=font_title_to_draw, fill=(220, 222, 230, 255))

            # Draw Device Switch Icon (always drawn now, but uses different assets based on device_switch_enabled)
            icon_to_draw = None
            if device_switch_enabled:
                if not hasattr(self, "_cached_device_switch_img_on") or self._cached_device_switch_img_on is None:
                    dev_switch_path = os.path.join(self.plugin_base.PATH, "assets", "device.png")
                    try:
                        loaded_img = Image.open(dev_switch_path).convert("RGBA")
                        self._cached_device_switch_img_on = loaded_img.resize((22 * RENDER_SCALE, 22 * RENDER_SCALE), Image.Resampling.LANCZOS)
                    except Exception:
                        self._cached_device_switch_img_on = None
                icon_to_draw = getattr(self, "_cached_device_switch_img_on", None)
            else:
                if not hasattr(self, "_cached_device_switch_img_off") or self._cached_device_switch_img_off is None:
                    dev_switch_path = os.path.join(self.plugin_base.PATH, "assets", "device_off.png")
                    try:
                        loaded_img = Image.open(dev_switch_path).convert("RGBA")
                        self._cached_device_switch_img_off = loaded_img.resize((22 * RENDER_SCALE, 22 * RENDER_SCALE), Image.Resampling.LANCZOS)
                    except Exception:
                        self._cached_device_switch_img_off = None
                icon_to_draw = getattr(self, "_cached_device_switch_img_off", None)

            if icon_to_draw is not None:
                mid_img.paste(icon_to_draw, (int(168 * RENDER_SCALE), int(28 * RENDER_SCALE)), icon_to_draw)

            # Dimmed volume level gradient arc OR blue volume meter (pre-rendered in midground)
            if not is_muted:
                is_live_enabled = settings.get("live_meter", True)
                if is_live_enabled:
                    grad_img = self._get_gauge_gradient_image(width, height, bbox)
                    if self._cached_vol_mask is None:
                        vol_mask = Image.new("L", (width, height), 0)
                        vol_mask_draw = ImageDraw.Draw(vol_mask)
                        vol_mask_draw.arc(bbox, start=180, end=360, fill=75, width=7 * RENDER_SCALE)
                        self._cached_vol_mask = vol_mask
                    mid_img.paste(grad_img, (0, 0), self._cached_vol_mask)
                else:
                    vol_angle = int(180 + 180 * (volume / 100.0))
                    if vol_angle > 180:
                        mid_draw.arc(bbox, start=180, end=vol_angle, fill=(0, 168, 255, 255), width=7 * RENDER_SCALE)

            self._cached_midground = mid_img
            self._cached_midground_key = midground_key

        # 3. Instantiate dynamic frame image from cached midground
        img = self._cached_midground.copy()
        draw = ImageDraw.Draw(img)
        
        # 4. Draw Active Gauge Segments: live audio peak and peak-hold marker
        if not is_muted:
            is_live_enabled = settings.get("live_meter", True)
            if is_live_enabled:
                # Bouncing audio peak arc
                if peak > 0.04:
                    scaled_peak = peak * (volume / 100.0)
                    peak_angle = int(180 + 180 * scaled_peak)
                    if peak_angle > 180:
                        if peak >= 0.99 or scaled_peak >= 0.99:
                            # Make the active meter solid red when it reaches 100% peak
                            draw.arc(bbox, start=180, end=min(360, peak_angle), fill=(255, 30, 30, 255), width=7 * RENDER_SCALE)
                        else:
                            # Reuse the pre-allocated sub-mask to avoid heavy object instantiation
                            self._peak_mask_sub_draw.rectangle([(0, 0), (self._sub_width, self._sub_height)], fill=0)
                            self._peak_mask_sub_draw.arc(self._sub_bbox, start=180, end=peak_angle, fill=255, width=7 * RENDER_SCALE)
                            grad_img_sub = self._get_gauge_gradient_image_sub(width, height, bbox)
                            img.paste(grad_img_sub, (self._gx1, self._gy1), self._peak_mask_sub)

                # Peak Hold marker (Floating bright indicator for studio console aesthetics)
                if self._peak_hold_val > 0.04:
                    scaled_hold = self._peak_hold_val * (volume / 100.0)
                    hold_angle = int(180 + 180 * scaled_hold)
                    if hold_angle > 180:
                        # Draw a small 2-degree bright highlight indicator directly on the image
                        draw.arc(bbox, start=hold_angle - 1, end=hold_angle + 1, fill=(255, 75, 75, 255), width=7 * RENDER_SCALE)

        # 5. Draw Inner Knob Core (Outer shadow/border + inner core chord & top curve arc)
        bbox_outer = [(cx - r_outer, cy - r_outer), (cx + r_outer, cy + r_outer)]
        draw.chord(bbox_outer, start=180, end=360, fill=(18, 18, 20, 255))
        bbox_inner = [(cx - r_inner, cy - r_inner), (cx + r_inner, cy + r_inner)]
        draw.chord(bbox_inner, start=180, end=360, fill=(28, 28, 32, 255))
        draw.arc(bbox_inner, start=180, end=360, fill=(60, 62, 72, 255), width=1 * RENDER_SCALE)
        
        # 6. Draw Pointer line on top of the knob
        pointer_angle = 180 + 180 * (volume / 100.0)
        rad_pt = math.radians(pointer_angle)
        xp1 = cx + 12 * RENDER_SCALE * math.cos(rad_pt)
        yp1 = cy + 12 * RENDER_SCALE * math.sin(rad_pt)
        xp2 = cx + 34 * RENDER_SCALE * math.cos(rad_pt)
        yp2 = cy + 34 * RENDER_SCALE * math.sin(rad_pt)
        pointer_color = (239, 68, 68, 255) if is_muted else (240, 242, 250, 255)
        draw.line([(xp1, yp1), (xp2, yp2)], fill=pointer_color, width=3 * RENDER_SCALE)
        
        if RENDER_SCALE > 1:
            return img.resize((200, 100), Image.Resampling.BILINEAR)
        return img

    def get_font_path(self) -> str:
        settings = self.get_settings()
        if settings is not None:
            return settings.get("font_path", "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf")
        return "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"


    def update_device_dropdown(self):
        if not hasattr(self, "pw_device_selector"):
            return
        self._updating_dropdown = True
        try:
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
                    
            self.pw_device_selector.set_selected(selected_index)
        finally:
            self._updating_dropdown = False

    def update_device_dropdown_2(self):
        if not hasattr(self, "pw_device_selector_2"):
            return
        self._updating_dropdown_2 = True
        try:
            settings = self.get_settings() or {}
            dtype_2 = settings.get("device_type_2", "sink")
            
            self.pw_devices_map_2 = []
            if dtype_2 == "sink":
                sinks, _ = self.get_pipewire_devices()
                for s_id, s_name in sinks:
                    self.pw_devices_map_2.append((s_id, s_name))
            else:
                _, sources = self.get_pipewire_devices()
                for s_id, s_name in sources:
                    self.pw_devices_map_2.append((s_id, s_name))
                    
            self.pw_device_model_2 = Gtk.StringList()
            for pw_id, display_name in self.pw_devices_map_2:
                self.pw_device_model_2.append(display_name)
                
            self.pw_device_selector_2.set_model(self.pw_device_model_2)
            
            current_pw_id = settings.get("pipewire_device_id_2")
            if not current_pw_id or not any(pw_id == current_pw_id for pw_id, _ in self.pw_devices_map_2):
                if self.pw_devices_map_2:
                    current_pw_id = self.pw_devices_map_2[0][0]
                    settings["pipewire_device_id_2"] = current_pw_id
                    settings["pipewire_device_name_2"] = self.pw_devices_map_2[0][1]
                    self.set_settings(settings)
                else:
                    current_pw_id = ""
                    settings["pipewire_device_id_2"] = ""
                    settings["pipewire_device_name_2"] = ""
                    self.set_settings(settings)
                
            selected_index = 0
            for idx, (pw_id, display_name) in enumerate(self.pw_devices_map_2):
                if pw_id == current_pw_id:
                    selected_index = idx
                    break
                    
            self.pw_device_selector_2.set_selected(selected_index)
        finally:
            self._updating_dropdown_2 = False

    def update_visibility(self, active: bool):
        if hasattr(self, "type_selector_2"):
            self.type_selector_2.set_visible(active)
        if hasattr(self, "pw_device_selector_2"):
            self.pw_device_selector_2.set_visible(active)
        if hasattr(self, "custom_name_row_2"):
            self.custom_name_row_2.set_visible(active)
        if hasattr(self, "icon_row_2"):
            self.icon_row_2.set_visible(active)
        if hasattr(self, "custom_name_row"):
            if active:
                self.custom_name_row.set_title("Device Name 1")
            else:
                self.custom_name_row.set_title("Device Name")
        if hasattr(self, "icon_row"):
            if active:
                self.icon_row.set_title("Device Icon 1")
            else:
                self.icon_row.set_title("Device Icon")

    def get_config_rows(self) -> "list[Adw.PreferencesRow]":
        settings = self.get_settings() or {}
        dtype = settings.get("device_type", "sink")

        # 1. Custom Name Row
        self.custom_name_row = Adw.EntryRow(
            title="Device Name",
            text=settings.get("custom_name", "")
        )

        # 1b. Custom Name 2 Row
        self.custom_name_row_2 = Adw.EntryRow(
            title="Device Name 2",
            text=settings.get("custom_name_2", "")
        )

        # 2. Device Type Selector
        self.type_model = Gtk.StringList()
        self.type_model.append("Output (sink)")
        self.type_model.append("Input (source)")
        self.type_selector = Adw.ComboRow(
            model=self.type_model,
            title="Device Type"
        )
        self.type_selector.set_selected(0 if dtype == "sink" else 1)

        # 3. PipeWire Device Selector ComboRow
        self.pw_device_selector = Adw.ComboRow(
            title="PipeWire Device"
        )
        
        # Populate initial list
        self.update_device_dropdown()

        # 3b. Device Switch Row
        self.device_switch_row = Adw.SwitchRow(
            title="Device Switch"
        )
        device_switch_active = settings.get("device_switch", False)
        self.device_switch_row.set_active(device_switch_active)

        # 3c. Device Type 2 Selector
        dtype_2 = settings.get("device_type_2", "sink")
        self.type_model_2 = Gtk.StringList()
        self.type_model_2.append("Output (sink)")
        self.type_model_2.append("Input (source)")
        self.type_selector_2 = Adw.ComboRow(
            model=self.type_model_2,
            title="Device Type 2"
        )
        self.type_selector_2.set_selected(0 if dtype_2 == "sink" else 1)

        # 3d. PipeWire Device 2 Selector ComboRow
        self.pw_device_selector_2 = Adw.ComboRow(
            title="PipeWire Device 2"
        )
        self.update_device_dropdown_2()
        
        # 4. Step size selector
        self.step_model = Gtk.StringList()
        step_sizes = ["1%", "2%", "5%", "10%"]
        for size in step_sizes:
            self.step_model.append(size)
            
        self.step_selector = Adw.ComboRow(
            model=self.step_model,
            title="Volume Step Size"
        )
        
        current_step = f"{self.get_step_size()}%"
        if current_step in step_sizes:
            self.step_selector.set_selected(step_sizes.index(current_step))
        else:
            self.step_selector.set_selected(2) # Default to 5%
            
        # 5. Live Meter Toggle Row
        self.live_meter_row = Adw.SwitchRow(
            title="Live Peak Meter"
        )
        is_live_meter_enabled = settings.get("live_meter", True)
        self.live_meter_row.set_active(is_live_meter_enabled)

        # 6. Custom Icon selection
        self.icon_row = Adw.ActionRow(
            title="Device Icon"
        )
        
        self.choose_icon_button = Gtk.Button.new_from_icon_name("document-open-symbolic")
        self.choose_icon_button.set_valign(Gtk.Align.CENTER)
        self.choose_icon_button.set_tooltip_text("Choose Icon")
        
        self.clear_icon_button = Gtk.Button.new_from_icon_name("edit-clear-symbolic")
        self.clear_icon_button.set_valign(Gtk.Align.CENTER)
        self.clear_icon_button.set_tooltip_text("Clear Icon")
        
        self.icon_row.add_suffix(self.choose_icon_button)
        self.icon_row.add_suffix(self.clear_icon_button)

        # 6b. Custom Icon 2 selection
        self.icon_row_2 = Adw.ActionRow(
            title="Device Icon 2"
        )
        
        self.choose_icon_button_2 = Gtk.Button.new_from_icon_name("document-open-symbolic")
        self.choose_icon_button_2.set_valign(Gtk.Align.CENTER)
        self.choose_icon_button_2.set_tooltip_text("Choose Icon 2")
        
        self.clear_icon_button_2 = Gtk.Button.new_from_icon_name("edit-clear-symbolic")
        self.clear_icon_button_2.set_valign(Gtk.Align.CENTER)
        self.clear_icon_button_2.set_tooltip_text("Clear Icon 2")
        
        self.icon_row_2.add_suffix(self.choose_icon_button_2)
        self.icon_row_2.add_suffix(self.clear_icon_button_2)

        # 7. Custom Font Row (using FontChooserDialog)
        friendly_font_name = settings.get("font_name", "DejaVu Sans Bold 15")
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
        self.custom_name_row_2.connect("notify::text", self.on_custom_name_2_changed)
        self.type_selector.connect("notify::selected-item", self.on_device_type_changed)
        self.pw_device_selector.connect("notify::selected-item", self.on_pw_device_changed)
        self.device_switch_row.connect("notify::active", self.on_device_switch_toggled)
        self.type_selector_2.connect("notify::selected-item", self.on_device_type_2_changed)
        self.pw_device_selector_2.connect("notify::selected-item", self.on_pw_device_2_changed)
        self.step_selector.connect("notify::selected-item", self.on_step_changed)
        self.live_meter_row.connect("notify::active", self.on_live_meter_toggled)
        self.choose_icon_button.connect("clicked", self.on_choose_icon_clicked)
        self.clear_icon_button.connect("clicked", self.on_clear_icon_clicked)
        self.choose_icon_button_2.connect("clicked", self.on_choose_icon_2_clicked)
        self.clear_icon_button_2.connect("clicked", self.on_clear_icon_2_clicked)
        self.font_row.connect("activated", self.on_choose_font_clicked)
        self.choose_font_button.connect("clicked", self.on_choose_font_clicked)
        
        # Update clear button sensitivity
        icon_path = settings.get("custom_icon", "")
        self.clear_icon_button.set_sensitive(bool(icon_path))
        icon_path_2 = settings.get("custom_icon_2", "")
        self.clear_icon_button_2.set_sensitive(bool(icon_path_2))
        
        # Create Text (Device Name) Expander Row
        self.text_expander = Adw.ExpanderRow(
            title="Device Name"
        )
        self.text_expander.add_row(self.custom_name_row)
        self.text_expander.add_row(self.custom_name_row_2)
        self.text_expander.add_row(self.font_row)

        # Create Icon Expander Row
        self.icon_expander = Adw.ExpanderRow(
            title="Icon Configuration"
        )
        self.icon_expander.add_row(self.icon_row)
        self.icon_expander.add_row(self.icon_row_2)
        
        # Update visibility of the secondary device rows based on switch state
        self.update_visibility(device_switch_active)
        
        return [
            self.text_expander,
            self.type_selector,
            self.pw_device_selector,
            self.device_switch_row,
            self.type_selector_2,
            self.pw_device_selector_2,
            self.step_selector,
            self.live_meter_row,
            self.icon_expander
        ]

    def on_custom_name_changed(self, entry, *args):
        settings = self.get_settings() or {}
        settings["custom_name"] = entry.get_text()
        self.set_settings(settings)
        self.update_ui_rendering(force=True)

    def on_custom_name_2_changed(self, entry, *args):
        settings = self.get_settings() or {}
        settings["custom_name_2"] = entry.get_text()
        self.set_settings(settings)
        self.update_ui_rendering(force=True)

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
        if getattr(self, "active_device_index", 1) == 1:
            self.restart_peak_monitor()
        self.update_ui_rendering(force=True)

    def on_device_type_2_changed(self, combo, *args):
        selected_index = combo.get_selected()
        new_type = "sink" if selected_index == 0 else "source"
        
        settings = self.get_settings() or {}
        settings["device_type_2"] = new_type
        
        sinks, sources = self.get_pipewire_devices()
        devices = sinks if new_type == "sink" else sources
        if devices:
            settings["pipewire_device_id_2"] = devices[0][0]
            settings["pipewire_device_name_2"] = devices[0][1]
        else:
            settings["pipewire_device_id_2"] = ""
            settings["pipewire_device_name_2"] = ""
        self.set_settings(settings)
        
        self.update_device_dropdown_2()
        
        if getattr(self, "active_device_index", 1) == 2:
            self.restart_peak_monitor()
        self.update_ui_rendering(force=True)

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
            
            if getattr(self, "active_device_index", 1) == 1:
                self.restart_peak_monitor()
            self.update_ui_rendering(force=True)

    def on_pw_device_2_changed(self, combo, *args):
        if getattr(self, "_updating_dropdown_2", False):
            return
        selected_index = combo.get_selected()
        if 0 <= selected_index < len(self.pw_devices_map_2):
            pw_id, display_name = self.pw_devices_map_2[selected_index]
            settings = self.get_settings() or {}
            settings["pipewire_device_id_2"] = pw_id
            settings["pipewire_device_name_2"] = display_name
            self.set_settings(settings)
            
            if getattr(self, "active_device_index", 1) == 2:
                self.restart_peak_monitor()
            self.update_ui_rendering(force=True)

    def on_step_changed(self, combo, *args):
        settings = self.get_settings() or {}
        selected_item = combo.get_selected_item()
        if selected_item is not None:
            settings["step_size"] = selected_item.get_string()
            self.set_settings(settings)

    def on_live_meter_toggled(self, row, *args):
        settings = self.get_settings() or {}
        settings["live_meter"] = row.get_active()
        self.set_settings(settings)
        
        # Stop or restart timers/threads based on the new setting
        if not settings["live_meter"]:
            if self.tick_timer_id:
                GLib.source_remove(self.tick_timer_id)
                self.tick_timer_id = 0
            self.peak_monitor.stop()
        else:
            self.restart_peak_monitor()
            # Start timer at 40 FPS (25ms interval) for premium animation
            if not self.tick_timer_id and self.running:
                self.tick_timer_id = GLib.timeout_add(25, self.on_tick_update)
                
        self.update_ui_rendering(force=True)

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
            self.update_ui_rendering(force=True)
            
        GLib.idle_add(gl.app.let_user_select_asset, current_val, on_select_callback)

    def on_choose_icon_2_clicked(self, button):
        settings = self.get_settings() or {}
        current_val = settings.get("custom_icon_2", "")
        
        def on_select_callback(path):
            if not path:
                return
            settings = self.get_settings() or {}
            settings["custom_icon_2"] = path
            self.set_settings(settings)
            
            self.clear_icon_button_2.set_sensitive(True)
            self.update_ui_rendering(force=True)
            
        GLib.idle_add(gl.app.let_user_select_asset, current_val, on_select_callback)

    def on_clear_icon_clicked(self, button):
        settings = self.get_settings() or {}
        settings["custom_icon"] = ""
        self.set_settings(settings)
        
        self.clear_icon_button.set_sensitive(False)
        self.update_ui_rendering(force=True)

    def on_clear_icon_2_clicked(self, button):
        settings = self.get_settings() or {}
        settings["custom_icon_2"] = ""
        self.set_settings(settings)
        
        self.clear_icon_button_2.set_sensitive(False)
        self.update_ui_rendering(force=True)

    def on_device_switch_toggled(self, row, *args):
        active = row.get_active()
        settings = self.get_settings() or {}
        settings["device_switch"] = active
        self.set_settings(settings)
        
        self.update_visibility(active)
        
        if not active:
            self.active_device_index = 1
            vol, mute = self.get_system_volume_status()
            self.current_volume = vol
            self.last_mute = mute
            self.restart_peak_monitor()
            
        self.update_ui_rendering(force=True)

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
        self.update_ui_rendering(force=True)

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
            if response_id in [Gtk.ResponseType.ACCEPT, Gtk.ResponseType.OK]:
                font_name = dialog.get_font()
                GLib.idle_add(self.update_font_setting, font_name)
            dialog.destroy()
            
        dialog.connect("response", on_response)
        dialog.present()

