# PyInstaller spec for CaptureKVM: one exe that is both ends of the link.
#
#   cd windows
#   .\build.ps1
#
# or, by hand:
#
#   .venv\Scripts\pyinstaller.exe capturekvm.spec --noconfirm
#
# The entry is capturekvm.launcher, which asks which end this machine is and then runs either the
# receiver window (capturekvm.app) or the Beamer fork's own window
# (beamer-vigem/win_app/kvm_bridge_win.py). Both are bundled; only one is imported per launch.
#
# A one-directory build, not one file. PySide6, OpenCV and both halves' modules together are a
# few hundred megabytes, and a one-file build unpacks all of that into a temporary directory on
# every launch, which costs seconds each time. The release ships the directory as a zip instead.
#
# Nothing here can be found by reading the entry script: `core` and host mode's modules are
# reached by putting folders on sys.path at run time (capturekvm/__init__.py and
# capturekvm.launcher._run_host), and host mode is then started with runpy. So the folders go on
# `pathex` and every module goes on `hiddenimports` by name.

from pathlib import Path

HERE = Path(SPECPATH).resolve()
ROOT = HERE.parent
BEAMER = ROOT / "beamer-vigem"
WIN_APP = BEAMER / "win_app"

for needed in (BEAMER / "core" / "protocol.py", WIN_APP / "kvm_bridge_win.py"):
    if not needed.is_file():
        raise SystemExit(f"the beamer-vigem subtree is incomplete: {needed} is missing")

# ViGEmBus's client library, which host mode loads to plug a virtual Xbox pad in. The driver is
# Windows's; this half ships with each program, and up to 0.4.0 it did not ship with this one, so
# host mode never offered the controller at all. Loaded through ctypes, so no analysis finds it and
# no import probe misses it: hence a hard stop here rather than a quiet build without it.
# tools/fetch_vigem_client.py puts it in place, and build.ps1 runs that first.
VIGEM_CLIENT = HERE / "vendor" / "ViGEmClient.dll"
if not VIGEM_CLIENT.is_file():
    raise SystemExit(f"{VIGEM_CLIENT} is missing: run python tools/fetch_vigem_client.py first")

# Host mode's modules and core's, by name: the same sets the fork's own build.spec names, and the
# same sets the import probe loads back out of the exe. Globbed rather than listed so a new
# module cannot be left out by drift here.
HOST_MODULES = ["kvm_bridge_win"] + sorted(
    path.stem for path in WIN_APP.glob("*.py") if path.stem != "kvm_bridge_win")
CORE_MODULES = ["core"] + sorted(
    f"core.{path.stem}" for path in (BEAMER / "core").glob("*.py") if path.stem != "__init__")

# Reached through ctypes, a late import or a name built at run time, so nothing in the source
# names them. nacl and _cffi_backend are the wire's crypto; segno renders a pairing QR; the
# win32 three are host mode's sign-in task and its administrator relaunch.
INDIRECT = [
    "nacl", "_cffi_backend", "segno",
    "win32com.client", "pythoncom", "pywintypes",
    "sounddevice", "_sounddevice_data",
]

# Host mode's runtime files: the window and tray icon, the bundled fonts, the version string the
# About box shows, the X11 helper it runs as a script, and the translations. `theme.py` and
# `kvm_bridge_win.py` look for these under sys._MEIPASS, which in a one-directory build is
# `_internal`, so "." is the right destination.
DATAS = [
    (str(WIN_APP / "Beamer.ico"), "."),
    (str(WIN_APP / "assets"), "assets"),
    (str(BEAMER / "VERSION"), "."),
    (str(WIN_APP / "identify_layer_shell.py"), "."),
] + [(str(path), "locales") for path in (BEAMER / "locales").glob("*.po")]

# Between them the two halves import QtCore, QtGui, QtWidgets and QtNetwork (host mode's
# single-instance QLocalServer) and nothing else; QtTest appears only in the fork's own tests.
# PySide6's hook is thorough rather than selective, and these are the large ones: without them
# the build is roughly twice the size.
QT_UNUSED = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtCharts", "PySide6.Qt3DCore", "PySide6.QtDataVisualization", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPrintSupport", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
]

EXCLUDES = QT_UNUSED + [
    "tkinter", "unittest", "pydoc", "doctest", "pdb",
    "matplotlib", "scipy", "pandas", "PIL", "pystray", "IPython", "setuptools", "pip",
    # The fork's own test suite, which is not part of either mode. core.receiver and
    # core.easy_pair are *not* here: they are host mode's link and pairing.
    "core.tests",
]


a = Analysis(
    [str(HERE / "run_capturekvm.py")],
    # win_app ahead of the fork's root, which is the order capturekvm.launcher._run_host sets at
    # run time and for the same reason: the root holds the Mac app's theme.py, and with the root
    # first that one would answer `import theme` and host mode would die on `import AppKit`.
    pathex=[str(HERE), str(WIN_APP), str(BEAMER)],
    # Into _internal, which is sys._MEIPASS, one of the places vigem.py looks.
    binaries=[(str(VIGEM_CLIENT), ".")],
    datas=DATAS,
    hiddenimports=CORE_MODULES + HOST_MODULES + INDIRECT,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# OpenCV's FFmpeg back end, about 29 MB, is loaded only when a video *file* is read or written.
# Receiver mode opens a capture device through Media Foundation and never touches it.
a.binaries = [b for b in a.binaries if 'opencv_videoio_ffmpeg' not in b[0].lower()]

# Windows 10 and 11 provide the UCRT and the ApiSet stubs; do not ship copies PyInstaller finds in
# third-party runtimes. Qt's versioned ICU DLLs stay. This is the installed Beamer build's policy.
a.binaries = [
    b for b in a.binaries
    if b[0].lower() != "ucrtbase.dll" and not b[0].lower().startswith("api-ms-win-")
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CaptureKVM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # a GUI app; a console window behind it would be noise
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Both modes run as an ordinary user. Host mode asks for administrator itself, per PC, and
    # only when its own setting says to: see win_app/admin_mode.py.
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CaptureKVM",
)
