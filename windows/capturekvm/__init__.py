"""CaptureKVM: watch a PC through a USB capture card and drive it over Beamer's link.

The video never touches the network -- it arrives as UVC frames on the capture card plugged into
this machine. Only keyboard and mouse travel, as Beamer wire version 6 frames to the PC running
Beamer, so the picture's lag is the card's and the control's lag is one Wi-Fi hop.

Importing this package puts `beamer-vigem/` on sys.path, so everything on the wire is built by the
same `core` package the PC runs, rather than by a copy of it. That is the point of keeping the two
in one repository: a second copy of the protocol is a second thing to keep in step, and the two
would drift silently -- a wrong byte in a key derivation does not raise, it just stops the link
working with no clue as to why.

`beamer-vigem/` is a git subtree of the Beamer fork (see ../../NOTICE.md), so it keeps its own
history and can still merge upstream Beamer.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BEAMER_ROOT = PROJECT_ROOT / "beamer-vigem"

# Running from source, `core` is found by putting the subtree on the path. In a PyInstaller
# build there is no subtree on disk -- `core` is frozen into the executable and already
# importable -- and PROJECT_ROOT points at somewhere inside the bundle, so the path is added
# only when it is really there.
if BEAMER_ROOT.is_dir() and str(BEAMER_ROOT) not in sys.path:
    sys.path.insert(0, str(BEAMER_ROOT))

FROZEN = getattr(sys, "frozen", False)

__version__ = "0.4.5"

APP_NAME = "CaptureKVM"
# `app` in hello/welcome. Beamer shows it, and its own apps send "beamer"; a different name keeps
# this client honest about what the peer is talking to.
WIRE_APP = "capturekvm"
