"""One executable, two roles.

A CaptureKVM link has two ends, and this exe is both of them:

* **receiver** -- the machine the capture card is plugged into. It shows the other PC's picture
  and sends the keyboard, mouse and controller over Beamer's link. That is `capturekvm.app`.
* **host** -- the PC being watched. Its HDMI goes into that card, and it takes the input back
  through a virtual Xbox pad. That is the Beamer fork, `beamer-vigem/win_app/kvm_bridge_win.py`.

One exe rather than two means one download and no "which of these do I install on which
machine": the role is chosen on the machine, not at build time.

The mode is settled before Qt starts, in this order:

1. `--mode host` or `--mode receiver`, which never changes what is remembered.
2. Any argument only Beamer takes -- `--hidden`, `--relaunched`, `--config`, `--import-probe`
   and friends. Beamer relaunches itself for administrator mode and runs itself from the
   sign-in task, both as plain `exe --hidden --relaunched` with no `--mode`, so those launches
   have to find their own way back to host mode or they would meet the picker instead.
3. The mode remembered in the config file, if the box was ticked.
4. The picker.

Whichever wins then owns the process: it builds its own QApplication, reads the argv left to it,
and gets sys.path with its own folder first. Nothing of the other mode is imported, so a
receiver never loads the virtual-pad stack and a host never loads OpenCV.
"""

import sys
from pathlib import Path

from . import APP_NAME, BEAMER_ROOT, FROZEN, __version__

HOST = "host"
RECEIVER = "receiver"
MODES = (HOST, RECEIVER)

# Arguments that belong to Beamer alone. Seeing one means a Beamer launch -- its own elevated
# relaunch, the sign-in task, the installer's hooks or the import probe -- so the mode is not in
# question and the picker must not appear. The client takes no arguments of its own, so there is
# nothing on this list to confuse with one.
BEAMER_ARGUMENTS = frozenset({
    "--hidden",
    "--relaunched",
    "--config",
    "--identify-x11",
    "--set-admin-mode",
    "--set-admin-startup",
    "--import-probe",
    "--enable-autostart",
    "--uninstall-cleanup",
})

USAGE = f"""{APP_NAME} {__version__} -- watch a PC through a capture card and drive it.

  CaptureKVM.exe                     ask which end this machine is, or use the remembered answer
  CaptureKVM.exe --mode receiver     this machine has the capture card: watch and drive
  CaptureKVM.exe --mode host         this machine is the one being watched
  CaptureKVM.exe --pick              ask again, and forget what was remembered
  CaptureKVM.exe --set-mode MODE     remember host, receiver or ask, and start nothing

Every other argument is passed to the mode that runs. Host mode takes Beamer's own
(--hidden, --config PATH); receiver mode takes none.
"""

# What --set-mode accepts. "ask" is the installer's "decide when it first starts": it clears
# whatever was remembered, so the picker comes up.
SET_MODES = {HOST: HOST, RECEIVER: RECEIVER, "ask": ""}


def set_mode(argv, config) -> int:
    """`--set-mode MODE`: remember MODE and exit, opening no window.

    For the installer, which asks which end the machine is on one of its own pages and has to
    record the answer somewhere the app will find it. It runs this rather than writing the config
    file itself: the file's shape is the app's business, and one writer keeps it that way.
    """
    _, wanted = _strip(list(argv), "--set-mode", takes_value=True)
    if wanted is MISSING or wanted not in SET_MODES:
        print(f"--set-mode takes {', '.join(SET_MODES)}", file=sys.stderr)
        return 2
    config.mode = SET_MODES[wanted]
    return 0


MISSING = object()      # `--mode` was given with nothing after it


def _strip(argv, flag, *, takes_value):
    """Remove `flag` from a copy of argv and return (rest, value). A value may be written as
    `--flag x` or `--flag=x`; without `takes_value` the value is just True. Qt and argparse both
    see what is left, so our own flags must not survive into either."""
    rest, value = [], None
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument == flag:
            index += 1
            if not takes_value:
                value = True
            elif index < len(argv):
                value = argv[index]
                index += 1
            else:
                value = MISSING
            continue
        if takes_value and argument.startswith(flag + "="):
            value = argument[len(flag) + 1:]
            index += 1
            continue
        rest.append(argument)
        index += 1
    return [argv[0], *rest], value


def _beamer_launch(argv) -> bool:
    return any(argument.split("=", 1)[0] in BEAMER_ARGUMENTS for argument in argv[1:])


def resolve(argv, remembered):
    """Work out the mode without touching Qt, the disk or the process.

    Returns `(mode, argv, ask)`: the mode or None if the picker has to run, the argv to hand on
    with our own flags removed, and whether `--pick` asked for the question to be put again.
    Kept separate from `run` so the order of precedence can be tested on its own.
    """
    argv, pick = _strip(list(argv), "--pick", takes_value=False)
    argv, wanted = _strip(argv, "--mode", takes_value=True)

    if wanted is not None:
        if wanted is MISSING:
            raise ValueError(f"--mode needs {' or '.join(MODES)} after it")
        if wanted not in MODES:
            raise ValueError(f"--mode takes {' or '.join(MODES)}, not {wanted!r}")
        return wanted, argv, False
    if pick:
        return None, argv, True
    if _beamer_launch(argv):
        return HOST, argv, False
    if remembered in MODES:
        return remembered, argv, False
    return None, argv, False


# --- the picker ---------------------------------------------------------------------------------

