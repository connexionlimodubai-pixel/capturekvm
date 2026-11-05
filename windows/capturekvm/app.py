"""The window: the card's picture, and the keyboard, mouse and pad that go to the PC.

Driving is a mode, entered deliberately (F9) and left the same way, because while it is on this
window swallows the keyboard and pins the cursor. Leaving it -- by the release chord, by losing
focus, or by the link dropping -- releases every key and button that is still held, so a key can
never be left stuck down on the PC.

Frames cross from the capture thread in a one-deep slot rather than a queued signal: if the UI
falls behind, the right thing is to skip to the newest frame, not to play out a backlog.
"""

import logging
import sys
import threading
import time

import cv2
from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QCursor, QDesktopServices, QGuiApplication, QImage, QKeySequence,
                           QPainter)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSizePolicy, QToolBar, QVBoxLayout, QWidget,
)

from core import protocol

from . import (APP_NAME, __version__, audio, capture, clipboard, discover, gamepad, inputmap,
               lowlevelkeys, pair, padwire, process, store)
from .picturedialog import PictureDialog
from .link import STATE_UP, BeamerLink

LOGGER = logging.getLogger("capturekvm.app")

OWN_PLATFORM = "windows"

# While the keyboard is grabbed this is the only way out that does not involve the mouse. Chosen
# to be something no application wants.
RELEASE_CHORD = (Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier, Qt.Key_Q)

RESOLUTIONS = (
    ("1920x1080 60", "MJPG", 1920, 1080, 60),
    ("1920x1080 30", "MJPG", 1920, 1080, 30),
    ("1280x720 60", "MJPG", 1280, 720, 60),
    ("1280x720 30", "MJPG", 1280, 720, 30),
    ("640x480 30", "YUY2", 640, 480, 30),
)


class FrameSlot(QObject):
    """A one-deep frame hand-off. The capture thread writes; the GUI thread reads the newest."""

    arrived = Signal()

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._image = None
        self._captured_at = None
        self.dropped = 0

    def put(self, frame, captured_at=None) -> None:
        """Called on the capture thread. The frame leaves it ready to draw.

        Converted here to 32-bit BGRX, which is QImage's Format_RGB32 byte for byte and the
        format Qt's raster painter draws without converting. The window thread used to get BGR888
        and turn it into a QPixmap itself, which on the thread that also handles the keyboard and
        mouse cost 7.6 ms a 1080p frame in a 720p window against 2.8 ms now; the conversion moved
        to this thread costs about 2 ms, a copy it was making anyway.

        cvtColor writes a new array, which nothing else holds, so the QImage can wrap it rather
        than copy it again; the array goes along with the image to keep it alive.
        """
        bgrx = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
        height, width = bgrx.shape[:2]
        image = QImage(bgrx.data, width, height, bgrx.strides[0], QImage.Format_RGB32)
        with self._lock:
            if self._image is not None:
                self.dropped += 1
            self._image = (image, bgrx)
            self._captured_at = captured_at
        self.arrived.emit()

    def take(self):
        """((image, buffer), captured_at), or (None, None). Keep the buffer as long as the image."""
        with self._lock:
            frame, self._image = self._image, None
            return frame, self._captured_at


