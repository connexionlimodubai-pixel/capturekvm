"""CaptureKVM: watch a PC through a USB capture card and drive it over Beamer's link.

The video never touches the network -- it arrives as UVC frames on the capture card plugged into
this machine. Only keyboard and mouse travel, as Beamer wire version 6 frames to the PC running
Beamer, so the picture's lag is the card's and the control's lag is one Wi-Fi hop.

Importing this package puts the vendored Beamer core on sys.path; `third_party/beamer/core` is a
verbatim copy of the upstream package (see ../../NOTICE.md) and everything on the wire is built by
it rather than reimplemented here.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BEAMER_ROOT = PROJECT_ROOT / "third_party" / "beamer"

if str(BEAMER_ROOT) not in sys.path:
    sys.path.insert(0, str(BEAMER_ROOT))

__version__ = "0.1.0"

APP_NAME = "CaptureKVM"
# `app` in hello/welcome. Beamer shows it, and its own apps send "beamer"; a different name keeps
# this client honest about what the peer is talking to.
WIRE_APP = "capturekvm"
