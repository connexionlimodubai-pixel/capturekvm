"""Where this machine's identity, its paired PCs and its settings live.

One JSON file under %APPDATA%\\CaptureKVM. The peer entries are exactly the dicts Beamer's
pairing builds (`core.pairing.peer_entry`), kept whole so a future version can hand them to more
of the upstream code without a migration.
"""

import base64
import json
import os
import secrets
import tempfile
import threading
from pathlib import Path

from core import pairing, protocol

MACHINE_ID_BYTES = 16

DEFAULT_SETTINGS = {
    # Capture
    "device_index": 0,
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "fourcc": "MJPG",
    # Control
    "key_style": "semantic",
    "resistance_px": 0,
    "scroll_lines_per_notch": 3.0,
    "mouse_scale": 1.0,
    "natural_scroll": False,
    # Clipboard. Upstream Beamer's capability: the clipboard crosses at the moment of a switch.
    "clipboard_enabled": True,
    "clipboard_images": True,
    # The card's sound, played locally. Never goes over the link.
    "audio_enabled": False,
    "audio_input": -1,            # -1 means "pick one that looks like a capture card"
    "audio_output": -1,           # -1 means this machine's default output
    "audio_block_frames": 256,
    # View
    "smooth_scaling": True,       # False gives nearest-neighbour, for a deliberate pixel look
    "scale_mode": "fit",          # "fit" or "actual" (1:1, nothing resampled)
    # Picture, applied to each frame on the capture thread. All off by default.
    "range_mode": "none",
    "brightness": 0,
    "contrast": 1.0,
    "gamma": 1.0,
    "saturation": 1.0,
    "sharpen": 0.0,
    "sharpen_radius": 1.0,
    "keep_aspect": True,
    "start_fullscreen": False,
}


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / "CaptureKVM"


class Store:
    """The config file, read once and written whole. Every mutation saves, because the file is
    small and a half-written pairing is worth less than a redundant flush."""

    def __init__(self, path: Path = None):
        self.path = Path(path) if path else config_dir() / "config.json"
        self._lock = threading.RLock()
        self._data = self._load()

    # --- file ---

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("machine_id", base64.b64encode(secrets.token_bytes(MACHINE_ID_BYTES)).decode("ascii"))
        raw.setdefault("name", _default_name())
        peers = raw.get("peers")
        raw["peers"] = [peer for peer in peers if isinstance(peer, dict)] if isinstance(peers, list) else []
        settings = raw.get("settings")
        merged = dict(DEFAULT_SETTINGS)
        if isinstance(settings, dict):
            merged.update({key: value for key, value in settings.items() if key in DEFAULT_SETTINGS})
        raw["settings"] = merged
        return raw

    def save(self) -> None:
        """Written to a temporary file in the same directory and renamed, so a crash mid-write
        cannot leave a truncated config where a working one was."""
        with self._lock:
            payload = json.dumps(self._data, indent=2, sort_keys=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as out:
                    out.write(payload)
                os.replace(temporary, self.path)
            except OSError:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise

    # --- identity ---

    @property
    def machine_id(self) -> bytes:
        ident = base64.b64decode(self._data["machine_id"], validate=True)
        if len(ident) != MACHINE_ID_BYTES or not any(ident):
            ident = secrets.token_bytes(MACHINE_ID_BYTES)
            self._data["machine_id"] = base64.b64encode(ident).decode("ascii")
            self.save()
        return ident

    @property
    def name(self) -> str:
        return self._data["name"][: protocol.MAX_NAME_CHARS] or _default_name()

    @name.setter
    def name(self, value: str) -> None:
        with self._lock:
            self._data["name"] = (value or _default_name())[: protocol.MAX_NAME_CHARS]
            self.save()

    # --- peers ---

    def peers(self) -> list:
        with self._lock:
            return [dict(peer) for peer in self._data["peers"]]

    def linkable_peers(self) -> list:
        """The entries this app can dial: a token that is a real pairing token, an address and a
        port. `protocol.linkable` is upstream's own test."""
        return [peer for peer in self.peers() if protocol.linkable(peer) and peer.get("host") and peer.get("port")]

    def peer_by_id(self, peer_id: str):
        return next((peer for peer in self.peers() if peer.get("id") == peer_id), None)

    def add_peer(self, entry: dict, replaces: dict = None) -> None:
        """Stores a freshly paired entry. An entry for the same machine id replaces the old one,
        which is what re-pairing a PC should do."""
        with self._lock:
            peers = [
                peer
                for peer in self._data["peers"]
                if peer.get("id") != entry.get("id") and not (replaces and peer.get("id") == replaces.get("id"))
            ]
            if len(peers) >= pairing.MAX_PEERS:
                raise ValueError(f"no room for another PC; {pairing.MAX_PEERS} is the limit")
            peers.append(dict(entry))
            self._data["peers"] = peers
            self.save()

    def forget_peer(self, peer_id: str) -> None:
        with self._lock:
            self._data["peers"] = [peer for peer in self._data["peers"] if peer.get("id") != peer_id]
            self.save()

    def set_peer_address(self, peer_id: str, address: str) -> None:
        with self._lock:
            for peer in self._data["peers"]:
                if peer.get("id") == peer_id:
                    peer["host"] = address
                    peer["host_pinned"] = True
            self.save()

    # --- settings ---

    def setting(self, key: str):
        return self._data["settings"].get(key, DEFAULT_SETTINGS.get(key))

    def set_setting(self, key: str, value) -> None:
        if key not in DEFAULT_SETTINGS:
            raise KeyError(key)
        with self._lock:
            self._data["settings"][key] = value
            self.save()

    def settings(self) -> dict:
        return dict(self._data["settings"])


def _default_name() -> str:
    try:
        return pairing.machine_name()[: protocol.MAX_NAME_CHARS]
    except Exception:
        return "CaptureKVM"
