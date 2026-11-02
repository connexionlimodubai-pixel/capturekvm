# PyInstaller spec for the Windows client.
#
#   cd windows
#   .venv\Scripts\pyinstaller.exe capturekvm.spec --noconfirm
#
# A one-directory build, not one file. PySide6 and OpenCV together are a couple of hundred
# megabytes, and a one-file build unpacks all of that into a temporary directory on every launch,
# which costs seconds each time. The release ships the directory as a zip instead.
#
# `core` needs declaring. The app reaches it by putting `beamer-vigem/` on sys.path at import
# time (see capturekvm/__init__.py), which PyInstaller cannot see by reading the source, so the
# subtree goes on `pathex` and the modules are named as hidden imports.

from pathlib import Path

HERE = Path(SPECPATH).resolve()
ROOT = HERE.parent
BEAMER = ROOT / "beamer-vigem"

if not (BEAMER / "core" / "protocol.py").is_file():
    raise SystemExit(f"the beamer-vigem subtree is missing from {BEAMER}")

CORE_MODULES = [
    "core",
    "core.protocol",   # the wire: handshake, keys, frames, every message
    "core.pairing",    # the CPace exchange
    "core.keytable",   # key names and the cross-family modifier mapping
    "core.gamepad",    # the pad messages
]

# Qt modules this app never touches. PySide6's hook is thorough rather than selective, and these
# are the large ones: without them the build roughly halves.
QT_UNUSED = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtNetwork", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtCharts", "PySide6.Qt3DCore", "PySide6.QtDataVisualization", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPrintSupport", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
]

EXCLUDES = QT_UNUSED + [
    "tkinter", "unittest", "pydoc", "doctest", "pdb",
    "matplotlib", "scipy", "pandas", "PIL", "IPython", "setuptools", "pip",
    # The tools and the fork's tests, which are not part of the app.
    "core.tests", "core.receiver", "core.easy_pair",
]


a = Analysis(
    [str(HERE / "run_capturekvm.py")],
    pathex=[str(HERE), str(BEAMER)],
    binaries=[],
    datas=[],
    hiddenimports=CORE_MODULES + [
        # Reached through ctypes and a late import, so nothing names them in the source.
        "sounddevice",
        "_sounddevice_data",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# OpenCV's FFmpeg back end, about 29 MB, is loaded only when a video *file* is read or written.
# This app opens a capture device through Media Foundation and never touches it.
a.binaries = [b for b in a.binaries if 'opencv_videoio_ffmpeg' not in b[0].lower()]

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
