"""Explicit, read-before-write controls for onboard RGB lighting."""

from __future__ import annotations

from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QColorDialog, QComboBox, QFormLayout,
                             QHBoxLayout, QLabel, QPushButton, QScrollArea,
                             QSpinBox, QVBoxLayout, QWidget)

from .. import lighting as L
from ..device import DeviceError
from .pages import Page
from .widgets import Card


class LightingPage(Page):
    def __init__(self, submit):
        super().__init__(
            "Lighting",
            "Edit an onboard profile's RGB lighting. Changes are sent only "
            "when you click Apply and save. Reading another profile "
            "temporarily selects it, then restores the previous active profile.")
        self.submit = submit
        self.expected: L.LightingConfig | None = None
        self._connected = False
        self._busy = False
        self._loading = True
        self._has_profile = False
        self._saving = False
        self._color = (255, 255, 255)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(14)
        scroll.setWidget(content)
        self.outer.addWidget(scroll, 1)

        profile = Card("Onboard profile")
        row = QHBoxLayout()
        self.picker = QComboBox()
        for index in range(4):
            self.picker.addItem(f"Profile {index + 1}", index)
        row.addWidget(self.picker)
        self.refresh_btn = QPushButton("Refresh")
        row.addWidget(self.refresh_btn)
        row.addStretch(1)
        profile.add_layout(row)
        self.summary = QLabel("Connect a controller to read its lighting.")
        self.summary.setObjectName("hint")
        self.summary.setWordWrap(True)
        profile.add(self.summary)
        self.content_layout.addWidget(profile)

        editor = Card("RGB lighting")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.effect = QComboBox()
        for name, mode in (("Keep current effect", None), ("Rainbow", 1),
                           ("Breathing", 2), ("Solid colour", 5), ("Off", 6)):
            self.effect.addItem(name, mode)
        form.addRow("Effect", self.effect)

        color_row = QHBoxLayout()
        self.color_btn = QPushButton("Choose…")
        self.color_hex = QLabel()
        color_row.addWidget(self.color_btn)
        color_row.addWidget(self.color_hex)
        color_row.addStretch(1)
        form.addRow("Colour", color_row)
        self._show_color()

        self.brightness = QSpinBox()
        self.brightness.setRange(0, 100)
        self.brightness.setSuffix(" %")
        form.addRow("Brightness", self.brightness)
        self.cycle_time = QSpinBox()
        self.cycle_time.setRange(1, 255)
        form.addRow("Cycle time", self.cycle_time)
        editor.add_layout(form)
        note = QLabel(
            "Higher cycle time = slower animation. Colour applies to "
            "Breathing and Solid colour. Keep current effect preserves the "
            "existing animation and colours when adjusting brightness or speed.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        editor.add(note)
        self.apply_btn = QPushButton("Apply and save")
        self.apply_btn.setObjectName("primary")
        editor.add(self.apply_btn)
        self.status = QLabel("No lighting configuration loaded.")
        self.status.setWordWrap(True)
        editor.add(self.status)
        self.content_layout.addWidget(editor)
        self.content_layout.addStretch(1)

        self.picker.currentIndexChanged.connect(self._profile_changed)
        self.refresh_btn.clicked.connect(self.refresh)
        self.effect.currentIndexChanged.connect(self._effect_changed)
        self.color_btn.clicked.connect(self._choose_color)
        self.apply_btn.clicked.connect(self._apply)
        self._loading = False
        self._update_enabled()
        self.setEnabled(False)

    def _update_enabled(self) -> None:
        idle = self._connected and not self._busy
        editable = idle and self.expected is not None
        self.picker.setEnabled(idle)
        self.refresh_btn.setEnabled(idle)
        self.effect.setEnabled(editable)
        self.color_btn.setEnabled(editable and self.effect.currentData() in (2, 5))
        self.brightness.setEnabled(editable)
        self.cycle_time.setEnabled(editable)
        self.apply_btn.setEnabled(editable)

    def _show_color(self) -> None:
        self.color_hex.setText("#{:02X}{:02X}{:02X}".format(*self._color))

    def _choose_color(self) -> None:
        if self._busy or self.expected is None:
            return
        color = QColorDialog.getColor(QColor(*self._color), self, "Lighting colour")
        if color.isValid():
            self._color = (color.red(), color.green(), color.blue())
            self._show_color()

    def _effect_changed(self, _index: int) -> None:
        if not self._loading:
            self._update_enabled()

    def _profile_changed(self, _index: int) -> None:
        if not self._loading and self._connected and not self._busy:
            self.refresh()

    def refresh(self) -> None:
        """Read the active profile initially, then the selected profile."""
        if self._busy:
            return
        profile = self.picker.currentData() if self._has_profile else None
        self._connected = True
        self._busy = True
        self._saving = False
        self.expected = None
        self.effect.setCurrentIndex(0)
        self.setEnabled(True)
        self._update_enabled()
        self.summary.setText("Reading lighting configuration…")
        self._has_profile = True
        self.status.setText("Reading; unsaved edits have been discarded.")
        self.submit("lighting_read", lambda ctl: L.read_lighting(ctl, profile))

    def apply_config(self, value: tuple[int, L.LightingConfig]) -> None:
        """Accept a verified read or save result from the device worker."""
        if not self._connected:
            return
        profile, config = value
        saved = self._saving
        self._loading = True
        self.picker.blockSignals(True)
        try:
            self.picker.setCurrentIndex(self.picker.findData(profile))
        finally:
            self.picker.blockSignals(False)
        self.expected = config
        self._has_profile = True
        self.effect.setCurrentIndex(0)
        self._color = config.color
        self._show_color()
        self.brightness.setValue(config.brightness)
        self.cycle_time.setValue(config.cycle_time)
        mode_name = {1: "Rainbow", 2: "Breathing", 5: "Solid colour", 6: "Off"}
        mode = mode_name.get(config.mode, "Device effect")
        self.summary.setText(
            f"Profile {profile + 1} · Current mode: {mode} ({config.mode}) · "
            f"{config.led_count} LEDs × {config.frame_count} frames")
        self.status.setText(
            "Saved to controller and read-back verified." if saved else
            "Configuration loaded. Changes are not sent until Apply and save.")
        self._loading = False
        self._busy = False
        self._saving = False
        self.setEnabled(True)
        self._update_enabled()

    def _apply(self) -> None:
        if not self._connected or self._busy or self.expected is None:
            return
        profile = self.picker.currentData()
        expected = self.expected
        mode = self.effect.currentData()
        try:
            updated = L.configure(
                expected, mode=mode,
                color=self._color if mode in (2, 5) else None,
                brightness=self.brightness.value(),
                cycle_time=self.cycle_time.value())
        except (ValueError, DeviceError) as error:
            self.failed(str(error))
            return
        self._busy = True
        self._saving = True
        self._update_enabled()
        self.status.setText("Saving lighting and verifying read-back…")
        self.submit(
            "lighting_saved",
            lambda ctl: L.apply_lighting(ctl, profile, expected, updated))

    def failed(self, message: str) -> None:
        """Require a fresh read after any failed lighting operation."""
        self.expected = None
        self._busy = False
        self._saving = False
        self.status.setText(f"Lighting operation failed: {message}\nRefresh before applying changes.")
        self._update_enabled()

    def disconnected(self) -> None:
        self._connected = False
        self._busy = False
        self._saving = False
        self._has_profile = False
        self.expected = None
        self.summary.setText("Controller disconnected.")
        self.status.setText("Reconnect to read the lighting configuration.")
        self._update_enabled()
        self.setEnabled(False)
