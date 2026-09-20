import os
import json
import socket
import threading
import time
import queue

class WaveControllerClient:
    """
    High-Performance Asynchronous Non-Blocking IPC Client for WaveController.
    Runs a dedicated background polling worker to stream audio peaks and mixer states
    at 40 FPS without blocking the GTK main UI thread.
    """
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = WaveControllerClient()
            return cls._instance

    def __init__(self):
        self.config_dir = os.path.expanduser("~/.config/WaveController")
        self.config_path = os.path.join(self.config_dir, "config.json")
        self.config_socket_path = os.path.join(self.config_dir, "wavecontroller.sock")

        self._cache_lock = threading.RLock()
        self._cached_peaks = {}
        self._last_peaks_time = 0.0
        self._cached_channels_data = None
        self._last_channels_time = 0.0
        self._cached_devices = []
        self._cached_hardware_status = {}

        self._cmd_sock = None
        self._cmd_lock = threading.Lock()
        self._running = True

        self._config_mtime = 0.0
        self._use_system_theme = False
        self._last_theme_check_time = 0.0

        # Preload initial fallback data
        self._load_config_fallback()

        # Dedicated sequential async command queue to prevent thread storm and race conditions
        self._async_queue = queue.Queue(maxsize=150)
        self._async_worker = threading.Thread(target=self._run_async_worker, daemon=True, name="WaveControllerAsyncWorker")
        self._async_worker.start()

        # Start dedicated background poller thread
        self._bg_thread = threading.Thread(target=self._run_bg_poller, daemon=True, name="WaveControllerClientPoller")
        self._bg_thread.start()

    def _get_socket_paths(self) -> list:
        paths = [self.config_socket_path]
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            paths.append(os.path.join(xdg, "wavecontroller.sock"))
        paths.append("/run/user/1000/wavecontroller.sock")
        paths.append("/tmp/wavecontroller.sock")
        return list(dict.fromkeys(paths))

    def _connect_socket(self, timeout: float = 0.25):
        for p in self._get_socket_paths():
            if os.path.exists(p):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(timeout)
                    s.connect(p)
                    return s
                except Exception:
                    try:
                        s.close()
                    except Exception:
                        pass
        return None

    def _read_socket_line(self, sock, buffer: str) -> tuple:
        """Reads until a complete newline is received from socket."""
        start_t = time.time()
        while "\n" not in buffer:
            if time.time() - start_t > 0.20:
                break
            try:
                chunk = sock.recv(8192).decode("utf-8")
                if not chunk:
                    raise ConnectionResetError("Socket closed by peer (EOF)")
                buffer += chunk
            except socket.timeout:
                break
            except ConnectionResetError:
                raise
            except Exception:
                break

        if "\n" in buffer:
            line, rest = buffer.split("\n", 1)
            return line.strip(), rest
        return "", buffer

    def _load_config_fallback(self) -> dict:
        """Reads channels, mixes, and states directly from WaveController config.json with mtime caching."""
        now = time.time()
        if hasattr(self, "_cached_config_fallback") and self._cached_config_fallback:
            if now - getattr(self, "_last_config_check_time", 0.0) < 1.0:
                return self._cached_config_fallback

        self._last_config_check_time = now
        if os.path.exists(self.config_path):
            try:
                mtime = os.path.getmtime(self.config_path)
                if hasattr(self, "_cached_config_fallback") and self._cached_config_fallback and mtime == getattr(self, "_config_fallback_mtime", 0.0):
                    return self._cached_config_fallback
                self._config_fallback_mtime = mtime

                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    def_ch_id = data.get("default_output_channel_id", "")
                    sys_defs_enabled = data.get("system_defaults_enabled", False)
                    channels = data.get("channels", [])
                    for c in channels:
                        c_id = c.get("id", "")
                        is_def = (c_id == def_ch_id) and bool(sys_defs_enabled)
                        c["is_default"] = is_def
                        if str(c.get("type", "")).lower() == "virtual":
                            c["icon"] = "video-display-symbolic" if is_def else "audio-card-symbolic"

                    fallback_data = {
                        "status": "ok",
                        "channels": channels,
                        "mixes": data.get("mixes", []),
                        "states": data.get("channel_states", {}),
                        "master_states": data.get("channel_master_states", {}),
                        "mix_states": data.get("mix_states", {}),
                        "device_aliases": data.get("device_aliases", {}),
                        "assigned_apps": data.get("assigned_apps", {}),
                        "use_system_theme": data.get("use_system_theme", False),
                        "default_output_channel_id": def_ch_id,
                        "system_defaults_enabled": sys_defs_enabled
                    }
                    self._use_system_theme = bool(data.get("use_system_theme", False))
                    self._cached_config_fallback = fallback_data
                    with self._cache_lock:
                        if not self._cached_channels_data:
                            self._cached_channels_data = fallback_data
                        else:
                            self._cached_channels_data["use_system_theme"] = self._use_system_theme
                            self._cached_channels_data["default_output_channel_id"] = def_ch_id
                            self._cached_channels_data["system_defaults_enabled"] = sys_defs_enabled
                            self._cached_channels_data["channels"] = channels
                    return fallback_data
            except Exception:
                pass
        return getattr(self, "_cached_config_fallback", {}) or {}

    def _enrich_channels_data(self, data_dict: dict, default_id: str = None) -> dict:
        """Enriches channel metadata with default output channel status and dynamic icons."""
        if not data_dict or not isinstance(data_dict, dict):
            return data_dict
        if default_id is not None:
            def_id = default_id
        else:
            with self._cache_lock:
                def_id = data_dict.get("default_output_channel_id") or (self._cached_channels_data.get("default_output_channel_id", "") if self._cached_channels_data else "")
        data_dict["default_output_channel_id"] = def_id
        for c in data_dict.get("channels", []):
            c_id = c.get("id", "")
            is_def = bool(def_id and c_id == def_id)
            c["is_default"] = is_def
            if str(c.get("type", "")).lower() == "virtual":
                c["icon"] = "video-display-symbolic" if is_def else "audio-card-symbolic"
        return data_dict

    def get_use_system_theme(self) -> bool:
        """Returns whether WaveController is configured to use the system GTK/Libadwaita theme."""
        now = time.time()
        if now - getattr(self, "_last_theme_check_time", 0.0) > 1.0:
            self._last_theme_check_time = now
            if os.path.exists(self.config_path):
                try:
                    mtime = os.path.getmtime(self.config_path)
                    if mtime != getattr(self, "_config_mtime", 0.0):
                        self._config_mtime = mtime
                        with open(self.config_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            self._use_system_theme = bool(data.get("use_system_theme", False))
                except Exception:
                    pass
        return getattr(self, "_use_system_theme", False)

    def _run_async_worker(self):
        """Sequential single-worker queue processing IPC commands strictly in chronological order."""
        while self._running:
            try:
                cmd_dict = self._async_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                res = self.send_command(cmd_dict, timeout=0.25)
                if res and (res.get("status") == "ok" or "device_name" in res or "phantom_48v" in res):
                    if cmd_dict.get("command") == "get_hardware_status":
                        with self._cache_lock:
                            self._cached_hardware_status = res
            except Exception:
                pass
            finally:
                self._async_queue.task_done()

    def _run_bg_poller(self):
        """Dedicated background loop streaming live peaks and states with 0ms UI impact."""
        sock = None
        buf = ""
        last_channels_poll = 0.0

        while self._running:
            if sock is None:
                sock = self._connect_socket(timeout=0.30)
                buf = ""
                if sock is None:
                    self._load_config_fallback()
                    time.sleep(0.20)
                    continue

            try:
                now = time.time()

                # 1. Background channel topology & devices refresh (every 5 seconds, non-intrusive)
                if now - last_channels_poll > 5.0:
                    last_channels_poll = now
                    try:
                        sock.sendall(b'{"command": "get_channels"}\n')
                        line, buf = self._read_socket_line(sock, buf)
                        if line:
                            res = json.loads(line)
                            if res.get("status") == "ok":
                                self._enrich_channels_data(res)
                                with self._cache_lock:
                                    self._cached_channels_data = res
                                    self._last_channels_time = now
                    except Exception:
                        pass

                # 2. High-frequency atomic 30 FPS telemetry stream (peaks, volumes, and hardware status)
                sock.sendall(b'{"command": "get_peaks"}\n')
                line, buf = self._read_socket_line(sock, buf)
                if line:
                    res = json.loads(line)
                    if res.get("status") == "ok":
                        with self._cache_lock:
                            if "peaks" in res:
                                self._cached_peaks = res["peaks"]
                                self._last_peaks_time = time.time()
                            if not self._cached_channels_data:
                                self._cached_channels_data = self._load_config_fallback() or {}
                            if "mix_states" in res and isinstance(res["mix_states"], dict):
                                self._cached_channels_data["mix_states"] = res["mix_states"]
                            if "channel_master_states" in res and isinstance(res["channel_master_states"], dict):
                                self._cached_channels_data["master_states"] = res["channel_master_states"]
                            if "channel_states" in res and isinstance(res["channel_states"], dict):
                                self._cached_channels_data["states"] = res["channel_states"]
                            if "hardware" in res and isinstance(res["hardware"], dict):
                                self._cached_hardware_status = res["hardware"]
                            if "default_output_channel_id" in res:
                                new_def = res["default_output_channel_id"]
                                old_def = self._cached_channels_data.get("default_output_channel_id", "")
                                if new_def != old_def:
                                    self._cached_channels_data["default_output_channel_id"] = new_def
                                    for ch in self._cached_channels_data.get("channels", []):
                                        ch["is_default"] = (ch.get("id") == new_def)
                                        if str(ch.get("type", "")).lower() == "virtual":
                                            ch["icon"] = "video-display-symbolic" if ch["is_default"] else "audio-card-symbolic"

                if len(buf) > 32768:
                    buf = ""

            except Exception:
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass
                sock = None
                buf = ""
                time.sleep(0.10)
                continue

            time.sleep(0.033) # 30 FPS smooth streaming

    def send_command(self, cmd_dict: dict, timeout: float = 0.20) -> dict:
        """Sends an immediate synchronous JSON command to WaveController over command socket."""
        acquired = self._cmd_lock.acquire(timeout=min(timeout, 0.05))
        if not acquired:
            return {}
        try:
            for attempt in range(2):
                if self._cmd_sock is None:
                    self._cmd_sock = self._connect_socket(timeout=timeout)
                if self._cmd_sock is None:
                    return {}

                try:
                    self._cmd_sock.settimeout(timeout)
                    payload = (json.dumps(cmd_dict) + "\n").encode("utf-8")
                    self._cmd_sock.sendall(payload)

                    raw_res = ""
                    while "\n" not in raw_res:
                        chunk = self._cmd_sock.recv(4096).decode("utf-8")
                        if not chunk:
                            raise ConnectionResetError("Command socket EOF")
                        raw_res += chunk

                    if raw_res:
                        line = raw_res.strip().split("\n")[0]
                        if line:
                            return json.loads(line)
                except Exception:
                    if self._cmd_sock:
                        try:
                            self._cmd_sock.close()
                        except Exception:
                            pass
                    self._cmd_sock = None
                    if attempt == 1:
                        return {}
            return {}
        finally:
            self._cmd_lock.release()

    def _send_command_async(self, cmd_dict: dict):
        """Enqueues an IPC command sequentially without spawning threads or blocking UI."""
        try:
            self._async_queue.put_nowait(cmd_dict)
        except queue.Full:
            try:
                self._async_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._async_queue.put_nowait(cmd_dict)
            except Exception:
                pass

    def get_channels_and_mixes(self, force: bool = False) -> dict:
        """Instantly returns active channels, mixes, and states from memory, or queries socket if force=True."""
        with self._cache_lock:
            if not force and self._cached_channels_data and self._cached_channels_data.get("channels"):
                return self._cached_channels_data
        if force:
            res = self.send_command({"command": "get_channels"}, timeout=0.20)
            if res and res.get("status") == "ok":
                self._enrich_channels_data(res)
                with self._cache_lock:
                    self._cached_channels_data = res
                return res
        fallback = self._load_config_fallback()
        return fallback or {}

    def get_channel_volume(self, channel_id: str, mix_id: str = None) -> tuple:
        """Instantly returns (volume_pct, is_muted) from memory in 0ms."""
        with self._cache_lock:
            if self._cached_channels_data:
                if mix_id:
                    st = self._cached_channels_data.get("states", {}).get(channel_id, {}).get(mix_id, {})
                    if st:
                        vol = st.get("volume", 80)
                        muted = st.get("muted", False)
                        if not st.get("enabled", True):
                            muted = True
                        return int(vol) if vol is not None else 80, bool(muted) if muted is not None else False
                else:
                    st = self._cached_channels_data.get("master_states", {}).get(channel_id, {})
                    if st:
                        vol = st.get("volume", 80)
                        muted = st.get("muted", False)
                        return int(vol) if vol is not None else 80, bool(muted) if muted is not None else False

        # Fallback to local config if cache is still initializing
        fallback = self._load_config_fallback()
        if mix_id:
            st = fallback.get("states", {}).get(channel_id, {}).get(mix_id, {})
            vol = st.get("volume", 80)
            muted = st.get("muted", False)
            if not st.get("enabled", True):
                muted = True
            return int(vol) if vol is not None else 80, bool(muted) if muted is not None else False
        else:
            st = fallback.get("master_states", {}).get(channel_id, {})
            vol = st.get("volume", 80)
            muted = st.get("muted", False)
            return int(vol) if vol is not None else 80, bool(muted) if muted is not None else False

    def set_channel_volume(self, channel_id: str, volume: int, mix_id: str = None, muted: bool = None):
        """Sets channel master volume or submix fader level with instant UI cache reflection."""
        # 1. Optimistically update local cache
        with self._cache_lock:
            if self._cached_channels_data:
                if mix_id:
                    st = self._cached_channels_data.setdefault("states", {}).setdefault(channel_id, {}).setdefault(mix_id, {})
                    st["volume"] = int(volume)
                    if muted is not None:
                        st["muted"] = bool(muted)
                else:
                    st = self._cached_channels_data.setdefault("master_states", {}).setdefault(channel_id, {})
                    st["volume"] = int(volume)
                    if muted is not None:
                        st["muted"] = bool(muted)

        # 2. Dispatch command asynchronously
        cmd = {"command": "set_volume", "channel_id": channel_id, "volume": int(volume)}
        if mix_id:
            cmd["mix_id"] = mix_id
        if muted is not None:
            cmd["muted"] = bool(muted)
        self._send_command_async(cmd)

    def toggle_channel_mute(self, channel_id: str, mix_id: str = None) -> bool:
        """Toggles mute for a channel master or submix with instant local state reflection."""
        curr_vol, curr_mute = self.get_channel_volume(channel_id, mix_id=mix_id)
        new_mute = not curr_mute
        self.set_channel_volume(channel_id, curr_vol, mix_id=mix_id, muted=new_mute)
        return new_mute

    def get_mix_master_volume(self, mix_id: str) -> tuple:
        """Instantly returns (volume_pct, is_muted) for a mix bus from memory."""
        with self._cache_lock:
            if self._cached_channels_data:
                st = self._cached_channels_data.get("mix_states", {}).get(mix_id, {})
                if st:
                    vol = st.get("volume", 100)
                    muted = st.get("muted", False)
                    return int(vol) if vol is not None else 100, bool(muted) if muted is not None else False

        fallback = self._load_config_fallback()
        st = fallback.get("mix_states", {}).get(mix_id, {})
        vol = st.get("volume", 100)
        muted = st.get("muted", False)
        return int(vol) if vol is not None else 100, bool(muted) if muted is not None else False

    def set_mix_master_volume(self, mix_id: str, volume: int, muted: bool = None):
        """Sets mix master bus volume with instant UI reflection."""
        with self._cache_lock:
            if self._cached_channels_data:
                st = self._cached_channels_data.setdefault("mix_states", {}).setdefault(mix_id, {})
                st["volume"] = int(volume)
                if muted is not None:
                    st["muted"] = bool(muted)

        cmd = {"command": "set_mix_master_volume", "mix_id": mix_id, "volume": int(volume)}
        if muted is not None:
            cmd["muted"] = bool(muted)
        self._send_command_async(cmd)

    def toggle_mix_master_mute(self, mix_id: str) -> bool:
        """Toggles mix master bus mute."""
        curr_vol, curr_mute = self.get_mix_master_volume(mix_id)
        new_mute = not curr_mute
        self.set_mix_master_volume(mix_id, curr_vol, muted=new_mute)
        return new_mute

    def get_output_devices(self) -> list:
        """Instantly returns output devices from memory."""
        with self._cache_lock:
            if self._cached_devices:
                return list(self._cached_devices)

        fallback = self._load_config_fallback()
        aliases = fallback.get("device_aliases", {})
        devs = []
        for dev_id, display_name in aliases.items():
            devs.append({"name": dev_id, "display_name": display_name})
        return devs

    def set_mix_target_device(self, mix_id: str, target_device: str):
        """Sets target physical output device for a mix."""
        cmd = {"command": "set_mix_target_device", "mix_id": mix_id, "target_device": target_device}
        self._send_command_async(cmd)

    def cycle_mix_target_device(self, mix_id: str) -> str:
        """Cycles target physical output device for a mix."""
        res = self.send_command({"command": "cycle_mix_target_device", "mix_id": mix_id}, timeout=0.25)
        return res.get("target_device", "none")

    def get_peaks(self) -> dict:
        """Instantly returns the latest audio peaks dictionary from memory in 0ms."""
        with self._cache_lock:
            return dict(self._cached_peaks)

    def get_hardware_status(self) -> dict:
        """Instantly returns the latest hardware status (phantom power, connection, gain) from memory."""
        with self._cache_lock:
            if self._cached_hardware_status:
                return dict(self._cached_hardware_status)
        now = time.time()
        if now - getattr(self, "_last_hw_req_time", 0.0) > 1.0:
            self._last_hw_req_time = now
            self._send_command_async({"command": "get_hardware_status"})
        return {}

    def toggle_phantom_power(self) -> bool:
        """Toggles 48V phantom power on hardware with instant cached reflection."""
        with self._cache_lock:
            curr = self._cached_hardware_status.get("phantom_48v", False)
            self._cached_hardware_status["phantom_48v"] = not curr
        res = self.send_command({"command": "toggle_phantom_power"}, timeout=0.25)
        new_val = res.get("phantom_48v", not curr)
        with self._cache_lock:
            self._cached_hardware_status["phantom_48v"] = new_val
        return new_val

    def get_fx_status(self, channel_id: str = "mic") -> bool:
        """Returns whether real-time DSP FX are enabled for a specific channel."""
        ch_clean = channel_id.strip() if channel_id else "mic"
        # 1. Fast path from cached channels data if present
        with self._cache_lock:
            if self._cached_channels_data and "channels" in self._cached_channels_data:
                for c in self._cached_channels_data["channels"]:
                    if c.get("id") == ch_clean and "fx_enabled" in c:
                        return bool(c["fx_enabled"])
        # 2. Query direct IPC command
        res = self.send_command({"command": "get_fx_status", "channel_id": ch_clean}, timeout=0.25)
        if "enabled" in res:
            return bool(res["enabled"])
        # 3. Fallback to config file
        cfg = self._load_config_fallback()
        ch_fx = cfg.get("channel_fx", {}).get(ch_clean, {})
        return bool(ch_fx.get("enabled", True))

    def toggle_fx(self, channel_id: str = "mic") -> bool:
        """Toggles real-time DSP FX on/off for a channel with instant local cache update."""
        ch_clean = channel_id.strip() if channel_id else "mic"
        res = self.send_command({"command": "toggle_fx", "channel_id": ch_clean}, timeout=0.35)
        new_val = res.get("enabled")
        if new_val is None:
            curr = self.get_fx_status(ch_clean)
            new_val = not curr
        with self._cache_lock:
            if self._cached_channels_data and "channels" in self._cached_channels_data:
                for c in self._cached_channels_data["channels"]:
                    if c.get("id") == ch_clean:
                        c["fx_enabled"] = new_val
        return bool(new_val)

    def is_connected(self) -> bool:
        """Checks if WaveController IPC socket is active."""
        for p in self._get_socket_paths():
            if os.path.exists(p):
                return True
        return False

    def get_default_output_channel_id(self) -> str:
        """Returns the channel ID of the system default playback channel."""
        with self._cache_lock:
            if self._cached_channels_data and self._cached_channels_data.get("default_output_channel_id"):
                return self._cached_channels_data.get("default_output_channel_id", "")
        fallback = self._load_config_fallback()
        return fallback.get("default_output_channel_id", "")

    def is_channel_system_default(self, channel_id: str) -> bool:
        """Returns whether a channel is the designated system default audio output."""
        if not channel_id:
            return False
        with self._cache_lock:
            if self._cached_channels_data:
                def_id = self._cached_channels_data.get("default_output_channel_id", "")
                if def_id:
                    return (def_id == channel_id)
                for c in self._cached_channels_data.get("channels", []):
                    if c.get("id") == channel_id:
                        return bool(c.get("is_default", False))
        def_id = self.get_default_output_channel_id()
        return bool(def_id and def_id == channel_id)

    def set_channel_system_default(self, channel_id: str, is_default: bool = True):
        """Selects or deselects a virtual channel as the system default playback output."""
        new_def = channel_id if is_default else ""
        with self._cache_lock:
            if self._cached_channels_data:
                self._cached_channels_data["default_output_channel_id"] = new_def
                for ch in self._cached_channels_data.get("channels", []):
                    ch["is_default"] = (ch.get("id") == new_def)
                    if str(ch.get("type", "")).lower() == "virtual":
                        ch["icon"] = "video-display-symbolic" if ch["is_default"] else "audio-card-symbolic"

        cmd = {"command": "set_channel_system_default", "channel_id": channel_id, "is_default": bool(is_default)}
        self._send_command_async(cmd)
