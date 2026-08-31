from __future__ import annotations

import ctypes
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageGrab

from butler.config import Settings
from butler.diagnostics import event as diagnostic_event


class ScreenCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class PixelBounds:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.right <= self.left or self.bottom <= self.top:
            raise ScreenCaptureError("Границы экрана должны иметь положительный размер.")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


@dataclass(frozen=True)
class MonitorSnapshot:
    device_name: str
    bounds: PixelBounds
    work_bounds: PixelBounds
    primary: bool
    dpi_x: int
    dpi_y: int

    def __post_init__(self) -> None:
        if not self.device_name or self.dpi_x <= 0 or self.dpi_y <= 0:
            raise ScreenCaptureError("Метаданные монитора повреждены.")


@dataclass(frozen=True)
class ScreenCapture:
    png: bytes
    desktop_bounds: PixelBounds
    monitors: tuple[MonitorSnapshot, ...]
    image_width: int
    image_height: int
    captured_monotonic: float

    def __post_init__(self) -> None:
        if not self.png or self.image_width <= 0 or self.image_height <= 0:
            raise ScreenCaptureError("Снимок экрана пуст или повреждён.")
        if not self.monitors:
            raise ScreenCaptureError("Для снимка не найдено ни одного монитора.")


@dataclass(frozen=True)
class PixelPoint:
    x: int
    y: int
    monitor_device: str


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


_MONITORINFOF_PRIMARY = 1
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_MDT_EFFECTIVE_DPI = 0
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


