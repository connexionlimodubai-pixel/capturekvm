"""The initiator half of one Beamer link: dial a paired PC, take its input, send what the user
does here, and keep the link honest about liveness.

Only the initiator direction exists here. This app never accepts a link and never injects
anything locally, so it declares its link port as 0, which is how Beamer marks a machine that is
never dialled -- `core.pairing.peer_entry` calls it a phone.

Every frame on the wire is built by `core.protocol`; the gamepad frames, which upstream has no
type for, are built by `padwire`. Threads: the worker dials, handshakes and then sends; a reader
thread owns `recv`, because `LinkSession.open` expects one reader per link while `send_msg`
serialises itself.
"""

import logging
import queue
import socket
import threading
import time

from core import protocol

from . import WIRE_APP, clipboard, padwire

LOGGER = logging.getLogger("capturekvm.link")

# WIRE.md section 9. An initiator pings after a second of quiet, gives up on silence at two, and
# gives up on an input it has waited 1.5 seconds to have acknowledged.
PING_AFTER = 1.0
SILENCE_LIMIT = 2.0
UNACKED_LIMIT = 1.5
REDIAL_SECONDS = 2.0
CONNECT_TIMEOUT = 1.0
HANDSHAKE_SECONDS = 5.0
READ_TIMEOUT = 0.25

# What this app can be sent. `ping_rtt` lets the PC answer probes; `clipboard` and
# `clipboard_image` ask it to send its clipboard back when input goes home, which it will not do
# otherwise; CAP_GAMEPAD tells a ViGEmBus peer that pad frames are welcome.
#
# A capability here is a promise to handle what arrives, so nothing goes on this list that the app
# would then drop: `clipboard_image` is here because `clipboard.write_local` decodes PNGs.
CAPS = ("ping_rtt", "clipboard", "clipboard_image", padwire.CAP_GAMEPAD, padwire.CAP_RUMBLE)

STATE_DOWN = "down"
STATE_DIALLING = "dialling"
STATE_HANDSHAKE = "handshake"
STATE_TAKING = "taking"
STATE_UP = "up"
STATE_REFUSED = "refused"
STATE_STOPPED = "stopped"


class LinkClosed(Exception):
    """Raised inside the worker to bring one connection down; the worker then redials."""


