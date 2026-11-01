"""The CaptureKVM client against the beamer-vigem fork's responder, over a real socket.

This is the check that matters once both halves exist: the client builds pad messages from its own
`padwire.py` and the fork validates them with its own `core/gamepad.py`, so a disagreement about a
field name, a range or the capability shows up here rather than as a controller that does nothing.

    python tools/interop_check.py [path to beamer-vigem]

The responder runs in a subprocess (the fork and the client's vendored copy are both called `core`)
with the fork's own test fake standing in for ViGEmBus, so no driver is needed.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party" / "beamer"))
sys.path.insert(0, str(ROOT / "windows"))

from core import protocol  # noqa: E402

from capturekvm import padwire  # noqa: E402
from capturekvm.link import BeamerLink  # noqa: E402

DEFAULT_FORK = ROOT.parent / "beamer-vigem"


def check(name, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {name}" + (f" -- {detail}" if detail and not condition else ""))
    return bool(condition)


def main(argv) -> int:
    fork = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_FORK
    if not (fork / "core" / "gamepad.py").is_file():
        print(f"the fork was not found at {fork}; pass its path", file=sys.stderr)
        return 2

    print(f"interop check: client <-> {fork.name}")
    responder = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "interop_responder.py"), str(fork)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        line = responder.stdout.readline()
        if not line:
            print("  FAIL the responder did not start", file=sys.stderr)
            return 1
        setup = json.loads(line)

        entry = {
            "id": setup["responder_id"], "name": "Fork PC", "platform": "windows",
            "token": setup["token"], "host": "127.0.0.1", "port": setup["port"],
            "from_1_4": False, "send": True, "allow_drive": True,
        }
        machine_id = protocol.read_id(setup["client_id"])

        events = []
        client = BeamerLink(entry, machine_id, "Client", "windows",
                            on_event=lambda kind, detail: events.append((kind, detail)))
        client.start()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not client.up:
            time.sleep(0.05)

        passed = [
            check("the link came up against the fork", client.up,
                  f"state={client.state} error={client.last_error}"),
            check("the fork offered the gamepad capability",
                  padwire.CAP_GAMEPAD in client.peer_caps, str(client.peer_caps)),
            check("the client reports the pad as ready", client.gamepad_ready),
        ]

        if client.up:
            client.send_unsequenced(padwire.attach(0, padwire.KIND_X360))
            time.sleep(0.3)
            state = padwire.PadState(
                buttons=padwire.BTN_A | padwire.BTN_DPAD_LEFT | padwire.BTN_GUIDE,
                lt=17, rt=255, lx=-32768, ly=32767, rx=-1, ry=1,
            )
            client.send_pad(padwire.report(state, 0))
            client.send_input(protocol.key_msg(protocol.MSG_KEYDOWN, "a", us="a"))
            client.send_input(protocol.mousemove_msg(3, -4))
            time.sleep(0.6)
            client.send_unsequenced(padwire.detach(0))
            time.sleep(0.4)

        client.stop()
        responder.stdin.write("dump\n")
        responder.stdin.flush()
        result = json.loads(responder.stdout.readline())
        injected = [tuple(call) for call in result["injected"]]

        passed.append(check("the fork plugged a virtual x360 pad in",
                            ("pad_attach", 0, "x360") in injected, str(injected)))
        reports = [call for call in injected if call[0] == "pad"]
        passed.append(check("a pad report reached the fork's injector", bool(reports), str(injected)))
        if reports:
            # ("pad", pad, buttons, lt, rt, lx, ly, rx, ry)
            passed.append(check(
                "every field survived the round trip",
                reports[0] == ("pad", 0, padwire.BTN_A | padwire.BTN_DPAD_LEFT | padwire.BTN_GUIDE,
                               17, 255, -32768, 32767, -1, 1),
                str(reports[0])))
        else:
            passed.append(check("every field survived the round trip", False, "no report"))
        passed.append(check("the detach was honoured",
                            ("pad_detach", 0) in injected, str(injected)))
        passed.append(check("keyboard and mouse still arrived",
                            any(call[0] == "key" for call in injected)
                            and any(call[0] == "move" for call in injected), str(injected)))
        passed.append(check("no pad is left plugged in after the link ends",
                            result["pads_still_plugged"] == {}, str(result["pads_still_plugged"])))

        failures = passed.count(False)
        print(f"\n{len(passed) - failures}/{len(passed)} checks passed")
        return 1 if failures else 0
    finally:
        if responder.poll() is None:
            responder.terminate()
            try:
                responder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                responder.kill()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
