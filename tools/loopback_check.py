"""Run the Windows client's link against a responder built from the vendored Beamer core.

This exercises the real thing end to end on one machine: the preamble exchange, the key
derivation, hello/welcome, the focus that takes the PC, the seq/ack cycle, a ping, and the pad
frames -- with the responder's half written from upstream's own builders and validators, so a
mistake in `link.py` shows up as a failure here rather than as a dead link on a tablet.

    python tools/loopback_check.py

It is not a test of the PC's Beamer: a real responder has more rules than this one. It is a test
of this client's half.
"""

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party" / "beamer"))
sys.path.insert(0, str(ROOT / "windows"))

from core import pairing, protocol  # noqa: E402

from capturekvm import padwire  # noqa: E402
from capturekvm.link import BeamerLink  # noqa: E402

TOKEN = protocol.id_text(bytes(range(32)))
RESPONDER_ID = bytes(range(16, 32))
CLIENT_ID = bytes(range(16))


class Responder(threading.Thread):
    """Just enough of upstream's receiver to answer one link honestly."""

    def __init__(self, accepts=True, offer_gamepad=True):
        super().__init__(daemon=True, name="test-responder")
        self.accepts = accepts
        self.offer_gamepad = offer_gamepad
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.received = []
        self.hello = None
        self.focus = None
        self.stop_flag = threading.Event()
        self.error = None

    def run(self):
        try:
            self._serve()
        except Exception as exc:                            # noqa: BLE001 - reported to the caller
            self.error = exc

    def _serve(self):
        self.listener.settimeout(5.0)
        conn, _ = self.listener.accept()
        with conn:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            deadline = time.monotonic() + 5.0
            session = protocol.LinkSession(TOKEN, protocol.ROLE_RESPONDER)
            session.accept_preamble(protocol.recv_link_preamble(conn, deadline))
            conn.sendall(session.preamble())

            self.hello = protocol.read_hello(protocol.recv_first_msg(conn, session, deadline))
            caps = ["clipboard", "text", "ping_rtt"] + ([padwire.CAP_GAMEPAD] if self.offer_gamepad else [])
            protocol.send_msg(conn, session, protocol.welcome_v6(
                RESPONDER_ID, "Test PC", "windows", "beamer", caps, accepts=self.accepts))

            conn.settimeout(0.25)
            last_ack = time.monotonic()
            while not self.stop_flag.is_set():
                try:
                    message = protocol.recv_msg(conn, session)
                except socket.timeout:
                    message = None
                except (OSError, protocol.ProtocolError, protocol.ConnectionClosed):
                    # The client closing at the end of the run is how this loop is meant to end.
                    return

                if message is not None:
                    self.received.append(message)
                    kind = message.get("type")
                    if kind == protocol.MSG_FOCUS:
                        read = protocol.read_focus(message, RESPONDER_ID)
                        self.focus = read
                        if read and read["here"] and not read["malformed"]:
                            protocol.send_msg(conn, session, protocol.accept_msg(read["route"]))
                        elif read:
                            protocol.send_msg(conn, session, protocol.refuse_msg(read["route"], "malformed"))
                    elif kind in protocol.INPUT_TYPES or kind == padwire.MSG_GAMEPAD:
                        seq = (message.get("data") or {}).get("seq")
                        if isinstance(seq, int):
                            protocol.send_msg(conn, session, protocol.ack_v6(seq, 50))
                            last_ack = time.monotonic()
                    elif kind == protocol.MSG_PING:
                        probe = protocol.read_ping(message)
                        if probe and probe.get("probe"):
                            protocol.send_msg(conn, session, protocol.ping_msg(echo=probe["probe"]))

                now = time.monotonic()
                if now - last_ack >= 0.4:
                    # The heartbeat an initiator needs to believe the link is alive.
                    protocol.send_msg(conn, session, protocol.ack_v6(0, 0))
                    last_ack = now


