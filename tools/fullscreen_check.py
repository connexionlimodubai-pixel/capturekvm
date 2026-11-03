"""Does fullscreen actually hide the chrome?

    python tools/fullscreen_check.py

Fullscreen used to call `showFullScreen()` and nothing else, so the toolbar and the status bar
came along and it was really just a bigger window. That is a hard thing to notice from the code
and an easy one to break again, so the real MainWindow is built here and asked.

Runs on Qt's offscreen platform: window state and widget visibility are tracked there exactly as
on a screen, and nothing flashes up. The device scan and the low-level key hook are stubbed out
first -- probing eight capture indices takes seconds and the hook is no business of a test -- but
everything being asserted is the real window's own behaviour.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "beamer-vigem"))
sys.path.insert(0, str(ROOT / "windows"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from capturekvm import app as client_app, lowlevelkeys, store  # noqa: E402

PASSED = []


def check(name, condition, detail="") -> bool:
    ok = bool(condition)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" -- {detail}" if detail and not ok else ""))
    PASSED.append(ok)
    return ok


def press(window, key):
    """A key as the picture receives it.

    Sent through QApplication, not by calling `view.keyPressEvent` directly: a key the view does
    not want reaches the window because Qt's own delivery walks up the parents when the handler
    ignores it, and calling the handler by hand skips exactly that. Getting this wrong made the
    first run of this file report a bug that was not there.
    """
    QApplication.sendEvent(window.view, QKeyEvent(QKeyEvent.KeyPress, key, Qt.NoModifier))


def chrome_shown(window):
    return window.toolbar.isVisible(), window.statusBar().isVisible()


def main() -> int:
    print("fullscreen: the real window, on Qt's offscreen platform\n")

    client_app.DeviceScanner.scan = lambda self, *args, **kwargs: None
    lowlevelkeys.ReservedKeys.install = lambda self: False

    application = QApplication(sys.argv[:1])
    folder = Path(tempfile.mkdtemp())
    try:
        config = store.Store(folder / "config.json")
        window = client_app.MainWindow(config)
        window.show()
        application.processEvents()

        print("1. A window, as it opens")
        check("it is not fullscreen", not window.isFullScreen())
        check("the toolbar and the status bar are both there", chrome_shown(window) == (True, True),
              str(chrome_shown(window)))

        print("\n2. Fullscreen")
        window._set_fullscreen(True)
        application.processEvents()
        check("the window went fullscreen", window.isFullScreen())
        check("the toolbar and the status bar are gone", chrome_shown(window) == (False, False),
              str(chrome_shown(window)))

        print("\n3. Escape leaves it")
        press(window, Qt.Key_Escape)
        application.processEvents()
        check("the window came back", not window.isFullScreen())
        check("and so did the chrome", chrome_shown(window) == (True, True), str(chrome_shown(window)))

        print("\n4. Escape belongs to the PC while driving")
        # Set the flag rather than calling set_driving: that grabs the keyboard and the mouse,
        # which a test has no business doing. The guard being checked reads this flag.
        window._set_fullscreen(True)
        window.view.driving = True
        press(window, Qt.Key_Escape)
        application.processEvents()
        check("Escape did not leave fullscreen", window.isFullScreen())
        check("and the chrome stayed hidden", chrome_shown(window) == (False, False),
              str(chrome_shown(window)))
        window.view.driving = False

        print("\n5. F11 both ways")
        window._toggle_fullscreen()
        application.processEvents()
        check("F11 left fullscreen", not window.isFullScreen())
        check("the chrome is back", chrome_shown(window) == (True, True), str(chrome_shown(window)))
        window._toggle_fullscreen()
        application.processEvents()
        check("F11 went back in", window.isFullScreen())
        check("the chrome is gone again", chrome_shown(window) == (False, False),
              str(chrome_shown(window)))

        print("\n6. The pointer watcher")
        check("it runs only in fullscreen", window._watch_pointer.isActive())
        window._set_fullscreen(False)
        application.processEvents()
        check("and stops on the way out", not window._watch_pointer.isActive())
        # Driving blanks the cursor itself and pins it to the centre, so a tick that read the
        # pointer's position would see it never move and a tick that set the shape would be
        # fighting drive mode. Both are meant to be skipped entirely.
        window._set_fullscreen(True)
        window.view.setCursor(Qt.BlankCursor)
        window.view.driving = True
        for _ in range(3):
            window._on_pointer_tick()
        check("a driving cursor is left blank", window.view.cursor().shape() == Qt.BlankCursor,
              str(window.view.cursor().shape()))
        window.view.driving = False
        window.view.unsetCursor()

        print("\n7. Starting fullscreen, from the setting")
        window.close()
        config.set_setting("start_fullscreen", True)
        second = client_app.MainWindow(store.Store(folder / "config.json"))
        second.show()
        application.processEvents()
        check("it opened fullscreen", second.isFullScreen())
        check("with no chrome", chrome_shown(second) == (False, False), str(chrome_shown(second)))
        second.close()

        failures = PASSED.count(False)
        print(f"\n{len(PASSED) - failures}/{len(PASSED)} checks passed")
        return 1 if failures else 0
    finally:
        application.shutdown()
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
