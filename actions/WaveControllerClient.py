import os
import json
import socket
import threading
import time

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

        self._cache_lock = threading.Lock()
        self._cached_peaks = {}
        self._last_peaks_time = 0.0
        self._cached_channels_data = None
        self._last_channels_time = 0.0
        self._cached_devices = []
        self._cached_hardware_status = {}

        self._cmd_sock = None
        self._cmd_lock = threading.Lock()
        self._running = True

        # Preload initial fallback data
        self._load_config_fallback()

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
            if time.time() - start_t > 0.35:
                break
            try:
                chunk = sock.recv(8192).decode("utf-8")
                if not chunk:
                    break
                buffer += chunk
            except socket.timeout:
                break
            except Exception:
                break

        if "\n" in buffer:
            line, rest = buffer.split("\n", 1)
            return line.strip(), rest
        return "", buffer

    def _load_config_fallback(self) -> dict:
        """Reads channels, mixes, and states directly from WaveController config.json."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    fallback_data = {
                        "status": "ok",
                        "channels": data.get("channels", []),
                        "mixes": data.get("mixes", []),
                        "states": data.get("channel_states", {}),
                        "master_states": data.get("channel_master_states", {}),
                        "mix_states": data.get("mix_states", {}),
                        "device_aliases": data.get("device_aliases", {}),
                        "assigned_apps": data.get("assigned_apps", {})
                    }
                    with self._cache_lock:
                        if not self._cached_channels_data:
                            self._cached_channels_data = fallback_data
                    return fallback_data
            except Exception:
                pass
        return {}

    def _run_bg_poller(self):
        """Dedicated background loop streaming live peaks and states with 0ms UI impact."""
        sock = None
        buf = ""
        last_channels_poll = 0.0
        last_devices_poll = 0.0
        last_hardware_poll = 0.0

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

                # 1. Periodic full channel state refresh (every 150ms)
                if now - last_channels_poll > 0.15:
                    last_channels_poll = now
                    sock.sendall(b'{"command": "get_channels"}\n')
                    line, buf = self._read_socket_line(sock, buf)
                    if line:
                        res = json.loads(line)
                        if res.get("status") == "ok":
                            with self._cache_lock:
                                self._cached_channels_data = res
                                self._last_channels_time = now

                # 2. Periodic devices refresh (every 500ms)
                if now - last_devices_poll > 0.50:
                    last_devices_poll = now
                    sock.sendall(b'{"command": "get_output_devices"}\n')
                    line, buf = self._read_socket_line(sock, buf)
                    if line:
                        res = json.loads(line)
                        if "devices" in res:
                            with self._cache_lock:
                                self._cached_devices = res.get("devices", [])

                # 3. Periodic hardware status refresh (every 200ms)
                if now - last_hardware_poll > 0.20:
                    last_hardware_poll = now
                    sock.sendall(b'{"command": "get_hardware_status"}\n')
                    line, buf = self._read_socket_line(sock, buf)
                    if line:
                        res = json.loads(line)
                        if res.get("status") == "ok" or "device_name" in res:
                            with self._cache_lock:
                                self._cached_hardware_status = res

                # 4. High-frequency live VU peak poll (every ~25ms / 40 FPS)
                sock.sendall(b'{"command": "get_peaks"}\n')
                line, buf = self._read_socket_line(sock, buf)
                if line:
                    res = json.loads(line)
                    if res.get("status") == "ok" and "peaks" in res:
                        with self._cache_lock:
                            self._cached_peaks = res["peaks"]
                            self._last_peaks_time = time.time()

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

            time.sleep(0.025) # 40 FPS smooth streaming

    def send_command(self, cmd_dict: dict, timeout: float = 0.20) -> dict:
        """Sends an immediate synchronous JSON command to WaveController over command socket."""
        with self._cmd_lock:
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
                            break
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

    def _send_command_async(self, cmd_dict: dict):
        """Dispatches an IPC command asynchronously in background without blocking UI."""
        def worker():
            try:
                self.send_command(cmd_dict, timeout=0.30)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def get_channels_and_mixes(self, force: bool = False) -> dict:
        """Instantly returns active channels, mixes, and states from memory."""
        with self._cache_lock:
            if self._cached_channels_data and self._cached_channels_data.get("channels"):
                return self._cached_channels_data
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
        res = self.send_command({"command": "get_hardware_status"}, timeout=0.15)
        if res and (res.get("status") == "ok" or "device_name" in res):
            with self._cache_lock:
                self._cached_hardware_status = res
            return res
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

    def is_connected(self) -> bool:
        """Checks if WaveController IPC socket is active."""
        for p in self._get_socket_paths():
            if os.path.exists(p):
                return True
        return False
