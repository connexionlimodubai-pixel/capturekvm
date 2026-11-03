"""Puts ViGEmClient.dll where the Windows build bundles it.

    python tools/fetch_vigem_client.py

ViGEmBus comes in two halves. The driver is installed into Windows once, by ViGEmBus's own setup,
and is shared by every program on the PC. The client library, ViGEmClient.dll, is how a program
talks to that driver, and each program ships its own copy. Host mode loads it through ctypes
(beamer-vigem/win_app/vigem.py) when a controller is first plugged in.

Up to 0.4.0 nothing put it in the zip. The driver was installed and working -- an older Beamer
fork on the same PC drove virtual pads fine, with the copy it shipped -- but CaptureKVM's host
mode never found a client library, never offered the `gamepad` capability, and the controller did
nothing. The build's import probe could not notice: a library loaded through ctypes at run time
is not an import.

The copy comes from vgamepad (MIT, https://github.com/yannbouteiller/vgamepad), which ships the
prebuilt x64 build of Nefarius's ViGEmClient (MIT). Only that one file is read out of the source
archive; nothing in the archive is run. Both the archive and the DLL are pinned by SHA-256, so a
changed file on PyPI stops the build instead of being shipped. The file is not committed: it is
fetched into windows/vendor/, which is ignored, and build.ps1 calls this before PyInstaller.
"""

import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "windows" / "vendor" / "ViGEmClient.dll"

SOURCE = ("https://files.pythonhosted.org/packages/06/a5/"
          "990701a89ea8d7528a269c2be1ba71d888de38a762a1b0f8d8eae4c2be62/vgamepad-0.2.0.tar.gz")
SOURCE_SHA256 = "6ac247e6441e194a0277b46f6f9abc02d7995de5ed8dc495b67eddd214364a88"
MEMBER = "vgamepad-0.2.0/vgamepad/win/vigem/client/x64/ViGEmClient.dll"
DLL_SHA256 = "2bf0cb1d809039573c922737d298a1653d4dbc61408060ff45a9bcfde82e97d2"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    if TARGET.is_file() and sha256(TARGET.read_bytes()) == DLL_SHA256:
        print(f"ok   {TARGET.relative_to(ROOT)} is already in place")
        return 0

    print(f"fetching ViGEmClient.dll from {SOURCE.rsplit('/', 1)[-1]}")
    with urllib.request.urlopen(SOURCE, timeout=60) as response:
        archive = response.read()
    if sha256(archive) != SOURCE_SHA256:
        print(f"FAIL the archive's SHA-256 is {sha256(archive)}, not the pinned {SOURCE_SHA256}",
              file=sys.stderr)
        return 1

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.extractfile(MEMBER)
        if member is None:
            print(f"FAIL {MEMBER} is not in the archive", file=sys.stderr)
            return 1
        data = member.read()
    if sha256(data) != DLL_SHA256:
        print(f"FAIL the DLL's SHA-256 is {sha256(data)}, not the pinned {DLL_SHA256}", file=sys.stderr)
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(data)
    print(f"ok   {TARGET.relative_to(ROOT)} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
