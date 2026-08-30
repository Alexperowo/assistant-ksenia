from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar

from butler.chat import ChatError, SentenceChunker, stream_chat
from butler.approval import approval_explanation
from butler.atomic_io import atomic_write_text
from butler.audio_capture import AudioCaptureService, AudioCaptureServiceError
from butler.confirmation import confirmation_text
from butler.config import (
    ConfigError,
    load_settings,
    reasoning_label,
    response_budget_label,
    set_user_capability_model,
    set_user_headset_control,
    set_user_microphone,
    set_user_reasoning,
    set_user_response_budget,
    write_user_settings,
)
from butler.doctor import run_checks
from butler.diagnostics import event as diagnostic_event
from butler.diagnostics import exception as diagnostic_exception
from butler.diagnostics import milestone as diagnostic_milestone
from butler.diagnostics import new_trace_id, trace_scope
from butler.diagnostic_report import format_summary, summarize
from butler.instance_lock import SingleInstance
from butler.lan import run_lan_server
from butler.live import ConversationCoordinator
from butler.model_manager import ModelManager, ModelManagerError
from butler.model_catalog import find_models
from butler.media_buttons import (
    BUTTON_LABELS,
    SUITABLE_ACTIVATION_BUTTONS,
    MediaButtonListener,
)
from butler.orchestrator import RoutedAgentSession
from butler.processes import current_process_image_path
from butler.resilience import RepeatingFailurePolicy
from butler.speech import SpeechAnnouncer
from butler.stt import SpeechRecognitionError, SpeechRecognizer
from butler.tasking import (
    DurableTaskStore,
    TaskCancelled,
    TaskControl,
    TaskState,
)
from butler.trusted_task import (
    TRUSTED_TASK_ARMED,
    TRUSTED_TASK_FINISHED,
    TRUSTED_TASK_STARTED,
    TRUSTED_TASK_WARNING,
    TrustedTaskStore,
)
from butler.user_messages import spoken_agent_error
from butler.wake import (
    MicrophoneCaptureGate,
    WakeListener,
    WakeListenerCancelled,
    WakeListenerError,
    WakeListenerTimeout,
)


_T = TypeVar("_T")


def _runtime_task_state(status: str) -> TaskState:
    normalized = status.casefold()
    if "план" in normalized:
        return TaskState.PLANNING
    if "проверяю результат" in normalized or "проверяю итог" in normalized:
        return TaskState.VERIFYING
    return TaskState.RUNNING


def _model_control(settings, operation: Callable[[], _T]) -> _T:
    """Prevent manual model control from interrupting an active agent task."""
    with SingleInstance(settings.root, "agent-task") as acquired:
        if not acquired:
            raise ModelManagerError(
                "Ксения уже выполняет другую задачу. Дождитесь сообщения «Готово»."
            )
        return operation()