class BeamerLink:
    """One link, redialled until `stop()`. `on_event(kind, detail)` is called from the worker and
    reader threads, so a UI must marshal it onto its own thread."""

    def __init__(self, peer: dict, machine_id: bytes, name: str, platform: str = "windows",
                 resistance_px: int = 0, on_event=None):
        self.peer = dict(peer)
        self.peer_id = protocol.read_id(peer.get("id"))
        if self.peer_id is None:
            raise ValueError("the peer entry has no usable machine id")
        self.token = peer["token"]
        self.address = peer.get("host") or ""
        self.port = int(peer.get("port") or protocol.DEFAULT_PORT)
        self.machine_id = bytes(machine_id)
        self.name = name
        self.platform = platform
        self.resistance_px = int(resistance_px)
        self._on_event = on_event or (lambda kind, detail: None)

        self._stop = threading.Event()
        self._worker = None
        self._reader = None

        self._sock = None
        self._session = None
        self._outbox = []
        self._outbox_cv = threading.Condition()
        self._seq = 0
        self._pending = []           # (seq, sent_at) awaiting an ack
        self._pending_lock = threading.Lock()
        self._last_recv = 0.0
        self._last_send = 0.0
        self._accepted = threading.Event()
        self._route = 0

        self.state = STATE_DOWN
        self.peer_name = peer.get("name") or ""
        self.peer_platform = peer.get("platform") or "windows"
        self.peer_caps = ()
        self.peer_accepts = False
        self.rtt_ms = None
        self.last_error = None
        self.pad_coalesced = 0      # stale pad reports replaced before they went out

    # --- lifecycle ---

    def start(self) -> None:
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="beamer-link", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        with self._outbox_cv:
            self._outbox_cv.notify_all()
        self._shutdown_socket()
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
        self._worker = None
        self._set_state(STATE_STOPPED)

    @property
    def up(self) -> bool:
        return self.state == STATE_UP

    @property
    def gamepad_ready(self) -> bool:
        return self.up and padwire.CAP_GAMEPAD in self.peer_caps

    # --- sending ---

    def send_input(self, msg: dict) -> None:
        """Queues an input message. The sequence number is put on at the moment it leaves, so two
        mouse moves that merge in the queue cost one number and one acknowledgement."""
        if self.state not in (STATE_UP, STATE_TAKING):
            return
        with self._outbox_cv:
            if self._outbox and msg.get("type") == protocol.MSG_MOUSEMOVE:
                merged = protocol.coalesce_mousemove(self._outbox[-1], msg)
                if merged is not None:
                    self._outbox[-1] = merged
                    self._outbox_cv.notify()
                    return
            self._outbox.append(msg)
            self._outbox_cv.notify()

    def send_pad(self, msg: dict) -> None:
        """A pad frame. Numbered only when the peer advertised CAP_GAMEPAD: a stock Beamer would
        drop an unknown type silently and never acknowledge the number, which would then look
        like a dead link."""
        if not self.up:
            return
        if padwire.CAP_GAMEPAD not in self.peer_caps:
            return
        with self._outbox_cv:
            # Replace any queued report for the same pad instead of queueing behind it. A report
            # is full state, not a delta, so the newest one says everything the older ones did --
            # and sending them in turn would replay stick positions the player has already left,
            # which is lag that persists rather than a frame that is simply late. Mouse moves are
            # coalesced for the same reason; this is the pad's version.
            pad = (msg.get("data") or {}).get("pad")
            for index, queued in enumerate(self._outbox):
                if (queued.get("type") == padwire.MSG_GAMEPAD
                        and (queued.get("data") or {}).get("pad") == pad
                        and "seq" not in (queued.get("data") or {})):
                    self._outbox[index] = msg
                    self._outbox_cv.notify()
                    self.pad_coalesced += 1
                    return
            self._outbox.append(msg)
            self._outbox_cv.notify()

    def send_unsequenced(self, msg: dict) -> None:
        with self._outbox_cv:
            self._outbox.append(dict(msg))
            self._outbox_cv.notify()

    def send_clipboard(self, text=None, png=None) -> bool:
        """This machine's clipboard, carrying only what the PC said it can take.

        Jumped to the front of the queue: WIRE.md section 3 has the owner send its clipboard after
        the taking `focus` and **before any input**, so a Control-V on the PC finds what was
        copied here rather than whatever was there before."""
        if not self.up:
            return False
        msg = clipboard.message_for(text=text, png=png, peer_caps=self.peer_caps)
        if msg is None:
            return False
        with self._outbox_cv:
            self._outbox.insert(0, msg)
            self._outbox_cv.notify()
        return True

    # --- the worker ---

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect_once()
            except LinkClosed as exc:
                self._fail(str(exc))
            except (OSError, socket.timeout) as exc:
                self._fail(f"no answer from {self.address}:{self.port} ({exc})")
            except protocol.VersionMismatch as exc:
                self._fail(f"the PC speaks link version {exc.peer}, this app speaks {protocol.LINK_VERSION}")
            except protocol.AuthenticationError:
                self._fail("the PC refused this pairing; pair again")
            except protocol.ProtocolError as exc:
                self._fail(f"protocol error: {exc}")
            except Exception as exc:                       # noqa: BLE001 - the worker must not die
                LOGGER.exception("link worker")
                self._fail(f"unexpected error: {exc}")
            finally:
                self._teardown()
            if self._stop.wait(REDIAL_SECONDS):
                break
        self._set_state(STATE_STOPPED)

    def _connect_once(self) -> None:
        self._set_state(STATE_DIALLING)
        sock = socket.create_connection((self.address, self.port), timeout=CONNECT_TIMEOUT)
        self._sock = sock
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._handshake(sock)

        # The reader starts before the focus goes out: the accept that answers it arrives on this
        # socket, and with no reader running nothing would take it and the take would time out.
        now = time.monotonic()
        self._last_recv = self._last_send = now
        self._reader = threading.Thread(target=self._read_loop, name="beamer-reader", daemon=True)
        self._reader.start()

        try:
            self._take_input(sock)
            self._send_loop()
        finally:
            self._accepted.clear()

    def _handshake(self, sock) -> None:
        self._set_state(STATE_HANDSHAKE)
        deadline = time.monotonic() + HANDSHAKE_SECONDS
        session = protocol.LinkSession(self.token, protocol.ROLE_INITIATOR)
        sock.sendall(session.preamble())
        session.accept_preamble(protocol.recv_link_preamble(sock, deadline))
        self._session = session

        hello = protocol.hello_v6(self.machine_id, self.name, self.platform, WIRE_APP, CAPS, port=0)
        protocol.send_msg(sock, session, hello)
        welcome = protocol.read_welcome(protocol.recv_first_msg(sock, session, deadline))
        if welcome.get("error"):
            raise LinkClosed(f"the PC refused the hello: {welcome['error']}")
        if welcome["id"] != self.peer_id:
            raise LinkClosed("another machine answered at this address; pair again or fix the address")

        self.peer_name = welcome["name"]
        self.peer_platform = welcome["platform"]
        self.peer_caps = tuple(welcome.get("caps") or ())
        self.peer_accepts = bool(welcome.get("accepts"))
        self._emit("welcome", {
            "name": self.peer_name, "platform": self.peer_platform, "app": welcome.get("app"),
            "caps": self.peer_caps, "accepts": self.peer_accepts,
        })
        if not self.peer_accepts:
            raise LinkClosed(f"{self.peer_name} is not letting this machine drive it "
                             "(turn its 'allow this machine to drive' on in Beamer)")

    def _take_input(self, sock) -> None:
        """One `focus` that takes the PC and stays taken. `reach` is empty because this app is not
        part of a chain: input comes here and goes to the PC, nowhere else."""
        self._set_state(STATE_TAKING)
        self._route += 1
        self._accepted.clear()
        focus = protocol.focus_v6(self._route, self.peer_id, reach=[], resistance_px=self.resistance_px)
        protocol.send_msg(sock, self._session, focus)
        if not self._accepted.wait(2.0):
            # The reader sets the event; a refusal raises there instead, so a timeout is silence.
            raise LinkClosed("the PC did not answer the request to take its keyboard and mouse")
        self._set_state(STATE_UP)

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            msg = self._next_message()
            now = time.monotonic()
            if msg is None:
                self._check_liveness(now)
                if now - self._last_send >= PING_AFTER:
                    self._write(protocol.ping_msg(probe=int(now * 1000) & 0x7FFFFFFF))
                continue
            kind = msg.get("type")
            if kind in protocol.INPUT_TYPES or (kind == padwire.MSG_GAMEPAD and padwire.CAP_GAMEPAD in self.peer_caps):
                self._seq += 1
                msg["data"]["seq"] = self._seq
                with self._pending_lock:
                    self._pending.append((self._seq, now))
            self._write(msg)
            self._check_liveness(now)

    def _next_message(self):
        with self._outbox_cv:
            if not self._outbox:
                self._outbox_cv.wait(0.05)
            return self._outbox.pop(0) if self._outbox else None

    def _write(self, msg: dict) -> None:
        sock, session = self._sock, self._session
        if sock is None or session is None:
            raise LinkClosed("the link closed while sending")
        try:
            protocol.send_msg(sock, session, msg)
        except (OSError, protocol.ProtocolError) as exc:
            raise LinkClosed(f"the link closed while sending: {exc}") from exc
        self._last_send = time.monotonic()

    def _check_liveness(self, now: float) -> None:
        if now - self._last_recv > SILENCE_LIMIT:
            raise LinkClosed(f"{self.peer_name or 'the PC'} went quiet")
        with self._pending_lock:
            oldest = self._pending[0][1] if self._pending else None
        if oldest is not None and now - oldest > UNACKED_LIMIT:
            raise LinkClosed("the PC stopped acknowledging input")

    # --- the reader ---

    def _read_loop(self) -> None:
        sock, session = self._sock, self._session
        try:
            sock.settimeout(READ_TIMEOUT)
            while not self._stop.is_set():
                try:
                    message = protocol.recv_msg(sock, session)
                except socket.timeout:
                    continue
                self._last_recv = time.monotonic()
                self._handle(message)
        except (OSError, protocol.ProtocolError, protocol.ConnectionClosed) as exc:
            self._fail(f"the link dropped: {exc}")
        finally:
            self._shutdown_socket()

    def _handle(self, message) -> None:
        kind = message.get("type") if isinstance(message, dict) else None
        if kind == protocol.MSG_ACK:
            ack = protocol.read_ack(message)
            with self._pending_lock:
                sent_at = next((at for seq, at in self._pending if seq == ack["seq"]), None)
                self._pending = [(seq, at) for seq, at in self._pending if seq > ack["seq"]]
            if sent_at is not None:
                # held_us is the responder's own hold, subtracted so what is left is the network.
                self.rtt_ms = max(0.0, (time.monotonic() - sent_at) * 1000 - ack["held_us"] / 1000)
            return
        if kind == protocol.MSG_ACCEPT:
            accept = protocol.read_accept(message)
            if accept and accept["route"] == self._route:
                self._accepted.set()
            return
        if kind == protocol.MSG_REFUSE:
            refuse = protocol.read_refuse(message) or {}
            why = refuse.get("why") or "no reason given"
            self._set_state(STATE_REFUSED)
            self._emit("refused", {"why": why})
            self._fail(f"the PC refused: {_refusal_text(why)}")
            self._shutdown_socket()
            return
        if kind == protocol.MSG_ACCEPTS:
            accepts = protocol.read_accepts(message)
            if accepts is not None:
                self.peer_accepts = accepts["accepts"]
                self._emit("accepts", accepts)
                if not self.peer_accepts:
                    self._shutdown_socket()
            return
        if kind == protocol.MSG_PING:
            probe = protocol.read_ping(message)
            if probe and probe.get("probe"):
                self.send_unsequenced(protocol.ping_msg(echo=probe["probe"]))
            return
        if kind == padwire.MSG_GAMEPAD_RUMBLE:
            read = padwire.read_rumble(message)
            if read is not None:
                self._emit("rumble", read)
            return
        if kind == protocol.MSG_CLIPBOARD:
            read = protocol.read_clipboard(message)
            if read is not None and (read["text"] or read["image"]):
                # Handed to the listener rather than set here: Qt's clipboard belongs to the GUI
                # thread and this is the reader's.
                self._emit("clipboard", {"text": read["text"], "image": read["image"]})
            return
        if kind == protocol.MSG_SWITCH:
            # The PC wants to hand input on to a third machine. This app is not a chain, so the
            # take simply ends and the user picks the PC again.
            self._emit("switch", {})
            return
        if kind == protocol.MSG_WELCOME:
            return
        self._emit("message", {"type": kind})

    # --- plumbing ---

    def _teardown(self) -> None:
        self._shutdown_socket()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(1.0)
        self._reader = None
        self._session = None
        self._sock = None
        with self._outbox_cv:
            self._outbox.clear()
        with self._pending_lock:
            self._pending.clear()
        self._seq = 0
        if self.state not in (STATE_STOPPED, STATE_REFUSED):
            self._set_state(STATE_DOWN)

    def _shutdown_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _set_state(self, state: str) -> None:
        if self.state != state:
            self.state = state
            self._emit("state", {"state": state})

    def _fail(self, text: str) -> None:
        if self.last_error != text:
            self.last_error = text
            LOGGER.info("link: %s", text)
            self._emit("error", {"text": text})

    def _emit(self, kind: str, detail: dict) -> None:
        try:
            self._on_event(kind, detail)
        except Exception:                                  # noqa: BLE001 - a listener must not kill the link
            LOGGER.exception("link listener")


def _refusal_text(why: str) -> str:
    return {
        "owned": "another machine is driving it",
        "not_allowed": "it is not set to let this machine drive it",
        "busy": "it is busy",
        "sent_home": "it sent input back to itself",
        "malformed": "it did not understand the request",
    }.get(why, why)
