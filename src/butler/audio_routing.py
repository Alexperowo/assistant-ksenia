from __future__ import annotations

import re
from typing import Any, Mapping

from butler.config import Settings, set_user_audio_output
from butler.speech import SpeechAnnouncer


def normalize_audio_command(text: str) -> str:
    """Normalize input text by lowering, expanding 'ё', and removing polite fillers."""
    cleaned = text.lower().replace("ё", "е")
    words = re.findall(r"[а-яa-z0-9]+", cleaned)
    prefixes = {
        "ксения",
        "сеня",
        "ассистент",
        "пожалуйста",
        "слушай",
        "скажи",
        "подскажи",
    }
    while words and words[0] in prefixes:
        words.pop(0)
    while words and words[-1] in {"пожалуйста", "сеня", "ксения"}:
        words.pop()
    return " ".join(words)


def parse_audio_output_command(text: str) -> str | None:
    """Parse user voice or text input for audio output routing commands.

    Returns:
        'speakers' | 'headphones' | 'default' | 'status' | None
    """
    normalized = normalize_audio_command(text)
    if not normalized:
        return None

    # Status queries: where is the sound going?
    status_patterns = [
        "где звук",
        "куда идет звук",
        "куда идет вывод",
        "куда выводится звук",
        "какой вывод звука",
        "устройство вывода",
        "текущий вывод звука",
        "где вывод звука",
    ]
    if any(pattern in normalized for pattern in status_patterns):
        return "status"

    # Default / system route commands
    default_patterns = [
        "звук по умолчанию",
        "вывод по умолчанию",
        "системный звук",
        "звук на систему",
        "звук в систему",
        "сбрось звук",
        "сбросить звук",
        "сбрось вывод",
        "верни системный звук",
        "автовыбор звука",
        "звук автоматически",
    ]
    if any(pattern in normalized for pattern in default_patterns):
        return "default"

    # Speakers / PC columns commands
    is_speaker_target = any(
        word in normalized for word in ("динамик", "динамики", "колонк", "колонки", "speaker")
    )
    # Headphones / headset commands
    is_headphone_target = any(
        word in normalized
        for word in (
            "наушник",
            "наушники",
            "гарнитур",
            "гарнитура",
            "гарнитуру",
            "headphone",
            "headset",
            "jbl",
        )
    )

    action_keywords = (
        "звук",
        "вывод",
        "переключи",
        "переключить",
        "переключись",
        "переведи",
        "перевести",
        "переведись",
        "выведи",
        "вывести",
        "выводи",
        "включи",
        "включить",
        "говори",
        "играй",
        "звучи",
        "вруби",
        "поставь",
    )
    has_action = any(act in normalized for act in action_keywords)
    has_preposition = normalized.startswith(("на ", "в ")) or " на " in normalized or " в " in normalized

    if is_speaker_target and (has_action or has_preposition or normalized in {"динамики", "колонки"}):
        # Exclude questions about speakers itself (e.g. "какие у тебя динамики", "расскажи про динамики")
        if not any(q in normalized for q in ("какие", "какой", "расскажи", "почему", "зачем")):
            return "speakers"

    if is_headphone_target and (has_action or has_preposition or normalized in {"наушники", "гарнитура", "гарнитуру"}):
        if not any(q in normalized for q in ("какие", "какой", "расскажи", "почему", "зачем")):
            return "headphones"

    return None


def _device_host_score(host_api: str) -> int:
    host_lower = host_api.casefold()
    if "wasapi" in host_lower:
        return 0
    if "directsound" in host_lower:
        return 10
    if "mme" in host_lower:
        return 20
    return 50


def find_matching_output_device(
    devices: list[dict[str, Any]], target: str
) -> tuple[str, str] | None:
    """Find the best output device selector and human-friendly Russian name.

    Returns:
        (selector_to_save, spoken_label) or None if no match found.
    """
    if not devices:
        return None

    if target == "speakers":
        # Search for speakers / realtek / PC sound output (strictly exclude headphones)
        candidates = []
        for dev in devices:
            name = str(dev.get("name", "")).strip()
            name_lower = name.casefold()
            host = str(dev.get("host_api", ""))
            if any(h in name_lower for h in ("headphone", "headset", "наушник", "гарнитур")):
                continue
            if any(term in name_lower for term in ("speaker", "динамик", "колонк", "realtek")):
                candidates.append((_device_host_score(host), name))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        best_name = candidates[0][1]
        selector = "Speakers" if "speaker" in best_name.casefold() else "Realtek"
        spoken_label = "динамики компьютера"
        return selector, spoken_label

    if target == "headphones":
        # Alexander specifically highlighted automatic discovery of JBL Sense Pro or JBL M3 / Tour One
        # Priority:
        # 1. Any active JBL Tour One M3 or JBL Sense Pro (WASAPI preferred)
        # 2. Any other JBL device
        # 3. Any headphone / headset
        candidates = []
        for dev in devices:
            name = str(dev.get("name", "")).strip()
            name_lower = name.casefold()
            host = str(dev.get("host_api", ""))
            host_score = _device_host_score(host)

            if "tour" in name_lower or "m3" in name_lower:
                candidates.append((host_score, 0, "JBL Tour", "наушники JBL Tour One M3", name))
            elif "sense" in name_lower:
                candidates.append((host_score, 0, "JBL Sense", "наушники JBL Sense Pro", name))
            elif "jbl" in name_lower:
                candidates.append((host_score, 1, "JBL", "наушники JBL", name))
            elif any(term in name_lower for term in ("headphone", "headset", "наушник", "гарнитур")):
                candidates.append((host_score, 2, "Headphones", f"наушники {name}", name))

        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1]))
            best = candidates[0]
            return best[2], best[3]

        return None

    return None


def execute_audio_output_command(
    command: str,
    settings: Settings,
    speech: SpeechAnnouncer,
    recognizer: Any | None = None,
) -> str:
    """Execute audio output routing command, update config, switch announcer, return spoken reply."""
    if command == "status":
        current = settings.output_device
        if current:
            return f"Сейчас звук направлен на {current}."
        return "Сейчас звук выводится по системному маршруту Windows по умолчанию."

    if command == "default":
        set_user_audio_output(settings.root, "")
        speech.switch_output_device("")
        return "Вывод звука переключён на системный маршрут Windows по умолчанию."

    if command in {"speakers", "headphones"}:
        devices: list[dict[str, Any]] = []
        if recognizer is not None and hasattr(recognizer, "list_output_devices"):
            try:
                devices = recognizer.list_output_devices(refresh=True)
            except Exception:
                devices = []

        matched = find_matching_output_device(devices, command)
        if matched is None:
            # If device list couldn't be retrieved dynamically, fallback to standard alias selector
            if command == "headphones":
                selector = "JBL"
                spoken_label = "наушники JBL"
            else:
                selector = "Speakers"
                spoken_label = "динамики компьютера"
            set_user_audio_output(settings.root, selector)
            speech.switch_output_device(selector)
            return f"Звук переключён на {spoken_label}."

        selector, spoken_label = matched
        set_user_audio_output(settings.root, selector)
        speech.switch_output_device(selector)
        return f"Звук переключён на {spoken_label}."

    return "Команда управления выводом звука не распознана."


def notify_bluetooth_disconnected(settings: Settings, speech: SpeechAnnouncer) -> str:
    """Fallback to computer speakers on Bluetooth headset disconnect and notify user."""
    set_user_audio_output(settings.root, "")
    speech.switch_output_device("")
    message = "Связь с наушниками потеряна. Переключилась на динамики компьютера."
    speech.say_and_wait(message)
    return message