def _fallback_speak(text: str) -> None:
    """Use Windows SAPI when configuration failed before Silero could start."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "speak.ps1"
    if not script.is_file():
        return
    try:
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            input=text,
            text=True,
            encoding="utf-8",
            timeout=45,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _record_unexpected(settings, where: str, exc: Exception) -> Path | None:
    source = settings if settings is not None else Path.cwd() / "runtime"
    return diagnostic_exception(source, "cli", "unexpected_error", exc, where=where)


def _print_checks(checks) -> bool:
    required_ok = True
    for check in checks:
        marker = "ГОТОВО" if check.ok else ("НУЖНО" if check.required else "ПОЗЖЕ")
        print(f"[{marker:6}] {check.name}: {check.detail}")
        if check.required and not check.ok:
            required_ok = False
    return required_ok


def _confirmation_text(name: str, arguments: dict) -> str:
    return f"{confirmation_text(name, arguments)} {approval_explanation(name)}"


def _spoken_microphone_error(error: Exception) -> str:
    """Turn technical capture failures into short, actionable spoken guidance."""
    detail = str(error).casefold()
    if "python" in detail:
        return "Голосовая среда недоступна. Запустите ярлык Ксения — полный аудит."
    if "модель" in detail and ("распозна" in detail or "активац" in detail):
        return "Не найдена модель распознавания речи. Запустите ярлык Ксения — полный аудит."
    if any(word in detail for word in ("device", "микроф", "portaudio", "wasapi", "wdm")):
        return (
            "Микрофон недоступен. Проверьте, что он подключён и разрешён в Windows, "
            "затем запустите ярлык Ксения — проверка микрофона."
        )
    return "Не удалось распознать речь. Запустите ярлык Ксения — проверка микрофона."


def _spoken_agent_error(error: Exception) -> str:
    return spoken_agent_error(error)


def _spoken_device_name(raw_name: object) -> str:
    """Remove Windows driver resource noise from a name before TTS."""
    name = " ".join(str(raw_name or "микрофон").split())
    candidates = re.findall(r"\(([^()]{2,100})\)", name)
    for candidate in reversed(candidates):
        if not any(marker in candidate for marker in ("\\", "@", "%", ";")):
            return candidate.strip()
    clean = name.split("(@", 1)[0].strip(" ;()")
    return clean or "микрофон"


def _status(
    settings,
    speech: SpeechAnnouncer,
    *,
    installation_mode: bool = False,
) -> int:
    checks = run_checks(settings, installation_mode=installation_mode)
    ok = _print_checks(checks)
    manager = ModelManager(settings)
    state = manager.running_state()
    message = (
        f"Сейчас работает модель роли {state.role}; рассуждения: "
        f"{reasoning_label(settings.model(state.role).reasoning)}."
        if state
        else "Ядро модели сейчас не запущено."
    )
    if TrustedTaskStore(settings).status() is not None:
        message += " Доверенная задача подготовлена и ожидает следующего запроса."
    print(f"\n{message}")
    speech.say_and_wait(message)
    return 0 if ok else 1


def _trusted_task_control(settings, speech: SpeechAnnouncer) -> int:
    """Arm or cancel the one-shot grant only after explicit local keyboard consent."""
    if not sys.stdin.isatty():
        message = (
            "Доверенную задачу можно подготовить только вручную в интерактивном окне "
            "на этом компьютере. Перенаправленный ввод отклонён."
        )
        print(f"\n{message}\n")
        speech.say_and_wait(message)
        return 1
    with SingleInstance(settings.root, "agent-task") as safe_to_change_trust:
        if not safe_to_change_trust:
            message = (
                "Доверенную задачу нельзя подготовить, пока Ксения выполняет другой запрос. "
                "Дождитесь его завершения и повторите."
            )
            print(f"\n{message}\n")
            speech.say_and_wait(message)
            return 1
        return _trusted_task_control_locked(settings, speech)


def _trusted_task_control_locked(settings, speech: SpeechAnnouncer) -> int:
    """Keep the agent-task mutex until the local choice has been applied."""
    store = TrustedTaskStore(settings)
    active = store.status() is not None
    state = (
        "Сейчас доверенная задача уже подготовлена."
        if active
        else "Сейчас доверенная задача не подготовлена."
    )
    print(
        "\n=== ДОВЕРЕННАЯ ЗАДАЧА ===\n"
        f"{state}\n\n{TRUSTED_TASK_WARNING}\n\n"
        "1. Включить для следующего запроса\n"
        "2. Отменить подготовленный допуск\n"
        "0. Ничего не менять\n"
    )
    speech.say_and_wait(
        f"{state} {TRUSTED_TASK_WARNING} "
        "Чтобы включить режим, нажмите цифру один и затем Enter. "
        "Чтобы отменить подготовленный допуск, нажмите два и Enter."
    )
    choice = input("Ваш выбор: ").strip().casefold()
    if choice == "1":
        store.arm()
        print(f"\n{TRUSTED_TASK_ARMED}\n")
        speech.say_and_wait(TRUSTED_TASK_ARMED)
        return 0
    if choice == "2":
        cancelled = store.cancel()
        message = (
            "Подготовленный допуск отменён. Обычные подтверждения включены."
            if cancelled
            else "Доверенная задача не была подготовлена. Обычные подтверждения включены."
        )
        print(f"\n{message}\n")
        speech.say_and_wait(message)
        return 0
    message = "Настройки доверия не изменены."
    print(f"\n{message}\n")
    speech.say_and_wait(message)
    return 0


def _setup(settings) -> int:
    print("\nПервая настройка. Можно нажать Enter и оставить предложенное значение.")
    llama_raw = input(f"Путь к llama-server.exe [{settings.llama_server}]: ").strip()
    models_raw = input(f"Папка моделей [{settings.models_dir}]: ").strip()
    llama = Path(llama_raw) if llama_raw else settings.llama_server
    models = Path(models_raw) if models_raw else settings.models_dir
    target = write_user_settings(settings.root, llama, models)
    print(f"Настройки сохранены: {target}")
    return 0


def _chat(settings, speech: SpeechAnnouncer) -> int:
    manager = ModelManager(settings)
    print(
        "\n=== Диалог с Ксенией ===\n"
        "Введите сообщение и нажмите Enter. Для выхода напишите: выход\n"
    )
    speech.say("Диалог готов. Введите сообщение.")
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "Ты локальный дворецкий-разработчик Александра. Отвечай по-русски, "
                "ясно и без лишнего многословия. Александр слабовидящий, поэтому ответы "
                "должны хорошо восприниматься на слух. Не используй таблицы без необходимости."
            ),
        }
    ]

    while True:
        try:
            user_text = input("Александр: ").strip()
        except EOFError:
            return 0
        if not user_text:
            continue
        if user_text.lower() in {"выход", "выйти", "стоп", "/exit", "/quit"}:
            speech.say_and_wait("Диалог завершён.")
            return 0

        messages.append({"role": "user", "content": user_text})
        print("Ксения: ", end="", flush=True)
        chunker = SentenceChunker()
        answer_parts: list[str] = []
        try:
            with SingleInstance(settings.root, "agent-task") as acquired:
                if not acquired:
                    raise ChatError(
                        "Ксения уже выполняет другую задачу. Дождитесь сообщения «Готово»."
                    )
                if not manager.is_current(settings.default_role):
                    print("Модель запускается. Это может занять некоторое время...")
                    speech.say_and_wait(
                        "Запускаю локальную модель. Это может занять около минуты."
                    )
                    manager.start(settings.default_role)
                for token in stream_chat(settings, messages):
                    print(token, end="", flush=True)
                    answer_parts.append(token)
                    for phrase in chunker.feed(token):
                        speech.say(phrase)
        except (ChatError, ModelManagerError) as exc:
            print(f"\nОшибка: {exc}")
            speech.say(spoken_agent_error(exc))
            messages.pop()
            continue

        tail = chunker.finish()
        if tail:
            speech.say(tail)
        answer = "".join(answer_parts).strip()
        print("\n")
        if answer:
            messages.append({"role": "assistant", "content": answer})


def _wake_test(settings, speech: SpeechAnnouncer) -> int:
    with SingleInstance(settings.root, "microphone") as acquired:
        if not acquired:
            speech.say_and_wait(
                "Микрофон уже используется другим режимом Ксении. "
                "Сначала завершите голосовой разговор."
            )
            return 1
        return _wake_test_active(settings, speech)


def _wake_test_active(settings, speech: SpeechAnnouncer) -> int:
    phrase = str(settings.raw.get("voice", {}).get("wake_word", "Ксения слушай"))
    print(f"Жду фразу: «{phrase}». Для отмены закройте окно.")
    speech.say_and_wait("Проверка активации готова. После сигнала произнесите фразу.")
    try:
        heard = WakeListener(settings).wait_once()
    except WakeListenerError as exc:
        print(f"Не удалось запустить слушатель: {exc}")
        speech.say_and_wait(_spoken_microphone_error(exc))
        return 1
    print(f"Фраза обнаружена: {heard}")
    speech.say_and_wait("Слушаю.")
    return 0


def _dictation_test(settings, speech: SpeechAnnouncer) -> int:
    with SingleInstance(settings.root, "microphone") as acquired:
        if not acquired:
            speech.say_and_wait(
                "Микрофон уже используется другим режимом Ксении. "
                "Сначала завершите голосовой разговор."
            )
            return 1
        return _dictation_test_active(settings, speech)


def _dictation_test_active(settings, speech: SpeechAnnouncer) -> int:
    recognizer = SpeechRecognizer(settings)
    print("Подготавливаю качественное распознавание речи. Пока не говорите.")
    speech.say_and_wait("Подготавливаю распознавание речи. Пока не говорите.")
    recognizer.prepare()
    print("Скажите одну фразу после слова «Слушаю», затем помолчите.")
    try:
        event = recognizer.listen_after_prompt(lambda: speech.say_and_wait("Слушаю."))
    except SpeechRecognitionError as exc:
        print(f"Ошибка микрофона: {exc}")
        speech.say_and_wait(_spoken_microphone_error(exc))
        return 1
    text = str(event.get("text", ""))
    device = str(event.get("device", ""))
    host_api = str(event.get("host_api", ""))
    capture_rate = event.get("capture_rate", "")
    print(f"Распознано: {text}")
    print(f"Микрофон: {device}; {host_api}; {capture_rate} Гц")
    print(
        "Время: запись "
        f"{event.get('capture_seconds', '?')} с; распознавание "
        f"{event.get('recognition_seconds', '?')} с"
    )
    speech.say_and_wait(f"Я услышала: {text}")
    return 0


def _audio_devices(
    settings,
    speech: SpeechAnnouncer,
    *,
    select: str | None = None,
    clear: bool = False,
    interactive: bool = False,
) -> int:
    if clear:
        set_user_microphone(settings.root, "")
        print(
            "Сохранённый выбор микрофона удалён. "
            "Будет использован вход Windows по умолчанию."
        )
        speech.say_and_wait(
            "Сохранённый выбор микрофона удалён. Будет использован вход по умолчанию."
        )
        return 0
    devices = SpeechRecognizer(settings).list_devices()
    print("\n=== Доступные микрофоны ===")
    if not devices:
        print("Входные устройства не найдены.")
        speech.say_and_wait("Входные микрофоны не найдены.")
        return 1
    for device in devices:
        marker = "ПО УМОЛЧАНИЮ" if device.get("default") else ""
        print(
            f"{device.get('index')}: {device.get('name')} — {device.get('host_api')}, "
            f"{device.get('sample_rate')} Гц {marker}"
        )
    if interactive and select is None:
        speech.say_and_wait(
            "Введите уникальную часть названия нужного микрофона. "
            "Чтобы ничего не менять, нажмите Enter. "
            "Чтобы вернуться к входу по умолчанию, введите дефис."
        )
        selected_text = input(
            "Уникальная часть названия; Enter — не менять; - — вход по умолчанию: "
        ).strip()
        if selected_text == "-":
            set_user_microphone(settings.root, "")
            print("Сохранённый выбор удалён. Будет использован вход Windows по умолчанию.")
            speech.say_and_wait("Сохранённый выбор микрофона удалён.")
            return 0
        if selected_text:
            select = selected_text
    if select is not None:
        selector = str(select).strip()
        if not selector:
            print("ОШИБКА: селектор микрофона не может быть пустым.")
            speech.say_and_wait("Название микрофона не может быть пустым.")
            return 1
        matches = [
            device
            for device in devices
            if selector.casefold() in str(device.get("name", "")).casefold()
        ]
        if not matches:
            print(f"ОШИБКА: микрофон, содержащий «{selector}», не найден.")
            speech.say_and_wait("Указанный микрофон не найден.")
            return 1
        distinct_names = {
            str(device.get("name", "")).strip().casefold() for device in matches
        }
        if len(distinct_names) > 1:
            print(
                "ОШИБКА: селектор неоднозначен. Уточните название так, чтобы он "
                "соответствовал одному микрофону:"
            )
            for name in sorted({str(device.get("name", "")) for device in matches}):
                print(f"  - {name}")
            speech.say_and_wait("Название микрофона неоднозначно. Уточните его.")
            return 1
        set_user_microphone(settings.root, selector)
        api_names = sorted(
            {str(device.get("host_api", "")) for device in matches if device.get("host_api")}
        )
        print(
            f"Выбран микрофон: {next(iter({str(device.get('name', '')) for device in matches}))}."
        )
        if api_names:
            print("Доступные пути захвата: " + ", ".join(api_names))
        print("Выбор сохранён атомарно в config/user.json.")
        speech.say_and_wait(f"Микрофон {_spoken_device_name(selector)} выбран.")
        return 0
    selection_required = len(devices) > 1 and not any(
        bool(device.get("default")) for device in devices
    )
    if selection_required:
        print(
            "ВНИМАНИЕ: Windows не выбрала микрофон по умолчанию. "
            "Укажите имя устройства в voice.wake_device; Ксения не станет "
            "открывать произвольный вход."
        )
    spoken_devices = []
    for device in devices[:8]:
        description = _spoken_device_name(device.get("name"))
        if device.get("default"):
            description += ", используется по умолчанию"
        spoken_devices.append(description)
    spoken_warning = (
        " Windows не выбрала микрофон по умолчанию. Укажите нужное устройство "
        "в настройке voice wake device."
        if selection_required
        else ""
    )
    speech.say_and_wait(
        f"Найдено микрофонов: {len(devices)}. "
        + "; ".join(spoken_devices)
        + "."
        + spoken_warning
    )
    return 0


def _show_models(
    settings, speech: SpeechAnnouncer, *, models: list[Path] | None = None
) -> list[Path]:
    models = find_models(settings) if models is None else models
    print("\n=== Найденные GGUF-модели ===")
    if not models:
        print("Модели не найдены в настроенных каталогах.")
        speech.say_and_wait("Джи джи ю эф модели не найдены.")
        return []
    for index, path in enumerate(models, 1):
        size_gb = path.stat().st_size / (1024**3)
        print(f"{index}. {path.name} — {size_gb:.1f} ГБ — {path.parent}")
    spoken_models = []
    spoken_limit = 12
    for index, path in enumerate(models[:spoken_limit], 1):
        size_gb = path.stat().st_size / (1024**3)
        spoken_models.append(f"номер {index}, {path.stem}, {size_gb:.1f} гигабайта")
    remainder = len(models) - len(spoken_models)
    tail = f" Ещё моделей: {remainder}." if remainder > 0 else ""
    speech.say_and_wait(
        f"Найдено моделей: {len(models)}. " + "; ".join(spoken_models) + "." + tail
    )
    return models


def _configure_capability_model(settings, speech: SpeechAnnouncer) -> int:
    capabilities = settings.capability_role_names()
    profiles = settings.model_roles()
    if not capabilities or not profiles:
        print("В конфигурации нет функциональных ролей или модельных профилей.")
        return 1

    print("\n=== Назначение модельного профиля ===")
    for index, name in enumerate(capabilities, 1):
        role = settings.capability_role(name)
        current = role.primary_model or "не назначен"
        print(f"{index}. {role.label} — сейчас: {current}")
    raw_capability = input("Номер функциональной роли или Enter для отмены: ").strip()
    if not raw_capability:
        return 0
    try:
        capability = capabilities[int(raw_capability) - 1]
    except (ValueError, IndexError):
        print("Такого номера нет. Настройка не изменена.")
        return 1

    for index, profile_name in enumerate(profiles, 1):
        profile = settings.model(profile_name)
        state = "включён" if profile.enabled else "выключен"
        print(f"{index}. {profile.label} — {state}")
    raw_profile = input("Номер настроенного профиля или Enter для отмены: ").strip()
    if not raw_profile:
        return 0
    try:
        profile_name = profiles[int(raw_profile) - 1]
        profile = settings.model(profile_name)
    except (ValueError, IndexError, ConfigError):
        print("Такого профиля нет. Настройка не изменена.")
        return 1
    if not profile.enabled:
        print("Выключенный или экспериментальный профиль сначала должен пройти приёмку.")
        return 1
    answer = input(
        f"Назначить «{profile.label}» выбранной роли? Введите ДА: "
    ).strip().casefold()
    if answer != "да":
        print("Настройка отменена.")
        return 0
    set_user_capability_model(settings.root, capability, profile_name)
    print(f"Профиль назначен: {profile.label}")
    speech.say_and_wait("Модельный профиль назначен. Он применится к следующей задаче.")
    return 0


def _configure_reasoning(settings, speech: SpeechAnnouncer) -> int:
    print("\n=== Режим рассуждений ===")
    profiles = settings.model_roles()
    for index, role in enumerate(profiles, 1):
        profile = settings.model(role)
        print(f"{index}. {profile.label}: {reasoning_label(profile.reasoning)}")
    print(f"{len(profiles) + 1}. Изменить для всех профилей")
    raw_role = input("Номер профиля или Enter для отмены: ").strip()
    if not raw_role:
        return 0
    try:
        selected_index = int(raw_role) - 1
    except ValueError:
        selected_index = -1
    if selected_index == len(profiles):
        selected_roles = profiles
    elif 0 <= selected_index < len(profiles):
        selected_roles = (profiles[selected_index],)
    else:
        print("Такого номера нет.")
        return 1

    levels = {
        "0": "off",
        "1": "brief",
        "2": "normal",
        "3": "deep",
    }
    print("0. Выключено — самый быстрый и устойчивый режим.")
    print("1. Кратко — до 256 токенов рассуждений.")
    print("2. Обычно — до 768 токенов рассуждений.")
    print("3. Глубоко — до 1536 токенов; может заметно замедлить ответ.")
    raw_level = input("Уровень: 0, 1, 2, 3 или Enter для отмены: ").strip()
    if not raw_level:
        return 0
    level = levels.get(raw_level)
    if level is None:
        print("Такого уровня нет.")
        return 1

    for role in selected_roles:
        set_user_reasoning(settings.root, role, level)
    label = reasoning_label(level)
    role_names = " и ".join(selected_roles)
    print(f"Режим «{label}» сохранён для: {role_names}.")
    speech.say_and_wait(
        f"Режим рассуждений: {label}. Он применится при следующем запуске модели."
    )
    return 0


def _configure_response_budget(settings, speech: SpeechAnnouncer) -> int:
    current = int(settings.raw.get("generation", {}).get("max_tokens", 4096))
    print("\n=== Длина итогового ответа ===")
    print(f"Сейчас: {response_budget_label(current)}, до {current} токенов.")
    print("1. Коротко — до 1024 токенов; план до 2048.")
    print("2. Обычно — до 4096 токенов; план до 4096.")
    print("3. Подробно — до 8192 токенов; план до 8192, ожидание заметно больше.")
    choice = input("Выберите 1, 2, 3 или Enter для отмены: ").strip()
    values = {"1": 1024, "2": 4096, "3": 8192}
    if not choice:
        return 0
    selected = values.get(choice)
    if selected is None:
        print("Такого номера нет.")
        return 1
    set_user_response_budget(settings.root, selected)
    label = response_budget_label(selected)
    print(f"Длина ответа: {label}, до {selected} токенов.")
    speech.say_and_wait(f"Длина ответа: {label}. Настройка применена.")
    return 0


def _headset_controls_test(settings, speech: SpeechAnnouncer) -> int:
    listener = MediaButtonListener(settings, consume=False, debounce_ms=700)
    if not listener.start():
        message = listener.error or "Windows не разрешила проверку кнопок наушников."
        print(message)
        speech.say_and_wait(message)
        return 1
    try:
        message = (
            "Проверка управления наушниками началась. В течение тридцати секунд "
            "нажмите выбранный жест на сенсорной панели один раз."
        )
        print(message, flush=True)
        speech.say_and_wait(message)
        event = listener.wait(timeout=30)
    finally:
        listener.stop()
    if event is None:
        message = (
            "Windows не получила мультимедийную кнопку. Настройте жест в приложении "
            "JBL Headphones как воспроизведение или пауза и повторите проверку."
        )
        print(message)
        speech.say_and_wait(message)
        return 2
    label = BUTTON_LABELS.get(event.name, event.name)
    if event.name not in SUITABLE_ACTIVATION_BUTTONS:
        message = (
            f"Кнопка распознана как {label}, но она не подходит для запуска диалога. "
            "Назначьте жест воспроизведения или паузы и повторите проверку."
        )
        print(message)
        speech.say_and_wait(message)
        return 3
    set_user_headset_control(settings.root, event.name, enabled=True, consume=True)
    message = (
        f"Кнопка распознана как {label}. Управление Ксенией с наушников включено. "
        "Перезапустите голосовой режим. После этого этот жест заменит фразу активации."
    )
    print(message)
    speech.say_and_wait(message)
    return 0


def _voice_agent(settings, speech: SpeechAnnouncer) -> int:
    with SingleInstance(settings.root, "microphone") as acquired:
        if not acquired:
            speech.say_and_wait(
                "Голосовой режим Ксении уже запущен и использует микрофон."
            )
            return 0
        state_path = settings.runtime_dir / "voice" / "agent.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "pid": os.getpid(),
            "executable": str(current_process_image_path()),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "voice-agent",
        }
        atomic_write_text(
            state_path, json.dumps(state, ensure_ascii=False, indent=2)
        )
        diagnostic_event(
            settings,
            "voice_agent",
            "started",
            executable=state["executable"],
        )
        try:
            return _voice_agent_active(settings, speech)
        finally:
            diagnostic_event(settings, "voice_agent", "stopped")
            try:
                current = json.loads(state_path.read_text(encoding="utf-8"))
                if int(current.get("pid", 0)) == os.getpid():
                    state_path.unlink(missing_ok=True)
            except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
                pass


def _voice_agent_active(settings, speech: SpeechAnnouncer) -> int:
    session = RoutedAgentSession(settings)
    task_store = DurableTaskStore(settings.runtime_dir)
    capture_service = AudioCaptureService(settings)
    capture_endpoint = None
    try:
        capture_endpoint = capture_service.start()
    except AudioCaptureServiceError as exc:
        diagnostic_exception(
            settings,
            "voice_agent",
            "shared_capture_unavailable",
            exc,
        )
    recognizer = SpeechRecognizer(settings, capture_endpoint)
    wake_listener = WakeListener(settings, capture_endpoint)
    live_config = settings.raw.get("live", {})
    live_enabled = bool(live_config.get("enabled", False))
    live_minimum_phrase_chars = max(
        1, int(live_config.get("minimum_phrase_chars", 24))
    )
    live_maximum_phrase_chars = max(
        live_minimum_phrase_chars,
        int(live_config.get("maximum_phrase_chars", 220)),
    )
    live_playback_timeout = max(
        30.0, float(live_config.get("playback_timeout_seconds", 600))
    )
    if live_enabled and not speech.live_available():
        diagnostic_event(
            settings,
            "voice_agent",
            "live_unavailable",
            level="warning",
            reason="ordered_silero_queue_unavailable",
        )
        message = (
            "Потоковый режим отключён: постоянная очередь Silero не готова. "
            "Продолжаю в обычном голосовом режиме."
        )
        print(message)
        speech.say_and_wait(message)
        live_enabled = False
    controls = settings.raw.get("headset_controls", {})
    headset_listener = None
    activation_button = str(controls.get("activation_button", "play_pause"))
    if bool(controls.get("enabled", False)):
        headset_listener = MediaButtonListener(
            settings,
            buttons={activation_button},
            consume=bool(controls.get("consume", True)),
            debounce_ms=int(controls.get("debounce_ms", 700)),
        )
        if not headset_listener.start():
            diagnostic_event(
                settings,
                "voice_agent",
                "headset_controls_unavailable",
                level="warning",
                detail=headset_listener.error,
            )
            headset_listener = None
    print("Подготавливаю качественное распознавание речи. Пока не говорите.")
    diagnostic_event(settings, "voice_agent", "recognizer_prepare_started")
    speech.say_and_wait("Подготавливаю распознавание речи. Пока не говорите.")
    prepare_started = time.monotonic()
    recognizer_info = recognizer.prepare()
    recognition_device = str(recognizer_info.get("device", "cpu"))
    recognition_engine = str(recognizer_info.get("engine", "распознаватель"))
    print(
        f"Распознавание готово: {recognition_engine}, устройство {recognition_device}.",
        flush=True,
    )
    diagnostic_event(
        settings,
        "voice_agent",
        "recognizer_ready",
        duration_ms=round((time.monotonic() - prepare_started) * 1000),
        engine=recognition_engine,
        device=recognition_device,
        compute_type=recognizer_info.get("compute_type", ""),
    )
    device_phrase = "на видеокарте" if recognition_device == "cuda" else "на процессоре"
    speech.say_and_wait(
        f"Голосовой режим готов. Распознавание работает {device_phrase}. "
        + (
            "Потоковое озвучивание включено. "
            if live_enabled
            else ""
        )
        + (
            "Нажмите настроенную кнопку наушников или скажите: Ксения слушай."
            if headset_listener is not None
            else "Для команды скажите: Ксения слушай."
        )
    )
    voice_config = settings.raw.get("voice", {})
    microphone_failures = RepeatingFailurePolicy(
        base_delay_seconds=float(
            voice_config.get("microphone_retry_base_seconds", 5.0)
        ),
        max_delay_seconds=float(
            voice_config.get("microphone_retry_max_seconds", 60.0)
        ),
        reminder_seconds=float(
            voice_config.get("microphone_error_reminder_seconds", 300.0)
        ),
    )
    while True:
        print("Ожидаю фразу «Ксения слушай»...")
        try:
            wake_event = wake_listener.wait_event(
                timeout=300,
                external_events=(
                    headset_listener.events if headset_listener is not None else None
                ),
            )
            diagnostic_event(
                settings,
                "voice_agent",
                "wake_received",
                wake_event=wake_event.get("event", ""),
                device=wake_event.get("device", ""),
                host_api=wake_event.get("host_api", ""),
            )
            microphone_failures.reset()
            if wake_event.get("event") == "stop":
                speech.stop()
                print("[Озвучивание остановлено]")
                continue
            if wake_event.get("event") == "headset":
                print(
                    f"[Активация кнопкой наушников: {wake_event.get('button', '')}]",
                    flush=True,
                )
            speech.stop()
            trace_id = new_trace_id()
            turn_id = new_trace_id()
            with trace_scope(trace_id=trace_id, turn_id=turn_id):
                event = recognizer.listen_after_prompt(
                    lambda: speech.say_and_wait("Слушаю.")
                )
        except WakeListenerTimeout:
            # The wake worker is periodically reopened so a transient Bluetooth
            # problem can recover. An idle window is normal and must stay silent.
            continue
        except WakeListenerError as exc:
            print(f"Ошибка микрофона: {exc}")
            decision = microphone_failures.record_failure(time.monotonic())
            diagnostic_event(
                settings,
                "voice_agent",
                "microphone_retry_scheduled",
                level="warning",
                failure_count=decision.failure_count,
                delay_seconds=decision.delay_seconds,
                announcement=decision.announce,
            )
            if decision.announce:
                speech.say_and_wait(
                    _spoken_microphone_error(exc)
                    + " Я продолжу проверять подключение молча."
                )
            if decision.delay_seconds:
                time.sleep(decision.delay_seconds)
            continue
        except SpeechRecognitionError as exc:
            print(f"Ошибка микрофона: {exc}")
            speech.say_and_wait(_spoken_microphone_error(exc))
            continue

        user_text = str(event.get("text", "")).strip()
        diagnostic_event(
            settings,
            "voice_agent",
            "command_recognized",
            trace_id=trace_id,
            turn_id=turn_id,
            transcript=user_text,
            engine=event.get("engine", ""),
            device=event.get("model_device", ""),
            capture_seconds=event.get("capture_seconds", 0),
            recognition_seconds=event.get("recognition_seconds", 0),
        )
        print(
            "[Распознавание: "
            f"{event.get('engine', 'неизвестно')}, {event.get('model_device', 'cpu')}; "
            f"запись {event.get('capture_seconds', '?')} с; "
            f"обработка {event.get('recognition_seconds', '?')} с]",
            flush=True,
        )
        normalized_voice = re.sub(r"[^а-яa-z0-9]+", " ", user_text.casefold()).strip()
        if normalized_voice in {"ксения слушай", "ксения"}:
            print("Распознана только повторная фраза активации; задача не отправлена модели.")
            try:
                with trace_scope(trace_id=trace_id, turn_id=turn_id):
                    event = recognizer.listen_after_prompt(
                        lambda: speech.say_and_wait(
                            "Я очистила старую запись. Теперь скажите саму задачу."
                        )
                    )
            except SpeechRecognitionError as exc:
                print(f"Ошибка микрофона: {exc}")
                speech.say_and_wait(_spoken_microphone_error(exc))
                continue
            user_text = str(event.get("text", "")).strip()
            normalized_voice = re.sub(
                r"[^а-яa-z0-9]+", " ", user_text.casefold()
            ).strip()
            if normalized_voice in {"ксения слушай", "ксения"}:
                speech.say_and_wait(
                    "Снова получена только фраза активации. Проверьте микрофон отдельным ярлыком."
                )
                continue
        if normalized_voice in {"ксения стоп", "стоп"}:
            speech.stop()
            print("[Озвучивание остановлено]")
            continue
        print(f"Александр: {user_text}")
        if user_text.lower() in {
            "выход",
            "выйти",
            "заверши работу",
            "завершить работу",
            "стоп диалог",
        }:
            if headset_listener is not None:
                headset_listener.stop()
            recognizer.close()
            capture_service.close()
            speech.say_and_wait("Голосовой диалог завершён.")
            return 0

        trusted_task_used = False
        live_stream_started = threading.Event()
        voice_output_lock = threading.Lock()
        microphone_gate = MicrophoneCaptureGate()
        confirmation_handoff_timeout = float(
            settings.raw.get("voice", {}).get(
                "confirmation_microphone_handoff_timeout_seconds", 5.0
            )
        )

        def report_status(status: str) -> None:
            nonlocal trusted_task_used
            if status == TRUSTED_TASK_STARTED:
                trusted_task_used = True
            print(f"[{status}]", flush=True)
            task_store.transition(
                task.id, _runtime_task_state(status), status, confirmation=None
            )
            with voice_output_lock:
                if live_enabled and live_stream_started.is_set():
                    return
                if status == TRUSTED_TASK_STARTED:
                    # The task must not race ahead before a blind user hears that
                    # repeat confirmations are disabled for this request.
                    speech.say_and_wait(status)
                else:
                    speech.say(status)

        def confirm_action(name: str, arguments: dict, _reason: str) -> bool:
            prompt = _confirmation_text(name, arguments)
            task_store.transition(
                task.id,
                TaskState.WAITING_CONFIRMATION,
                "Ожидаю подтверждение",
                confirmation={"tool": name, "message": prompt},
            )
            print(f"[ПОДТВЕРЖДЕНИЕ] {prompt} Скажите «да» или «нет».")
            approved = False
            try:
                with microphone_gate.exclusive_capture(confirmation_handoff_timeout):
                    answer = str(
                        recognizer.listen_after_prompt(
                            lambda: speech.say_and_wait(
                                prompt + " Скажите да или нет."
                            )
                        ).get("text", "")
                    ).strip().casefold()
                approved = answer in {
                    "да",
                    "подтверждаю",
                    "разрешаю",
                    "выполняй",
                    "согласен",
                }
            except (SpeechRecognitionError, TimeoutError):
                speech.say_and_wait(
                    "Подтверждение не распознано. Действие отменено."
                )
            finally:
                task_store.transition(
                    task.id, TaskState.RUNNING, "Продолжаю", confirmation=None
                )
            speech.say_and_wait("Подтверждено." if approved else "Действие отменено.")
            return approved

        task = task_store.create(user_text, channel="voice")
        task_started = time.monotonic()
        diagnostic_event(
            settings,
            "voice_agent",
            "task_started",
            task_id=task.id,
            trace_id=trace_id,
            turn_id=turn_id,
            request=user_text,
        )
        control = TaskControl(
            task_store,
            task.id,
            trace_id=trace_id,
            turn_id=turn_id,
        )
        task_store.transition(task.id, TaskState.RUNNING, "Начинаю")
        task_finished = threading.Event()
        agent_finished = threading.Event()
        stop_monitor_cancel = microphone_gate.monitor_cancel_event(task_finished)
        live_output = (
            ConversationCoordinator(
                speech,
                minimum_phrase_chars=live_minimum_phrase_chars,
                maximum_phrase_chars=live_maximum_phrase_chars,
            )
            if live_enabled
            else None
        )
        live_token = live_output.begin_response(user_text) if live_output else None

        def monitor_voice_stop() -> None:
            stop_listener = WakeListener(settings, capture_endpoint)
            while not task_finished.is_set():
                if not microphone_gate.monitor_checkpoint(task_finished):
                    return
                try:
                    stop_event = stop_listener.wait_event(
                        timeout=300,
                        external_events=(
                            headset_listener.events
                            if headset_listener is not None
                            else None
                        ),
                        cancel_event=stop_monitor_cancel,
                    )
                except WakeListenerCancelled:
                    if not microphone_gate.monitor_checkpoint(task_finished):
                        return
                    continue
                except WakeListenerTimeout:
                    continue
                except WakeListenerError as exc:
                    diagnostic_exception(
                        settings,
                        "voice_agent",
                        "stop_monitor_failed",
                        exc,
                        task_id=task.id,
                    )
                    task_finished.wait(15)
                    continue
                event_name = str(stop_event.get("event", ""))
                if event_name not in {"stop", "headset"}:
                    diagnostic_event(
                        settings,
                        "voice_agent",
                        "activation_ignored_while_busy",
                        task_id=task.id,
                        wake_event=event_name,
                    )
                    continue
                diagnostic_milestone(
                    settings,
                    "interrupt_detected",
                    task_id=task.id,
                    source=event_name,
                )
                response_only = live_output is not None and agent_finished.is_set()
                if not response_only:
                    try:
                        task_store.cancel(task.id)
                    except (KeyError, OSError, ValueError):
                        return
                if live_output is not None:
                    live_output.interrupt(f"voice_{event_name}")
                else:
                    speech.stop()
                diagnostic_event(
                    settings,
                    "voice_agent",
                    (
                        "response_interrupted_by_voice_or_headset"
                        if response_only
                        else "task_cancelled_by_voice_or_headset"
                    ),
                    task_id=task.id,
                    wake_event=event_name,
                )
                return

        def monitor_voice_stop_traced() -> None:
            with trace_scope(
                trace_id=trace_id,
                turn_id=turn_id,
                task_id=task.id,
            ):
                monitor_voice_stop()

        stop_monitor = threading.Thread(
            target=monitor_voice_stop_traced,
            daemon=True,
        )
        stop_monitor.start()
        live_snapshot = None

        def accept_final_delta(delta: str) -> None:
            if live_output is None or live_token is None:
                return
            with voice_output_lock:
                if not live_stream_started.is_set():
                    live_stream_started.set()
                    # The stop command reaches the persistent worker before the
                    # first final phrase and flushes obsolete progress messages.
                    speech.stop()
                live_output.accept_delta(live_token, delta)

        try:
            with trace_scope(
                trace_id=trace_id,
                turn_id=turn_id,
                task_id=task.id,
            ):
                reply = session.ask(
                    user_text,
                    on_status=report_status,
                    on_confirmation=confirm_action,
                    max_steps=settings.developer_max_steps,
                    control=control,
                    on_final_delta=accept_final_delta if live_enabled else None,
                )
            agent_finished.set()
            if live_output is not None and live_token is not None:
                with trace_scope(
                    trace_id=trace_id,
                    turn_id=turn_id,
                    task_id=task.id,
                ):
                    live_output.finish_response(live_token)
                if not live_output.session.wait_until_settled(
                    live_token.turn_id,
                    timeout=live_playback_timeout,
                ):
                    with trace_scope(
                        trace_id=trace_id,
                        turn_id=turn_id,
                        task_id=task.id,
                    ):
                        live_output.interrupt("playback_timeout")
                    diagnostic_event(
                        settings,
                        "voice_agent",
                        "live_playback_timeout",
                        level="warning",
                        task_id=task.id,
                        timeout_seconds=live_playback_timeout,
                    )
                live_snapshot = live_output.snapshot()
                if not session.commit_spoken_reply(
                    live_snapshot.generated_text,
                    live_snapshot.spoken_text,
                ):
                    diagnostic_event(
                        settings,
                        "voice_agent",
                        "live_memory_commit_refused",
                        level="error",
                        task_id=task.id,
                        generated_chars=len(live_snapshot.generated_text),
                        spoken_chars=len(live_snapshot.spoken_text),
                    )
                    live_enabled = False
                    with trace_scope(
                        trace_id=trace_id,
                        turn_id=turn_id,
                        task_id=task.id,
                    ):
                        speech.say_and_wait(
                            "Потоковый режим безопасно выключен: память ответа не совпала. "
                            "Обычный голосовой режим продолжит работу."
                        )
        except TaskCancelled:
            if live_output is not None:
                with trace_scope(
                    trace_id=trace_id,
                    turn_id=turn_id,
                    task_id=task.id,
                ):
                    live_snapshot = live_output.interrupt("task_cancelled")
                session.commit_spoken_reply(
                    live_snapshot.generated_text,
                    live_snapshot.spoken_text,
                )
                try:
                    task_store.transition(
                        task.id,
                        TaskState.CANCELLED,
                        "Отменено",
                        generated_answer=live_snapshot.generated_text,
                        spoken_answer=live_snapshot.spoken_text,
                        confirmation=None,
                        resumable=False,
                    )
                except (KeyError, OSError, ValueError):
                    pass
            diagnostic_event(
                settings,
                "voice_agent",
                "task_cancelled",
                task_id=task.id,
                trace_id=trace_id,
                turn_id=turn_id,
                duration_ms=round((time.monotonic() - task_started) * 1000),
            )
            print("Задача отменена.")
            with trace_scope(
                trace_id=trace_id,
                turn_id=turn_id,
                task_id=task.id,
            ):
                speech.say_and_wait("Задача отменена.")
            continue
        except ChatError as exc:
            if live_output is not None:
                with trace_scope(
                    trace_id=trace_id,
                    turn_id=turn_id,
                    task_id=task.id,
                ):
                    live_snapshot = live_output.interrupt("agent_error")
                session.commit_spoken_reply(
                    live_snapshot.generated_text,
                    live_snapshot.spoken_text,
                )
            diagnostic_exception(
                settings,
                "voice_agent",
                "task_failed",
                exc,
                task_id=task.id,
                trace_id=trace_id,
                turn_id=turn_id,
                duration_ms=round((time.monotonic() - task_started) * 1000),
            )
            task_store.transition(
                task.id,
                TaskState.FAILED,
                "Ошибка",
                generated_answer=(
                    live_snapshot.generated_text if live_snapshot is not None else None
                ),
                spoken_answer=(
                    live_snapshot.spoken_text if live_snapshot is not None else None
                ),
                error=str(exc),
                resumable=True,
            )
            print(f"Ошибка агента: {exc}")
            with trace_scope(
                trace_id=trace_id,
                turn_id=turn_id,
                task_id=task.id,
            ):
                speech.say_and_wait(_spoken_agent_error(exc))
            continue
        finally:
            task_finished.set()
            stop_monitor.join(timeout=4)
        task_store.transition(
            task.id,
            TaskState.COMPLETED,
            "Готово",
            answer=reply.text,
            generated_answer=(
                live_snapshot.generated_text if live_snapshot is not None else None
            ),
            spoken_answer=(
                live_snapshot.spoken_text if live_snapshot is not None else None
            ),
            confirmation=None,
            resumable=False,
        )
        diagnostic_event(
            settings,
            "voice_agent",
            "task_completed",
            task_id=task.id,
            trace_id=trace_id,
            turn_id=turn_id,
            duration_ms=round((time.monotonic() - task_started) * 1000),
            answer=reply.text,
        )
        print(f"Ксения: {reply.text}\n")
        if live_output is not None and trusted_task_used:
            with trace_scope(
                trace_id=trace_id,
                turn_id=turn_id,
                task_id=task.id,
            ):
                speech.say_and_wait(TRUSTED_TASK_FINISHED)
        elif live_output is None:
            # The final answer is more important than queued progress messages.
            speech.stop()
            if trusted_task_used:
                speech.say(TRUSTED_TASK_FINISHED)
            with trace_scope(
                trace_id=trace_id,
                turn_id=turn_id,
                task_id=task.id,
            ):
                speech.say(reply.text)


def _agent_chat(settings, speech: SpeechAnnouncer) -> int:
    session = RoutedAgentSession(settings)
    task_store = DurableTaskStore(settings.runtime_dir)
    print(
        "\n=== Агентный диалог ===\n"
        "Доступны безопасные инструменты состояния, списка и чтения файлов.\n"
        "Для выхода напишите: выход\n"
    )
    speech.say("Агентный диалог готов.")
    while True:
        try:
            user_text = input("Александр: ").strip()
        except EOFError:
            return 0
        if not user_text:
            continue
        if user_text.lower() in {"выход", "выйти", "/exit", "/quit"}:
            speech.say_and_wait("Диалог завершён.")
            return 0
        try:
            def report_status(status: str) -> None:
                print(f"[{status}]", flush=True)
                task_store.transition(
                    task.id, _runtime_task_state(status), status, confirmation=None
                )
                speech.say(status)

            def confirm_action(name: str, arguments: dict, _reason: str) -> bool:
                prompt = _confirmation_text(name, arguments)
                task_store.transition(
                    task.id,
                    TaskState.WAITING_CONFIRMATION,
                    "Ожидаю подтверждение",
                    confirmation={"tool": name, "message": prompt},
                )
                print(f"\n[ПОДТВЕРЖДЕНИЕ] {prompt}")
                speech.say_and_wait(prompt)
                answer = input("Введите ДА для выполнения: ").strip().casefold()
                approved = answer in {"да", "yes", "y"}
                task_store.transition(
                    task.id, TaskState.RUNNING, "Продолжаю", confirmation=None
                )
                return approved

            trace_id = new_trace_id()
            task = task_store.create(user_text, channel="console")
            control = TaskControl(
                task_store,
                task.id,
                trace_id=trace_id,
                turn_id=task.id,
            )
            task_store.transition(task.id, TaskState.RUNNING, "Начинаю")
            with trace_scope(
                trace_id=trace_id,
                turn_id=task.id,
                task_id=task.id,
            ):
                reply = session.ask(
                    user_text,
                    on_status=report_status,
                    on_confirmation=confirm_action,
                    max_steps=settings.developer_max_steps,
                    control=control,
                )
            task_store.transition(
                task.id,
                TaskState.COMPLETED,
                "Готово",
                answer=reply.text,
                confirmation=None,
                resumable=False,
            )
            for event in reply.tool_events:
                print(f"[инструмент: {event.name} → {event.result.status}]")
            print(f"Ксения: {reply.text}\n")
            with trace_scope(
                trace_id=trace_id,
                turn_id=task.id,
                task_id=task.id,
            ):
                speech.say(reply.text)
        except TaskCancelled:
            print("Задача отменена.\n")
            speech.say("Задача отменена.")
        except ChatError as exc:
            if "task" in locals():
                task_store.transition(
                    task.id, TaskState.FAILED, "Ошибка", error=str(exc), resumable=True
                )
            print(f"Ошибка агента: {exc}")
            speech.say(_spoken_agent_error(exc))


def _menu(settings, speech: SpeechAnnouncer) -> int:
    manager = ModelManager(settings)
    speech.say_and_wait(
        f"{settings.assistant_name} готова к управлению. "
        "Можно ввести: статус, диалог, микрофон, модели, сеть или выход."
    )
    while True:
        print(
            "\n=== Локальный дворецкий ===\n"
            "1. Состояние системы\n"
            "2. Запустить основную модель\n"
            "3. Переключиться на модель сложного планирования\n"
            "4. Остановить модель\n"
            "5. Проверить голос\n"
            "6. Агентный диалог\n"
            "7. Первая настройка путей\n"
            "8. Проверить фразу «Ксения слушай»\n"
            "9. Открыть панель в локальной сети\n"
            "10. Проверить диктовку с микрофона\n"
            "11. Голосовой режим с фразой активации\n"
            "12. Показать все микрофоны\n"
            "13. Показать найденные модели\n"
            "14. Назначить модельный профиль функциональной роли\n"
            "15. Настроить глубину рассуждений\n"
            "16. Настроить длину ответа\n"
            "17. Проверить управление с наушников\n"
            "18. Доверенная задача — один запрос без подтверждений\n"
            "0. Выход\n"
        )
        choice = input("Выберите действие: ").strip().lower()
        try:
            if choice in {"1", "статус", "состояние"}:
                _status(settings, speech)
            elif choice in {"2", "разработчик"}:
                profile = settings.capability_model("assistant")
                state = _model_control(settings, lambda: manager.start(profile))
                message = f"Основная модель запущена. PID {state.pid}."
                print(message)
                speech.say(message)
            elif choice in {"3", "планировщик"}:
                profile = settings.capability_model(
                    "heavy_brain", fallback="researcher"
                )
                state = _model_control(settings, lambda: manager.switch(profile))
                message = f"Модель сложного планирования запущена. PID {state.pid}."
                print(message)
                speech.say(message)
            elif choice in {"4", "стоп", "остановить"}:
                stopped = _model_control(settings, manager.stop)
                message = "Модель остановлена." if stopped else "Запущенная модель не найдена."
                print(message)
                speech.say(message)
            elif choice in {"5", "голос"}:
                print("Сейчас последовательно прозвучат три голоса. Выберите понравившийся.")
                speech.test_voices()
            elif choice in {"6", "диалог", "разговор"}:
                _agent_chat(settings, speech)
            elif choice in {"7", "настройка"}:
                _setup(settings)
                settings = load_settings(settings.root)
                manager = ModelManager(settings)
            elif choice in {"8", "активация", "слушать"}:
                _wake_test(settings, speech)
            elif choice in {"9", "сеть", "панель"}:
                lan_config = settings.raw.get("lan", {})
                run_lan_server(
                    settings,
                    speech,
                    host=str(lan_config.get("host", "auto")),
                    port=int(lan_config.get("port", 8765)),
                )
            elif choice in {"10", "микрофон", "диктовка"}:
                _dictation_test(settings, speech)
            elif choice in {"11", "голосовой режим", "голосовой диалог"}:
                _voice_agent(settings, speech)
            elif choice in {"12", "устройства", "список микрофонов"}:
                _audio_devices(settings, speech, interactive=True)
            elif choice in {"13", "модели", "список моделей"}:
                _show_models(settings, speech)
            elif choice in {"14", "выбрать модель", "настроить планировщик"}:
                _configure_capability_model(settings, speech)
                settings = load_settings(settings.root)
                manager = ModelManager(settings)
            elif choice in {"15", "рассуждения", "мышление"}:
                _configure_reasoning(settings, speech)
                settings = load_settings(settings.root)
                manager = ModelManager(settings)
            elif choice in {"16", "длина ответа", "лимит ответа"}:
                _configure_response_budget(settings, speech)
                settings = load_settings(settings.root)
                manager = ModelManager(settings)
            elif choice in {"17", "наушники", "кнопки наушников"}:
                _headset_controls_test(settings, speech)
                settings = load_settings(settings.root)
            elif choice in {"18", "доверие", "доверенная задача"}:
                _trusted_task_control(settings, speech)
            elif choice in {"0", "выход", "выйти"}:
                return 0
            else:
                print("Команда не распознана. Введите число от 0 до 18.")
                speech.say_and_wait("Команда не распознана. Введите номер или слово.")
        except (
            ModelManagerError,
            ConfigError,
            ChatError,
            WakeListenerError,
            SpeechRecognitionError,
        ) as exc:
            diagnostic_event(
                settings,
                "cli",
                "menu_action_failed",
                level="error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            print(f"Не удалось выполнить действие: {exc}")
            speech.say_and_wait(
                "Не удалось выполнить действие. Если ошибка повторяется, "
                "запустите ярлык Ксения — полный аудит."
            )
        except Exception as exc:
            log_path = _record_unexpected(settings, "menu", exc)
            detail = f" Журнал: {log_path}" if log_path else ""
            print(f"Произошла внутренняя ошибка, но меню продолжает работать.{detail}")
            speech.say(
                "Произошла внутренняя ошибка. Я сохранила подробности и продолжаю работать."
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="butler", description="Локальный дворецкий")
    parser.add_argument("--no-speech", action="store_true", help="не озвучивать состояние")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("menu")
    sub.add_parser("status")
    doctor = sub.add_parser("doctor")
    doctor.add_argument(
        "--installation-mode",
        action="store_true",
        help="проверить среду до отдельной установки больших моделей",
    )
    sub.add_parser("diagnostics")
    sub.add_parser("setup")
    sub.add_parser("voice-test")
    sub.add_parser("chat")
    sub.add_parser("agent")
    sub.add_parser("wake-test")
    sub.add_parser("dictation-test")
    sub.add_parser("voice-agent")
    audio_devices = sub.add_parser("audio-devices")
    selection = audio_devices.add_mutually_exclusive_group()
    selection.add_argument(
        "--select",
        metavar="NAME",
        help="сохранить устойчивый фрагмент имени микрофона",
    )
    selection.add_argument(
        "--clear",
        action="store_true",
        help="вернуться к входу Windows по умолчанию",
    )
    audio_devices.add_argument(
        "--interactive",
        action="store_true",
        help="предложить выбрать микрофон по устойчивой части имени",
    )
    sub.add_parser("headset-test")
    sub.add_parser("models")
    sub.add_parser("trust-next-task")
    lan = sub.add_parser("lan")
    lan.add_argument("--host", default=None)
    lan.add_argument("--port", type=int, default=None)
    lan.add_argument("--pin", default=None)
    start = sub.add_parser("start")
    start.add_argument("role", nargs="?", default=None)
    switch = sub.add_parser("switch")
    switch.add_argument("role")
    sub.add_parser("stop")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = None
    speech: SpeechAnnouncer | None = None
    try:
        settings = load_settings()
        diagnostic_event(
            settings,
            "cli",
            "process_started",
            argv_count=len(argv if argv is not None else sys.argv[1:]),
            python_executable=current_process_image_path(),
        )
        speech = SpeechAnnouncer(
            settings.root,
            enabled=settings.announce_status and not args.no_speech,
            voice_config=settings.raw.get("voice", {}),
            diagnostics_source=settings,
        )
        command = args.command or "menu"
        diagnostic_event(settings, "cli", "command_started", command_name=command)
        manager = ModelManager(settings)
        if command == "menu":
            return _menu(settings, speech)
        if command in {"status", "doctor"}:
            return _status(
                settings,
                speech,
                installation_mode=bool(
                    command == "doctor" and getattr(args, "installation_mode", False)
                ),
            )
        if command == "diagnostics":
            summary = summarize(settings.runtime_dir)
            print(format_summary(summary))
            return 1 if summary.invalid_line_count else 0
        if command == "setup":
            return _setup(settings)
        if command == "voice-test":
            print("Проверка трёх русских голосов запущена.")
            speech.test_voices()
            return 0
        if command == "chat":
            return _chat(settings, speech)
        if command == "agent":
            return _agent_chat(settings, speech)
        if command == "wake-test":
            return _wake_test(settings, speech)
        if command == "dictation-test":
            return _dictation_test(settings, speech)
        if command == "voice-agent":
            return _voice_agent(settings, speech)
        if command == "audio-devices":
            return _audio_devices(
                settings,
                speech,
                select=getattr(args, "select", None),
                clear=bool(getattr(args, "clear", False)),
                interactive=bool(getattr(args, "interactive", False)),
            )
        if command == "headset-test":
            return _headset_controls_test(settings, speech)
        if command == "models":
            _show_models(settings, speech)
            return 0
        if command == "trust-next-task":
            return _trusted_task_control(settings, speech)
        if command == "lan":
            lan_config = settings.raw.get("lan", {})
            run_lan_server(
                settings,
                speech,
                host=args.host or str(lan_config.get("host", "auto")),
                port=args.port or int(lan_config.get("port", 8765)),
                pin=args.pin,
            )
            return 0
        if command == "start":
            role = args.role or settings.default_role
            state = _model_control(settings, lambda: manager.start(role))
            print(f"Модель {role} запущена. PID {state.pid}.")
            return 0
        if command == "switch":
            state = _model_control(settings, lambda: manager.switch(args.role))
            print(f"Активная роль: {state.role}. PID {state.pid}.")
            return 0
        if command == "stop":
            stopped = _model_control(settings, manager.stop)
            print("Модель остановлена." if stopped else "Запущенная модель не найдена.")
            return 0
        parser.error(f"Неизвестная команда: {command}")
    except (ConfigError, ModelManagerError) as exc:
        diagnostic_event(
            settings if settings is not None else Path.cwd() / "runtime",
            "cli",
            "startup_configuration_failed",
            level="error",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        print(f"Ошибка: {exc}", file=sys.stderr)
        if speech is not None:
            speech.say_and_wait(
                "Ксения не смогла запуститься. Запустите ярлык Ксения — полный аудит."
            )
        else:
            _fallback_speak(
                "Конфигурация Ксении повреждена. Запустите ярлык Ксения — полный аудит."
            )
        return 2
    except Exception as exc:
        log_path = _record_unexpected(settings, "startup", exc)
        detail = f" Журнал: {log_path}" if log_path else ""
        print(f"Не удалось запустить Ксению.{detail}", file=sys.stderr)
        if speech is not None:
            speech.say_and_wait(
                "Произошла внутренняя ошибка. Подробности сохранены. "
                "Запустите ярлык Ксения — полный аудит."
            )
        else:
            _fallback_speak(
                "Ксения не смогла запуститься. Запустите ярлык Ксения — полный аудит."
            )
        return 3
    return 0