class VideoView(QWidget):
    """The picture, and the input surface. Owns nothing about the link beyond a send callback."""

    def __init__(self, send, settings, parent=None):
        super().__init__(parent)
        self._send = send
        self._settings = settings
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(QSize(480, 270))
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._frame = None          # FrameSlot's (image, buffer), or None for the placeholder
        self._placeholder = None
        self.driving = False
        self._centre = QPoint()
        self._held_keys = set()
        self._held_buttons = set()
        self._remainder_x = 0.0
        self._remainder_y = 0.0
        self.peer_platform = "windows"

    # --- painting ---

    def show_image(self, frame) -> None:
        """`frame` is FrameSlot's (image, buffer): the QImage wraps the buffer rather than owning
        a copy of it, so both are held until the next frame replaces them."""
        self._frame = frame
        self.update()

    def show_text(self, text: str) -> None:
        self._frame = None
        self._placeholder = text
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # Without this Qt resamples with nearest-neighbour, which turns any window that is not
        # exactly the frame's size into jagged, aliased video. It is the single biggest thing
        # between the card and a picture that looks right.
        painter.setRenderHint(QPainter.SmoothPixmapTransform, self._settings.get("smooth_scaling", True))
        painter.fillRect(self.rect(), Qt.black)
        if self._frame is None:
            painter.setPen(Qt.gray)
            # Wrapped and inset: the placeholder is where an empty card list explains itself, in
            # a paragraph rather than a line.
            painter.drawText(self.rect().adjusted(48, 48, -48, -48), Qt.AlignCenter | Qt.TextWordWrap,
                             self._placeholder or "no picture")
            return
        # drawImage, not drawPixmap: the image is already in the raster painter's own format, and
        # turning it into a QPixmap first was a conversion of every frame on this thread.
        image = self._frame[0]
        target = self.rect()
        if self._settings.get("scale_mode", "fit") == "actual":
            # 1:1. Nothing is resampled at all, which is as sharp as the card can look; the
            # picture is centred and clipped if the window is smaller than it.
            painter.drawImage(
                target.x() + (target.width() - image.width()) // 2,
                target.y() + (target.height() - image.height()) // 2,
                image,
            )
        elif self._settings.get("keep_aspect", True):
            scaled = image.size().scaled(target.size(), Qt.KeepAspectRatio)
            painter.drawImage(
                QRect(target.x() + (target.width() - scaled.width()) // 2,
                      target.y() + (target.height() - scaled.height()) // 2,
                      scaled.width(), scaled.height()),
                image,
            )
        else:
            painter.drawImage(target, image)
        if self.driving:
            painter.setPen(Qt.red)
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))


    # --- drive mode ---

    def set_driving(self, driving: bool) -> None:
        if driving == self.driving:
            return
        if driving:
            self.driving = True
            self.setFocus(Qt.OtherFocusReason)
            self.grabKeyboard()
            self.grabMouse()
            self.setCursor(Qt.BlankCursor)
            self._recentre()
        else:
            self._release_everything()
            self.driving = False
            self.releaseKeyboard()
            self.releaseMouse()
            self.unsetCursor()
        self.update()

    def _recentre(self) -> None:
        self._centre = self.mapToGlobal(self.rect().center())
        QCursor.setPos(self._centre)

    def note_held(self, name: str) -> None:
        """A key sent by something other than this widget's own events -- the reserved-key hook.

        Recorded here so `_release_everything` lets it go: a Windows key left down on the PC
        because it arrived by a different route is exactly the stuck-key failure drive mode is
        meant to make impossible."""
        self._held_keys.add(name)

    def note_released(self, name: str) -> None:
        self._held_keys.discard(name)

    def _release_everything(self) -> None:
        """Every key and button still down goes up, so nothing is left held on the PC."""
        for name in sorted(self._held_keys):
            self._send(protocol.key_msg(protocol.MSG_KEYUP, name))
        self._held_keys.clear()
        for name in sorted(self._held_buttons):
            self._send({"type": protocol.MSG_MOUSEUP, "data": {"button": name}})
        self._held_buttons.clear()
        self._remainder_x = self._remainder_y = 0.0

    # --- events ---

    def keyPressEvent(self, event) -> None:
        if (event.modifiers() & RELEASE_CHORD[0]) == RELEASE_CHORD[0] and event.key() == RELEASE_CHORD[1]:
            self.window().stop_driving()
            return
        if not self.driving:
            super().keyPressEvent(event)
            return
        name, us = inputmap.key_fields(event, OWN_PLATFORM, self.peer_platform,
                                      self._settings.get("key_style", "semantic"))
        if name is None:
            return
        self._held_keys.add(name)
        self._send(protocol.key_msg(protocol.MSG_KEYDOWN, name, us))

    def keyReleaseEvent(self, event) -> None:
        if not self.driving:
            super().keyReleaseEvent(event)
            return
        name, us = inputmap.key_fields(event, OWN_PLATFORM, self.peer_platform,
                                      self._settings.get("key_style", "semantic"))
        if name is None:
            return
        self._held_keys.discard(name)
        self._send(protocol.key_msg(protocol.MSG_KEYUP, name, us))

    def mouseMoveEvent(self, event) -> None:
        if not self.driving:
            return
        position = event.globalPosition().toPoint()
        delta = position - self._centre
        if delta.isNull():
            return
        scale = float(self._settings.get("mouse_scale", 1.0) or 1.0)
        exact_x = delta.x() * scale + self._remainder_x
        exact_y = delta.y() * scale + self._remainder_y
        whole_x, whole_y = int(exact_x), int(exact_y)
        # Fractions stay here, as WIRE.md section 7 asks; only whole pixels go out.
        self._remainder_x, self._remainder_y = exact_x - whole_x, exact_y - whole_y
        if whole_x or whole_y:
            self._send(protocol.mousemove_msg(whole_x, whole_y))
        self._recentre()

    def mousePressEvent(self, event) -> None:
        if not self.driving:
            self.window().start_driving()
            return
        name = inputmap.button_name(event.button())
        if name:
            self._held_buttons.add(name)
            self._send({"type": protocol.MSG_MOUSEDOWN, "data": {"button": name}})

    def mouseReleaseEvent(self, event) -> None:
        if not self.driving:
            return
        name = inputmap.button_name(event.button())
        if name:
            self._held_buttons.discard(name)
            self._send({"type": protocol.MSG_MOUSEUP, "data": {"button": name}})

    def wheelEvent(self, event) -> None:
        if not self.driving:
            return
        dy, dx, mode = inputmap.scroll_fields(
            event,
            float(self._settings.get("scroll_lines_per_notch", 3.0) or 3.0),
            bool(self._settings.get("natural_scroll", False)),
        )
        if dy or dx:
            self._send(protocol.scroll_msg(dy, dx, mode))

    def focusOutEvent(self, event) -> None:
        # Alt-Tab, a notification stealing focus, the screen locking: let go rather than keep a
        # grab the user can no longer see the edge of.
        if self.driving:
            self.window().stop_driving()
        super().focusOutEvent(event)

    def resizeEvent(self, event) -> None:
        if self.driving:
            self._recentre()
        super().resizeEvent(event)


class DeviceScanner(QObject):
    """Probing eight indices takes seconds, and every one of them is a blocked UI thread, so the
    scan runs on its own and the box is filled when it comes back."""

    # The devices that opened, the names Windows lists, and which privacy switch (if any) is
    # refusing the camera -- the last two only matter when the first is empty, to say why.
    found = Signal(list, list, str)

    def scan(self, backends=capture.BACKENDS) -> None:
        def work():
            try:
                devices = capture.list_devices(backends=backends)
                names = capture.pnp_camera_names()
                blocked = capture.camera_blocked() or ""
            except Exception:                               # noqa: BLE001 - report nothing found
                LOGGER.exception("device scan")
                devices, names, blocked = [], [], ""
            self.found.emit(devices, names, blocked)

        threading.Thread(target=work, name="device-scan", daemon=True).start()


