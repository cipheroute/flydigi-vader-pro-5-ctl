"""Hardware-free regression coverage for the controller's v3.0 RGB blobs."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flydigi.device import DeviceError
from flydigi.lighting import LightingConfig, configure, read_lighting


HEADER_SIZE = 20


def make_blob(led_count=10, frame_count=10, grip_sync=1):
    """Build nonuniform frames so preservation and zeroing are observable."""
    header = bytes((0, 3, 1, 2, 7, 15, 37, led_count, 4, grip_sync))
    reserved = bytes(range(0xA0, 0xAA))
    frames = bytes((index * 17 + 23) % 256
                   for index in range(led_count * frame_count * 3))
    return header + reserved + frames


def frames_of(config):
    stride = config.led_count * 3
    return [config.blob[offset:offset + stride]
            for offset in range(HEADER_SIZE, len(config.blob), stride)]


class LightingConfigTests(unittest.TestCase):
    def test_actual_ten_led_ten_frame_device_blob(self):
        blob = make_blob()
        config = LightingConfig(blob)

        self.assertEqual(len(blob), 320)
        self.assertEqual(config.blob, blob)
        self.assertIsInstance(config.blob, bytes)
        self.assertEqual(config.led_count, 10)
        self.assertEqual(config.frame_count, 10)
        self.assertEqual(config.mode, 4)
        self.assertEqual(config.brightness, 37)
        self.assertEqual(config.cycle_time, 15)
        self.assertEqual(config.color, tuple(blob[20:23]))

    def test_geometry_is_read_from_blob_not_a_preset_table(self):
        config = LightingConfig(make_blob(led_count=12, frame_count=4))

        self.assertEqual(config.led_count, 12)
        self.assertEqual(config.frame_count, 4)
        self.assertEqual(len(config.blob), 164)

    def test_rejects_unsupported_versions(self):
        for minor, major in ((0, 2), (1, 3), (0, 4)):
            with self.subTest(minor=minor, major=major):
                blob = bytes((minor, major)) + make_blob()[2:]
                with self.assertRaises(DeviceError):
                    LightingConfig(blob)

    def test_rejects_zero_led_geometry(self):
        blob = make_blob()
        blob = blob[:7] + b"\x00" + blob[8:]
        with self.assertRaises(DeviceError):
            LightingConfig(blob)

    def test_rejects_truncated_or_misaligned_payload(self):
        blob = make_blob()
        malformed = (b"", blob[:2], blob[:19], blob[:20],
                     blob[:21], blob[:-1], blob + b"\x00")
        for candidate in malformed:
            with self.subTest(length=len(candidate)):
                with self.assertRaises(DeviceError):
                    LightingConfig(candidate)


class ConfigureLightingTests(unittest.TestCase):
    def setUp(self):
        self.original_blob = make_blob()
        self.original = LightingConfig(self.original_blob)
        self.color = (17, 129, 250)

    def assert_preserved_geometry_and_reserved(self, original, updated):
        self.assertEqual(len(updated.blob), len(original.blob))
        self.assertEqual(updated.led_count, original.led_count)
        self.assertEqual(updated.frame_count, original.frame_count)
        self.assertEqual(updated.blob[:2], original.blob[:2])
        self.assertEqual(updated.blob[7], original.blob[7])
        self.assertEqual(updated.blob[9:20], original.blob[9:20])

    def assert_effect_header(self, updated, mode, loop_end):
        self.assertEqual(updated.mode, mode)
        self.assertEqual(updated.blob[8], mode)
        self.assertEqual(updated.blob[2], 0)
        self.assertEqual(updated.blob[3:5], bytes((0, loop_end)))
        self.assertEqual(updated.brightness, self.original.brightness)
        self.assertEqual(updated.cycle_time, self.original.cycle_time)
        self.assert_preserved_geometry_and_reserved(self.original, updated)
        self.assertEqual(self.original.blob, self.original_blob)

    def test_solid_fills_all_ten_leds_in_first_frame_only(self):
        updated = configure(self.original, mode=5, color=self.color)

        self.assert_effect_header(updated, mode=5, loop_end=0)
        frames = frames_of(updated)
        self.assertEqual(frames[0], bytes(self.color) * 10)
        self.assertEqual(frames[1:], [bytes(30)] * 9)
        self.assertEqual(updated.color, self.color)

    def test_breathing_alternates_color_and_black(self):
        updated = configure(self.original, mode=2, color=self.color)

        self.assert_effect_header(updated, mode=2, loop_end=1)
        frames = frames_of(updated)
        self.assertEqual(frames[0], bytes(self.color) * 10)
        self.assertEqual(frames[1], bytes(30))
        self.assertEqual(frames[2:], [bytes(30)] * 8)

    def test_off_clears_every_frame(self):
        updated = configure(self.original, mode=6)

        self.assert_effect_header(updated, mode=6, loop_end=0)
        self.assertEqual(updated.blob[HEADER_SIZE:], bytes(300))

    def test_rainbow_uses_full_geometry_and_a_valid_loop(self):
        updated = configure(self.original, mode=1)

        self.assert_effect_header(updated, mode=1, loop_end=9)
        frames = frames_of(updated)
        self.assertEqual(len(frames), 10)
        self.assertTrue(all(len(frame) == 30 for frame in frames))
        self.assertTrue(all(any(frame) for frame in frames))
        self.assertGreater(len(set(frames)), 1)
        for frame in frames:
            colors = {frame[index:index + 3]
                      for index in range(0, len(frame), 3)}
            self.assertGreater(len(colors), 1)
            self.assertTrue(any(max(color) > min(color) for color in colors))

    def test_effects_preserve_grip_sync_and_reserved_bytes(self):
        for grip_sync in (0, 1):
            for mode in (1, 2, 5, 6):
                with self.subTest(grip_sync=grip_sync, mode=mode):
                    original = LightingConfig(make_blob(grip_sync=grip_sync))
                    updated = configure(original, mode=mode, color=self.color)
                    self.assert_preserved_geometry_and_reserved(original, updated)

    def test_effects_never_resize_nondefault_geometry(self):
        original = LightingConfig(make_blob(led_count=12, frame_count=4))
        for mode, loop_end in ((1, 3), (2, 1), (5, 0), (6, 0)):
            with self.subTest(mode=mode):
                updated = configure(original, mode=mode, color=self.color)
                self.assert_preserved_geometry_and_reserved(original, updated)
                self.assertEqual(updated.blob[3:5], bytes((0, loop_end)))
                if mode in (2, 5):
                    self.assertEqual(frames_of(updated)[0], bytes(self.color) * 12)

    def test_brightness_only_preserves_existing_effect_and_frames(self):
        updated = configure(self.original, mode=None, brightness=73)

        self.assertEqual(updated.brightness, 73)
        self.assertEqual(updated.blob[6], 73)
        self.assertEqual(updated.mode, self.original.mode)
        self.assertEqual(updated.blob[3:5], self.original.blob[3:5])
        self.assertEqual(updated.cycle_time, self.original.cycle_time)
        self.assertEqual(updated.blob[HEADER_SIZE:],
                         self.original.blob[HEADER_SIZE:])
        self.assert_preserved_geometry_and_reserved(self.original, updated)
        self.assertEqual(self.original.blob, self.original_blob)
        self.assertEqual(self.original.brightness, 37)

    def test_brightness_and_period_boundaries_are_supported(self):
        for brightness in (0, 100):
            for cycle_time in (1, 255):
                with self.subTest(brightness=brightness, cycle_time=cycle_time):
                    updated = configure(self.original, brightness=brightness,
                                        cycle_time=cycle_time)
                    self.assertEqual(updated.brightness, brightness)
                    self.assertEqual(updated.cycle_time, cycle_time)
                    self.assertEqual(updated.blob[5:7],
                                     bytes((cycle_time, brightness)))
                    self.assertEqual(updated.blob[HEADER_SIZE:],
                                     self.original.blob[HEADER_SIZE:])
                    self.assert_preserved_geometry_and_reserved(self.original,
                                                               updated)
        self.assertEqual(self.original.blob, self.original_blob)

    def test_rejects_out_of_range_brightness(self):
        for brightness in (-1, 101):
            with self.subTest(brightness=brightness):
                with self.assertRaises(ValueError):
                    configure(self.original, brightness=brightness)
        self.assertEqual(self.original.blob, self.original_blob)

    def test_rejects_out_of_range_period(self):
        for cycle_time in (0, 256):
            with self.subTest(cycle_time=cycle_time):
                with self.assertRaises(ValueError):
                    configure(self.original, cycle_time=cycle_time)
        self.assertEqual(self.original.blob, self.original_blob)

    def test_rejects_malformed_rgb(self):
        malformed = ((), (1, 2), (1, 2, 3, 4), (-1, 2, 3),
                     (1, 256, 3), (1, 2, 3.5), ("1", 2, 3), "red")
        for color in malformed:
            with self.subTest(color=color):
                with self.assertRaises(ValueError):
                    configure(self.original, mode=5, color=color)
        self.assertEqual(self.original.blob, self.original_blob)


class ReadLightingTests(unittest.TestCase):
    def setUp(self):
        self.ctl = object()
        self.active = 1
        self.blob = make_blob()
        self.read_error = None
        self.restore_succeeds = True
        self.selected_profiles = []
        patches = patch.multiple(
            "flydigi.lighting.C",
            read_profile_versions=Mock(side_effect=self.profile_versions),
            read_blob=Mock(side_effect=self.read_blob),
            activate_profile=Mock(side_effect=self.activate_profile),
        )
        patches.start()
        self.addCleanup(patches.stop)

    def profile_versions(self, ctl):
        self.assertIs(ctl, self.ctl)
        return SimpleNamespace(active=self.active, active_is_ns=False,
                               data_versions=(101, 102, 103, 104))

    def read_blob(self, ctl, command, profile):
        self.assertIs(ctl, self.ctl)
        self.active = profile
        self.selected_profiles.append(profile)
        if self.read_error is not None:
            raise self.read_error
        return self.blob

    def activate_profile(self, ctl, profile):
        self.assertIs(ctl, self.ctl)
        if self.restore_succeeds:
            self.active = profile
        return self.restore_succeeds

    def test_inactive_profile_read_restores_initial_active_profile(self):
        profile, config = read_lighting(self.ctl, profile=2)

        self.assertEqual(profile, 2)
        self.assertEqual(config.blob, self.blob)
        self.assertIn(2, self.selected_profiles)
        self.assertEqual(self.active, 1)

    def test_default_profile_reads_currently_active_profile(self):
        self.active = 2

        profile, config = read_lighting(self.ctl, profile=None)

        self.assertEqual(profile, 2)
        self.assertEqual(config.blob, self.blob)
        self.assertTrue(self.selected_profiles)
        self.assertTrue(all(index == 2 for index in self.selected_profiles))
        self.assertEqual(self.active, 2)

    def test_malformed_blob_restores_initial_active_profile(self):
        self.blob = make_blob()[:-1]

        with self.assertRaises(DeviceError):
            read_lighting(self.ctl, profile=2)

        self.assertIn(2, self.selected_profiles)
        self.assertEqual(self.active, 1)

    def test_read_failure_restores_initial_active_profile(self):
        self.read_error = DeviceError("RGB transfer failed after selection")

        with self.assertRaises(DeviceError):
            read_lighting(self.ctl, profile=2)

        self.assertIn(2, self.selected_profiles)
        self.assertEqual(self.active, 1)

    def test_failed_profile_restore_raises_device_error(self):
        self.restore_succeeds = False

        with self.assertRaises(DeviceError):
            read_lighting(self.ctl, profile=2)

        self.assertIn(2, self.selected_profiles)
        self.assertEqual(self.active, 2)


if __name__ == "__main__":
    unittest.main()
