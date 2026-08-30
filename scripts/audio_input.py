from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class OpenedInput:
    stream: Any
    device_index: int
    device_name: str
    host_api: str
    sample_rate: int
    failed_attempts: tuple[str, ...] = ()
    candidate_count: int = 0

    def close(self) -> None:
        try:
            self.stream.stop()
        finally:
            self.stream.close()


def _host_api_name(sd, info: dict[str, Any]) -> str:
    try:
        return str(sd.query_hostapis(int(info.get("hostapi", -1))).get("name", ""))
    except (TypeError, ValueError, sd.PortAudioError):
        return ""


def _default_input_index(sd) -> int | None:
    try:
        index = int(sd.default.device[0])
    except (AttributeError, IndexError, TypeError, ValueError, sd.PortAudioError):
        return None
    return index if index >= 0 else None


def ranked_input_devices(sd, selector: str) -> list[tuple[int, dict[str, Any], str]]:
    devices = []
    host_priority = {
        "windows wasapi": 0,
        "mme": 10,
        "windows directsound": 20,
        "windows wdm-ks": 100,
    }
    selector = selector.strip()
    default_index = _default_input_index(sd) if not selector else None
    numeric_selector: int | None = None
    try:
        numeric_selector = int(selector) if selector else None
    except ValueError:
        pass
    needle = selector.casefold()

    for index, raw_info in enumerate(sd.query_devices()):
        info = dict(raw_info)
        if int(info.get("max_input_channels", 0)) < 1:
            continue
        name = str(info.get("name", ""))
        host = _host_api_name(sd, info)
        if numeric_selector is not None and index != numeric_selector:
            continue
        if needle and numeric_selector is None and needle not in name.casefold():
            continue
        score = host_priority.get(host.casefold(), 50)
        if default_index is not None and index == int(default_index):
            score = -10
        if needle and name.casefold() == needle:
            score -= 5
        if "headset" in name.casefold() or "hands-free" in name.casefold():
            score -= 2
        devices.append((score, index, info, host))

    if not devices and selector and numeric_selector is None:
        # Windows 11 may expose a unified Bluetooth endpoint simply as "Headset".
        for index, raw_info in enumerate(sd.query_devices()):
            info = dict(raw_info)
            name = str(info.get("name", ""))
            if int(info.get("max_input_channels", 0)) < 1:
                continue
            if "headset" not in name.casefold() and "hands-free" not in name.casefold():
                continue
            host = _host_api_name(sd, info)
            score = host_priority.get(host.casefold(), 50) + 30
            devices.append((score, index, info, host))

    devices.sort(key=lambda item: (item[0], item[1]))
    return [(index, info, host) for _, index, info, host in devices]


def open_best_input_stream(
    sd,
    selector: str,
    callback: Callable[..., None],
    *,
    target_rate: int = 16000,
) -> OpenedInput:
    candidates = ranked_input_devices(sd, selector)
    if not candidates:
        raise RuntimeError(f"Не найден входной микрофон: {selector or 'устройство по умолчанию'}")
    if (
        not selector.strip()
        and len(candidates) > 1
        and _default_input_index(sd) is None
    ):
        raise RuntimeError(
            "Windows не сообщает микрофон по умолчанию, а доступно несколько "
            "входов. Выберите устройство через voice.wake_device после команды "
            "audio-devices; произвольный вход не будет открыт автоматически."
        )

    attempts: list[str] = []
    for index, info, host in candidates:
        default_rate = max(8000, int(round(float(info.get("default_samplerate", target_rate)))))
        formats: list[tuple[int, Any]] = []
        if "wasapi" in host.casefold():
            formats.append(
                (target_rate, sd.WasapiSettings(exclusive=False, auto_convert=True))
            )
        formats.extend([(default_rate, None), (target_rate, None), (8000, None)])
        seen_rates: set[tuple[int, bool]] = set()
        for sample_rate, extra_settings in formats:
            key = (sample_rate, extra_settings is not None)
            if key in seen_rates:
                continue
            seen_rates.add(key)
            try:
                sd.check_input_settings(
                    device=index,
                    channels=1,
                    dtype="int16",
                    samplerate=sample_rate,
                    extra_settings=extra_settings,
                )
                stream = sd.RawInputStream(
                    samplerate=sample_rate,
                    blocksize=max(800, sample_rate // 4),
                    dtype="int16",
                    channels=1,
                    device=index,
                    callback=callback,
                    extra_settings=extra_settings,
                )
                stream.start()
                return OpenedInput(
                    stream=stream,
                    device_index=index,
                    device_name=str(info.get("name", index)),
                    host_api=host,
                    sample_rate=sample_rate,
                    failed_attempts=tuple(attempts),
                    candidate_count=len(candidates),
                )
            except (OSError, ValueError, sd.PortAudioError) as exc:
                attempts.append(
                    f"{info.get('name', index)} / {host} / {sample_rate} Гц: {exc}"
                )

    detail = "; ".join(attempts[-8:])
    raise RuntimeError(f"Не удалось открыть микрофон. Проверенные режимы: {detail}")
