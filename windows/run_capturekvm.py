"""PyInstaller's entry point.

A separate file from `capturekvm/__main__.py` because a frozen entry script is run as a top-level
module, and `__main__.py`'s `from .app import main` is a relative import, which has no package to
be relative to there.
"""

import sys

from capturekvm.app import main

if __name__ == "__main__":
    sys.exit(main())
