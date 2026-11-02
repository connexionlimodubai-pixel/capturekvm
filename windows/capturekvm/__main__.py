"""`python -m capturekvm`.

The launcher, not the receiver window: this exe is both ends of the link, and which one runs is
decided per machine. `python -m capturekvm --mode receiver` goes straight to the window.
"""

import sys

from .launcher import main

if __name__ == "__main__":
    sys.exit(main())