class PairDialog(QDialog):
    """Pick the PC from the ones broadcasting, or type its address, plus the six digits."""

    # Discovery runs on its own thread. A queued signal is the way across; QTimer.singleShot
    # from a thread with no event loop of its own simply never fires, which is how the list
    # stayed empty the first time.
    foundChanged = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pair with a PC")

        # Beamer broadcasts only while it is showing a code, so anything in this list is ready
        # to pair now. It is a convenience, not a trust boundary: a beacon is unauthenticated
        # and the code is what makes the exchange safe.
        self.found = QListWidget()
        self.found.setMaximumHeight(108)
        self.found.itemSelectionChanged.connect(self._pick)
        self.searching = QLabel("Looking for PCs showing a pairing code...")
        self.searching.setWordWrap(True)

        self.foundChanged.connect(self._show_found)
        self.discovery = discover.Discovery(self.foundChanged.emit)
        if not self.discovery.start():
            self.searching.setText(f"Not searching: {self.discovery.error}. Type the address instead.")

        self.address = QLineEdit()
        self.address.setPlaceholderText("192.168.1.20")
        self.code = QLineEdit()
        self.code.setPlaceholderText("123456")
        self.code.setMaxLength(7)
        form = QFormLayout()
        form.addRow("Found", self.found)
        form.addRow("", self.searching)
        form.addRow("PC address", self.address)
        form.addRow("Pairing code", self.code)
        hint = QLabel("On the PC: open Beamer, choose Pair, and read off the six digits.\n"
                      "The code lasts one minute and works once.")
        hint.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def _show_found(self, machines) -> None:
        chosen = self.address.text().strip()
        self.found.clear()
        for entry in machines:
            item = QListWidgetItem(discover.describe(entry))
            item.setData(Qt.UserRole, entry["address"])
            self.found.addItem(item)
            if entry["address"] == chosen:
                item.setSelected(True)
        self.searching.setText(
            "Nothing is broadcasting. A PC only appears while Beamer is showing a code -- "
            "open Pair on it, or type the address."
            if not machines else f"{len(machines)} found. Pick one, or type an address.")

    def _pick(self) -> None:
        for item in self.found.selectedItems():
            self.address.setText(item.data(Qt.UserRole))
            self.code.setFocus()

    def done(self, result) -> None:
        self.discovery.stop()
        super().done(result)

    def values(self) -> tuple:
        return self.address.text().strip(), self.code.text().strip()


class LinkEvents(QObject):
    """Link callbacks arrive on the link's threads; this carries them to the GUI thread."""

    event = Signal(str, dict)


