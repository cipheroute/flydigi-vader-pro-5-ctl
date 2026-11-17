"""Vader 5 Pro onboard RGB configuration (NewXInput 0xA7/0xA8/0xA9).

The v3 layout and effect ids are documented in docs/protocol-rgb-mapping.md.
Geometry comes from the device: a Vader 5 Pro can return 10 LEDs x 10 frames,
not the 12 LEDs assumed by some Apex-based implementations.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import commands as C
from .device import Controller, DeviceError

HEADER_SIZE = 20
SUPPORTED_MODES = (1, 2, 5, 6)


@dataclass(frozen=True)
class LightingConfig:
    blob: bytes

    def __post_init__(self):
        object.__setattr__(self, "blob", bytes(self.blob))
        if len(self.blob) < HEADER_SIZE or self.blob[:2] != b"\x00\x03":
            raise DeviceError("Unsupported RGB layout; only version 3.0 is writable")
        count = self.blob[7]
        payload = len(self.blob) - HEADER_SIZE
        if not count or not payload or payload % (count * 3):
            raise DeviceError("Invalid RGB frame geometry; refusing to write")
        if not 0 < self.frame_count <= 256:
            raise DeviceError("Invalid RGB frame count")

    @property
    def mode(self) -> int:
        return self.blob[8]

    @property
    def brightness(self) -> int:
        return self.blob[6]

    @property
    def cycle_time(self) -> int:
        return self.blob[5]

    @property
    def led_count(self) -> int:
        return self.blob[7]

    @property
    def frame_count(self) -> int:
        return (len(self.blob) - HEADER_SIZE) // (self.led_count * 3)

    @property
    def color(self) -> tuple[int, int, int]:
        return tuple(self.blob[HEADER_SIZE:HEADER_SIZE + 3])


def _integer(value, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def configure(config: LightingConfig, *, mode: int | None = None,
              color: tuple[int, int, int] | None = None,
              brightness: int | None = None,
              cycle_time: int | None = None) -> LightingConfig:
    """Build frames without changing the input or any unrelated header bytes."""
    if mode is not None and (type(mode) is not int or mode not in SUPPORTED_MODES):
        raise ValueError("Unsupported RGB effect")
    if color is not None:
        if not isinstance(color, (tuple, list)) or len(color) != 3:
            raise ValueError("Colour must contain exactly three RGB channels")
        color = tuple(_integer(c, 0, 255, "RGB channel") for c in color)
    buf = bytearray(config.blob)
    if brightness is not None:
        buf[6] = _integer(brightness, 0, 100, "Brightness")
    if cycle_time is not None:
        buf[5] = _integer(cycle_time, 1, 255, "Cycle time")
    if mode is not None:
        rgb = bytes(config.color if color is None else color)
        buf[2] = 0  # ClickFeedback belongs only to the feedback effect.
        buf[3] = buf[4] = 0
        buf[8] = mode
        buf[HEADER_SIZE:] = bytes(len(buf) - HEADER_SIZE)
        stride = config.led_count * 3
        if mode in (2, 5):
            buf[HEADER_SIZE:HEADER_SIZE + stride] = rgb * config.led_count
        if mode == 2:
            if config.frame_count < 2:
                raise DeviceError("Breathing needs at least two RGB frames")
            buf[4] = 1  # One colour followed by a black frame.
        elif mode == 1:
            buf[4] = config.frame_count - 1
            for frame in range(config.frame_count):
                for led in range(config.led_count):
                    hue = (led / config.led_count - frame / config.frame_count) % 1.0
                    rgb = bytes(round(c * 255) for c in colorsys.hsv_to_rgb(hue, 1, 1))
                    offset = HEADER_SIZE + frame * stride + led * 3
                    buf[offset:offset + 3] = rgb
    return LightingConfig(bytes(buf))


def _profile(index: int) -> int:
    return _integer(index, 0, C.PROFILE_COUNT - 1, "Profile index")


def read_lighting(ctl: Controller, profile: int | None = None
                  ) -> tuple[int, LightingConfig]:
    before = C.read_profile_versions(ctl)
    if profile is None:
        profile = before.active
    _profile(profile)
    try:
        # On a Vader 5 Pro even 0xA7 selects its profile, unlike the Apex 5.
        return profile, LightingConfig(C.read_blob(ctl, C.ProfileCmd.READ_LED, profile))
    finally:
        _restore_active(ctl, before)


def _restore_active(ctl: Controller, before: C.ProfileVersions) -> None:
    now = C.read_profile_versions(ctl)
    if (now.active, now.active_is_ns) == (before.active, before.active_is_ns):
        return
    cfg_id = before.active + (C.NS_CFG_BASE if before.active_is_ns else 0)
    if not C.activate_profile(ctl, cfg_id):
        raise DeviceError("Could not restore the previously active profile")
    now = C.read_profile_versions(ctl)
    if (now.active, now.active_is_ns) != (before.active, before.active_is_ns):
        raise DeviceError("Previously active profile was not restored")


def _backup(profile: int, config: LightingConfig) -> None:
    # Preserve an exact recovery copy before the first upload, including effects
    # the editor deliberately leaves intact when only brightness is changed.
    import os
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    folder = root / "flydigi-control" / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    (folder / f"rgb-profile-{profile + 1}-{stamp}.bin").write_bytes(config.blob)


def apply_lighting(ctl: Controller, profile: int, expected: LightingConfig,
                   updated: LightingConfig) -> tuple[int, LightingConfig]:
    """Save only RGB, verify it, and restore the user's active profile.

    Keep the existing revision tag: a new random tag without a matching mapping
    upload would make the mapping's embedded dataVersion inconsistent. No mapping
    or macro blob is uploaded by this operation.
    """
    _profile(profile)
    before = C.read_profile_versions(ctl)
    if before.active_is_ns:
        raise DeviceError("Configure RGB in PC mode, not Nintendo Switch mode")
    _, current = read_lighting(ctl, profile)
    if current.blob != expected.blob:
        raise DeviceError("RGB changed on the controller; refresh before saving")
    if (len(updated.blob) != len(current.blob)
            or updated.blob[:2] != current.blob[:2]
            or updated.led_count != current.led_count
            or updated.blob[9:HEADER_SIZE] != current.blob[9:HEADER_SIZE]):
        raise DeviceError("RGB geometry or unrelated settings changed; refusing to write")
    if updated.blob == current.blob:
        return profile, current
    _backup(profile, current)
    try:
        if before.active != profile and not C.activate_profile(ctl, profile):
            raise DeviceError("Cannot activate the RGB profile; nothing uploaded")
        active = C.read_profile_versions(ctl)
        if active.active != profile or active.active_is_ns:
            raise DeviceError("Wrong profile active; refusing to upload RGB")
        chunks = [updated.blob[i:i + C.PKG_SIZE]
                  for i in range(0, len(updated.blob), C.PKG_SIZE)]
        if len(chunks) > 255:
            raise DeviceError("RGB configuration is too large for this protocol")
        ctl.send_expect(C.ProfileCmd.WRITE_LED_START,
                        bytes((profile, 0, len(chunks), C.PKG_SIZE)), timeout=1.0)
        for index, chunk in enumerate(chunks):
            ctl.send_expect(C.ProfileCmd.WRITE_LED_PACK, bytes((index,)) + chunk,
                            timeout=1.0)
        # Verify the target again: 0xA6 always commits the active profile.
        active = C.read_profile_versions(ctl)
        if active.active != profile or active.active_is_ns:
            raise DeviceError("Profile changed during RGB upload; refresh before saving")
        if C.save_config(ctl, before.data_versions[profile]) is None:
            raise DeviceError("RGB uploaded but flash save was not acknowledged")
        _, readback = read_lighting(ctl, profile)
        if readback.blob != updated.blob:
            raise DeviceError("RGB read-back mismatch; saved settings were not verified")
        return profile, readback
    finally:
        _restore_active(ctl, before)
