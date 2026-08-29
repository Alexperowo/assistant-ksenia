from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from butler.atomic_io import atomic_write_text, exclusive_file_lock


class ConfigError(RuntimeError):
    pass


REASONING_LEVELS = {
    "off": ("выключено", "off", None),
    "brief": ("кратко", "on", 256),
    "normal": ("обычно", "on", 768),
    "deep": ("глубоко", "on", 1536),
}

RESPONSE_BUDGETS = {
    1024: ("коротко", 1024, 2048),
    4096: ("обычно", 1600, 4096),
    8192: ("подробно", 2048, 8192),
}


def reasoning_arguments(level: str) -> tuple[str, ...]:
    try:
        _label, mode, budget = REASONING_LEVELS[level]
    except KeyError as exc:
        available = ", ".join(REASONING_LEVELS)
        raise ConfigError(f"Неизвестный уровень рассуждений: {level}. Доступно: {available}.") from exc
    result = ["--reasoning", mode]
    if budget is not None:
        result.extend(["--reasoning-budget", str(budget)])
    return tuple(result)


def reasoning_label(level: str) -> str:
    try:
        return str(REASONING_LEVELS[level][0])
    except KeyError as exc:
        available = ", ".join(REASONING_LEVELS)
        raise ConfigError(f"Неизвестный уровень рассуждений: {level}. Доступно: {available}.") from exc


def response_budget_label(max_tokens: int) -> str:
    try:
        return str(RESPONSE_BUDGETS[int(max_tokens)][0])
    except (KeyError, TypeError, ValueError) as exc:
        available = ", ".join(str(value) for value in RESPONSE_BUDGETS)
        raise ConfigError(f"Неизвестный лимит ответа: {max_tokens}. Доступно: {available}.") from exc


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Не найден файл конфигурации: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Ошибка JSON в {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"Корень конфигурации должен быть объектом: {path}")
    return value


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


def _edit_user_settings(
    target: Path,
    editor: Callable[[dict[str, Any]], None],
) -> Path:
    with exclusive_file_lock(target):
        value = _read_json(target) if target.exists() else {}
        editor(value)
        _write_json_atomic(target, value)
    return target