@contextmanager
def _physical_dpi_context() -> Iterator[None]:
    if not hasattr(ctypes, "windll"):
        raise ScreenCaptureError("Windows API захвата экрана недоступен.")
    user32 = ctypes.windll.user32
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    if setter is None:
        raise ScreenCaptureError(
            "Windows не поддерживает безопасный Per-Monitor DPI-контекст."
        )
    setter.argtypes = [ctypes.c_void_p]
    setter.restype = ctypes.c_void_p
    previous = setter(ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    if not previous:
        raise ScreenCaptureError("Не удалось включить физический DPI-контекст потока.")
    try:
        yield
    finally:
        setter(previous)


def _bounds_from_rect(rect: _Rect) -> PixelBounds:
    return PixelBounds(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _virtual_bounds_from_metrics(user32: object) -> PixelBounds:
    left = int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
    top = int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
    width = int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
    height = int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    return PixelBounds(left, top, left + width, top + height)


def _union_bounds(monitors: tuple[MonitorSnapshot, ...]) -> PixelBounds:
    if not monitors:
        raise ScreenCaptureError("Windows не вернула ни одного монитора.")
    return PixelBounds(
        min(item.bounds.left for item in monitors),
        min(item.bounds.top for item in monitors),
        max(item.bounds.right for item in monitors),
        max(item.bounds.bottom for item in monitors),
    )


def _windows_monitor_inventory() -> tuple[MonitorSnapshot, ...]:
    user32 = ctypes.windll.user32
    shcore = ctypes.windll.shcore
    get_dpi = shcore.GetDpiForMonitor
    get_dpi.argtypes = [
        wintypes.HMONITOR,
        ctypes.c_int,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
    ]
    get_dpi.restype = ctypes.c_long
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(_Rect),
        wintypes.LPARAM,
    )
    monitors: list[MonitorSnapshot] = []

    @callback_type
    def callback(handle, _device_context, _rect, _data):
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return False
        dpi_x = wintypes.UINT()
        dpi_y = wintypes.UINT()
        if get_dpi(
            handle,
            _MDT_EFFECTIVE_DPI,
            ctypes.byref(dpi_x),
            ctypes.byref(dpi_y),
        ) != 0:
            return False
        try:
            monitors.append(
                MonitorSnapshot(
                    device_name=str(info.szDevice),
                    bounds=_bounds_from_rect(info.rcMonitor),
                    work_bounds=_bounds_from_rect(info.rcWork),
                    primary=bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                    dpi_x=int(dpi_x.value),
                    dpi_y=int(dpi_y.value),
                )
            )
        except ScreenCaptureError:
            return False
        return True

    if not user32.EnumDisplayMonitors(0, None, callback, 0):
        raise ScreenCaptureError("Windows не смогла перечислить физические мониторы.")
    result = tuple(monitors)
    union = _union_bounds(result)
    metrics = _virtual_bounds_from_metrics(user32)
    if union != metrics:
        raise ScreenCaptureError(
            "Физические monitor bounds не совпали с virtual desktop metrics."
        )
    if sum(1 for item in result if item.primary) != 1:
        raise ScreenCaptureError("Windows не вернула ровно один основной монитор.")
    return result


def _grab_virtual_desktop() -> Image.Image:
    return ImageGrab.grab(all_screens=True, include_layered_windows=True)


class ScreenCaptureService:
    """Capture one physical virtual desktop without performing any UI action."""

    def __init__(
        self,
        settings: Settings,
        *,
        inventory_provider: Callable[[], tuple[MonitorSnapshot, ...]] | None = None,
        grabber: Callable[[], Image.Image] | None = None,
        dpi_context: Callable[[], object] | None = None,
        max_image_dimension: int = 4096,
        max_png_bytes: int = 24_000_000,
    ) -> None:
        if not 512 <= max_image_dimension <= 8192:
            raise ScreenCaptureError("Предел изображения должен быть от 512 до 8192.")
        if not 1_000_000 <= max_png_bytes <= 25_000_000:
            raise ScreenCaptureError("Предел PNG должен быть от 1 до 25 МБ.")
        self.settings = settings
        self._inventory_provider = inventory_provider or _windows_monitor_inventory
        self._grabber = grabber or _grab_virtual_desktop
        self._dpi_context = dpi_context or _physical_dpi_context
        self.max_image_dimension = max_image_dimension
        self.max_png_bytes = max_png_bytes

    def monitor_inventory(self) -> tuple[MonitorSnapshot, ...]:
        with self._dpi_context():
            return self._inventory_provider()

    def capture(self) -> ScreenCapture:
        started = time.monotonic()
        with self._dpi_context():
            monitors = self._inventory_provider()
            bounds = _union_bounds(monitors)
            try:
                image = self._grabber()
            except OSError as exc:
                raise ScreenCaptureError(
                    "Windows не смогла получить снимок virtual desktop."
                ) from exc
        try:
            if image.size != (bounds.width, bounds.height):
                raise ScreenCaptureError(
                    "Размер физического снимка не совпал с virtual desktop bounds."
                )
            encoded = image.convert("RGB")
            if max(encoded.size) > self.max_image_dimension:
                scale = self.max_image_dimension / max(encoded.size)
                resized = (
                    max(1, round(encoded.width * scale)),
                    max(1, round(encoded.height * scale)),
                )
                encoded = encoded.resize(resized, Image.Resampling.LANCZOS)
            buffer = BytesIO()
            encoded.save(buffer, format="PNG", optimize=True)
            png = buffer.getvalue()
            if len(png) > self.max_png_bytes:
                raise ScreenCaptureError(
                    "PNG рабочего стола слишком велик для безопасного vision-запроса."
                )
            capture = ScreenCapture(
                png=png,
                desktop_bounds=bounds,
                monitors=monitors,
                image_width=encoded.width,
                image_height=encoded.height,
                captured_monotonic=time.monotonic(),
            )
        finally:
            image.close()
            if "encoded" in locals() and encoded is not image:
                encoded.close()
        diagnostic_event(
            self.settings,
            "screen_capture",
            "captured",
            duration_ms=round((time.monotonic() - started) * 1000),
            desktop_width=bounds.width,
            desktop_height=bounds.height,
            encoded_width=capture.image_width,
            encoded_height=capture.image_height,
            monitor_count=len(monitors),
            png_bytes=len(capture.png),
        )
        return capture

    def validate_layout(self, capture: ScreenCapture) -> None:
        current = self.monitor_inventory()
        if current != capture.monitors:
            raise ScreenCaptureError(
                "Конфигурация мониторов или DPI изменилась после снимка."
            )

    @staticmethod
    def normalized_to_pixel(
        capture: ScreenCapture, coordinate: tuple[int, int]
    ) -> PixelPoint:
        if (
            not isinstance(coordinate, tuple)
            or len(coordinate) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in coordinate)
            or any(value < 0 or value > 999 for value in coordinate)
        ):
            raise ScreenCaptureError("Нормализованные координаты должны быть целыми 0–999.")
        normalized_x, normalized_y = coordinate
        bounds = capture.desktop_bounds
        x = bounds.left + (normalized_x * (bounds.width - 1) + 499) // 999
        y = bounds.top + (normalized_y * (bounds.height - 1) + 499) // 999
        matching = [item for item in capture.monitors if item.bounds.contains(x, y)]
        if len(matching) != 1:
            raise ScreenCaptureError(
                "Координата попала вне физического монитора или в перекрытие bounds."
            )
        return PixelPoint(x, y, matching[0].device_name)