class MainWindow(QMainWindow):
    def __init__(self, config: store.Store):
        super().__init__()
        self.config = config
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1280, 760)

        self.link = None
        self.link_events = LinkEvents()
        self.link_events.event.connect(self._on_link_event)

        self.picture = process.Picture(**{
            key: config.setting(key) for key in process.DEFAULTS if config.setting(key) is not None})
        # The newest frame as the card sent it, before processing: Measure must read what the
        # card is doing, not what the correction has already done to it.
        self._raw_frame = None
        self._pipeline_ms = None
        self.slot = FrameSlot()
        self.slot.arrived.connect(self._drain_frames)
        self.capture = capture.CaptureWorker(self.slot.put, self._on_capture_status,
                                             picture=self.picture)
        self.capture.on_raw = self._keep_raw

        self.view = VideoView(self._send_input, config.settings(), self)
        self.view.show_text("pick a capture device, then Connect")
        self.setCentralWidget(self.view)

        self.audio = audio.Passthrough(self._on_audio_status)
        self.pad_state = padwire.PadState()
        self.pad = gamepad.PadPoller(self._on_pad_report, self._on_pad_presence)
        self.pad_attached = False

        self.scanner = DeviceScanner()
        self.scanner.found.connect(self._on_devices_found)

        self.reserved_keys = lowlevelkeys.ReservedKeys(
            self._reserved_key, lambda: self.view.driving, self._is_release_chord)

        # Fullscreen's state. The pointer is polled rather than tracked through events, so that
        # a cursor resting outside this window still counts as still (see _on_pointer_tick).
        self._hint = None
        self._hint_timer = None
        self._pointer_at = QCursor.pos()
        self._pointer_moved_at = time.monotonic()
        self._watch_pointer = QTimer(self)
        self._watch_pointer.timeout.connect(self._on_pointer_tick)

        self._build_toolbar()
        self._build_status()
        self._refresh_devices()
        self._refresh_peers()
        self._refresh_audio_devices()

        self.heartbeat = QTimer(self)
        self.heartbeat.timeout.connect(self._update_status)
        self.heartbeat.start(500)

        if not self.reserved_keys.install():
            LOGGER.info("the Windows key and Alt+Tab cannot be forwarded: %s", self.reserved_keys.error)

        if config.setting("start_fullscreen"):
            self._set_fullscreen(True)

    # --- chrome ---

    def _build_toolbar(self) -> None:
        bar = self.toolbar = QToolBar("Main", self)
        bar.setMovable(False)
        self.addToolBar(bar)

        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(240)
        bar.addWidget(QLabel(" Card "))
        bar.addWidget(self.device_box)

        # How to read the card. Auto takes the first that works for each card; the others are
        # there to compare them on one card, by the fps and the milliseconds the status bar shows.
        if capture.HAVE_FFMPEG:
            self.config.adopt_ffmpeg()
        self.api_box = QComboBox()
        for key, label in capture.API_CHOICES:
            self.api_box.addItem(label, key)
        self.api_box.setCurrentIndex(max(0, self.api_box.findData(self.config.setting("capture_api"))))
        self.api_box.setToolTip("FFmpeg reads the card as OBS does, decoding MJPG itself.\n"
                                "DirectShow and Media Foundation are OpenCV's two ways, decoding through Windows.\n"
                                "Auto tries them in that order and uses the first that gets a picture.")
        self.api_box.currentIndexChanged.connect(self._on_api_changed)
        bar.addWidget(self.api_box)

        self.format_box = QComboBox()
        for label, *_ in RESOLUTIONS:
            self.format_box.addItem(label)
        wanted = f"{self.config.setting('width')}x{self.config.setting('height')} {self.config.setting('fps')}"
        if self.format_box.findText(wanted) >= 0:
            self.format_box.setCurrentText(wanted)
        bar.addWidget(QLabel(" Format "))
        bar.addWidget(self.format_box)

        self.capture_button = QPushButton("Start card")
        self.capture_button.clicked.connect(self._toggle_capture)
        bar.addWidget(self.capture_button)

        bar.addSeparator()

        self.peer_box = QComboBox()
        self.peer_box.setMinimumWidth(200)
        bar.addWidget(QLabel(" PC "))
        bar.addWidget(self.peer_box)

        self.link_button = QPushButton("Connect")
        self.link_button.clicked.connect(self._toggle_link)
        bar.addWidget(self.link_button)

        pair_action = QAction("Pair...", self)
        pair_action.triggered.connect(self._pair)
        bar.addAction(pair_action)

        forget_action = QAction("Forget", self)
        forget_action.triggered.connect(self._forget)
        bar.addAction(forget_action)

        bar.addSeparator()
        self.drive_action = QAction("Drive (F9)", self)
        self.drive_action.setCheckable(True)
        self.drive_action.setShortcut(QKeySequence(Qt.Key_F9))
        self.drive_action.triggered.connect(self._toggle_drive)
        bar.addAction(self.drive_action)

        keys_button = QPushButton("Send key")
        keys_menu = QMenu(keys_button)
        for label, names in lowlevelkeys.CHORDS:
            keys_menu.addAction(label, lambda n=names: self._send_chord(n))
        keys_button.setMenu(keys_menu)
        keys_button.setToolTip(
            "Chords Windows will not let a window receive. The Windows key and Alt+Tab are also "
            "forwarded automatically while driving; Ctrl+Alt+Del cannot be sent by anything.")
        bar.addWidget(keys_button)

        gaming_action = QAction("Gaming", self)
        gaming_action.setToolTip(
            "Frame rate over resolution, and no post-processing. On a USB 2.0 card 720p60 looks "
            "far better in motion than 1080p30, and the picture pipeline costs up to 16 ms a "
            "frame that a game would rather spend on being current.")
        gaming_action.triggered.connect(self._gaming_preset)
        bar.addAction(gaming_action)

        picture_action = QAction("Picture...", self)
        picture_action.triggered.connect(self._open_picture)
        bar.addAction(picture_action)

        fullscreen = QAction("Fullscreen (F11)", self)
        fullscreen.setShortcut(QKeySequence(Qt.Key_F11))
        fullscreen.triggered.connect(self._toggle_fullscreen)
        self.addAction(fullscreen)
        bar.addAction(fullscreen)

        bar.addSeparator()
        self.audio_box = QComboBox()
        self.audio_box.setMinimumWidth(190)
        bar.addWidget(QLabel(" Sound "))
        bar.addWidget(self.audio_box)
        self.audio_button = QPushButton("Sound on")
        self.audio_button.clicked.connect(self._toggle_audio)
        bar.addWidget(self.audio_button)

        refresh = QAction("Rescan cards", self)
        refresh.triggered.connect(self._refresh_devices)
        bar.addAction(refresh)

        mode_action = QAction("Mode...", self)
        mode_action.setToolTip("Start this machine as the PC being watched instead")
        mode_action.triggered.connect(self._change_mode)
        bar.addAction(mode_action)

    def _build_status(self) -> None:
        self.status_card = QLabel("card: off")
        self.status_link = QLabel("link: off")
        self.status_pad = QLabel("pad: none")
        self.status_audio = QLabel("sound: off")
        for widget in (self.status_card, self.status_link, self.status_pad, self.status_audio):
            self.statusBar().addWidget(widget)
            self.statusBar().addWidget(QLabel("  "))
        self.statusBar().addPermanentWidget(QLabel(f"release: Ctrl+Alt+Shift+Q  "))

    # --- devices and peers ---

    def _refresh_devices(self) -> None:
        if self.capture.running:
            # The card this window is reading opens for nothing else, this scan included, so a
            # rescan now would report it missing and blame "another app" for holding it.
            self.statusBar().showMessage("stop the card before rescanning", 4000)
            return
        self.device_box.clear()
        self.device_box.addItem("scanning for cards...", "")
        self.scanner.scan(capture.backends_for(self.config.setting("capture_api")))

    def _on_api_changed(self) -> None:
        self.config.set_setting("capture_api", self.api_box.currentData())
        if self.capture.running:
            self.statusBar().showMessage("stop the card, then Rescan cards to use the other API", 5000)
            return
        self._refresh_devices()

    @staticmethod
    def _device_key(index: int, api: int) -> str:
        """The combo box's data for one card. Index and back end together: the same index through
        the other API is not guaranteed to be the same device."""
        return f"{int(index)}:{int(api)}"

    def _on_devices_found(self, found: list, names: list, blocked: str = "") -> None:
        self.device_box.clear()
        for device in found:
            # FFmpeg knows the card's name; OpenCV's two back ends only know its number.
            name = f"  {device['name']}" if device.get("name") else ""
            label = (f"#{device['index']}  {device['backend']}{name}  {device['width']}x{device['height']} "
                     f"{device['fourcc']} @{device['fps']}")
            if not device.get("reads"):
                label += "  (no picture yet)"
            self.device_box.addItem(label, self._device_key(device["index"], device["api"]))
        if not found:
            self.device_box.addItem("no capture device found", "")
            self.view.show_text(capture.explain_no_devices(names, blocked))
            if blocked:
                self._offer_camera_settings(blocked)
        # The saved card, through the saved back end if it is still offered that way; otherwise the
        # same index through whatever works now, which is what a setting from before there was a
        # choice of back end has to settle for.
        index = self.config.setting("device_index")
        wanted = self.device_box.findData(self._device_key(index, self.config.setting("device_api") or 0))
        if wanted < 0:
            wanted = next((row for row in range(self.device_box.count())
                           if str(self.device_box.itemData(row)).startswith(f"{index}:")), -1)
        if wanted >= 0:
            self.device_box.setCurrentIndex(wanted)
        self.device_box.setToolTip("Windows sees: " + ", ".join(names) if names else "Windows lists no USB video device")

    def _offer_camera_settings(self, blocked: str) -> None:
        """A privacy switch is the one cause of an empty list that has a button to fix it."""
        answer = QMessageBox.question(
            self, APP_NAME,
            f"Windows is blocking the camera for desktop apps: {blocked}.\n\n"
            "Open Windows's camera settings? Turn on camera access and \"Let desktop apps access "
            "your camera\", then press Rescan cards.",
            QMessageBox.Open | QMessageBox.Cancel, QMessageBox.Open)
        if answer == QMessageBox.Open:
            QDesktopServices.openUrl(QUrl("ms-settings:privacy-webcam"))

    def _refresh_peers(self) -> None:
        self.peer_box.clear()
        for peer in self.config.linkable_peers():
            self.peer_box.addItem(f"{peer.get('name')}  ({peer.get('host')})", peer.get("id"))
        if self.peer_box.count() == 0:
            self.peer_box.addItem("not paired with any PC", None)

    # --- capture ---

    def _toggle_capture(self) -> None:
        if self.capture.running:
            self.capture.stop()
            self.capture_button.setText("Start card")
            self.view.show_text("card stopped")
            return
        key = self.device_box.currentData()
        if not key:
            QMessageBox.warning(self, APP_NAME, "No capture device was found. Plug the card in and press Rescan cards.")
            return
        index, api = (int(part) for part in str(key).split(":"))
        label = self.format_box.currentText()
        chosen = next((item for item in RESOLUTIONS if item[0] == label), RESOLUTIONS[1])
        _, fourcc, width, height, fps = chosen
        self.config.set_setting("device_index", index)
        self.config.set_setting("device_api", api)
        self.config.set_setting("fourcc", fourcc)
        self.config.set_setting("width", width)
        self.config.set_setting("height", height)
        self.config.set_setting("fps", fps)
        self.capture.start(index, fourcc, width, height, fps, api)
        self.capture_button.setText("Stop card")

    def _keep_raw(self, frame, captured_at=None) -> None:
        # Called on the capture thread with the untouched frame. Only a reference is kept and
        # only Measure reads it, so there is no copy on the hot path.
        self._raw_frame = frame

    def _open_picture(self) -> None:
        dialog = PictureDialog(self.picture, self.config, lambda: self._raw_frame, self)
        dialog.exec()

    def _change_mode(self) -> None:
        """Ask the mode question again, in a new process.

        This window cannot become the other mode: that mode builds its own QApplication, and
        this one is already running. So the exe is started again with `--pick` and this one
        closes behind it. Host mode has no button for this -- it is Beamer's window, not ours --
        which is what the "Change mode" command beside the exe is for.
        """
        from . import launcher

        answer = QMessageBox.question(
            self, "Change mode",
            "Close this window and ask again which end of the link this machine is?\n\n"
            "Nothing paired is forgotten.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return
        try:
            launcher.relaunch("--pick")
        except OSError as exc:
            QMessageBox.warning(self, "Change mode", f"Could not start CaptureKVM again: {exc}")
            return
        self.close()

    def _drain_frames(self) -> None:
        frame, captured_at = self.slot.take()
        if frame is None:
            return
        self.view.show_image(frame)
        if captured_at is not None:
            # Device to painted, which is what this app controls. It is not glass-to-glass: the
            # card's own delay before the frame reaches USB cannot be seen from here, and is
            # usually the larger half.
            spent = (time.perf_counter() - captured_at) * 1000.0
            self._pipeline_ms = spent if self._pipeline_ms is None else self._pipeline_ms * 0.9 + spent * 0.1

    def _on_capture_status(self, status: dict) -> None:
        state = status.get("state")
        if state == "failed":
            QTimer.singleShot(0, lambda: self.view.show_text(status.get("text", "capture failed")))
        elif state == "lost":
            QTimer.singleShot(0, lambda: self.view.show_text("no signal from the card"))

    # --- pairing ---

    def _pair(self) -> None:
        dialog = PairDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        address, code = dialog.values()
        try:
            entry = pair.pair_by_address(
                address, code,
                machine_id=self.config.machine_id, name=self.config.name,
                platform=OWN_PLATFORM, peers=self.config.peers,
            )
        except pair.AlreadyPaired:
            QMessageBox.information(self, APP_NAME, "This app is paired with that PC already.")
            return
        except pair.PairingError as exc:
            QMessageBox.warning(self, APP_NAME, f"Pairing failed: {pair.error_text(exc)}")
            return
        except Exception as exc:                            # noqa: BLE001 - report, never crash
            LOGGER.exception("pairing")
            QMessageBox.warning(self, APP_NAME, f"Pairing failed: {exc}")
            return
        try:
            self.config.add_peer(entry)
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self._refresh_peers()
        position = self.peer_box.findData(entry.get("id"))
        if position >= 0:
            self.peer_box.setCurrentIndex(position)
        QMessageBox.information(self, APP_NAME, f"Paired with {entry.get('name')}.")

    def _forget(self) -> None:
        peer_id = self.peer_box.currentData()
        if peer_id is None:
            return
        peer = self.config.peer_by_id(peer_id)
        name = peer.get("name") if peer else peer_id
        if QMessageBox.question(self, APP_NAME, f"Forget {name}?") != QMessageBox.Yes:
            return
        self._stop_link()
        self.config.forget_peer(peer_id)
        self._refresh_peers()

    # --- the link ---

    def _toggle_link(self) -> None:
        if self.link is not None:
            self._stop_link()
        else:
            self._start_link()

    def _start_link(self) -> None:
        peer_id = self.peer_box.currentData()
        peer = self.config.peer_by_id(peer_id) if peer_id else None
        if peer is None:
            QMessageBox.warning(self, APP_NAME, "Pair with a PC first.")
            return
        self.link = BeamerLink(
            peer, self.config.machine_id, self.config.name, OWN_PLATFORM,
            resistance_px=int(self.config.setting("resistance_px") or 0),
            on_event=lambda kind, detail: self.link_events.event.emit(kind, detail),
        )
        self.link.start()
        self.link_button.setText("Disconnect")
        self.pad.start()

    def _stop_link(self) -> None:
        self.stop_driving()
        self.pad.stop()
        link, self.link = self.link, None
        self.pad.stop_rumble()
        if link is not None:
            if self.pad_attached:
                link.send_unsequenced(padwire.detach(0))
                self.pad_attached = False
            link.stop()
        self.link_button.setText("Connect")

    def _on_link_event(self, kind: str, detail: dict) -> None:
        if kind == "welcome":
            self.view.peer_platform = detail.get("platform") or "windows"
            if padwire.CAP_GAMEPAD not in (detail.get("caps") or ()):
                self.status_pad.setText("pad: PC does not offer 'gamepad'")
        elif kind == "state" and detail.get("state") == STATE_UP:
            if self.pad.connected_slot is not None:
                self._attach_pad()
            # Before any input, as WIRE.md section 3 asks, so a paste on the PC finds what was
            # copied here. send_clipboard puts it at the front of the queue for the same reason.
            self._send_clipboard()
        elif kind == "state" and detail.get("state") != STATE_UP:
            self.pad_attached = False
            # The PC sends a stop when it lets go, but a link that simply died sends nothing,
            # and a pad left buzzing is the rumble version of a stuck key.
            self.pad.stop_rumble()
            if self.view.driving:
                self.stop_driving()
        elif kind == "rumble":
            # The game on the PC asked for it. Straight to the pad in the user's hands.
            self.pad.rumble(detail.get("large", 0), detail.get("small", 0))
        elif kind == "clipboard":
            self._receive_clipboard(detail)
        elif kind == "error":
            self.statusBar().showMessage(detail.get("text", ""), 6000)

    def _send_input(self, msg: dict) -> None:
        if self.link is not None:
            self.link.send_input(msg)

    # --- the pad ---

    def _attach_pad(self) -> None:
        if self.link is None or not self.link.gamepad_ready or self.pad_attached:
            return
        self.link.send_unsequenced(padwire.attach(0, padwire.KIND_X360))
        self.pad_attached = True

    def _on_pad_presence(self, connected: bool) -> None:
        if self.link is None:
            return
        if connected:
            QTimer.singleShot(0, self._attach_pad)
        elif self.pad_attached:
            self.link.send_unsequenced(padwire.detach(0))
            self.pad_attached = False

    def _on_pad_report(self, state: padwire.PadState) -> None:
        self.pad_state = state
        link = self.link
        if link is None or not link.gamepad_ready:
            return
        if not self.pad_attached:
            return
        link.send_pad(padwire.report(state, 0))

    def _gaming_preset(self) -> None:
        """Frame rate over resolution, processing off.

        Two things matter in a game and neither is sharpness: how recent the picture is, and how
        smooth it is. A USB 2.0 card cannot do both 1080p and 60, so this takes 60; and the
        picture pipeline, which is worth it for reading text, is up to 16 ms a frame that would
        otherwise go on being current."""
        best = None
        for index in range(self.format_box.count()):
            label = self.format_box.itemText(index)
            try:
                size, fps = label.split()
                width, height = (int(part) for part in size.split("x"))
                rate = int(fps)
            except ValueError:
                continue
            # Highest rate wins; resolution only breaks a tie.
            key = (rate, width * height)
            if best is None or key > best[0]:
                best = (key, index)
        if best is not None:
            self.format_box.setCurrentIndex(best[1])

        for key, value in process.DEFAULTS.items():
            self.config.set_setting(key, value)
        self.picture.update(**process.DEFAULTS)
        self.config.set_setting("smooth_scaling", True)
        self.capture.drain = True

        chosen = self.format_box.currentText()
        restart = "  Press Start card to apply." if self.capture.running else ""
        self.statusBar().showMessage(
            f"Gaming: {chosen}, processing off, newest frame only.{restart}", 8000)

    # --- the keys Windows keeps for itself ---

    @staticmethod
    def _is_release_chord(vk: int) -> bool:
        # Q, so Ctrl+Alt+Shift+Q still reaches Qt and the user can always let go.
        return vk == 0x51

    def _reserved_key(self, name: str, us, down: bool) -> None:
        """From the hook, on the GUI thread. Only the keys the shell would have eaten."""
        if self.link is None or not self.link.up:
            return
        kind = protocol.MSG_KEYDOWN if down else protocol.MSG_KEYUP
        self.link.send_input(protocol.key_msg(kind, name, us))
        if down:
            self.view.note_held(name)
        else:
            self.view.note_released(name)

    def _send_chord(self, names) -> None:
        """A whole chord, pressed in order and released in reverse."""
        if self.link is None or not self.link.up:
            self.statusBar().showMessage("connect to a PC first", 3000)
            return
        for name in names:
            self.link.send_input(protocol.key_msg(protocol.MSG_KEYDOWN, name))
        for name in reversed(names):
            self.link.send_input(protocol.key_msg(protocol.MSG_KEYUP, name))
        self.statusBar().showMessage(f"sent {' + '.join(names)}", 2500)

    # --- the clipboard (upstream Beamer's capability, section 3) ---

    def _send_clipboard(self) -> None:
        """This machine's clipboard to the PC. On the GUI thread: Qt's clipboard allows no other."""
        if self.link is None or not self.link.up:
            return
        if not self.config.setting("clipboard_enabled"):
            return
        try:
            text, png = clipboard.read_local()
        except Exception:                                   # noqa: BLE001 - never break a take
            LOGGER.exception("reading the clipboard")
            return
        if not self.config.setting("clipboard_images"):
            png = None
        if self.link.send_clipboard(text=text, png=png):
            self.statusBar().showMessage(f"clipboard sent: {clipboard.describe(text, png)}", 4000)

    def _receive_clipboard(self, detail: dict) -> None:
        if not self.config.setting("clipboard_enabled"):
            return
        text, png = detail.get("text"), detail.get("image")
        if not self.config.setting("clipboard_images"):
            png = None
        try:
            if clipboard.write_local(text=text, png=png):
                self.statusBar().showMessage(f"clipboard from the PC: {clipboard.describe(text, png)}", 4000)
        except Exception:                                   # noqa: BLE001
            LOGGER.exception("writing the clipboard")

    # --- the card's sound, played here ---

    def _refresh_audio_devices(self) -> None:
        self.audio_box.clear()
        if not audio.available():
            self.audio_box.addItem("sounddevice unavailable", -1)
            self.audio_button.setEnabled(False)
            return
        found = audio.input_devices()
        for device in found:
            mark = "* " if device["looks_like_a_card"] else "  "
            self.audio_box.addItem(
                f"{mark}{device['name'][:38]}  [{device['api'].replace('Windows ', '')}]",
                device["index"])
        if not found:
            self.audio_box.addItem("no audio input found", -1)
            self.audio_button.setEnabled(False)
            return
        wanted = self.config.setting("audio_input")
        position = self.audio_box.findData(wanted)
        self.audio_box.setCurrentIndex(position if position >= 0 else 0)
        self.audio_box.setToolTip("* marks a device whose name looks like a capture card. "
                                  "A name is a guess, not evidence -- pick the right one.")

    def _toggle_audio(self) -> None:
        if self.audio.running:
            self.audio.stop()
            self.config.set_setting("audio_enabled", False)
            self.audio_button.setText("Sound on")
            return
        index = self.audio_box.currentData()
        if index is None or index < 0:
            QMessageBox.warning(self, APP_NAME, "No audio input was found for the card.")
            return
        output = self.config.setting("audio_output")
        started = self.audio.start(
            int(index),
            None if output is None or output < 0 else int(output),
            block_frames=int(self.config.setting("audio_block_frames") or 256),
        )
        if started:
            self.config.set_setting("audio_input", int(index))
            self.config.set_setting("audio_enabled", True)
            self.audio_button.setText("Sound off")

    def _on_audio_status(self, status: dict) -> None:
        if status.get("state") == "failed":
            text = status.get("text", "the card's audio would not open")
            QTimer.singleShot(0, lambda: self.statusBar().showMessage(f"sound: {text}", 8000))

    # --- drive mode ---

    def start_driving(self) -> None:
        if self.link is None or not self.link.up:
            self.statusBar().showMessage("connect to a PC first", 3000)
            return
        self.view.set_driving(True)
        self.drive_action.setChecked(True)

    def stop_driving(self) -> None:
        self.view.set_driving(False)
        self.drive_action.setChecked(False)

    def _toggle_drive(self, checked: bool) -> None:
        if checked:
            self.start_driving()
        else:
            self.stop_driving()

    # --- fullscreen -----------------------------------------------------------------------------
    #
    # Fullscreen means the picture and nothing else. Going fullscreen used to keep the toolbar and
    # the status bar, which made it a bigger window rather than a fullscreen one.
    #
    # The toolbar does not come back on a pointer at the top edge, the way a video player's
    # controls do. It is docked in the window's layout, so showing it again moves the picture down
    # and rescales it: a jump every time the pointer grazed the top. Leaving fullscreen is the way
    # back to it, and the hint on the way in says so.

    CURSOR_IDLE_MS = 2000       # a pointer that has not moved for this long disappears

    def _toggle_fullscreen(self) -> None:
        self._set_fullscreen(not self.isFullScreen())

    def _set_fullscreen(self, on: bool) -> None:
        if on:
            self.showFullScreen()
            self._pointer_at = QCursor.pos()
            self._pointer_moved_at = time.monotonic()
            self._watch_pointer.start(150)
            # After the resize, so it lands in the middle of the screen and not of the old window.
            QTimer.singleShot(80, lambda: self._flash(
                "Fullscreen — F9 to drive, F11 or Esc to leave"))
        else:
            self._watch_pointer.stop()
            self.showNormal()
            if not self.view.driving:
                self.view.unsetCursor()
        self._apply_chrome()

    def _apply_chrome(self) -> None:
        """The toolbar and the status bar are the chrome; fullscreen is the absence of it."""
        show = not self.isFullScreen()
        self.toolbar.setVisible(show)
        self.statusBar().setVisible(show)

    def _on_pointer_tick(self) -> None:
        """Hide a pointer that is only sitting there. Polled rather than wired to mouse events:
        a pointer resting outside this window still counts as not moving, and a widget gets no
        event for that."""
        if not self.isFullScreen() or self.view.driving:
            # Driving pins the cursor to the centre and blanks it itself, so its position says
            # nothing and its shape is not ours to set.
            return
        where = QCursor.pos()
        if where != self._pointer_at:
            self._pointer_at = where
            self._pointer_moved_at = time.monotonic()
            if self.view.cursor().shape() == Qt.BlankCursor:
                self.view.unsetCursor()
        elif ((time.monotonic() - self._pointer_moved_at) * 1000 > self.CURSOR_IDLE_MS
                and self.view.cursor().shape() != Qt.BlankCursor):
            self.view.setCursor(Qt.BlankCursor)

    def _flash(self, text: str, ms: int = 3500) -> None:
        """A line of text over the picture. The status bar cannot say anything in fullscreen,
        which is exactly when there is something worth saying."""
        if self._hint is None:
            self._hint = QLabel(self.view)
            self._hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._hint.setStyleSheet(
                "background: rgba(0, 0, 0, 180); color: #e8f5f3;"
                "padding: 9px 16px; border-radius: 7px; font-size: 13px;")
            self._hint_timer = QTimer(self)
            self._hint_timer.setSingleShot(True)
            self._hint_timer.timeout.connect(self._hint.hide)
        self._hint.setText(text)
        self._hint.adjustSize()
        self._hint.move(max(0, (self.view.width() - self._hint.width()) // 2), 28)
        self._hint.show()
        self._hint.raise_()
        self._hint_timer.start(ms)

    def keyPressEvent(self, event) -> None:
        """Escape leaves fullscreen -- but only when not driving, where every key belongs to the
        PC and swallowing one here would be a key the game never sees."""
        if (event.key() == Qt.Key_Escape and self.isFullScreen() and not self.view.driving):
            self._set_fullscreen(False)
            return
        super().keyPressEvent(event)

    # --- status ---

    def _update_status(self) -> None:
        if self.capture.running and self.capture.format:
            shape = self.capture.format
            fps = self.capture.measured_fps
            asked = (shape.get("asked") or {}).get("fps") or 0
            # The usual reason a capture feels laggy is not the API but a card that fell back to
            # uncompressed video at a fraction of the rate asked for; so say so when it has.
            short = f" (asked {asked})" if asked and 0 < fps < asked * 0.66 else ""
            how = shape.get("backend", "")
            self.status_card.setText(
                f"card: {shape['width']}x{shape['height']} {shape['fourcc']} {fps:.0f} fps{short}"
                + (f" · {how}" if how else "")
            )
        else:
            self.status_card.setText("card: off")

        link = self.link
        if link is None:
            self.status_link.setText("link: off")
        else:
            rtt = f"  {link.rtt_ms:.0f} ms" if link.rtt_ms is not None else ""
            name = link.peer_name or link.address
            self.status_link.setText(f"link: {link.state} {name}{rtt}")

        if self._pipeline_ms is not None and self.capture.running:
            stale = self.capture.dropped_stale
            self.status_card.setText(
                self.status_card.text() + f"  {self._pipeline_ms:.0f} ms"
                + (f"  {stale} skipped" if stale else ""))

        if self.audio.running and self.audio.format:
            shape = self.audio.format
            overflows, underruns = self.audio.drops()
            drops = f"  {overflows + underruns} drops" if (overflows or underruns) else ""
            self.status_audio.setText(
                f"sound: {shape['sample_rate'] // 1000} kHz x{shape['channels']} "
                f"~{shape['latency_ms']:.0f} ms{drops}")
        else:
            self.status_audio.setText("sound: off")

        if not self.pad.available:
            self.status_pad.setText("pad: XInput unavailable")
        elif self.pad.connected_slot is None:
            self.status_pad.setText("pad: none")
        elif self.link is not None and not self.link.gamepad_ready:
            self.status_pad.setText(f"pad: slot {self.pad.connected_slot} (PC not accepting)")
        else:
            self.status_pad.setText(f"pad: slot {self.pad.connected_slot} -> PC"
                                    f"{'' if self.pad_attached else ' (attaching)'}")

    def closeEvent(self, event) -> None:
        self.reserved_keys.remove()
        self.stop_driving()
        self.audio.stop()
        self.capture.stop()
        self._stop_link()
        super().closeEvent(event)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    QGuiApplication.setApplicationName(APP_NAME)
    application = QApplication(list(argv or sys.argv))
    config = store.Store()
    window = MainWindow(config)
    window.show()
    return application.exec()