def _resolve(root: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


@dataclass(frozen=True)
class EngineBackend:
    name: str
    executable: Path
    cors_controls: bool = True


@dataclass(frozen=True)
class ModelProfile:
    role: str
    label: str
    model_path: Path
    context_size: int
    gpu_layers: int | str
    enabled: bool
    experimental: bool
    extra_args: tuple[str, ...]
    reasoning: str
    expected_size_bytes: int = 0
    sha256: str = ""
    draft_model_path: Path | None = None
    draft_expected_size_bytes: int = 0
    draft_sha256: str = ""
    projector_path: Path | None = None
    projector_expected_size_bytes: int = 0
    projector_sha256: str = ""
    acceleration_type: str = "none"
    acceleration_max_tokens: int = 0
    draft_gpu_layers: int | str = 0
    backend: EngineBackend | None = None


@dataclass(frozen=True)
class CapabilityRole:
    name: str
    label: str
    purpose: str
    primary_model: str | None
    candidate_model: str | None
    enabled: bool


@dataclass(frozen=True)
class Settings:
    root: Path
    raw: dict[str, Any]
    llama_server: Path
    models_dir: Path
    runtime_dir: Path
    host: str
    port: int
    startup_timeout_seconds: int

    @property
    def assistant_name(self) -> str:
        return str(self.raw["assistant"]["name"])

    @property
    def announce_status(self) -> bool:
        return bool(self.raw["assistant"].get("announce_status", True))

    @property
    def agent_max_steps(self) -> int:
        return int(self.raw.get("agent", {}).get("max_steps", 8))

    @property
    def developer_max_steps(self) -> int:
        agent = self.raw.get("agent", {})
        return int(agent.get("developer_max_steps", agent.get("max_steps", 8)))

    @property
    def default_role(self) -> str:
        configured = str(self.raw["assistant"]["default_role"])
        if configured in self.capability_role_names():
            return self.capability_model(configured)
        if configured in self.model_roles():
            return configured
        raise ConfigError(f"Неизвестная роль по умолчанию: {configured}")

    @property
    def state_file(self) -> Path:
        return self.runtime_dir / "state.json"

    def engine_backend(self, name: str) -> EngineBackend:
        backends = self.raw.get("engine_backends")
        if backends is None:
            if name != "default":
                raise ConfigError(f"Неизвестный backend модели: {name}")
            return EngineBackend(name=name, executable=self.llama_server)
        if not isinstance(backends, Mapping):
            raise ConfigError("Раздел engine_backends должен быть объектом.")
        try:
            item = backends[name]
        except KeyError as exc:
            raise ConfigError(f"Неизвестный backend модели: {name}") from exc
        if not isinstance(item, Mapping):
            raise ConfigError(f"Backend модели {name} повреждён.")
        executable = str(item.get("executable", "")).strip()
        if not executable:
            raise ConfigError(f"Backend модели {name} не содержит executable.")
        capabilities = item.get("capabilities", {})
        if not isinstance(capabilities, Mapping):
            raise ConfigError(f"Capabilities backend-а {name} повреждены.")
        cors_controls = capabilities.get("cors_controls", True)
        if not isinstance(cors_controls, bool):
            raise ConfigError(
                f"engine_backends.{name}.capabilities.cors_controls должен быть логическим."
            )
        return EngineBackend(
            name=name,
            executable=_resolve(self.root, executable),
            cors_controls=cors_controls,
        )

    def engine_backend_names(self) -> tuple[str, ...]:
        backends = self.raw.get("engine_backends")
        if backends is None:
            return ("default",)
        if not isinstance(backends, Mapping):
            raise ConfigError("Раздел engine_backends должен быть объектом.")
        return tuple(str(name) for name in backends)

    def _model_roots(self) -> tuple[Path, ...]:
        configured = self.raw.get("paths", {}).get("model_search_dirs", [])
        if not isinstance(configured, list):
            raise ConfigError("paths.model_search_dirs должен быть списком каталогов.")
        roots = [self.models_dir]
        for value in configured:
            roots.append(_resolve(self.root, str(value)))
        seen: set[Path] = set()
        result: list[Path] = []
        for root in roots:
            resolved = root.resolve()
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
        return tuple(result)

    def _artifact(
        self,
        profile: str,
        item: Mapping[str, Any],
        name: str,
        *,
        required: bool,
    ) -> tuple[Path | None, int, str]:
        artifacts = item.get("artifacts")
        value: Mapping[str, Any] | None = None
        if isinstance(artifacts, Mapping):
            candidate = artifacts.get(name)
            if isinstance(candidate, Mapping):
                value = candidate
        elif name == "model":
            # Read old personal overrides and pre-catalog releases without
            # coupling runtime code to any historical model family.
            value = item
        if value is None:
            if required:
                raise ConfigError(
                    f"Профиль {profile} не содержит обязательный артефакт {name}."
                )
            return None, 0, ""

        raw_path = item.get("path") if name == "model" and item.get("path") else value.get("path")
        if raw_path:
            path = _resolve(self.root, str(raw_path))
        else:
            filename = str(value.get("filename", "")).strip()
            if (
                not filename
                or filename in {".", ".."}
                or Path(filename).name != filename
                or not filename.casefold().endswith(".gguf")
            ):
                raise ConfigError(
                    f"Артефакт {name} профиля {profile} должен содержать одно имя GGUF-файла."
                )
            roots = self._model_roots()
            direct = [root / filename for root in roots]
            path = next((candidate for candidate in direct if candidate.is_file()), direct[0])
            if not path.is_file():
                for root in roots:
                    if not root.is_dir():
                        continue
                    try:
                        matches = sorted(root.rglob(filename), key=lambda entry: str(entry).casefold())
                    except OSError:
                        continue
                    path = next((match for match in matches if match.is_file()), path)
                    if path.is_file():
                        break
        try:
            expected_size = max(0, int(value.get("expected_size_bytes", 0) or 0))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"Некорректный размер артефакта {name} профиля {profile}."
            ) from exc
        sha256 = str(value.get("sha256", "")).strip().casefold()
        if path.is_symlink():
            raise ConfigError(
                f"Артефакт {name} профиля {profile} не может быть символической ссылкой."
            )
        return path.resolve(), expected_size, sha256

    def model(self, role: str) -> ModelProfile:
        try:
            item = self.raw["models"][role]
        except KeyError as exc:
            raise ConfigError(f"Неизвестная роль модели: {role}") from exc
        if not isinstance(item, Mapping):
            raise ConfigError(f"Профиль модели {role} повреждён.")
        enabled = item.get("enabled", True)
        experimental = item.get("experimental", False)
        if not isinstance(enabled, bool) or not isinstance(experimental, bool):
            raise ConfigError(
                f"Флаги enabled/experimental профиля {role} должны быть логическими."
            )
        default_backend = (
            "default"
            if self.raw.get("engine_backends") is None
            else str(self.raw.get("default_engine_backend", "")).strip()
        )
        if not default_backend:
            raise ConfigError(
                "Конфигурация с engine_backends должна содержать default_engine_backend."
            )
        backend_name = str(item.get("backend", default_backend)).strip()
        if not backend_name:
            raise ConfigError(f"Профиль {role} содержит пустой backend.")
        backend = self.engine_backend(backend_name)
        model_path, expected_size, sha256 = self._artifact(
            role, item, "model", required=True
        )
        draft_path, draft_size, draft_sha256 = self._artifact(
            role, item, "draft", required=False
        )
        projector_path, projector_size, projector_sha256 = self._artifact(
            role, item, "projector", required=False
        )
        acceleration = item.get("acceleration", {})
        if not isinstance(acceleration, Mapping):
            raise ConfigError(f"Раздел acceleration профиля {role} повреждён.")
        acceleration_type = str(acceleration.get("type", "none")).strip().casefold()
        supported_acceleration = {
            "none",
            "draft-mtp",
            "draft-dflash",
            "draft-dspark",
        }
        if acceleration_type not in supported_acceleration:
            raise ConfigError(
                f"Профиль {role} использует неизвестное ускорение: {acceleration_type}."
            )
        if acceleration_type in {"draft-dflash", "draft-dspark"} and draft_path is None:
            raise ConfigError(
                f"Ускорение {acceleration_type} профиля {role} требует артефакт draft."
            )
        try:
            acceleration_max_tokens = int(acceleration.get("max_tokens", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Числовые параметры acceleration профиля {role} повреждены.") from exc
        if acceleration_type == "none" and acceleration_max_tokens != 0:
            raise ConfigError(
                f"Профиль {role} не должен задавать max_tokens без ускорения."
            )
        if acceleration_type != "none" and not 1 <= acceleration_max_tokens <= 32:
            raise ConfigError(
                f"acceleration.max_tokens профиля {role} должен быть от 1 до 32."
            )
        draft_gpu_layers_raw = acceleration.get("draft_gpu_layers", 0)
        draft_gpu_layers = self._gpu_layers(
            draft_gpu_layers_raw, field=f"models.{role}.acceleration.draft_gpu_layers"
        )
        if acceleration_type not in {"draft-dflash", "draft-dspark"} and draft_gpu_layers:
            raise ConfigError(
                f"Профиль {role} задаёт draft_gpu_layers без отдельной draft-модели."
            )
        try:
            context_size = int(item["context_size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"Некорректный context_size профиля {role}.") from exc
        if not 512 <= context_size <= 1_048_576:
            raise ConfigError(
                f"context_size профиля {role} должен быть от 512 до 1048576."
            )
        extra_args = item.get("extra_args", [])
        if not isinstance(extra_args, list) or not all(
            isinstance(argument, str) and argument for argument in extra_args
        ):
            raise ConfigError(f"extra_args профиля {role} должен быть списком строк.")
        return ModelProfile(
            role=role,
            label=str(item["label"]),
            model_path=model_path,
            context_size=context_size,
            gpu_layers=self._gpu_layers(
                item.get("gpu_layers", "auto"), field=f"models.{role}.gpu_layers"
            ),
            enabled=enabled,
            experimental=experimental,
            extra_args=tuple(extra_args),
            reasoning=str(item.get("reasoning", "off")).casefold(),
            expected_size_bytes=expected_size,
            sha256=sha256,
            draft_model_path=draft_path,
            draft_expected_size_bytes=draft_size,
            draft_sha256=draft_sha256,
            projector_path=projector_path,
            projector_expected_size_bytes=projector_size,
            projector_sha256=projector_sha256,
            acceleration_type=acceleration_type,
            acceleration_max_tokens=acceleration_max_tokens,
            draft_gpu_layers=draft_gpu_layers,
            backend=backend,
        )

    @staticmethod
    def _gpu_layers(value: Any, *, field: str) -> int | str:
        if isinstance(value, bool):
            raise ConfigError(f"{field} не должен быть логическим значением.")
        if isinstance(value, int):
            if value < 0:
                raise ConfigError(f"{field} не должен быть отрицательным.")
            return value
        normalized = str(value).strip().casefold()
        if normalized in {"auto", "all"}:
            return normalized
        if normalized.isdecimal():
            return int(normalized)
        raise ConfigError(f"{field} должен быть числом, auto или all.")

    def model_roles(self) -> tuple[str, ...]:
        models = self.raw.get("models", {})
        if not isinstance(models, Mapping):
            raise ConfigError("Раздел models должен быть объектом.")
        return tuple(str(name) for name in models)

    def capability_role(self, name: str) -> CapabilityRole:
        try:
            item = self.raw["capability_roles"][name]
        except KeyError as exc:
            raise ConfigError(f"Неизвестная функциональная роль: {name}") from exc
        if not isinstance(item, Mapping):
            raise ConfigError(f"Функциональная роль {name} повреждена.")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(
                f"Флаг capability_roles.{name}.enabled должен быть логическим."
            )
        models = set(self.model_roles())
        primary_model = item.get("primary_model")
        candidate_model = item.get("candidate_model")
        for field, value in (
            ("primary_model", primary_model),
            ("candidate_model", candidate_model),
        ):
            if value is not None and str(value) not in models:
                raise ConfigError(
                    f"Роль «{name}» ссылается на неизвестную модель в поле {field}: {value}"
                )
        return CapabilityRole(
            name=name,
            label=str(item["label"]),
            purpose=str(item.get("purpose", "")),
            primary_model=str(primary_model) if primary_model is not None else None,
            candidate_model=(
                str(candidate_model) if candidate_model is not None else None
            ),
            enabled=enabled,
        )

    def capability_role_names(self) -> tuple[str, ...]:
        roles = self.raw.get("capability_roles", {})
        if not isinstance(roles, Mapping):
            raise ConfigError("Раздел capability_roles должен быть объектом.")
        return tuple(str(name) for name in roles)

    def capability_model(self, name: str, *, fallback: str | None = None) -> str:
        for candidate in (name, fallback):
            if candidate is None:
                continue
            role = self.capability_role(candidate)
            if not role.enabled or not role.primary_model:
                continue
            profile = self.model(role.primary_model)
            if profile.enabled:
                return role.primary_model
        raise ConfigError(f"Для функциональной роли {name} нет включённой модели.")


def load_settings(root: Path | None = None) -> Settings:
    root = (root or project_root()).resolve()
    default_path = root / "config" / "default.json"
    user_path = root / "config" / "user.json"
    raw = _read_json(default_path)
    if user_path.exists():
        raw = _deep_merge(raw, _read_json(user_path))

    boolean_sections: dict[str, tuple[str, ...]] = {
        "assistant": ("announce_status",),
        "voice": ("enabled",),
        "diagnostics": ("enabled", "include_content", "allow_during_tests"),
        "browser": (
            "enabled",
            "headless",
            "persistent",
            "active_control_enabled",
        ),
        "windows": ("active_control_enabled",),
        "headset_controls": ("enabled", "consume"),
        "routing": ("enabled",),
        "memory": ("persistent", "compression_enabled"),
        "rag": ("enabled", "auto_index"),
    }
    for section_name, fields in boolean_sections.items():
        section = raw.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ConfigError(f"Раздел {section_name} должен быть объектом.")
        for field in fields:
            if field in section and not isinstance(section[field], bool):
                raise ConfigError(
                    f"Параметр {section_name}.{field} должен быть логическим значением."
                )

    paths = raw.get("paths", {})
    server = raw.get("server", {})
    host = str(server.get("host", "127.0.0.1")).casefold().strip()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigError(
            "Сервер языковой модели разрешено привязывать только к loopback-адресу. "
            "Для телефона используйте отдельную LAN-панель с PIN."
        )
    try:
        port = int(server.get("port", 18080))
    except (TypeError, ValueError) as exc:
        raise ConfigError("Порт сервера модели должен быть целым числом.") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("Порт сервера модели должен находиться в диапазоне 1–65535.")
    agent = raw.get("agent", {})
    if not isinstance(agent, dict):
        raise ConfigError("Раздел agent должен быть объектом.")
    try:
        agent_max_steps = int(agent.get("max_steps", 8))
        developer_max_steps = int(
            agent.get("developer_max_steps", agent_max_steps)
        )
        max_tool_calls = int(agent.get("max_tool_calls_total", 16))
        max_confirmation_requests = int(agent.get("max_confirmation_requests", 4))
        max_directory_list_calls = int(agent.get("max_directory_list_calls", 6))
    except (TypeError, ValueError) as exc:
        raise ConfigError("Числовые лимиты agent повреждены.") from exc
    if not 1 <= agent_max_steps <= developer_max_steps <= 64:
        raise ConfigError(
            "Лимиты agent должны удовлетворять условию "
            "1 <= max_steps <= developer_max_steps <= 64."
        )
    if not developer_max_steps <= max_tool_calls <= 256:
        raise ConfigError(
            "agent.max_tool_calls_total должен быть не меньше developer_max_steps "
            "и не больше 256."
        )
    if not 1 <= max_confirmation_requests <= 8:
        raise ConfigError(
            "agent.max_confirmation_requests должен находиться в диапазоне 1–8."
        )
    if not 1 <= max_directory_list_calls <= 32:
        raise ConfigError(
            "agent.max_directory_list_calls должен находиться в диапазоне 1–32."
        )
    agent["max_steps"] = agent_max_steps
    agent["developer_max_steps"] = developer_max_steps
    agent["max_tool_calls_total"] = max_tool_calls
    agent["max_confirmation_requests"] = max_confirmation_requests
    agent["max_directory_list_calls"] = max_directory_list_calls
    voice = raw.get("voice", {})
    if not isinstance(voice, dict):
        raise ConfigError("Раздел voice должен быть объектом.")
    try:
        confirmation_handoff_timeout = float(
            voice.get("confirmation_microphone_handoff_timeout_seconds", 5.0)
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "Тайм-аут передачи микрофона для подтверждения повреждён."
        ) from exc
    if not math.isfinite(confirmation_handoff_timeout) or not (
        0.5 <= confirmation_handoff_timeout <= 30
    ):
        raise ConfigError(
            "Тайм-аут передачи микрофона для подтверждения должен быть "
            "от 0,5 до 30 секунд."
        )
    voice["confirmation_microphone_handoff_timeout_seconds"] = (
        confirmation_handoff_timeout
    )
    live = raw.get("live", {})
    if not isinstance(live, dict):
        raise ConfigError("Раздел live должен быть объектом.")
    live_enabled = live.get("enabled", False)
    if not isinstance(live_enabled, bool):
        raise ConfigError("Параметр live.enabled должен быть логическим значением.")
    semantic_endpointing = live.get("semantic_endpointing", True)
    if not isinstance(semantic_endpointing, bool):
        raise ConfigError(
            "Параметр live.semantic_endpointing должен быть логическим значением."
        )
    try:
        minimum_phrase_chars = int(live.get("minimum_phrase_chars", 24))
        maximum_phrase_chars = int(live.get("maximum_phrase_chars", 220))
        playback_timeout_seconds = float(
            live.get("playback_timeout_seconds", 600)
        )
        turn_silences = (
            float(live.get("turn_complete_silence_seconds", 0.45)),
            float(live.get("turn_ordinary_silence_seconds", 0.85)),
            float(live.get("turn_incomplete_silence_seconds", 2.2)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError("Числовые параметры live повреждены.") from exc
    if not 1 <= minimum_phrase_chars <= maximum_phrase_chars <= 2_000:
        raise ConfigError(
            "Границы Live-фраз должны удовлетворять условию "
            "1 <= minimum_phrase_chars <= maximum_phrase_chars <= 2000."
        )
    if not 30 <= playback_timeout_seconds <= 3_600:
        raise ConfigError(
            "Тайм-аут Live-озвучивания должен быть от 30 до 3600 секунд."
        )
    if not all(math.isfinite(value) for value in turn_silences) or not (
        0 < turn_silences[0] <= turn_silences[1] <= turn_silences[2] <= 10
    ):
        raise ConfigError(
            "Паузы Live-реплики должны удовлетворять условию "
            "0 < complete <= ordinary <= incomplete <= 10."
        )
    if live:
        live["semantic_endpointing"] = semantic_endpointing
        live["turn_complete_silence_seconds"] = turn_silences[0]
        live["turn_ordinary_silence_seconds"] = turn_silences[1]
        live["turn_incomplete_silence_seconds"] = turn_silences[2]
        live["minimum_phrase_chars"] = minimum_phrase_chars
        live["maximum_phrase_chars"] = maximum_phrase_chars
        live["playback_timeout_seconds"] = playback_timeout_seconds
    settings = Settings(
        root=root,
        raw=raw,
        llama_server=_resolve(root, str(paths["llama_server"])),
        models_dir=_resolve(root, str(paths["models_dir"])),
        runtime_dir=_resolve(root, str(paths["runtime_dir"])),
        host=host,
        port=port,
        startup_timeout_seconds=int(server.get("startup_timeout_seconds", 120)),
    )
    for profile_name in settings.model_roles():
        settings.model(profile_name)
    for capability_name in settings.capability_role_names():
        settings.capability_role(capability_name)
    settings.default_role
    return settings


def write_user_settings(root: Path, llama_server: Path, models_dir: Path) -> Path:
    target = root.resolve() / "config" / "user.json"

    def edit(value: dict[str, Any]) -> None:
        paths = value.setdefault("paths", {})
        if not isinstance(paths, dict):
            raise ConfigError("Раздел paths в пользовательской конфигурации повреждён.")
        paths.update(
            {
                "llama_server": str(llama_server.resolve()),
                "models_dir": str(models_dir.resolve()),
            }
        )

    return _edit_user_settings(target, edit)


def set_user_model(root: Path, role: str, model_path: Path, *, enabled: bool = True) -> Path:
    target = root.resolve() / "config" / "user.json"

    def edit(value: dict[str, Any]) -> None:
        models = value.setdefault("models", {})
        if not isinstance(models, dict):
            raise ConfigError("Раздел models в пользовательской конфигурации повреждён.")
        profile = models.setdefault(role, {})
        if not isinstance(profile, dict):
            raise ConfigError(f"Профиль модели {role} повреждён.")
        artifacts = profile.setdefault("artifacts", {})
        if not isinstance(artifacts, dict):
            raise ConfigError(f"Артефакты профиля модели {role} повреждены.")
        model = artifacts.setdefault("model", {})
        if not isinstance(model, dict):
            raise ConfigError(f"Основной артефакт профиля модели {role} повреждён.")
        model["path"] = str(model_path.resolve())
        profile.pop("path", None)
        profile["enabled"] = enabled

    return _edit_user_settings(target, edit)


def set_user_capability_model(root: Path, capability: str, profile: str) -> Path:
    target = root.resolve() / "config" / "user.json"

    def edit(value: dict[str, Any]) -> None:
        roles = value.setdefault("capability_roles", {})
        if not isinstance(roles, dict):
            raise ConfigError("Раздел capability_roles в пользовательской конфигурации повреждён.")
        role = roles.setdefault(capability, {})
        if not isinstance(role, dict):
            raise ConfigError(f"Функциональная роль {capability} повреждена.")
        role["primary_model"] = profile
        role["enabled"] = True

    return _edit_user_settings(target, edit)


def set_user_reasoning(root: Path, role: str, level: str) -> Path:
    reasoning_arguments(level)
    target = root.resolve() / "config" / "user.json"

    def edit(value: dict[str, Any]) -> None:
        models = value.setdefault("models", {})
        if not isinstance(models, dict):
            raise ConfigError("Раздел models в пользовательской конфигурации повреждён.")
        profile = models.setdefault(role, {})
        if not isinstance(profile, dict):
            raise ConfigError(f"Профиль модели {role} повреждён.")
        profile["reasoning"] = level

    return _edit_user_settings(target, edit)


def set_user_response_budget(root: Path, max_tokens: int) -> Path:
    try:
        _label, research_max_tokens, plan_max_tokens = RESPONSE_BUDGETS[int(max_tokens)]
    except (KeyError, TypeError, ValueError) as exc:
        available = ", ".join(str(value) for value in RESPONSE_BUDGETS)
        raise ConfigError(f"Неизвестный лимит ответа: {max_tokens}. Доступно: {available}.") from exc
    target = root.resolve() / "config" / "user.json"

    def edit(value: dict[str, Any]) -> None:
        generation = value.setdefault("generation", {})
        routing = value.setdefault("routing", {})
        if not isinstance(generation, dict) or not isinstance(routing, dict):
            raise ConfigError("Раздел генерации или маршрутизации повреждён.")
        generation["max_tokens"] = int(max_tokens)
        routing["research_turn_max_tokens"] = research_max_tokens
        routing["plan_max_tokens"] = plan_max_tokens

    return _edit_user_settings(target, edit)


def set_user_headset_control(
    root: Path,
    button: str,
    *,
    enabled: bool = True,
    consume: bool = True,
) -> Path:
    target = root.resolve() / "config" / "user.json"

    def edit(value: dict[str, Any]) -> None:
        controls = value.setdefault("headset_controls", {})
        if not isinstance(controls, dict):
            raise ConfigError("Раздел headset_controls в пользовательской конфигурации повреждён.")
        controls.update(
            {
                "enabled": bool(enabled),
                "activation_button": str(button),
                "consume": bool(consume),
            }
        )

    return _edit_user_settings(target, edit)
