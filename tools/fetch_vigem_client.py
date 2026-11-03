"""Puts ViGEmClient.dll, and ViGEmBus's driver installer, where the Windows build bundles them.

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
VENDOR = ROOT / "windows" / "vendor"

SOURCE = ("https://files.pythonhosted.org/packages/06/a5/"
          "990701a89ea8d7528a269c2be1ba71d888de38a762a1b0f8d8eae4c2be62/vgamepad-0.2.0.tar.gz")
SOURCE_SHA256 = "6ac247e6441e194a0277b46f6f9abc02d7995de5ed8dc495b67eddd214364a88"

# What is taken out of the archive, each pinned. The second is ViGEmBus's own driver installer,
# Nefarius's signed MSI (1.17.333), which the CaptureKVM installer offers on a PC that has no
# driver yet: the zip was never the place for a driver, an installer is.
FILES = (
    ("vgamepad-0.2.0/vgamepad/win/vigem/client/x64/ViGEmClient.dll", "ViGEmClient.dll",
     "2bf0cb1d809039573c922737d298a1653d4dbc61408060ff45a9bcfde82e97d2"),
    ("vgamepad-0.2.0/vgamepad/win/vigem/install/x64/ViGEmBusSetup_x64.msi", "ViGEmBusSetup_x64.msi",
     "5abbba8a4a07aaaeb50b4666183b2f243e0e5ad288026d2a9f3595ed237c4b28"),
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def in_place(name: str, digest: str) -> bool:
    target = VENDOR / name
    return target.is_file() and sha256(target.read_bytes()) == digest


def main() -> int:
    if all(in_place(name, digest) for _, name, digest in FILES):
        for _, name, _ in FILES:
            print(f"ok   windows/vendor/{name} is already in place")
        return 0

    print(f"fetching ViGEm's files from {SOURCE.rsplit('/', 1)[-1]}")
    with urllib.request.urlopen(SOURCE, timeout=60) as response:
        archive = response.read()
    if sha256(archive) != SOURCE_SHA256:
        print(f"FAIL the archive's SHA-256 is {sha256(archive)}, not the pinned {SOURCE_SHA256}",
              file=sys.stderr)
        return 1

    VENDOR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member_name, name, digest in FILES:
            member = tar.extractfile(member_name)
            if member is None:
                print(f"FAIL {member_name} is not in the archive", file=sys.stderr)
                return 1
            data = member.read()
            if sha256(data) != digest:
                print(f"FAIL {name}'s SHA-256 is {sha256(data)}, not the pinned {digest}", file=sys.stderr)
                return 1
            (VENDOR / name).write_bytes(data)
            print(f"ok   windows/vendor/{name} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
