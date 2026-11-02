"""Finding PCs that are waiting to pair, so nobody has to type an IP address.

Beamer broadcasts a beacon on UDP 24821 every two seconds **while it is showing a pairing code**,
and stops when the code expires. That timing is the useful part: the list shows the machines that
are ready to pair right now, rather than every machine that has ever run Beamer.

Only the beacon is read here. The exchange itself still goes over TCP through `pair.py`, which
already works -- discovery supplies the address and nothing else, so a PC that broadcasts nothing
can still be paired with by typing it.

A beacon is unauthenticated: anything on the network can send one. The name shown is a claim, the
address is where the datagram came from, and neither proves anything. What makes the pairing safe
is the six-digit code, not the discovery.
"""

import logging
import socket
import threading
import time

from core import pairing

LOGGER = logging.getLogger("capturekvm.discover")

PORT = pairing.PAIRING_PORT
STALE_SECONDS = pairing.BEACON_STALE_SECONDS
POLL_SECONDS = 0.5


class Discovery:
    """Listens for beacons and keeps a list of what is broadcasting.

    `on_change(list)` fires when the list changes, from the listener thread.
    """

    def __init__(self, on_change=None, port: int = PORT):
        self._on_change = on_change or (lambda machines: None)
        self._port = port
        self._socket = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._seen = {}            # address: entry
        self.error = None

    # --- lifecycle ---

    def start(self) -> bool:
        if self._thread is not None:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", self._port))
            sock.settimeout(POLL_SECONDS)
        except OSError as exc:
            # Beamer itself binds this port. On a machine running both, discovery simply does
            # not run and the address can still be typed.
            self.error = f"could not listen on UDP {self._port}: {exc}"
            LOGGER.info(self.error)
            return False
        self._socket = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="discovery", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(2.0)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def machines(self) -> list:
        """What is broadcasting now, freshest first, with stale entries dropped."""
        now = time.monotonic()
        with self._lock:
            live = [entry for entry in self._seen.values() if now - entry["seen_at"] <= STALE_SECONDS]
        return sorted(live, key=lambda entry: -entry["seen_at"])

    # --- the listener ---

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, sender = self._socket.recvfrom(pairing.MAX_DATAGRAM_BYTES)
            except socket.timeout:
                if self._expire():
                    self._announce()
                continue
            except OSError:
                return
            if self._take(data, sender[0]):
                self._announce()

    def _take(self, data: bytes, address: str) -> bool:
        # `decode` is upstream's: it bounds the length and refuses anything that is not a Beamer
        # datagram, so whatever else is on the port costs one failed parse.
        message = pairing.decode(data)
        if not isinstance(message, dict) or message.get("type") != pairing.MSG_BEACON:
            return False
        name = message.get("name")
        port = message.get("port")
        if not isinstance(name, str) or not name or len(name) > pairing.MAX_NAME_CHARS:
            return False
        if type(port) is not int or not 0 < port <= 65535:
            return False

        entry = {
            "address": address,
            "name": name,
            "port": port,
            "pairing": message.get("pairing"),
            # Present only while a code is up, which is exactly when pairing can succeed.
            "showing_code": isinstance(message.get("pair"), str) and bool(message.get("pair")),
            "seen_at": time.monotonic(),
        }
        with self._lock:
            before = self._seen.get(address)
            self._seen[address] = entry
        return (before is None or before["name"] != name
                or before.get("showing_code") != entry["showing_code"])

    def _expire(self) -> bool:
        now = time.monotonic()
        with self._lock:
            gone = [key for key, entry in self._seen.items() if now - entry["seen_at"] > STALE_SECONDS]
            for key in gone:
                del self._seen[key]
        return bool(gone)

    def _announce(self) -> None:
        try:
            self._on_change(self.machines())
        except Exception:                                   # noqa: BLE001 - a listener must not kill this
            LOGGER.exception("discovery listener")


def describe(entry: dict) -> str:
    """One line for the list."""
    ready = "" if entry.get("showing_code") else "  (not showing a code)"
    return f"{entry['name']}  —  {entry['address']}{ready}"
