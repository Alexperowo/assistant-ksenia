import unittest
from contextlib import nullcontext

from PIL import Image

from butler.config import load_settings
from butler.screen_capture import (
    MonitorSnapshot,
    PixelBounds,
    ScreenCapture,
    ScreenCaptureError,
    ScreenCaptureService,
)


def _monitor(
    name: str,
    bounds: PixelBounds,
    *,
    primary: bool = False,
    dpi: int = 96,
) -> MonitorSnapshot:
    return MonitorSnapshot(name, bounds, bounds, primary, dpi, dpi)


def _capture(
    bounds: PixelBounds, monitors: tuple[MonitorSnapshot, ...]
) -> ScreenCapture:
    return ScreenCapture(b"png", bounds, monitors, bounds.width, bounds.height, 1.0)


class ScreenCaptureServiceTests(unittest.TestCase):
    def test_normalized_coordinates_map_full_negative_virtual_desktop(self):
        left = _monitor("LEFT", PixelBounds(-1920, 0, 0, 1080))
        primary = _monitor("PRIMARY", PixelBounds(0, 0, 3840, 2160), primary=True, dpi=192)
        capture = _capture(PixelBounds(-1920, 0, 3840, 2160), (left, primary))

        first = ScreenCaptureService.normalized_to_pixel(capture, (0, 0))
        last = ScreenCaptureService.normalized_to_pixel(capture, (999, 999))

        self.assertEqual((first.x, first.y, first.monitor_device), (-1920, 0, "LEFT"))
        self.assertEqual((last.x, last.y, last.monitor_device), (3839, 2159, "PRIMARY"))

    def test_coordinate_in_gap_between_monitors_fails_closed(self):
        left = _monitor("LEFT", PixelBounds(0, 0, 1000, 1000), primary=True)
        right = _monitor("RIGHT", PixelBounds(2000, 0, 3000, 1000))
        capture = _capture(PixelBounds(0, 0, 3000, 1000), (left, right))

        with self.assertRaisesRegex(ScreenCaptureError, "вне физического монитора"):
            ScreenCaptureService.normalized_to_pixel(capture, (500, 500))

    def test_capture_requires_physical_image_and_bounds_to_match(self):
        monitors = (
            _monitor("PRIMARY", PixelBounds(0, 0, 3840, 2160), primary=True, dpi=192),
        )
        service = ScreenCaptureService(
            load_settings(),
            inventory_provider=lambda: monitors,
            grabber=lambda: Image.new("RGB", (1920, 1080), "black"),
            dpi_context=nullcontext,
        )

        with self.assertRaisesRegex(ScreenCaptureError, "не совпал"):
            service.capture()

    def test_capture_downscales_without_changing_physical_bounds(self):
        monitors = (
            _monitor("PRIMARY", PixelBounds(0, 0, 3840, 2160), primary=True, dpi=192),
        )
        service = ScreenCaptureService(
            load_settings(),
            inventory_provider=lambda: monitors,
            grabber=lambda: Image.new("RGB", (3840, 2160), "white"),
            dpi_context=nullcontext,
            max_image_dimension=2048,
        )

        capture = service.capture()

        self.assertEqual(capture.desktop_bounds, PixelBounds(0, 0, 3840, 2160))
        self.assertEqual((capture.image_width, capture.image_height), (2048, 1152))
        self.assertTrue(capture.png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_changed_dpi_or_monitor_layout_invalidates_capture(self):
        original = (
            _monitor("PRIMARY", PixelBounds(0, 0, 1920, 1080), primary=True, dpi=96),
        )
        changed = (
            _monitor("PRIMARY", PixelBounds(0, 0, 3840, 2160), primary=True, dpi=192),
        )
        service = ScreenCaptureService(
            load_settings(),
            inventory_provider=lambda: changed,
            grabber=lambda: Image.new("RGB", (3840, 2160), "white"),
            dpi_context=nullcontext,
        )

        with self.assertRaisesRegex(ScreenCaptureError, "изменилась"):
            service.validate_layout(
                _capture(PixelBounds(0, 0, 1920, 1080), original)
            )


if __name__ == "__main__":
    unittest.main()
