"""The Windows clipboard, in and out of the link.

Clipboard is an upstream Beamer capability, not an addition: `core.protocol` builds and validates
the messages and the PC's responder already answers them. This file is only the local end -- what
Windows has on its clipboard, and what to put there when the PC sends its own.

Two rules from WIRE.md section 3 shape it. The owner sends its clipboard **after the taking
`focus` and before any input**, so a paste on the PC finds what was copied here. And the clipboard
must never break a switch: anything too large, unreadable or not a PNG is dropped quietly rather
than raised, because losing a copied image is a smaller failure than losing the keyboard.

Qt's clipboard belongs to the GUI thread. Nothing here may be called from the link's threads;
`app.py` marshals both directions through signals.
"""

import logging

from PySide6.QtCore import QBuffer, QIODevice, QMimeData
from PySide6.QtGui import QGuiApplication, QImage

from core import protocol

LOGGER = logging.getLogger("capturekvm.clipboard")

TEXT_MAX_BYTES = protocol.CLIPBOARD_MAX_BYTES
IMAGE_MAX_BYTES = protocol.CLIPBOARD_IMAGE_MAX_BYTES


def read_local() -> tuple:
    """(text, png) from the Windows clipboard, either None.

    `text` is dropped when it is over the wire's 256 KiB; `png` when the image will not encode, is
    over 8 MiB, or would not pass `protocol.png_fits` at the other end -- there is no point
    sending something the peer is obliged to discard."""
    clipboard = QGuiApplication.clipboard()
    if clipboard is None:
        return None, None
    data = clipboard.mimeData()
    if data is None:
        return None, None

    text = None
    if data.hasText():
        candidate = data.text()
        if candidate:
            if len(candidate.encode("utf-8", "surrogatepass")) <= TEXT_MAX_BYTES:
                text = candidate
            else:
                LOGGER.info("clipboard text not sent: over %d bytes", TEXT_MAX_BYTES)

    png = None
    if data.hasImage():
        png = _encode_png(clipboard.image())
    return text, png


def _encode_png(image: QImage):
    if image is None or image.isNull():
        return None
    # png_fits also bounds the dimensions, so an enormous but well-compressed image is refused
    # before it is encoded rather than after.
    if image.width() > protocol.PNG_MAX_SIDE or image.height() > protocol.PNG_MAX_SIDE:
        LOGGER.info("clipboard image not sent: %dx%d is over the limit", image.width(), image.height())
        return None
    buffer = QBuffer()
    if not buffer.open(QIODevice.WriteOnly):
        return None
    try:
        if not image.save(buffer, "PNG"):
            LOGGER.info("clipboard image not sent: it would not encode as PNG")
            return None
        png = bytes(buffer.data())
    finally:
        buffer.close()
    if not protocol.png_fits(png):
        LOGGER.info("clipboard image not sent: %d bytes does not fit the wire's limits", len(png))
        return None
    return png


def write_local(text=None, png=None) -> bool:
    """Puts what the PC sent on this machine's clipboard. True when anything was set.

    Both are set together when both arrive, so a paste into an editor gets the text and a paste
    into an image program gets the picture, rather than whichever happened to be written last."""
    clipboard = QGuiApplication.clipboard()
    if clipboard is None:
        return False
    data = QMimeData()
    set_any = False
    if text:
        data.setText(text)
        set_any = True
    if png:
        image = QImage()
        if image.loadFromData(png, "PNG") and not image.isNull():
            data.setImageData(image)
            set_any = True
        else:
            LOGGER.info("clipboard image from the PC did not decode")
    if not set_any:
        return False
    clipboard.setMimeData(data)
    return True


def message_for(text=None, png=None, peer_caps=()):
    """A `clipboard` message carrying only what the peer said it can take, or None when that
    leaves nothing. A sender must not send what a capability does not cover (WIRE.md section 2)."""
    send_text = text if "clipboard" in peer_caps else None
    send_png = png if "clipboard_image" in peer_caps else None
    if send_text is None and send_png is None:
        return None
    return protocol.clipboard_msg(text=send_text, image=send_png)


def describe(text=None, png=None) -> str:
    """A short line for the status bar."""
    parts = []
    if text:
        parts.append(f"{len(text)} characters")
    if png:
        parts.append(f"{len(png) // 1024} KiB image")
    return " and ".join(parts) if parts else "nothing"
