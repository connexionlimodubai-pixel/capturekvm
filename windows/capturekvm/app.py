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

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCursor, QGuiApplication, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSizePolicy,
    QToolBar, QVBoxLayout, QWidget,
)

from core import protocol

from . import APP_NAME, __version__, audio, capture, clipboard, gamepad, inputmap, pair, padwire, process, store
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
        self.dropped = 0

    def put(self, frame) -> None:
        height, width = frame.shape[:2]
        # copy(): the numpy buffer belongs to OpenCV and is reused on the next read.
        image = QImage(frame.data, width, height, frame.strides[0], QImage.Format_BGR888).copy()
        with self._lock:
            if self._image is not None:
                self.dropped += 1
            self._image = image
        self.arrived.emit()

    def take(self):
        with self._lock:
            image, self._image = self._image, None
            return image


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
        self._pixmap = None
        self._placeholder = None
        self.driving = False
        self._centre = QPoint()
        self._held_keys = set()
        self._held_buttons = set()
        self._remainder_x = 0.0
        self._remainder_y = 0.0
        self.peer_platform = "windows"

    # --- painting ---

    def show_image(self, image: QImage) -> None:
        self._pixmap = QPixmap.fromImage(image)
        self.update()

    def show_text(self, text: str) -> None:
        self._pixmap = None
        self._placeholder = text
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # Without this Qt resamples with nearest-neighbour, which turns any window that is not
        # exactly the frame's size into jagged, aliased video. It is the single biggest thing
        # between the card and a picture that looks right.
        painter.setRenderHint(QPainter.SmoothPixmapTransform, self._settings.get("smooth_scaling", True))
        painter.fillRect(self.rect(), Qt.black)
        if self._pixmap is None:
            painter.setPen(Qt.gray)
            painter.drawText(self.rect(), Qt.AlignCenter, self._placeholder or "no picture")
            return
        target = self.rect()
        if self._settings.get("scale_mode", "fit") == "actual":
            # 1:1. Nothing is resampled at all, which is as sharp as the card can look; the
            # picture is centred and clipped if the window is smaller than it.
            painter.drawPixmap(
                target.x() + (target.width() - self._pixmap.width()) // 2,
                target.y() + (target.height() - self._pixmap.height()) // 2,
                self._pixmap,
            )
        elif self._settings.get("keep_aspect", True):
            scaled = self._pixmap.size().scaled(target.size(), Qt.KeepAspectRatio)
            painter.drawPixmap(
                target.x() + (target.width() - scaled.width()) // 2,
                target.y() + (target.height() - scaled.height()) // 2,
                scaled.width(), scaled.height(), self._pixmap,
            )
        else:
            painter.drawPixmap(target, self._pixmap)
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

    found = Signal(list, list)

    def scan(self) -> None:
        def work():
            try:
                devices = capture.list_devices()
                names = capture.pnp_camera_names()
            except Exception:                               # noqa: BLE001 - report nothing found
                LOGGER.exception("device scan")
                devices, names = [], []
            self.found.emit(devices, names)

        threading.Thread(target=work, name="device-scan", daemon=True).start()


class PairDialog(QDialog):
    """Type the PC's address and the six digits it is showing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pair with a PC")
        self.address = QLineEdit()
        self.address.setPlaceholderText("192.168.1.20")
        self.code = QLineEdit()
        self.code.setPlaceholderText("123456")
        self.code.setMaxLength(7)
        form = QFormLayout()
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

        self._build_toolbar()
        self._build_status()
        self._refresh_devices()
        self._refresh_peers()
        self._refresh_audio_devices()

        self.heartbeat = QTimer(self)
        self.heartbeat.timeout.connect(self._update_status)
        self.heartbeat.start(500)

        if config.setting("start_fullscreen"):
            self.showFullScreen()

    # --- chrome ---

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main", self)
        bar.setMovable(False)
        self.addToolBar(bar)

        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(240)
        bar.addWidget(QLabel(" Card "))
        bar.addWidget(self.device_box)

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
        self.device_box.clear()
        self.device_box.addItem("scanning for cards...", -1)
        self.scanner.scan()

    def _on_devices_found(self, found: list, names: list) -> None:
        self.device_box.clear()
        for device in found:
            label = (f"#{device['index']}  {device['width']}x{device['height']} "
                     f"{device['fourcc']} @{device['fps']}")
            self.device_box.addItem(label, device["index"])
        if not found:
            self.device_box.addItem("no capture device found", -1)
        wanted = self.config.setting("device_index")
        index = self.device_box.findData(wanted)
        if index >= 0:
            self.device_box.setCurrentIndex(index)
        self.device_box.setToolTip("Windows sees: " + ", ".join(names) if names else "")

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
        index = self.device_box.currentData()
        if index is None or index < 0:
            QMessageBox.warning(self, APP_NAME, "No capture device was found. Plug the card in and press Rescan cards.")
            return
        label = self.format_box.currentText()
        chosen = next((item for item in RESOLUTIONS if item[0] == label), RESOLUTIONS[1])
        _, fourcc, width, height, fps = chosen
        self.config.set_setting("device_index", int(index))
        self.config.set_setting("fourcc", fourcc)
        self.config.set_setting("width", width)
        self.config.set_setting("height", height)
        self.config.set_setting("fps", fps)
        self.capture.start(index, fourcc, width, height, fps)
        self.capture_button.setText("Stop card")

    def _keep_raw(self, frame) -> None:
        # Called on the capture thread with the untouched frame. Only a reference is kept and
        # only Measure reads it, so there is no copy on the hot path.
        self._raw_frame = frame

    def _open_picture(self) -> None:
        dialog = PictureDialog(self.picture, self.config, lambda: self._raw_frame, self)
        dialog.exec()

    def _drain_frames(self) -> None:
        image = self.slot.take()
        if image is not None:
            self.view.show_image(image)

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
            if self.view.driving:
                self.stop_driving()
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

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    # --- status ---

    def _update_status(self) -> None:
        if self.capture.running and self.capture.format:
            shape = self.capture.format
            self.status_card.setText(
                f"card: {shape['width']}x{shape['height']} {shape['fourcc']} "
                f"{self.capture.measured_fps:.0f} fps"
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
