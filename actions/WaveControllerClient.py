import os
import json
import socket
import threading
import time

class WaveControllerClient:
    """
    High-Performance Unix Domain Socket IPC Client for WaveController.
    Connects to ~/.config/WaveController/wavecontroller.sock, $XDG_RUNTIME_DIR/wavecontroller.sock,
    or /tmp/wavecontroller.sock with persistent config.json fallback.
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
        self._cached_peaks = {}
        self._last_peaks_time = 0.0
        self._cached_channels_data = None
        self._last_channels_time = 0.0
        self._req_lock = threading.Lock()
        self._socket = None

    def _get_socket_paths(self) -> list:
        paths = [self.config_socket_path]
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            paths.append(os.path.join(xdg, "wavecontroller.sock"))
        paths.append("/run/user/1000/wavecontroller.sock")
        paths.append("/tmp/wavecontroller.sock")
        return list(dict.fromkeys(paths))

    def _get_connected_socket(self, timeout: float = 0.20):
        if self._socket is not None:
            return self._socket

        for p in self._get_socket_paths():
            if os.path.exists(p):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(timeout)
                    s.connect(p)
                    self._socket = s
                    return self._socket
                except Exception:
                    try:
                        s.close()
                    except Exception:
                        pass
        return None

    def send_command(self, cmd_dict: dict, timeout: float = 0.15) -> dict:
        """Sends a JSON command to WaveController over persistent socket and returns the parsed response."""
        with self._req_lock:
            for attempt in range(2):
                sock = self._get_connected_socket(timeout=timeout)
                if sock is None:
                    return {}

                try:
                    sock.settimeout(timeout)
                    payload = (json.dumps(cmd_dict) + "\n").encode("utf-8")
                    sock.sendall(payload)

                    raw_res = ""
                    while "\n" not in raw_res:
                        chunk = sock.recv(4096).decode("utf-8")
                        if not chunk:
                            break
                        raw_res += chunk

                    if raw_res:
                        line = raw_res.strip().split("\n")[0]
                        if line:
                            return json.loads(line)
                except Exception:
                    if self._socket:
                        try:
                            self._socket.close()
                        except Exception:
                            pass
                    self._socket = None
                    if attempt == 1:
                        return {}
            return {}

    def _load_config_fallback(self) -> dict:
        """Reads channels, mixes, and states directly from WaveController config.json."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {
                        "status": "ok",
                        "channels": data.get("channels", []),
                        "mixes": data.get("mixes", []),
                        "states": data.get("channel_states", {}),
                        "master_states": data.get("channel_master_states", {}),
                        "mix_states": data.get("mix_states", {}),
                        "device_aliases": data.get("device_aliases", {}),
                        "assigned_apps": data.get("assigned_apps", {})
                    }
            except Exception:
                pass
        return {}

    def get_channels_and_mixes(self, force: bool = False) -> dict:
        """Queries active channels, mixes, and submix states from WaveController."""
        now = time.time()
        if not force and self._cached_channels_data and (now - self._last_channels_time < 0.5):
            return self._cached_channels_data

        res = self.send_command({"command": "get_channels"})
        if res and res.get("status") == "ok" and res.get("channels"):
            self._cached_channels_data = res
            self._last_channels_time = now
            return res

        fallback = self._load_config_fallback()
        if fallback and fallback.get("channels"):
            self._cached_channels_data = fallback
            self._last_channels_time = now
            return fallback

        return self._cached_channels_data or {}

    def get_channel_volume(self, channel_id: str, mix_id: str = None) -> tuple:
        """Returns (volume_pct, is_muted) for a channel or submix."""
        cmd = {"command": "get_volume", "channel_id": channel_id}
        if mix_id:
            cmd["mix_id"] = mix_id
        res = self.send_command(cmd)
        if res and "state" in res:
            state = res["state"]
            return state.get("volume", 80), state.get("muted", False)

        # Fallback to local config
        fallback = self._load_config_fallback()
        if mix_id:
            st = fallback.get("states", {}).get(channel_id, {}).get(mix_id, {})
            return st.get("volume", 80), st.get("muted", False)
        else:
            st = fallback.get("master_states", {}).get(channel_id, {})
            return st.get("volume", 80), st.get("muted", False)

    def set_channel_volume(self, channel_id: str, volume: int, mix_id: str = None, muted: bool = None):
        """Sets channel master volume or submix fader level."""
        cmd = {"command": "set_volume", "channel_id": channel_id, "volume": int(volume)}
        if mix_id:
            cmd["mix_id"] = mix_id
        if muted is not None:
            cmd["muted"] = bool(muted)
        self.send_command(cmd)

    def toggle_channel_mute(self, channel_id: str, mix_id: str = None) -> bool:
        """Toggles mute for a channel master or submix."""
        cmd = {"command": "toggle_mute", "channel_id": channel_id}
        if mix_id:
            cmd["mix_id"] = mix_id
        res = self.send_command(cmd)
        if res and "muted" in res:
            return res["muted"]
        curr_vol, curr_mute = self.get_channel_volume(channel_id, mix_id=mix_id)
        new_mute = not curr_mute
        self.set_channel_volume(channel_id, curr_vol, mix_id=mix_id, muted=new_mute)
        return new_mute

    def get_mix_master_volume(self, mix_id: str) -> tuple:
        """Returns (volume_pct, is_muted) for a mix bus."""
        res = self.send_command({"command": "get_mix_master_volume", "mix_id": mix_id})
        if res and "volume" in res:
            return res["volume"], res.get("muted", False)

        fallback = self._load_config_fallback()
        st = fallback.get("mix_states", {}).get(mix_id, {})
        return st.get("volume", 100), st.get("muted", False)

    def set_mix_master_volume(self, mix_id: str, volume: int, muted: bool = None):
        """Sets mix master bus volume."""
        cmd = {"command": "set_mix_master_volume", "mix_id": mix_id, "volume": int(volume)}
        if muted is not None:
            cmd["muted"] = bool(muted)
        self.send_command(cmd)

    def toggle_mix_master_mute(self, mix_id: str) -> bool:
        """Toggles mix master bus mute."""
        res = self.send_command({"command": "toggle_mix_master_mute", "mix_id": mix_id})
        if res and "muted" in res:
            return res["muted"]
        curr_vol, curr_mute = self.get_mix_master_volume(mix_id)
        new_mute = not curr_mute
        self.set_mix_master_volume(mix_id, curr_vol, muted=new_mute)
        return new_mute

    def get_output_devices(self) -> list:
        """Queries active physical output devices detected by WaveController."""
        res = self.send_command({"command": "get_output_devices"})
        if res and "devices" in res and res["devices"]:
            return res["devices"]

        # Fallback to config device aliases
        fallback = self._load_config_fallback()
        aliases = fallback.get("device_aliases", {})
        devs = []
        for dev_id, display_name in aliases.items():
            devs.append({"name": dev_id, "display_name": display_name})
        return devs

    def set_mix_target_device(self, mix_id: str, target_device: str):
        """Sets target physical output device for a mix."""
        self.send_command({"command": "set_mix_target_device", "mix_id": mix_id, "target_device": target_device})

    def cycle_mix_target_device(self, mix_id: str) -> str:
        """Cycles target physical output device for a mix and returns new device ID."""
        res = self.send_command({"command": "cycle_mix_target_device", "mix_id": mix_id})
        return res.get("target_device", "none")

    def get_peaks(self) -> dict:
        """Retrieves real-time stereo peaks with 25ms coalescing across all dials."""
        now = time.time()
        if (now - self._last_peaks_time) < 0.025 and self._cached_peaks:
            return self._cached_peaks

        res = self.send_command({"command": "get_peaks"}, timeout=0.08)
        if res and "peaks" in res:
            self._cached_peaks = res["peaks"]
            self._last_peaks_time = now
            return self._cached_peaks
        return self._cached_peaks or {}

    def is_connected(self) -> bool:
        """Checks if WaveController IPC socket is active."""
        for p in self._get_socket_paths():
            if os.path.exists(p):
                return True
        return False
