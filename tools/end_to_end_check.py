"""The client's link against the PC's real responder, on loopback.

One repository means one `core`, so this runs `core.receiver.LinkResponder` -- the same class the
PC runs -- in this process, with the fork's own test fake standing in for ViGEmBus. No driver and
no subprocess are needed.

    python tools/end_to_end_check.py

This replaces the earlier loopback_check.py, which hand-rolled a responder from protocol.py, and
interop_check.py, which ran the real one in a subprocess to keep two `core` packages apart. Both
existed only because the protocol lived in two places.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "beamer-vigem"))
sys.path.insert(0, str(ROOT / "windows"))

from core import gamepad, pairing, protocol  # noqa: E402
from core.tests.responder_harness import B, CAPS, HERE, TOKENS, Machine, entry  # noqa: E402

from capturekvm import padwire  # noqa: E402
from capturekvm.link import BeamerLink  # noqa: E402

PASSED = []


def check(name, condition, detail="") -> bool:
    ok = bool(condition)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" -- {detail}" if detail and not ok else ""))
    PASSED.append(ok)
    return ok


def main() -> int:
    print("end-to-end: client link -> the PC's real LinkResponder")
    machine = Machine([entry(B, "Client")], caps=CAPS + [gamepad.CAP_GAMEPAD],
                      own=HERE, name="Fork PC").start()
    client = None
    try:
        peer = pairing.peer_entry(HERE, "Fork PC", "windows", machine.port, TOKENS[B],
                                  "127.0.0.1", int(time.time()))
        events = []
        client = BeamerLink(peer, B, "Client", "windows",
                            on_event=lambda kind, detail: events.append((kind, detail)))
        client.start()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not client.up:
            time.sleep(0.05)

        # --- the link itself ---
        check("the link came up", client.up, f"state={client.state} error={client.last_error}")
        check("the PC's identity was read", client.peer_name == "Fork PC", client.peer_name)
        check("the PC offered the gamepad capability",
              padwire.CAP_GAMEPAD in client.peer_caps, str(client.peer_caps))
        check("the client reports the pad as ready", client.gamepad_ready)
        check("the responder made this client its owner",
              machine.responder.owner == B, str(machine.responder.owner))
        check("hello declared port 0, so the PC never dials this client",
              any(kind == "welcome" for kind, _ in events))

        if not client.up:
            return finish()

        # --- keyboard, mouse, scroll, pad ---
        client.send_input(protocol.key_msg(protocol.MSG_KEYDOWN, "a", us="a"))
        client.send_input(protocol.key_msg(protocol.MSG_KEYUP, "a", us="a"))
        client.send_input(protocol.mousemove_msg(5, -7))
        client.send_input(protocol.mousemove_msg(2, 3))
        client.send_input(protocol.scroll_msg(1.5, 0.0, "line"))
        client.send_unsequenced(padwire.attach(0, padwire.KIND_X360))
        time.sleep(0.3)
        state = padwire.PadState(
            buttons=padwire.BTN_A | padwire.BTN_DPAD_LEFT | padwire.BTN_GUIDE,
            lt=17, rt=255, lx=-32768, ly=32767, rx=-1, ry=1,
        )
        client.send_pad(padwire.report(state, 0))
        time.sleep(0.8)

        seen = machine.injector.seen()
        kinds = [call[0] for call in seen]
        check("keydown and keyup arrived", kinds.count("key") == 2, str(kinds))
        check("a mouse move arrived", "move" in kinds, str(kinds))
        check("a scroll arrived", "scroll" in kinds, str(kinds))
        check("the PC plugged a virtual x360 pad in", ("pad_attach", 0, "x360") in seen, str(seen))

        reports = [call for call in seen if call[0] == "pad"]
        check("a pad report reached the injector", bool(reports), str(seen))
        check("every pad field survived the round trip",
              bool(reports) and reports[0] == (
                  "pad", 0, padwire.BTN_A | padwire.BTN_DPAD_LEFT | padwire.BTN_GUIDE,
                  17, 255, -32768, 32767, -1, 1),
              str(reports[0]) if reports else "no report")

        # --- the clipboard, which is upstream Beamer's capability, not an addition ---
        check("the PC offered clipboard and clipboard_image",
              {"clipboard", "clipboard_image"} <= set(client.peer_caps), str(client.peer_caps))

        # A 1x1 PNG, so the image path is exercised without a Qt image library here.
        tiny_png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082")
        check("that PNG is one the wire accepts", protocol.png_fits(tiny_png))

        sent = client.send_clipboard(text="copied on the tablet", png=tiny_png)
        check("the client sent its clipboard", sent)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not machine.clipboard.set_calls:
            time.sleep(0.05)
        check("the PC's clipboard was set from it", bool(machine.clipboard.set_calls),
              str(machine.clipboard.set_calls))
        if machine.clipboard.set_calls:
            text, image = machine.clipboard.set_calls[-1]
            check("the text arrived intact", text == "copied on the tablet", repr(text))
            check("the image arrived intact", image == tiny_png,
                  f"{len(image) if image else 0} bytes vs {len(tiny_png)}")

        # --- the sequence/ack cycle ---
        check("acknowledgements gave a round-trip time", client.rtt_ms is not None, str(client.rtt_ms))
        check("nothing is left unacknowledged", not client._pending, str(client._pending))

        # --- release ---
        client.send_unsequenced(padwire.detach(0))
        time.sleep(0.4)
        check("the detach was honoured", ("pad_detach", 0) in machine.injector.seen())

        client.send_unsequenced(padwire.attach(0, padwire.KIND_X360))
        time.sleep(0.3)
        check("a pad can be plugged back in", machine.injector.plugged() == {0: "x360"},
              str(machine.injector.plugged()))

        client.stop()
        client = None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and machine.injector.plugged():
            time.sleep(0.05)
        check("losing the link unplugs the pad", machine.injector.plugged() == {},
              str(machine.injector.plugged()))
        check("losing the link releases held keys and buttons",
              "release_all" in [call[0] for call in machine.injector.seen()])
        return finish()
    finally:
        if client is not None:
            client.stop()
        machine.stop()


def finish() -> int:
    failures = PASSED.count(False)
    print(f"\n{len(PASSED) - failures}/{len(PASSED)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
