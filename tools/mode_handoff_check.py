"""Does the mode picker really hand the process over?

One exe, two modes (see windows/capturekvm/launcher.py). The picker runs in a QApplication of
its own and shuts it down, and then the chosen mode builds its own in the same process. Qt is not
documented to enjoy that, so it is checked rather than assumed -- and checked with the demanding
half, the font loading and tray icon Beamer's `main()` does before its window exists.

    python tools/mode_handoff_check.py

A window flashes up and answers itself; nothing is paired, no card is opened and no link is made.
Needs a desktop: there is no offscreen mode here, because an offscreen Qt would not be testing
the thing that breaks.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "beamer-vigem" / "win_app"))
sys.path.insert(1, str(ROOT / "beamer-vigem"))
sys.path.insert(2, str(ROOT / "windows"))

from capturekvm import launcher  # noqa: E402

PASSED = []


def check(name, condition, detail="") -> bool:
    ok = bool(condition)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" -- {detail}" if detail and not ok else ""))
    PASSED.append(ok)
    return ok


def pick(mode, *, remember):
    """Answer the picker as a person would: tick or untick the box, then click a button."""
    def driver(dialog):
        dialog.remember_box.setChecked(remember)
        (dialog.host_button if mode == launcher.HOST else dialog.receiver_button).click()
    return launcher.ask(driver=driver)


def beamer_shaped_app():
    """What Beamer's main() asks of Qt before it has a window: application identity, the bundled
    fonts through QFontDatabase, a default font and a tray icon. If a second QApplication in one
    process is going to fall over, it falls over on these."""
    from PySide6.QtGui import QIcon, QPixmap
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget
    import theme

    app = QApplication(sys.argv[:1])
    try:
        app.setApplicationName("Beamer")
        app.setDesktopFileName("uk.co.kalkman.beamer")
        app.setQuitOnLastWindowClosed(False)
        theme.init_fonts()
        app.setFont(theme.font(theme.TYPE["body"]))
        pixmap = QPixmap(16, 16)
        pixmap.fill()
        tray = QSystemTrayIcon(QIcon(pixmap))
        tray.show()
        window = QWidget()
        window.setWindowTitle("handoff probe")
        window.show()
        app.processEvents()
        title = window.windowTitle()
        family = app.font().family()
        tray.hide()
        window.close()
        app.processEvents()
        return title, family
    finally:
        app.shutdown()


def main() -> int:
    print("mode hand-off: picker -> a mode's own QApplication, in one process\n")
    from PySide6.QtWidgets import QApplication

    print("1. The picker answers and gets out of the way")
    mode, remember = pick(launcher.HOST, remember=True)
    check("clicking the host button chose host", mode == launcher.HOST, repr(mode))
    check("the ticked box asked to be remembered", remember is True, repr(remember))
    check("no QApplication is left behind", QApplication.instance() is None,
          repr(QApplication.instance()))

    print("\n2. A mode builds its own QApplication afterwards")
    title, family = beamer_shaped_app()
    check("fonts, tray and window all came up", title == "handoff probe", repr(title))
    check("the bundled font was installed", bool(family), repr(family))
    check("and that one got out of the way too", QApplication.instance() is None,
          repr(QApplication.instance()))

    print("\n3. The other answer, and a third QApplication after it")
    mode, remember = pick(launcher.RECEIVER, remember=False)
    check("clicking the receiver button chose receiver", mode == launcher.RECEIVER, repr(mode))
    check("the unticked box asked not to be remembered", remember is False, repr(remember))
    title, _ = beamer_shaped_app()
    check("a third QApplication in the same process is fine", title == "handoff probe", repr(title))

    print("\n4. Closing the picker starts nothing")
    mode, remember = launcher.ask(driver=lambda dialog: dialog.reject())
    check("a closed window chooses no mode", mode is None, repr(mode))
    check("and remembers nothing", remember is False, repr(remember))

    failures = PASSED.count(False)
    print(f"\n{len(PASSED) - failures}/{len(PASSED)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
