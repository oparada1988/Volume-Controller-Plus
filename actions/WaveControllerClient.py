import os
import json
import socket
import threading
import time

class WaveControllerClient:
    """
    High-Performance Unix Domain Socket IPC Client for WaveController.
    Connects to $XDG_RUNTIME_DIR/wavecontroller.sock or /tmp/wavecontroller.sock.
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
        self.socket_paths = [
            os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "wavecontroller.sock"),
            "/tmp/wavecontroller.sock"
        ]
        self._cached_peaks = {}
        self._last_peaks_time = 0.0
        self._cached_channels_data = None
        self._last_channels_time = 0.0
        self._req_lock = threading.Lock()

    def send_command(self, cmd_dict: dict, timeout: float = 0.15) -> dict:
        """Sends a JSON command to WaveController and returns the parsed response."""
        with self._req_lock:
            sock = None
            for p in self.socket_paths:
                if os.path.exists(p):
                    try:
                        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        sock.settimeout(timeout)
                        sock.connect(p)
                        break
                    except Exception:
                        if sock:
                            try:
                                sock.close()
                            except Exception:
                                pass
                        sock = None

            if sock is None:
                return {}

            try:
                payload = json.dumps(cmd_dict).encode("utf-8")
                sock.sendall(payload)
                raw_res = sock.recv(8192).decode("utf-8")
                if raw_res:
                    return json.loads(raw_res)
            except Exception:
                pass
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

            return {}

    def get_channels_and_mixes(self, force: bool = False) -> dict:
        """Queries active channels, mixes, and submix states from WaveController."""
        now = time.time()
        if not force and self._cached_channels_data and (now - self._last_channels_time < 1.0):
            return self._cached_channels_data

        res = self.send_command({"command": "get_channels"})
        if res and res.get("status") == "ok":
            self._cached_channels_data = res
            self._last_channels_time = now
            return res
        return self._cached_channels_data or {}

    def get_channel_volume(self, channel_id: str, mix_id: str = None) -> tuple:
        """Returns (volume_pct, is_muted) for a channel or submix."""
        cmd = {"command": "get_volume", "channel_id": channel_id}
        if mix_id:
            cmd["mix_id"] = mix_id
        res = self.send_command(cmd)
        state = res.get("state", {})
        return state.get("volume", 80), state.get("muted", False)

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
        return res.get("muted", False)

    def get_mix_master_volume(self, mix_id: str) -> tuple:
        """Returns (volume_pct, is_muted) for a mix bus."""
        res = self.send_command({"command": "get_mix_master_volume", "mix_id": mix_id})
        return res.get("volume", 100), res.get("muted", False)

    def set_mix_master_volume(self, mix_id: str, volume: int, muted: bool = None):
        """Sets mix master bus volume."""
        cmd = {"command": "set_mix_master_volume", "mix_id": mix_id, "volume": int(volume)}
        if muted is not None:
            cmd["muted"] = bool(muted)
        self.send_command(cmd)

    def toggle_mix_master_mute(self, mix_id: str) -> bool:
        """Toggles mix master bus mute."""
        res = self.send_command({"command": "toggle_mix_master_mute", "mix_id": mix_id})
        return res.get("muted", False)

    def get_output_devices(self) -> list:
        """Queries active physical output devices detected by WaveController."""
        res = self.send_command({"command": "get_output_devices"})
        return res.get("devices", [])

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
        for p in self.socket_paths:
            if os.path.exists(p):
                return True
        return False
