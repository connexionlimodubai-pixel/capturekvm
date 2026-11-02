"""The Picture dialog: what to do about what the card sends.

Live. Every control writes straight through to the `process.Picture` the capture thread is using,
so the change is on screen within a frame -- tuning a sharpen amount by applying it, closing a
dialog and looking is no way to judge it.

**Measure** reads the frame currently on screen rather than asking the user to guess at the range.
It can say "I cannot tell", which matters: a dark menu gives no evidence either way, and expanding
the range of a picture that was never limited crushes it.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QSlider, QVBoxLayout,
)

from . import process

# (setting, label, minimum, maximum, scale, suffix) -- sliders are integers, so a scale of 100
# gives two decimal places on the value the pipeline sees.
SLIDERS = (
    ("brightness", "Brightness", -100, 100, 1, ""),
    ("contrast", "Contrast", 50, 200, 100, "x"),
    ("gamma", "Gamma", 40, 250, 100, ""),
    ("saturation", "Saturation", 0, 200, 100, "x"),
    ("sharpen", "Sharpen", 0, 150, 100, ""),
    ("sharpen_radius", "Sharpen radius", 30, 400, 100, " px"),
)

RANGE_LABELS = (
    ("Leave alone", process.RANGE_NONE),
    ("Expand 16-235 to 0-255", process.RANGE_LIMITED_TO_FULL),
    ("Compress 0-255 to 16-235", process.RANGE_FULL_TO_LIMITED),
)


class PictureDialog(QDialog):
    """`picture` is the live process.Picture; `config` persists; `frame_source` gives the newest
    frame for Measure, and may return None when the card is not running."""

    def __init__(self, picture: process.Picture, config, frame_source, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Picture")
        self.picture = picture
        self.config = config
        self.frame_source = frame_source
        self._sliders = {}

        layout = QVBoxLayout(self)

        # --- range ---
        range_box = QGroupBox("Levels")
        range_form = QFormLayout(range_box)
        self.range_combo = QComboBox()
        for label, value in RANGE_LABELS:
            self.range_combo.addItem(label, value)
        current = self.config.setting("range_mode")
        position = self.range_combo.findData(current)
        self.range_combo.setCurrentIndex(position if position >= 0 else 0)
        self.range_combo.currentIndexChanged.connect(self._range_changed)
        range_form.addRow("Range", self.range_combo)

        measure_row = QHBoxLayout()
        self.measure_button = QPushButton("Measure the picture")
        self.measure_button.clicked.connect(self._measure)
        measure_row.addWidget(self.measure_button)
        range_form.addRow(measure_row)

        self.measured = QLabel("Show something with real blacks and whites, then measure.")
        self.measured.setWordWrap(True)
        self.measured.setMinimumWidth(420)
        range_form.addRow(self.measured)
        layout.addWidget(range_box)

        # --- sliders ---
        adjust_box = QGroupBox("Adjust")
        adjust_form = QFormLayout(adjust_box)
        for key, label, low, high, scale, suffix in SLIDERS:
            slider = QSlider(Qt.Horizontal)
            slider.setRange(low, high)
            slider.setValue(int(round(float(self.config.setting(key)) * scale)))
            readout = QLabel()
            slider.valueChanged.connect(
                lambda value, k=key, s=scale, r=readout, x=suffix: self._slider_changed(k, value, s, r, x))
            row = QHBoxLayout()
            row.addWidget(slider, 1)
            row.addWidget(readout)
            adjust_form.addRow(label, row)
            self._sliders[key] = (slider, scale, readout, suffix)
            self._show_value(key)
        layout.addWidget(adjust_box)

        # --- view ---
        view_box = QGroupBox("View")
        view_form = QFormLayout(view_box)
        self.smooth = QCheckBox("Smooth scaling")
        self.smooth.setToolTip(
            "Off gives nearest-neighbour, which is crisp for pixel art and jagged for everything else.")
        self.smooth.setChecked(bool(self.config.setting("smooth_scaling")))
        self.smooth.toggled.connect(lambda on: self.config.set_setting("smooth_scaling", bool(on)))
        view_form.addRow(self.smooth)

        self.one_to_one = QCheckBox("Show 1:1 (no scaling at all)")
        self.one_to_one.setToolTip(
            "Sharpest possible, because nothing is resampled. The picture is centred, and clipped "
            "if the window is smaller than it.")
        self.one_to_one.setChecked(self.config.setting("scale_mode") == "actual")
        self.one_to_one.toggled.connect(
            lambda on: self.config.set_setting("scale_mode", "actual" if on else "fit"))
        view_form.addRow(self.one_to_one)
        layout.addWidget(view_box)

        self.cost = QLabel("")
        self.cost.setWordWrap(True)
        layout.addWidget(self.cost)

        buttons = QDialogButtonBox()
        reset = buttons.addButton("Reset", QDialogButtonBox.ResetRole)
        reset.clicked.connect(self._reset)
        buttons.addButton(QDialogButtonBox.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._cost_timer = QTimer(self)
        self._cost_timer.timeout.connect(self._show_cost)
        self._cost_timer.start(700)
        self._show_cost()

    # --- changes ---

    def _slider_changed(self, key, raw, scale, readout, suffix) -> None:
        value = raw / scale if scale != 1 else raw
        self.config.set_setting(key, value)
        self.picture.update(**{key: value})
        self._show_value(key)

    def _show_value(self, key) -> None:
        slider, scale, readout, suffix = self._sliders[key]
        value = slider.value() / scale if scale != 1 else slider.value()
        readout.setText(f"{value:g}{suffix}" if scale != 1 else f"{value:+d}{suffix}")
        readout.setMinimumWidth(52)

    def _range_changed(self) -> None:
        mode = self.range_combo.currentData()
        self.config.set_setting("range_mode", mode)
        self.picture.update(range_mode=mode)

    def _measure(self) -> None:
        frame = self.frame_source()
        if frame is None:
            self.measured.setText("No frame to measure -- start the card first.")
            return
        # Measured before processing, or it would be reading back its own correction.
        verdict = process.detect_range(frame)
        self.measured.setText(verdict["text"])
        if verdict["looks_limited"] and verdict["confident"]:
            position = self.range_combo.findData(process.RANGE_LIMITED_TO_FULL)
            if position >= 0 and self.range_combo.currentIndex() != position:
                self.range_combo.setCurrentIndex(position)

    def _reset(self) -> None:
        for key, default in process.DEFAULTS.items():
            self.config.set_setting(key, default)
            if key in self._sliders:
                slider, scale, _readout, _suffix = self._sliders[key]
                slider.blockSignals(True)
                slider.setValue(int(round(float(default) * scale)))
                slider.blockSignals(False)
                self._show_value(key)
        position = self.range_combo.findData(process.RANGE_NONE)
        self.range_combo.setCurrentIndex(max(0, position))
        self.picture.update(**process.DEFAULTS)

    def _show_cost(self) -> None:
        if not self.picture.active:
            self.cost.setText("Nothing switched on, so frames are passed through untouched.")
            return
        spent = self.picture.last_ms
        budget = 1000.0 / max(1, int(self.config.setting("fps") or 30))
        warning = "  — over the frame budget, so the rate will drop" if spent > budget else ""
        self.cost.setText(f"Processing costs {spent:.1f} ms a frame, out of {budget:.0f} ms.{warning}")