def check(name, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print(f"  {mark} {name}{(' -- ' + detail) if detail and not condition else ''}")
    return bool(condition)


def main() -> int:
    print("loopback link check")
    responder = Responder()
    responder.start()

    entry = pairing.peer_entry(RESPONDER_ID, "Test PC", "windows", responder.port, TOKEN,
                               "127.0.0.1", int(time.time()))
    events = []
    client = BeamerLink(entry, CLIENT_ID, "Test client", "windows",
                        on_event=lambda kind, detail: events.append((kind, detail)))
    client.start()

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not client.up:
        time.sleep(0.05)

    passed = []
    passed.append(check("the link came up", client.up, f"state={client.state} error={client.last_error}"))
    passed.append(check("the PC's identity was read", client.peer_name == "Test PC", client.peer_name))
    passed.append(check("the gamepad capability was seen", client.gamepad_ready, str(client.peer_caps)))
    passed.append(check("hello declared port 0 (never dialled)",
                        responder.hello is not None and responder.hello.get("port") == 0,
                        str(responder.hello)))
    passed.append(check("hello carried this client's id",
                        responder.hello is not None and responder.hello.get("id") == CLIENT_ID))
    passed.append(check("the focus took this machine",
                        responder.focus is not None and responder.focus["here"] and not responder.focus["malformed"],
                        str(responder.focus)))

    if client.up:
        client.send_input(protocol.key_msg(protocol.MSG_KEYDOWN, "a", us="a"))
        client.send_input(protocol.key_msg(protocol.MSG_KEYUP, "a", us="a"))
        client.send_input(protocol.mousemove_msg(5, -7))
        client.send_input(protocol.mousemove_msg(2, 3))
        client.send_input(protocol.scroll_msg(1.5, 0.0, "line"))
        client.send_pad(padwire.report(padwire.PadState(buttons=padwire.BTN_A, rt=255, lx=-32768), 0))
        time.sleep(1.2)

    kinds = [message.get("type") for message in responder.received]
    passed.append(check("keydown and keyup arrived",
                        kinds.count(protocol.MSG_KEYDOWN) == 1 and kinds.count(protocol.MSG_KEYUP) == 1, str(kinds)))
    passed.append(check("a mouse move arrived", protocol.MSG_MOUSEMOVE in kinds, str(kinds)))
    passed.append(check("a scroll arrived", protocol.MSG_SCROLL in kinds, str(kinds)))
    passed.append(check("a pad report arrived", padwire.MSG_GAMEPAD in kinds, str(kinds)))

    pad_frames = [m for m in responder.received if m.get("type") == padwire.MSG_GAMEPAD]
    if pad_frames:
        fields = padwire.read_report(pad_frames[0])
        passed.append(check("the pad report validates and round-trips",
                            fields is not None and fields["buttons"] == padwire.BTN_A
                            and fields["rt"] == 255 and fields["lx"] == -32768, str(fields)))
    else:
        passed.append(check("the pad report validates and round-trips", False, "no pad frame"))

    seqs = [(m.get("data") or {}).get("seq") for m in responder.received
            if m.get("type") in protocol.INPUT_TYPES or m.get("type") == padwire.MSG_GAMEPAD]
    seqs = [value for value in seqs if isinstance(value, int)]
    passed.append(check("sequence numbers start at 1 and only rise",
                        seqs and seqs[0] == 1 and seqs == sorted(seqs) and len(set(seqs)) == len(seqs), str(seqs)))
    passed.append(check("acknowledgements gave a round-trip time", client.rtt_ms is not None, str(client.rtt_ms)))
    passed.append(check("nothing is left unacknowledged", not client._pending, str(client._pending)))

    client.stop()
    responder.stop_flag.set()
    responder.join(2.0)
    if responder.error is not None:
        print(f"  FAIL responder raised: {responder.error!r}")
        passed.append(False)

    failures = passed.count(False)
    print(f"\n{len(passed) - failures}/{len(passed)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
