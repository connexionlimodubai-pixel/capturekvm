"""Is ViGEmBus actually working on this PC?

Run this **on the PC you are driving**, when the controller connects but does nothing:

    python tools\\vigem_check.py

It plugs a virtual Xbox 360 pad in, holds it there, moves a stick, and unplugs it -- without
Beamer, without a link and without a client. That separates the two possibilities:

* It fails here, so the driver or the library is the problem and nothing in Beamer can fix it.
* It works here, so the plumbing is fine and the fault is in how Beamer is using it, which the
  Beamer log will say more about (`%LOCALAPPDATA%\\Beamer\\Beamer.log`).

While it is holding the pad, open **joy.cpl** -- "Controller (XBOX 360 For Windows)" should be
listed, and the test page should show the left stick moving.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "win_app"))


def main() -> int:
    print("ViGEmBus check\n")

    try:
        import vigem
    except Exception as exc:                                # noqa: BLE001
        print(f"  FAIL  could not import win_app/vigem.py: {exc}")
        return 2

    print("1. Finding ViGEmClient.dll")
    if not vigem.available():
        print(f"  FAIL  {vigem.unavailable_reason()}")
        print("\n  Install the ViGEmBus driver from")
        print("    https://github.com/nefarius/ViGEmBus/releases")
        print("  and put ViGEmClient.dll beside win_app, or `pip install vgamepad`, or set")
        print("    set BEAMER_VIGEM_DLL=C:\\path\\to\\ViGEmClient.dll")
        return 1
    print(f"  ok    loaded {vigem.dll_path}")
    print("  ok    connected to the driver")

    print("\n2. Plugging a virtual pad in")
    if not vigem.attach(0, "x360"):
        print("  FAIL  vigem_target_add refused")
        print("        Usually a version mismatch: the installed ViGEmBus driver and this")
        print("        ViGEmClient.dll disagree. Reinstall the driver, or take the DLL from a")
        print("        vgamepad matching it.")
        return 1
    print("  ok    plugged in as pad 0")

    print("\n3. Sending reports for 10 seconds -- open joy.cpl now")
    print("   (the left stick should swing left and right; the A button should blink)")
    started = time.monotonic()
    sent = failed = 0
    try:
        while time.monotonic() - started < 10.0:
            phase = time.monotonic() - started
            swing = int(30000 * __import__("math").sin(phase * 2))
            buttons = 0x1000 if int(phase * 2) % 2 == 0 else 0      # A, blinking
            if vigem.report(0, buttons, 0, 0, swing, 0, 0, 0):
                sent += 1
            else:
                failed += 1
            time.sleep(1 / 60)
    except KeyboardInterrupt:
        print("\n  stopped early")
    print(f"  {'ok  ' if failed == 0 else 'FAIL'}  {sent} reports accepted, {failed} refused")

    print("\n4. Rumble")
    felt = []
    if vigem.set_rumble_callback(0, lambda pad, large, small: felt.append((large, small))):
        print("  ok    listening; set a rumble from a game or joy.cpl to test it")
        print("        (nothing will arrive unless something on this PC drives the motors)")
    else:
        print("  note  rumble notifications unavailable -- input will still work")
    time.sleep(1.0)
    if felt:
        print(f"  ok    {len(felt)} rumble update(s) seen, last {felt[-1]}")

    print("\n5. Unplugging")
    vigem.detach(0)
    vigem.shutdown()
    print("  ok    gone")

    print("\nDone. If every step passed, ViGEmBus is fine and the fault is in how Beamer uses it:")
    print("  send the last 50 lines of %LOCALAPPDATA%\\Beamer\\Beamer.log")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
