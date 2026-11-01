"""A responder from the beamer-vigem fork, for tools/interop_check.py to talk to.

Run as a subprocess with the fork's path, because the fork and the client's vendored copy are both
called `core` and cannot share one interpreter. It prints one line of JSON saying where to connect
and with what, then waits; a line on stdin makes it print what the fake injector saw and exit.

    python tools/interop_responder.py <path to beamer-vigem>

The injector is the fork's own test fake, so no ViGEmBus driver is needed: this proves the two
halves agree on the wire, not that the driver works.
"""

import json
import sys
from pathlib import Path


def main(argv) -> int:
    if len(argv) < 2:
        print("usage: interop_responder.py <path to beamer-vigem>", file=sys.stderr)
        return 2
    fork = Path(argv[1]).resolve()
    if not (fork / "core" / "gamepad.py").is_file():
        print(f"{fork} does not look like the beamer-vigem fork", file=sys.stderr)
        return 2
    sys.path.insert(0, str(fork))

    from core import gamepad, protocol                       # noqa: PLC0415 - after the path
    from core.tests.responder_harness import B, CAPS, HERE, TOKENS, Machine, entry

    machine = Machine([entry(B, "Peer")], caps=CAPS + [gamepad.CAP_GAMEPAD],
                      own=HERE, name="Fork PC").start()
    print(json.dumps({
        "port": machine.port,
        "responder_id": protocol.id_text(HERE),
        "client_id": protocol.id_text(B),
        "token": TOKENS[B],
        "caps": CAPS + [gamepad.CAP_GAMEPAD],
    }), flush=True)

    sys.stdin.readline()
    print(json.dumps({
        "injected": [list(call) for call in machine.injector.seen()],
        "pads_still_plugged": machine.injector.plugged(),
        "owners": [protocol.id_text(peer) if isinstance(peer, bytes) else peer for peer in machine.owners],
    }), flush=True)
    machine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
