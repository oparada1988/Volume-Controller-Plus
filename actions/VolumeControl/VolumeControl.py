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

        is_flatpak = os.path.exists("/.flatpak-info") or os.environ.get("FLATPAK_ID") is not None
        node_name = f"vcp_monitor_{id(self)}"

        # 1. Inside Flatpak container: spawn pw-record on host via flatpak-spawn
        if is_flatpak:
            cmd = [
                'flatpak-spawn', '--host',
                'pw-record',
                '-P', f'node.name={node_name}',
                '--raw',
                '--format=s16',
                '--rate=48000',
                '--channels=2',
                '--latency=20ms',
                '-'
            ]
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                time.sleep(0.12)
                if not self.is_source:
                    try:
                        out = subprocess.check_output(['flatpak-spawn', '--host', 'pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
                        mon_fl, mon_fr = None, None
                        for l in out.splitlines():
                            line = l.strip()
                            if 'iec958' in line.lower():
                                continue
                            if 'monitor_FL' in line and ('analog' in line or 'pci' in line or 'output' in line):
                                mon_fl = line
                            elif 'monitor_FR' in line and ('analog' in line or 'pci' in line or 'output' in line):
                                mon_fr = line
                        if mon_fl and mon_fr:
                            subprocess.run(['flatpak-spawn', '--host', 'pw-link', '-d', 'alsa_input.usb-3142_fifine_Microphone-00.analog-stereo:capture_FL', f'{node_name}:input_FL'], stderr=subprocess.DEVNULL)
                            subprocess.run(['flatpak-spawn', '--host', 'pw-link', '-d', 'alsa_input.usb-3142_fifine_Microphone-00.analog-stereo:capture_FR', f'{node_name}:input_FR'], stderr=subprocess.DEVNULL)
                            subprocess.run(['flatpak-spawn', '--host', 'pw-link', mon_fl, f'{node_name}:input_FL'], stderr=subprocess.DEVNULL)
                            subprocess.run(['flatpak-spawn', '--host', 'pw-link', mon_fr, f'{node_name}:input_FR'], stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
            except Exception:
                self.proc = None

        # 2. Native host fallback (pw-record, parecord, parec)
        if not self.proc:
            cmd = [
                'pw-record',
                '-P', f'node.name={node_name}',
                '--raw',
                '--format=s16',
                '--rate=48000',
                '--channels=2',
                '--latency=20ms',
                '-'
            ]
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except Exception:
                cmd_pa = [
                    'parecord',
                    '--raw',
                    '--format=s16le',
                    '--channels=2',
                    '--rate=44100',
                    '--latency-msec=30',
                    '--device=' + target_device
                ]
                try:
                    self.proc = subprocess.Popen(cmd_pa, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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

        # Reusable draw masks & pre-computed geometry constants for peak performance
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

        # Pre-compute fixed 210-degree start cap coordinates
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
        self._peak_mask_sub = Image.new("L", (self._sub_width, self._sub_height), 0)
        self._peak_mask_sub_draw = ImageDraw.Draw(self._peak_mask_sub)
        sub_box_w = 2 * self._r_arc_box
        self._sub_bbox = [(8 * RENDER_SCALE, 8 * RENDER_SCALE), (8 * RENDER_SCALE + sub_box_w, 8 * RENDER_SCALE + sub_box_w)]
        self._sub_cx = self._sub_bbox[0][0] + self._r_arc_box
        self._sub_cy = self._sub_bbox[0][1] + self._r_arc_box
        self._sub_start_cap_x = self._sub_cx + self._r_arc_center * self._cos_210
        self._sub_start_cap_y = self._sub_cy + self._r_arc_center * self._sin_210

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
        self._last_volume_adjust_time = 0.0
        self._volume_adjust_timer_id = None
        self.poll_timer_id = 0
        self.presence_timer_id = 0
        self._event_proc = None
        self._poll_event = threading.Event()

    def on_ready(self) -> None:
        if getattr(self, "_on_ready_run", False):
            self.update_ui_rendering()
            return
        self._on_ready_run = True
        self.running = True
        
        # Load initial status once (in a background thread to avoid GTK block)
        threading.Thread(target=self._initial_load_status, daemon=True).start()
        
        # Start persistent event listener for instant volume updates without CPU polling overhead
        self._start_event_listener()
        
        # Start GLib tick timer if live peak meter is enabled (increased to 25ms / 40 FPS for premium animation)
        settings = self.get_settings() or {}
        if settings.get("live_meter", True):
            self.tick_timer_id = GLib.timeout_add(25, self.on_tick_update)

        # Start GLib presence check timer to sync UI when action becomes visible (every 500ms)
        self.presence_timer_id = GLib.timeout_add(500, self.on_presence_check)

    def on_update(self) -> None:
        self.update_ui_rendering()

    def on_presence_check(self) -> bool:
        if not self.running:
            return False
        if self.get_is_present():
            if (self.current_volume != self.last_drawn_volume or 
                self.last_mute != self.last_drawn_mute):
                self.update_ui_rendering()
        return True

    def _start_event_listener(self):
        threading.Thread(target=self._volume_poll_worker, daemon=True).start()
        threading.Thread(target=self._listen_for_volume_events, daemon=True).start()

    def _volume_poll_worker(self):
        while self.running:
            self._poll_event.wait(timeout=1.0)
            if not self.running:
                break
            if self._poll_event.is_set():
                self._poll_event.clear()
                self._poll_system_volume_bg()

    def _listen_for_volume_events(self):
        retry_count = 0
        max_retries = 10
        
        while self.running:
            proc = None
            try:
                proc = subprocess.Popen(
                    ["pactl", "subscribe"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True
                )
                self._event_proc = proc
                retry_count = 0  # Reset on successful launch
                
                # Keep reading lines from pactl subscribe
                while self.running and proc.poll() is None:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    if "change" in line and ("sink" in line or "source" in line):
                        self._poll_event.set()
                
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            if not self.running:
                break

            retry_count += 1
            if retry_count > max_retries:
                # Fall back to periodic polling timer
                GLib.idle_add(self._start_fallback_polling)
                break

            time.sleep(min(5.0, retry_count * 0.5))

    def _start_fallback_polling(self):
        if not self.poll_timer_id and self.running:
            self.poll_timer_id = GLib.timeout_add(500, self.on_poll_tick)

    def on_poll_tick(self) -> bool:
        if not self.running:
            return False
        self._poll_event.set()
        return True

    def _initial_load_status(self):
        # Retry loading PipeWire devices on startup if sound system is not ready yet
        sinks, sources = [], []
        retries = 10
        while retries > 0 and self.running:
            try:
                sinks, sources = self.get_pipewire_devices()
                if sinks or sources:
                    break
            except Exception:
                pass
            retries -= 1
            time.sleep(1.0)

        settings = self.get_settings() or {}
        updated = False
        if not settings.get("device_type"):
            settings["device_type"] = "sink"
            updated = True
            
        dtype = settings.get("device_type", "sink")
        
        if not settings.get("pipewire_device_id"):
            settings["pipewire_device_id"] = "@DEFAULT_SINK@" if dtype == "sink" else "@DEFAULT_SOURCE@"
            settings["pipewire_device_name"] = "System Default Audio Output" if dtype == "sink" else "System Default Mic"
            updated = True

        if not settings.get("device_type_2"):
            settings["device_type_2"] = "sink"
            updated = True
            
        dtype_2 = settings.get("device_type_2", "sink")
        if not settings.get("pipewire_device_id_2"):
            settings["pipewire_device_id_2"] = "@DEFAULT_SINK@" if dtype_2 == "sink" else "@DEFAULT_SOURCE@"
            settings["pipewire_device_name_2"] = "System Default Audio Output" if dtype_2 == "sink" else "System Default Mic"
            updated = True
            
        if updated:
            self.set_settings(settings)
            
        # Retry system volume status check if it initially returns default/fails
        vol, mute = 50, False
        retries = 5
        while retries > 0 and self.running:
            try:
                vol, mute = self.get_system_volume_status()
                break
            except Exception:
                pass
            retries -= 1
            time.sleep(0.5)

        self.current_volume = vol
        self.last_mute = mute
        
        # Trigger initial rendering (presence check timer will catch it once active)
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
        if getattr(self, "presence_timer_id", 0):
            GLib.source_remove(self.presence_timer_id)
            self.presence_timer_id = 0
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
        if getattr(self, "presence_timer_id", 0):
            GLib.source_remove(self.presence_timer_id)
            self.presence_timer_id = 0
        if self._event_proc:
            try:
                self._event_proc.kill()
            except OSError:
                pass
            self._event_proc = None
        self.peak_monitor.stop()

    def on_removed_from_cache(self) -> None:
        self.on_remove()

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

    def resolve_device_id(self, device_id: str) -> str:
        if device_id not in ["@DEFAULT_SINK@", "@DEFAULT_SOURCE@"]:
            return device_id
        
        # 1. Try pulsectl first (native Python binding)
        try:
            import pulsectl
            with pulsectl.Pulse("volume-controller-plus-resolver") as pulse:
                if device_id == "@DEFAULT_SINK@":
                    return pulse.server_info().default_sink_name
                elif device_id == "@DEFAULT_SOURCE@":
                    return pulse.server_info().default_source_name
        except Exception:
            pass
            
        # 2. Try wpctl fallback (available on GNOME and KDE with PipeWire)
        try:
            import subprocess
            out = subprocess.check_output(["wpctl", "status"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if "Audio/Sink" in line and device_id == "@DEFAULT_SINK@":
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        return parts[-1]
                elif "Audio/Source" in line and device_id == "@DEFAULT_SOURCE@":
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        return parts[-1]
        except Exception:
            pass
            
        # 3. Try pactl fallback (standard PulseAudio CLI)
        try:
            import subprocess
            out = subprocess.check_output(["pactl", "info"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if "Default Sink:" in line and device_id == "@DEFAULT_SINK@":
                    return line.split(":", 1)[1].strip()
                elif "Default Source:" in line and device_id == "@DEFAULT_SOURCE@":
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
            
        return device_id

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
            
        if dtype == "app":
            return self.resolve_device_id("@DEFAULT_SINK@")

        if dev_id in ["@DEFAULT_AUDIO_SINK@", "@DEFAULT_SINK@"]:
            target_id = "@DEFAULT_SINK@"
        elif dev_id in ["@DEFAULT_AUDIO_SOURCE@", "@DEFAULT_SOURCE@"]:
            target_id = "@DEFAULT_SOURCE@"
        else:
            target_id = dev_id
            
        return self.resolve_device_id(target_id)

    def restart_peak_monitor(self):
        def _bg_restart():
            device_id = self.get_configured_device_id()
            dtype = self.get_active_device_type()
            is_source = (dtype == "source")
            self.peak_monitor.start(device_id, is_source)
        threading.Thread(target=_bg_restart, daemon=True).start()

    def on_tick_update(self) -> bool:
        if not self.running:
            return False
            
        if not self.get_is_present():
            # Action is on a background page/profile; skip 40 FPS calculations
            return True
            
        raw_peak = self.peak_monitor.get_peak()
        # Apply a 1.5x gain boost to ensure standard audio peaks reach the red/orange zone at 100% volume
        raw_peak = max(0.0, min(1.0, raw_peak * 1.5))
        if raw_peak < 0.005:
            raw_peak = 0.0
            
        # Fast attack, slow release exponential smoothing for premium hardware meter physics
        if raw_peak >= self._current_peak:
            self._current_peak = raw_peak
        else:
            self._current_peak = max(raw_peak, self._current_peak * 0.88 - 0.002)
            
        peak = self._current_peak
        if peak < 0.005:
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
            
        state = self.get_state()
        if state is None or state.state != self.state:
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

    def get_application_streams(self) -> list[dict]:
        """
        Discovers currently active application audio playback streams.
        Returns a list of dicts: [{'id': str, 'name': str, 'binary': str, 'icon': str, 'volume': int, 'mute': bool}]
        """
        apps = []
        seen_names = set()

        # 1. Try pulsectl first (native Python binding)
        try:
            import pulsectl
            with pulsectl.Pulse("volume-controller-plus-apps") as pulse:
                for si in pulse.sink_input_list():
                    props = si.proplist
                    name = props.get("application.name") or props.get("media.name") or si.name
                    binary = props.get("application.process.binary")
                    icon = props.get("application.icon_name") or props.get("application.icon-name")
                    if name:
                        vol = int(round(pulse.volume_get_all_chans(si) * 100))
                        apps.append({
                            "id": str(si.index),
                            "name": name,
                            "binary": binary or name.lower(),
                            "icon": icon,
                            "volume": vol,
                            "mute": bool(si.mute)
                        })
                        seen_names.add(name.lower())
        except Exception:
            pass

        # 2. Try pw-dump / wpctl for modern PipeWire environments
        if not apps:
            try:
                import subprocess, json
                out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
                data = json.loads(out)
                for obj in data:
                    info = obj.get("info", {})
                    props = info.get("props", {})
                    media_class = props.get("media.class", "")
                    if "Stream/Output/Audio" in media_class or "Audio/Sink" in media_class:
                        name = props.get("application.name") or props.get("node.description") or props.get("node.name")
                        binary = props.get("application.process.binary")
                        icon = props.get("application.icon-name") or props.get("application.icon_name")
                        node_id = str(obj.get("id"))
                        if name and name.lower() not in seen_names:
                            vol = 100
                            mute = False
                            try:
                                vout = subprocess.check_output(["wpctl", "get-volume", node_id], text=True, stderr=subprocess.DEVNULL)
                                import re
                                m = re.search(r'Volume:\s*([\d\.]+)', vout)
                                if m:
                                    vol = int(round(float(m.group(1)) * 100))
                                if "[MUTED]" in vout:
                                    mute = True
                            except Exception:
                                pass
                            apps.append({
                                "id": node_id,
                                "name": name,
                                "binary": binary or name.lower(),
                                "icon": icon,
                                "volume": vol,
                                "mute": mute
                            })
                            seen_names.add(name.lower())
            except Exception:
                pass

        # 3. Try pactl fallback
        if not apps:
            try:
                out = self.run_cmd(["pactl", "list", "sink-inputs"])
                current_id = None
                current_name = None
                current_icon = None
                current_bin = None
                current_vol = 100
                current_mute = False
                import re
                for line in out.splitlines():
                    line_s = line.strip()
                    if line.startswith("Sink Input #"):
                        if current_name and current_name.lower() not in seen_names:
                            apps.append({
                                "id": current_id,
                                "name": current_name,
                                "binary": current_bin or current_name.lower(),
                                "icon": current_icon,
                                "volume": current_vol,
                                "mute": current_mute
                            })
                            seen_names.add(current_name.lower())
                        current_id = line.split("#")[-1].strip()
                        current_name = None
                        current_icon = None
                        current_bin = None
                        current_vol = 100
                        current_mute = False
                    elif "application.name =" in line_s:
                        current_name = line_s.split("=", 1)[1].strip(' "')
                    elif "application.icon_name =" in line_s:
                        current_icon = line_s.split("=", 1)[1].strip(' "')
                    elif "application.process.binary =" in line_s:
                        current_bin = line_s.split("=", 1)[1].strip(' "')
                    elif "Mute:" in line_s:
                        current_mute = ("yes" in line_s)
                    elif "Volume:" in line_s:
                        vm = re.search(r'/\s*(\d+)%', line_s)
                        if vm:
                            current_vol = int(vm.group(1))
                if current_name and current_name.lower() not in seen_names:
                    apps.append({
                        "id": current_id,
                        "name": current_name,
                        "binary": current_bin or current_name.lower(),
                        "icon": current_icon,
                        "volume": current_vol,
                        "mute": current_mute
                    })
            except Exception:
                pass

        return apps

    def get_application_status(self, app_target: str) -> "tuple[int, bool, str, str | None]":
        """
        Finds matching active application stream and returns (volume, is_muted, display_name, icon_name).
        """
        if not app_target:
            return self.current_volume, self.last_mute, "App Audio", None

        streams = self.get_application_streams()
        target_lower = app_target.lower().strip()

        for st in streams:
            name_lower = st["name"].lower()
            bin_lower = st.get("binary", "").lower()
            id_str = str(st["id"])
            if target_lower in name_lower or target_lower in bin_lower or target_lower == id_str:
                return st["volume"], st["mute"], st["name"], st.get("icon")

        return self.current_volume, self.last_mute, app_target, None

    def change_application_volume(self, app_target: str, target_vol: int):
        target_lower = app_target.lower().strip()
        matched = False

        # 1. pulsectl
        try:
            import pulsectl
            with pulsectl.Pulse("volume-controller-plus-app-vol") as pulse:
                for si in pulse.sink_input_list():
                    props = si.proplist
                    name = (props.get("application.name") or si.name or "").lower()
                    binary = (props.get("application.process.binary") or "").lower()
                    if target_lower in name or target_lower in binary or target_lower == str(si.index):
                        pulse.volume_set_all_chans(si, target_vol / 100.0)
                        matched = True
        except Exception:
            pass

        # 2. wpctl fallback
        if not matched:
            streams = self.get_application_streams()
            for st in streams:
                if target_lower in st["name"].lower() or target_lower in st.get("binary", "").lower() or target_lower == str(st["id"]):
                    try:
                        self.execute_cmd(["wpctl", "set-volume", str(st["id"]), f"{target_vol / 100.0:.2f}"])
                        matched = True
                    except Exception:
                        pass

        # 3. pactl fallback
        if not matched:
            try:
                streams = self.get_application_streams()
                for st in streams:
                    if target_lower in st["name"].lower() or target_lower in st.get("binary", "").lower() or target_lower == str(st["id"]):
                        self.execute_cmd(["pactl", "set-sink-input-volume", str(st["id"]), f"{target_vol}%"])
            except Exception:
                pass

    def toggle_application_mute(self, app_target: str):
        target_lower = app_target.lower().strip()
        matched = False

        # 1. pulsectl
        try:
            import pulsectl
            with pulsectl.Pulse("volume-controller-plus-app-mute") as pulse:
                for si in pulse.sink_input_list():
                    props = si.proplist
                    name = (props.get("application.name") or si.name or "").lower()
                    binary = (props.get("application.process.binary") or "").lower()
                    if target_lower in name or target_lower in binary or target_lower == str(si.index):
                        pulse.mute(si, not si.mute)
                        matched = True
        except Exception:
            pass

        # 2. wpctl fallback
        if not matched:
            streams = self.get_application_streams()
            for st in streams:
                if target_lower in st["name"].lower() or target_lower in st.get("binary", "").lower() or target_lower == str(st["id"]):
                    try:
                        self.execute_cmd(["wpctl", "set-mute", str(st["id"]), "toggle"])
                        matched = True
                    except Exception:
                        pass

        # 3. pactl fallback
        if not matched:
            try:
                streams = self.get_application_streams()
                for st in streams:
                    if target_lower in st["name"].lower() or target_lower in st.get("binary", "").lower() or target_lower == str(st["id"]):
                        self.execute_cmd(["pactl", "set-sink-input-mute", str(st["id"]), "toggle"])
            except Exception:
                pass

    def get_application_icon_path(self, app_name: str, icon_name: str | None = None) -> str | None:
        """
        Resolves the high-resolution SVG or PNG icon for an application from the desktop icon theme.
        """
        if not app_name and not icon_name:
            return None

        daemon_tags = {
            "io.github.pipeweaver", "pipeweaver",
            "org.pulseaudio.pavucontrol", "pavucontrol",
            "pulseaudio", "audio-speakers", "audio-volume-high", "audio-card"
        }

        candidates = []
        fallback_candidates = []

        if app_name:
            clean = app_name.strip().lower()
            for prefix in ["pipeweaver ", "pipeweaver-", "alsa "]:
                if clean.startswith(prefix):
                    clean = clean[len(prefix):].strip()

            candidates.append(clean)
            candidates.append(clean.replace(" ", "-"))
            candidates.append(clean.replace(" ", ""))
            candidates.append(clean.replace(" ", "_"))

            alias_map = {
                "spotify": ["spotify", "spotify-client", "com.spotify.Client"],
                "discord": ["discord", "com.discordapp.Discord", "discord-canary", "vesktop"],
                "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
                "google chrome": ["google-chrome", "google-chrome-stable", "chromium"],
                "firefox": ["firefox", "org.mozilla.firefox", "firefox-esr"],
                "vlc": ["vlc", "org.videolan.VLC"],
                "steam": ["steam", "com.valvesoftware.Steam"],
                "obs": ["com.obsproject.Studio", "obs-studio", "obs"]
            }
            for k, v in alias_map.items():
                if k in clean:
                    candidates.extend(v)

        if icon_name:
            clean_icon = icon_name.strip().lower()
            if clean_icon in daemon_tags or any(d in clean_icon for d in daemon_tags):
                fallback_candidates.append(icon_name)
            else:
                candidates.insert(0, icon_name)

        all_candidates = candidates + fallback_candidates

        try:
            from gi.repository import Gtk, Gdk
            display = Gdk.Display.get_default()
            if display:
                icon_theme = Gtk.IconTheme.get_for_display(display)
                for cand in all_candidates:
                    if icon_theme.has_icon(cand):
                        paintable = icon_theme.lookup_icon(cand, None, 128, 1, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.NONE)
                        if paintable:
                            f = paintable.get_file()
                            if f and os.path.exists(f.get_path()):
                                return f.get_path()
        except Exception:
            pass

        # Check standard icons directories as fallback
        for cand in all_candidates:
            for base in ["/usr/share/icons/hicolor/scalable/apps", "/usr/share/icons/hicolor/48x48/apps", "/usr/share/pixmaps", "/run/host/share/icons/Papirus/48x48/apps"]:
                for ext in [".svg", ".png"]:
                    p = os.path.join(base, cand + ext)
                    if os.path.exists(p):
                        return p

        return None

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
        dtype = self.get_active_device_type()
        if dtype == "app":
            settings = self.get_settings() or {}
            active = getattr(self, "active_device_index", 1)
            app_target = settings.get("app_name_2" if (active == 2 and settings.get("device_switch", False)) else "app_name", "")
            vol, mute, _, _ = self.get_application_status(app_target)
            return vol, mute
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
            
        # Fallback to wpctl (native PipeWire)
        try:
            target_frac = f"{target_vol / 100.0:.2f}"
            wp_target = "@DEFAULT_AUDIO_SOURCE@" if dtype == "source" else "@DEFAULT_AUDIO_SINK@"
            self.execute_cmd(["wpctl", "set-volume", wp_target, target_frac])
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
        self._last_volume_adjust_time = time.time()
        
        if getattr(self, "_volume_adjust_timer_id", None):
            try:
                GLib.source_remove(self._volume_adjust_timer_id)
            except Exception:
                pass
            self._volume_adjust_timer_id = None
            
        def on_adjust_timeout():
            self._volume_adjust_timer_id = None
            self.update_ui_rendering(force=True)
            return False
            
        self._volume_adjust_timer_id = GLib.timeout_add(1250, on_adjust_timeout)
        self.update_ui_rendering()
        threading.Thread(target=self._change_volume_bg, args=(self.current_volume,), daemon=True).start()

    def _notify_wavecontroller_ipc(self, target_vol: int = None, is_muted: bool = None):
        try:
            if os.path.exists("/tmp/wavecontroller.sock"):
                import socket, json
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.08)
                    s.connect("/tmp/wavecontroller.sock")
                    dtype = self.get_active_device_type()
                    settings = self.get_settings() or {}
                    active = getattr(self, "active_device_index", 1)
                    app_target = settings.get("app_name_2" if (active == 2 and settings.get("device_switch", False)) else "app_name", "")
                    dev_id = self.get_configured_device_id()
                    target_name = app_target if dtype == "app" else ("mic" if dtype == "source" else "master")
                    payload = {
                        "command": "sync_volume",
                        "target": target_name,
                        "volume": target_vol,
                        "muted": is_muted
                    }
                    s.sendall(json.dumps(payload).encode('utf-8'))
        except Exception:
            pass

    def _change_volume_bg(self, target_vol: int):
        dtype = self.get_active_device_type()
        if dtype == "app":
            settings = self.get_settings() or {}
            active = getattr(self, "active_device_index", 1)
            app_target = settings.get("app_name_2" if (active == 2 and settings.get("device_switch", False)) else "app_name", "")
            self.change_application_volume(app_target, target_vol)
        else:
            device_id = self.get_configured_device_id()
            self.change_pipewire_volume(device_id, target_vol)
        self._notify_wavecontroller_ipc(target_vol=target_vol)

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
            
        # Fallback to wpctl (native PipeWire)
        try:
            wp_target = "@DEFAULT_AUDIO_SOURCE@" if dtype == "source" else "@DEFAULT_AUDIO_SINK@"
            self.execute_cmd(["wpctl", "set-mute", wp_target, "toggle"])
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
        dtype = self.get_active_device_type()
        if dtype == "app":
            settings = self.get_settings() or {}
            active = getattr(self, "active_device_index", 1)
            app_target = settings.get("app_name_2" if (active == 2 and settings.get("device_switch", False)) else "app_name", "")
            self.toggle_application_mute(app_target)
        else:
            device_id = self.get_configured_device_id()
            self.toggle_pipewire_mute(device_id)
        self._notify_wavecontroller_ipc(is_muted=self.last_mute)

    def load_icon_image(self, path: str) -> Image.Image | None:
        if not path or not os.path.exists(path):
            return None
        try:
            if path.endswith(".svg"):
                # 1. Try cairosvg direct rasterization
                try:
                    import cairosvg, io
                    with open(path, 'rb') as f:
                        svg_bytes = f.read()
                    png_bytes = cairosvg.svg2png(bytestring=svg_bytes, output_width=128, output_height=128)
                    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")
                except Exception:
                    pass

                # 2. Try StreamController media manager
                try:
                    img = gl.media_manager.generate_svg_thumbnail(path, 128, 128)
                    if img is not None:
                        return img.convert("RGBA")
                except Exception:
                    pass

                # 3. Try GdkPixbuf fallback
                try:
                    import gi
                    gi.require_version("GdkPixbuf", "2.0")
                    from gi.repository import GdkPixbuf
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 128, 128, True)
                    png_bytes = pixbuf.save_to_bufferv("png", [], [])[1]
                    import io
                    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")
                except Exception:
                    pass

                return None
            else:
                return Image.open(path).convert("RGBA")
        except Exception:
            return None

    def _get_gauge_gradient_image(self, width: int, height: int, bbox: list) -> Image.Image:
        with self._render_lock:
            if self._gauge_gradient_img is not None:
                return self._gauge_gradient_img
                
            grad_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            grad_draw = ImageDraw.Draw(grad_img)
            arc_w = 7 * RENDER_SCALE
            for angle in range(210, 331):
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
                bg = Image.new("RGBA", (width, height), (30, 30, 32, 255))
                
            bg_draw = ImageDraw.Draw(bg)
            
            # Pre-render Ticks (5 Major tall ticks at 0, 25, 50, 75, 100% and 4 Minor short ticks in between)
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
                
            # Pre-render Gauge Track (clean dark background arc with rounded caps matching screenshot)
            r_arc_box = 51 * RENDER_SCALE
            arc_w = 7 * RENDER_SCALE
            r_arc_center = r_arc_box - arc_w / 2.0
            cap_r = arc_w / 2.0
            bbox_bg = [(cx_bg - r_arc_box, cy_bg - r_arc_box), (cx_bg + r_arc_box, cy_bg + r_arc_box)]

            # Track color slightly darker than widget background (widget bg is ~34, 34, 38 -> track is 20, 20, 24)
            track_color = (20, 20, 24, 255)
            bg_draw.arc(bbox_bg, start=210, end=330, fill=track_color, width=arc_w)
            for cap_angle in (210, 330):
                rad_cap = math.radians(cap_angle)
                xc = cx_bg + r_arc_center * math.cos(rad_cap)
                yc = cy_bg + r_arc_center * math.sin(rad_cap)
                bg_draw.ellipse([(xc - cap_r, yc - cap_r), (xc + cap_r, yc + cap_r)], fill=track_color)
            
            self._cached_base_bg = bg

        # 2. Get settings/labels that form the midground cache key
        settings = self.get_settings() or {}
        active = getattr(self, "active_device_index", 1)
        device_switch_enabled = settings.get("device_switch", False)
        volume_format = settings.get("volume_format", "percent")
        
        if active == 2 and device_switch_enabled:
            custom_icon_path = settings.get("custom_icon_2", "")
            custom_name = settings.get("custom_name_2", "")
            pw_name = settings.get("pipewire_device_name_2", "Default Sink")
            dtype = settings.get("device_type_2", "sink")
            app_target = settings.get("app_name_2", "")
        else:
            custom_icon_path = settings.get("custom_icon", "")
            custom_name = settings.get("custom_name", "")
            pw_name = settings.get("pipewire_device_name", "Default Sink")
            dtype = settings.get("device_type", "sink")
            app_target = settings.get("app_name", "")
            
        app_icon_auto = None
        if dtype == "app":
            vol, mute, app_display_name, icon_name = self.get_application_status(app_target)
            title_text = custom_name if custom_name else (app_display_name if app_display_name else "App Audio")
            if not custom_icon_path:
                app_icon_auto = self.get_application_icon_path(app_target, icon_name)
        else:
            title_text = custom_name if custom_name else pw_name

        font_name = settings.get("font_name", "DejaVu Sans Bold 15")
        font_path = settings.get("font_path", "")

        midground_key = (
            volume,
            is_muted,
            title_text,
            custom_icon_path,
            app_icon_auto,
            dtype,
            volume_format,
            font_name,
            font_path,
            active,
            device_switch_enabled
        )

        cx, cy = 70 * RENDER_SCALE, 104 * RENDER_SCALE
        r_outer = 44 * RENDER_SCALE
        r_inner = 39 * RENDER_SCALE
        r_arc_box = 51 * RENDER_SCALE
        arc_w = 7 * RENDER_SCALE
        r_arc_center = r_arc_box - arc_w / 2.0
        cap_r = arc_w / 2.0
        bbox = [(cx - r_arc_box, cy - r_arc_box), (cx + r_arc_box, cy + r_arc_box)]
        bbox_outer = getattr(self, "_bbox_outer", [(cx - r_outer, cy - r_outer), (cx + r_outer, cy + r_outer)])
        bbox_inner = getattr(self, "_bbox_inner", [(cx - r_inner, cy - r_inner), (cx + r_inner, cy + r_inner)])
        start_cap_x = getattr(self, "_start_cap_x", cx + r_arc_center * math.cos(math.radians(210)))
        start_cap_y = getattr(self, "_start_cap_y", cy + r_arc_center * math.sin(math.radians(210)))
        sub_cx = getattr(self, "_sub_cx", 8 * RENDER_SCALE + r_arc_box)
        sub_cy = getattr(self, "_sub_cy", 8 * RENDER_SCALE + r_arc_box)
        sub_start_cap_x = getattr(self, "_sub_start_cap_x", sub_cx + r_arc_center * math.cos(math.radians(210)))
        sub_start_cap_y = getattr(self, "_sub_start_cap_y", sub_cy + r_arc_center * math.sin(math.radians(210)))

        # If cache misses, rebuild static midground card (Text, Icon, Background tracks)
        if self._cached_midground is None or self._cached_midground_key != midground_key:
            mid_img = self._cached_base_bg.copy()
            if is_muted:
                red_tint = Image.new("RGBA", (width, height), (239, 68, 68, 48))
                mid_img = Image.alpha_composite(mid_img, red_tint)
            mid_draw = ImageDraw.Draw(mid_img)

            # Draw Volume Text (MUTE in red when muted, volume % / dB when unmuted)
            if is_muted:
                vol_text = "MUTE"
                vol_color = (239, 68, 68, 255)
            elif volume_format == "db":
                if volume <= 0:
                    vol_text = "-inf dB"
                elif volume >= 100:
                    vol_text = "0.0 dB"
                else:
                    db_val = 20.0 * math.log10(volume / 100.0)
                    vol_text = f"{db_val:.1f} dB"
                vol_color = (255, 255, 255, 255)
            else:
                vol_text = f"{volume}%"
                vol_color = (255, 255, 255, 255)
            
            # Resolve and cache fonts if they have changed or are not cached
            if (self._cached_font_title is None or 
                self._cached_font_vol is None or 
                font_name != self._cached_font_name or 
                font_path != self._cached_font_path or
                getattr(self, "_cached_vol_format", None) != volume_format):
                
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
                    
                # In dB mode, use 16px font to ensure '-xx.x dB' fits comfortably
                vol_font_size = 15 if volume_format == "db" else 19
                try:
                    if vol_font_file:
                        self._cached_font_vol = ImageFont.truetype(vol_font_file, vol_font_size * RENDER_SCALE)
                    else:
                        self._cached_font_vol = ImageFont.load_default()
                except Exception:
                    self._cached_font_vol = ImageFont.load_default()
                    
                self._cached_font_file = font_file
                self._cached_title_font_size = title_font_size
                self._cached_font_name = font_name
                self._cached_font_path = font_path
                self._cached_vol_format = volume_format

            font_title = self._cached_font_title
            font_vol = self._cached_font_vol
            font_file = self._cached_font_file
            title_font_size = self._cached_title_font_size

            try:
                vol_w = font_vol.getlength(vol_text)
            except Exception:
                vol_w = 40
                
            try:
                mid_draw.text((165 * RENDER_SCALE, 64 * RENDER_SCALE), vol_text, font=font_vol, fill=vol_color, anchor="mm")
            except TypeError:
                vol_w_unscaled = vol_w / RENDER_SCALE
                mid_draw.text((int((165 - vol_w_unscaled / 2) * RENDER_SCALE), int((64 - 10) * RENDER_SCALE)), vol_text, font=font_vol, fill=vol_color)

            # Icon Placement Area
            icon_drawn = False
            icon_w = 26
            effective_icon_path = custom_icon_path or app_icon_auto
            if not effective_icon_path:
                icon_filename = "input.png" if dtype == "source" else "output.png"
                effective_icon_path = os.path.join(self.plugin_base.PATH, "assets", icon_filename)

            if effective_icon_path:
                if effective_icon_path != self._cached_icon_path or self._cached_icon_img is None:
                    loaded_img = self.load_icon_image(effective_icon_path)
                    if loaded_img is not None:
                        loaded_img = loaded_img.convert("RGBA")
                        orig_w, orig_h = loaded_img.size
                        target_max = int(27 * RENDER_SCALE)
                        if orig_w > orig_h:
                            new_w = target_max
                            new_h = max(1, int(orig_h * target_max / orig_w))
                        else:
                            new_h = target_max
                            new_w = max(1, int(orig_w * target_max / orig_h))
                        self._cached_icon_img = loaded_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    else:
                        self._cached_icon_img = None
                    self._cached_icon_path = effective_icon_path
                    
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
            right_bound = 195
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

            # Draw Device Switch Icon in top-right corner
            icon_to_draw = None
            if device_switch_enabled:
                if not hasattr(self, "_cached_device_switch_img_on") or self._cached_device_switch_img_on is None:
                    dev_switch_path = os.path.join(self.plugin_base.PATH, "assets", "device.png")
                    try:
                        loaded_img = Image.open(dev_switch_path).convert("RGBA")
                        self._cached_device_switch_img_on = loaded_img.resize((18 * RENDER_SCALE, 18 * RENDER_SCALE), Image.Resampling.LANCZOS)
                    except Exception:
                        self._cached_device_switch_img_on = None
                icon_to_draw = getattr(self, "_cached_device_switch_img_on", None)
            else:
                if not hasattr(self, "_cached_device_switch_img_off") or self._cached_device_switch_img_off is None:
                    dev_switch_path = os.path.join(self.plugin_base.PATH, "assets", "device_off.png")
                    try:
                        loaded_img = Image.open(dev_switch_path).convert("RGBA")
                        self._cached_device_switch_img_off = loaded_img.resize((18 * RENDER_SCALE, 18 * RENDER_SCALE), Image.Resampling.LANCZOS)
                    except Exception:
                        self._cached_device_switch_img_off = None
                icon_to_draw = getattr(self, "_cached_device_switch_img_off", None)

            if icon_to_draw is not None:
                mid_img.paste(icon_to_draw, (int(168 * RENDER_SCALE), int(10 * RENDER_SCALE)), icon_to_draw)

            # When live meter is disabled, pre-render steady blue volume arc
            if not is_muted:
                is_live_enabled = settings.get("live_meter", True)
                if not is_live_enabled:
                    vol_angle = int(210 + 120 * (volume / 100.0))
                    if vol_angle > 210:
                        mid_draw.arc(bbox, start=210, end=vol_angle, fill=(0, 168, 255, 255), width=arc_w)
                        rc_start = math.radians(210)
                        xs = cx + r_arc_center * math.cos(rc_start)
                        ys = cy + r_arc_center * math.sin(rc_start)
                        mid_draw.ellipse([(xs - cap_r, ys - cap_r), (xs + cap_r, ys + cap_r)], fill=(0, 168, 255, 255))
                        rc_end = math.radians(vol_angle)
                        xe = cx + r_arc_center * math.cos(rc_end)
                        ye = cy + r_arc_center * math.sin(rc_end)
            # Pre-render Inner Knob Core directly into midground cache (eliminates 3 chord/arc operations per frame)
            mid_draw.chord(bbox_outer, start=180, end=360, fill=(35, 35, 38, 255))
            mid_draw.chord(bbox_inner, start=180, end=360, fill=(66, 66, 70, 255))
            mid_draw.arc(bbox_inner, start=180, end=360, fill=(85, 85, 92, 255), width=1 * RENDER_SCALE)

            self._cached_midground = mid_img
            self._cached_midground_key = midground_key

        # 3. Instantiate dynamic frame image from cached midground
        img = self._cached_midground.copy()
        draw = ImageDraw.Draw(img)
        
        # 4. Draw Active Gauge Segments: volume adjustment white curve OR live audio peak and peak-hold marker
        if not is_muted:
            now = time.time()
            is_adjusting = (now - getattr(self, "_last_volume_adjust_time", 0.0)) < 1.2
            
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
                    # Bouncing audio peak arc
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
                                # Reuse the pre-allocated sub-mask to avoid heavy object instantiation
                                self._peak_mask_sub_draw.rectangle([(0, 0), (self._sub_width, self._sub_height)], fill=0)
                                self._peak_mask_sub_draw.arc(self._sub_bbox, start=210, end=peak_angle, fill=255, width=arc_w)
                                
                                sub_xe = sub_cx + r_arc_center * math.cos(rad_e)
                                sub_ye = sub_cy + r_arc_center * math.sin(rad_e)
                                self._peak_mask_sub_draw.ellipse([(sub_start_cap_x - cap_r, sub_start_cap_y - cap_r), (sub_start_cap_x + cap_r, sub_start_cap_y + cap_r)], fill=255)
                                self._peak_mask_sub_draw.ellipse([(sub_xe - cap_r, sub_ye - cap_r), (sub_xe + cap_r, sub_ye + cap_r)], fill=255)
                                
                                grad_img_sub = self._get_gauge_gradient_image_sub(width, height, bbox)
                                img.paste(grad_img_sub, (self._gx1, self._gy1), self._peak_mask_sub)

                    # Peak Hold marker (Floating bright indicator for studio console aesthetics)
                    if self._peak_hold_val > 0.04:
                        scaled_hold = self._peak_hold_val * (volume / 100.0)
                        hold_angle = int(210 + 120 * min(1.0, scaled_hold))
                        if hold_angle > 210:
                            draw.arc(bbox, start=max(210, hold_angle - 1), end=min(330, hold_angle + 1), fill=(255, 75, 75, 255), width=arc_w)
        
        # 6. Draw Pointer notch rotating on top of the knob
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

        # 7. Red perimeter border when muted
        if is_muted:
            border_w = 2 * RENDER_SCALE
            draw.rounded_rectangle(
                [(1 * RENDER_SCALE, 1 * RENDER_SCALE), (width - 1 - 1 * RENDER_SCALE, height - 1 - 1 * RENDER_SCALE)],
                radius=12 * RENDER_SCALE,
                outline=(255, 59, 48, 255),
                width=border_w
            )
        
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
            
            # Add System Default option at index 0
            default_id = "@DEFAULT_SINK@" if dtype == "sink" else "@DEFAULT_SOURCE@"
            default_name = "System Default Audio Output" if dtype == "sink" else "System Default Mic"
            self.pw_devices_map.append((default_id, default_name))
            
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
            
            # Add System Default option at index 0
            default_id = "@DEFAULT_SINK@" if dtype_2 == "sink" else "@DEFAULT_SOURCE@"
            default_name = "System Default Audio Output" if dtype_2 == "sink" else "System Default Mic"
            self.pw_devices_map_2.append((default_id, default_name))
            
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

    def update_app_dropdown(self):
        if not hasattr(self, "app_selector"):
            return
        self._updating_app_dropdown = True
        try:
            settings = self.get_settings() or {}
            streams = self.get_application_streams()
            
            self.apps_map = []
            seen = set()
            for st in streams:
                name = st["name"]
                if name.lower() not in seen:
                    self.apps_map.append(name)
                    seen.add(name.lower())
            
            # Common defaults if not currently active
            for default_app in ["Spotify", "Discord", "Google Chrome", "Firefox", "VLC", "Steam"]:
                if default_app.lower() not in seen:
                    self.apps_map.append(default_app)
                    seen.add(default_app.lower())
                    
            self.apps_map.append("Custom Application...")
            
            self.app_model = Gtk.StringList()
            for app_title in self.apps_map:
                self.app_model.append(app_title)
                
            self.app_selector.set_model(self.app_model)
            
            current_app = settings.get("app_name", "Spotify")
            selected_idx = 0
            if current_app in self.apps_map:
                selected_idx = self.apps_map.index(current_app)
            elif current_app:
                selected_idx = len(self.apps_map) - 1
                
            self.app_selector.set_selected(selected_idx)
        finally:
            self._updating_app_dropdown = False

    def update_app_dropdown_2(self):
        if not hasattr(self, "app_selector_2"):
            return
        self._updating_app_dropdown_2 = True
        try:
            settings = self.get_settings() or {}
            streams = self.get_application_streams()
            
            self.apps_map_2 = []
            seen = set()
            for st in streams:
                name = st["name"]
                if name.lower() not in seen:
                    self.apps_map_2.append(name)
                    seen.add(name.lower())
            
            for default_app in ["Spotify", "Discord", "Google Chrome", "Firefox", "VLC", "Steam"]:
                if default_app.lower() not in seen:
                    self.apps_map_2.append(default_app)
                    seen.add(default_app.lower())
                    
            self.apps_map_2.append("Custom Application...")
            
            self.app_model_2 = Gtk.StringList()
            for app_title in self.apps_map_2:
                self.app_model_2.append(app_title)
                
            self.app_selector_2.set_model(self.app_model_2)
            
            current_app = settings.get("app_name_2", "Discord")
            selected_idx = 0
            if current_app in self.apps_map_2:
                selected_idx = self.apps_map_2.index(current_app)
            elif current_app:
                selected_idx = len(self.apps_map_2) - 1
                
            self.app_selector_2.set_selected(selected_idx)
        finally:
            self._updating_app_dropdown_2 = False

    def update_visibility(self, active: bool):
        settings = self.get_settings() or {}
        dtype = settings.get("device_type", "sink")
        dtype_2 = settings.get("device_type_2", "sink")
        
        # Primary device controls visibility
        is_app = (dtype == "app")
        if hasattr(self, "pw_device_selector"):
            self.pw_device_selector.set_visible(not is_app)
        if hasattr(self, "app_selector"):
            self.app_selector.set_visible(is_app)
        if hasattr(self, "custom_app_row"):
            self.custom_app_row.set_visible(is_app)

        # Secondary device controls visibility
        if hasattr(self, "type_selector_2"):
            self.type_selector_2.set_visible(active)
        if hasattr(self, "custom_name_row_2"):
            self.custom_name_row_2.set_visible(active)
        if hasattr(self, "icon_row_2"):
            self.icon_row_2.set_visible(active)
            
        is_app_2 = (dtype_2 == "app")
        if hasattr(self, "pw_device_selector_2"):
            self.pw_device_selector_2.set_visible(active and not is_app_2)
        if hasattr(self, "app_selector_2"):
            self.app_selector_2.set_visible(active and is_app_2)
        if hasattr(self, "custom_app_row_2"):
            self.custom_app_row_2.set_visible(active and is_app_2)

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
        dtype_2 = settings.get("device_type_2", "sink")
        vol_format = settings.get("volume_format", "percent")

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
        self.type_model.append("Application Stream")
        self.type_selector = Adw.ComboRow(
            model=self.type_model,
            title="Device Type"
        )
        type_idx = 0 if dtype == "sink" else (1 if dtype == "source" else 2)
        self.type_selector.set_selected(type_idx)

        # 3. PipeWire Device Selector ComboRow
        self.pw_device_model = Gtk.StringList()
        self.pw_device_selector = Adw.ComboRow(
            model=self.pw_device_model,
            title="PipeWire Device"
        )
        self.update_device_dropdown()

        # 3b. Application Selector ComboRow & Custom Process Entry
        self.app_model = Gtk.StringList()
        self.app_selector = Adw.ComboRow(
            model=self.app_model,
            title="Application"
        )
        self.custom_app_row = Adw.EntryRow(
            title="Application Process / Name",
            text=settings.get("app_name", "Spotify")
        )
        self.update_app_dropdown()

        # 4. Volume Format Selector
        self.vol_format_model = Gtk.StringList()
        self.vol_format_model.append("Percentage (%)")
        self.vol_format_model.append("Decibels (dB)")
        self.vol_format_selector = Adw.ComboRow(
            model=self.vol_format_model,
            title="Volume Display Format"
        )
        self.vol_format_selector.set_selected(0 if vol_format == "percent" else 1)

        # 5. Device Switch Row
        self.device_switch_row = Adw.SwitchRow(
            title="Device Switch"
        )
        device_switch_active = settings.get("device_switch", False)
        self.device_switch_row.set_active(device_switch_active)

        # 6. Device Type 2 Selector
        self.type_model_2 = Gtk.StringList()
        self.type_model_2.append("Output (sink)")
        self.type_model_2.append("Input (source)")
        self.type_model_2.append("Application Stream")
        self.type_selector_2 = Adw.ComboRow(
            model=self.type_model_2,
            title="Device Type 2"
        )
        type_idx_2 = 0 if dtype_2 == "sink" else (1 if dtype_2 == "source" else 2)
        self.type_selector_2.set_selected(type_idx_2)

        # 6b. PipeWire Device 2 Selector ComboRow
        self.pw_device_model_2 = Gtk.StringList()
        self.pw_device_selector_2 = Adw.ComboRow(
            model=self.pw_device_model_2,
            title="PipeWire Device 2"
        )
        self.update_device_dropdown_2()

        # 6c. Application 2 Selector ComboRow & Custom Process Entry
        self.app_model_2 = Gtk.StringList()
        self.app_selector_2 = Adw.ComboRow(
            model=self.app_model_2,
            title="Application 2"
        )
        self.custom_app_row_2 = Adw.EntryRow(
            title="Application Process / Name 2",
            text=settings.get("app_name_2", "Discord")
        )
        self.update_app_dropdown_2()
        
        # 7. Step size selector
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
            
        # 8. Live Meter Toggle Row
        self.live_meter_row = Adw.SwitchRow(
            title="Live Peak Meter"
        )
        is_live_meter_enabled = settings.get("live_meter", True)
        self.live_meter_row.set_active(is_live_meter_enabled)

        # 9. Custom Icon selection
        custom_icon_val = settings.get("custom_icon", "")
        self.icon_row = Adw.ActionRow(
            title="Device Icon",
            subtitle=os.path.basename(custom_icon_val) if custom_icon_val else ""
        )
        
        self.choose_icon_button = Gtk.Button.new_from_icon_name("document-open-symbolic")
        self.choose_icon_button.set_valign(Gtk.Align.CENTER)
        self.choose_icon_button.set_tooltip_text("Choose Icon")
        
        self.clear_icon_button = Gtk.Button.new_from_icon_name("edit-clear-symbolic")
        self.clear_icon_button.set_valign(Gtk.Align.CENTER)
        self.clear_icon_button.set_tooltip_text("Clear Icon")
        
        self.icon_row.add_suffix(self.choose_icon_button)
        self.icon_row.add_suffix(self.clear_icon_button)

        # 9b. Custom Icon 2 selection
        custom_icon_2_val = settings.get("custom_icon_2", "")
        self.icon_row_2 = Adw.ActionRow(
            title="Device Icon 2",
            subtitle=os.path.basename(custom_icon_2_val) if custom_icon_2_val else ""
        )
        
        self.choose_icon_button_2 = Gtk.Button.new_from_icon_name("document-open-symbolic")
        self.choose_icon_button_2.set_valign(Gtk.Align.CENTER)
        self.choose_icon_button_2.set_tooltip_text("Choose Icon 2")
        
        self.clear_icon_button_2 = Gtk.Button.new_from_icon_name("edit-clear-symbolic")
        self.clear_icon_button_2.set_valign(Gtk.Align.CENTER)
        self.clear_icon_button_2.set_tooltip_text("Clear Icon 2")
        
        self.icon_row_2.add_suffix(self.choose_icon_button_2)
        self.icon_row_2.add_suffix(self.clear_icon_button_2)

        # 10. Custom Font Row
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
        self.app_selector.connect("notify::selected-item", self.on_app_changed)
        self.custom_app_row.connect("notify::text", self.on_custom_app_changed)
        self.vol_format_selector.connect("notify::selected-item", self.on_vol_format_changed)
        self.device_switch_row.connect("notify::active", self.on_device_switch_toggled)
        self.type_selector_2.connect("notify::selected-item", self.on_device_type_2_changed)
        self.pw_device_selector_2.connect("notify::selected-item", self.on_pw_device_2_changed)
        self.app_selector_2.connect("notify::selected-item", self.on_app_2_changed)
        self.custom_app_row_2.connect("notify::text", self.on_custom_app_2_changed)
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
        
        # Update visibility of conditional rows based on initial state
        self.update_visibility(device_switch_active)
        
        return [
            self.text_expander,
            self.type_selector,
            self.pw_device_selector,
            self.app_selector,
            self.custom_app_row,
            self.vol_format_selector,
            self.device_switch_row,
            self.type_selector_2,
            self.pw_device_selector_2,
            self.app_selector_2,
            self.custom_app_row_2,
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
        type_mapping = {0: "sink", 1: "source", 2: "app"}
        new_type = type_mapping.get(selected_index, "sink")
        
        settings = self.get_settings() or {}
        settings["device_type"] = new_type
        
        if new_type in ["sink", "source"]:
            settings["pipewire_device_id"] = "@DEFAULT_SINK@" if new_type == "sink" else "@DEFAULT_SOURCE@"
            settings["pipewire_device_name"] = "System Default Audio Output" if new_type == "sink" else "System Default Mic"
            self.update_device_dropdown()
        else:
            if not settings.get("app_name"):
                settings["app_name"] = "Spotify"
            self.update_app_dropdown()
            
        self.set_settings(settings)
        self.update_visibility(settings.get("device_switch", False))
        
        if getattr(self, "active_device_index", 1) == 1:
            self.restart_peak_monitor()
        self.update_ui_rendering(force=True)

    def on_device_type_2_changed(self, combo, *args):
        selected_index = combo.get_selected()
        type_mapping = {0: "sink", 1: "source", 2: "app"}
        new_type = type_mapping.get(selected_index, "sink")
        
        settings = self.get_settings() or {}
        settings["device_type_2"] = new_type
        
        if new_type in ["sink", "source"]:
            settings["pipewire_device_id_2"] = "@DEFAULT_SINK@" if new_type == "sink" else "@DEFAULT_SOURCE@"
            settings["pipewire_device_name_2"] = "System Default Audio Output" if new_type == "sink" else "System Default Mic"
            self.update_device_dropdown_2()
        else:
            if not settings.get("app_name_2"):
                settings["app_name_2"] = "Discord"
            self.update_app_dropdown_2()
            
        self.set_settings(settings)
        self.update_visibility(settings.get("device_switch", False))
        
        if getattr(self, "active_device_index", 1) == 2:
            self.restart_peak_monitor()
        self.update_ui_rendering(force=True)

    def on_app_changed(self, combo, *args):
        if getattr(self, "_updating_app_dropdown", False):
            return
        selected_index = combo.get_selected()
        if hasattr(self, "apps_map") and 0 <= selected_index < len(self.apps_map):
            chosen = self.apps_map[selected_index]
            settings = self.get_settings() or {}
            if chosen != "Custom Application...":
                settings["app_name"] = chosen
                if hasattr(self, "custom_app_row"):
                    self.custom_app_row.set_text(chosen)
            self.set_settings(settings)
            self.update_ui_rendering(force=True)

    def on_custom_app_changed(self, entry, *args):
        settings = self.get_settings() or {}
        val = entry.get_text().strip()
        if val:
            settings["app_name"] = val
            self.set_settings(settings)
            self.update_ui_rendering(force=True)

    def on_app_2_changed(self, combo, *args):
        if getattr(self, "_updating_app_dropdown_2", False):
            return
        selected_index = combo.get_selected()
        if hasattr(self, "apps_map_2") and 0 <= selected_index < len(self.apps_map_2):
            chosen = self.apps_map_2[selected_index]
            settings = self.get_settings() or {}
            if chosen != "Custom Application...":
                settings["app_name_2"] = chosen
                if hasattr(self, "custom_app_row_2"):
                    self.custom_app_row_2.set_text(chosen)
            self.set_settings(settings)
            self.update_ui_rendering(force=True)

    def on_custom_app_2_changed(self, entry, *args):
        settings = self.get_settings() or {}
        val = entry.get_text().strip()
        if val:
            settings["app_name_2"] = val
            self.set_settings(settings)
            self.update_ui_rendering(force=True)

    def on_vol_format_changed(self, combo, *args):
        selected_index = combo.get_selected()
        fmt = "percent" if selected_index == 0 else "db"
        settings = self.get_settings() or {}
        settings["volume_format"] = fmt
        self.set_settings(settings)
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
            
            if hasattr(self, "icon_row"):
                self.icon_row.set_subtitle(os.path.basename(path))
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
            
            if hasattr(self, "icon_row_2"):
                self.icon_row_2.set_subtitle(os.path.basename(path))
            self.clear_icon_button_2.set_sensitive(True)
            self.update_ui_rendering(force=True)
            
        GLib.idle_add(gl.app.let_user_select_asset, current_val, on_select_callback)

    def on_clear_icon_clicked(self, button):
        settings = self.get_settings() or {}
        settings["custom_icon"] = ""
        self.set_settings(settings)
        
        if hasattr(self, "icon_row"):
            self.icon_row.set_subtitle("")
        self.clear_icon_button.set_sensitive(False)
        self.update_ui_rendering(force=True)

    def on_clear_icon_2_clicked(self, button):
        settings = self.get_settings() or {}
        settings["custom_icon_2"] = ""
        self.set_settings(settings)
        
        if hasattr(self, "icon_row_2"):
            self.icon_row_2.set_subtitle("")
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