def ask(parent_mode=None, *, driver=None):
    """Put the question. Returns `(mode, remember)`, or `(None, False)` if the window was closed.

    Its QApplication is built and shut down here, because each mode builds its own: Beamer's
    `main()` does `QApplication(sys.argv)` itself, and a second instance cannot exist while the
    first is alive. Shutting this one down first is what lets the chosen mode run in this process
    rather than in a relaunch.

    `driver` is called once with the dialog after the window is up, and is how
    tools/mode_handoff_check.py answers the question without a person at the keyboard. The
    hand-off it checks -- one QApplication torn down and the mode's own built in its place -- is
    the part of this file that no amount of reading proves.
    """
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (QApplication, QCheckBox, QCommandLinkButton, QDialog, QLabel,
                                   QVBoxLayout)

    application = QApplication(sys.argv[:1])
    try:
        dialog = QDialog()
        # Not the receiver window's title, which is "CaptureKVM <version>": these two are told
        # apart by title in the smoke test that starts the built exe in each mode.
        dialog.setWindowTitle(f"{APP_NAME} — choose a mode")
        dialog.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        chosen = {}

        layout = QVBoxLayout(dialog)
        heading = QLabel("Which end of the link is this machine?")
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(heading)

        receiver = QCommandLinkButton(
            "This machine watches another PC",
            "The capture card is plugged in here. Shows that PC's picture, and sends it the "
            "keyboard, mouse and controller.")
        host = QCommandLinkButton(
            "This machine is the one being watched",
            "Its HDMI goes out to the capture card. Takes the keyboard, mouse and controller "
            "from the other end, the controller through a virtual Xbox pad.")
        for button in (receiver, host):
            button.setMinimumWidth(460)
            layout.addWidget(button)

        remember = QCheckBox("Always start in this mode on this machine")
        remember.setChecked(True)
        layout.addWidget(remember)

        note = QLabel("Run CaptureKVM.exe --pick to be asked again.")
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)

        def choose(mode):
            chosen["mode"] = mode
            dialog.accept()

        receiver.clicked.connect(lambda: choose(RECEIVER))
        host.clicked.connect(lambda: choose(HOST))
        dialog.host_button, dialog.receiver_button, dialog.remember_box = host, receiver, remember
        if parent_mode in MODES:
            (host if parent_mode == HOST else receiver).setDefault(True)

        if driver is not None:
            QTimer.singleShot(0, lambda: driver(dialog))
        dialog.exec()
        return chosen.get("mode"), (bool(remember.isChecked()) if chosen else False)
    finally:
        # Hand the process back as Qt found it, so the mode that follows can own it.
        application.shutdown()
        del application


# --- running a mode -----------------------------------------------------------------------------

def _run_host(argv) -> int:
    """Beamer's own entry, in this process.

    `win_app` goes on the front of sys.path, ahead of the `beamer-vigem` root that importing this
    package put there: the root holds the Mac app's `theme.py`, and with the root first that one
    answers `import theme` and the app dies on `import AppKit`.

    `run_module` with `run_name="__main__"` runs the file's own `if __name__ == "__main__"` block,
    which is where Beamer handles `--import-probe` and the installer's switches before `main()`.
    `portable_entry.py` starts Beamer the same way.
    """
    import runpy

    if BEAMER_ROOT.is_dir():                       # from source; a frozen build has it bundled
        win_app = str(BEAMER_ROOT / "win_app")
        while win_app in sys.path:
            sys.path.remove(win_app)
        sys.path.insert(0, win_app)

    sys.argv = list(argv)
    runpy.run_module("kvm_bridge_win", run_name="__main__")
    return 0


def _run_receiver(argv) -> int:
    from .app import main as receiver_main

    sys.argv = list(argv)
    return receiver_main(argv)


def run(mode, argv) -> int:
    return _run_host(argv) if mode == HOST else _run_receiver(argv)


def relaunch(*arguments) -> None:
    """Start this exe again with `arguments` and leave it running after we exit.

    A mode cannot change its mind in place -- its QApplication is already up, and the other mode
    wants to build its own -- so changing mode means a new process. Detached and in its own
    process group, so closing the window that asked for it does not take the new one with it.
    """
    import subprocess

    if FROZEN:
        command = [sys.executable, *arguments]
        folder = Path(sys.executable).resolve().parent
    else:
        # `windows/`, which is where `-m capturekvm` can be found, whatever the shell's own
        # directory happens to be.
        command = [sys.executable, "-m", "capturekvm", *arguments]
        folder = Path(__file__).resolve().parents[1]
    subprocess.Popen(
        command,
        cwd=str(folder),
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True,
    )


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # `--help` belongs to whichever mode is already settled; it is ours only while none is.
    asked_for_help = any(argument in ("-h", "--help") for argument in argv[1:])
    settled = _beamer_launch(argv) or any(
        argument == "--mode" or argument.startswith("--mode=") for argument in argv[1:])
    if asked_for_help and not settled:
        print(USAGE)
        return 0

    from . import store

    if any(argument == "--set-mode" or argument.startswith("--set-mode=") for argument in argv[1:]):
        # Before anything that could open a window or start a mode: the installer waits on this.
        return set_mode(argv, store.Store())

    try:
        config = store.Store()
    except Exception:                               # noqa: BLE001 -- a bad config must not block
        config = None

    try:
        mode, argv, pick = resolve(argv, config.mode if config else None)
    except ValueError as exc:
        print(f"{exc}\n\n{USAGE}", file=sys.stderr)
        return 2

    if mode is None:
        mode, remember = ask(config.mode if config else None)
        if mode is None:
            return 0                                # the window was closed; nothing to start
        if config is not None and (remember or pick):
            config.mode = mode if remember else ""
    return run(mode, argv)
