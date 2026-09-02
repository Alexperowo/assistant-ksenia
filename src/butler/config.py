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


def reasoning_arguments(
    level: str,
    *,
    budget_tokens: int | None = None,
    budget_message: str = "",
) -> tuple[str, ...]:
    try:
        _label, mode, budget = REASONING_LEVELS[level]
    except KeyError as exc:
        available = ", ".join(REASONING_LEVELS)
        raise ConfigError(f"Неизвестный уровень рассуждений: {level}. Доступно: {available}.") from exc
    selected_budget = budget if budget_tokens is None else int(budget_tokens)
    if mode == "off" and (budget_tokens is not None or budget_message):
        raise ConfigError("Выключенные рассуждения не могут задавать budget или budget_message.")
    result = ["--reasoning", mode]
    if selected_budget is not None:
        result.extend(["--reasoning-budget", str(selected_budget)])
    if budget_message:
        result.extend(["--reasoning-budget-message", budget_message])
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
class ModelService:
    """One independently managed loopback llama.cpp process."""

    name: str
    host: str
    port: int
    state_file: Path


@dataclass(frozen=True)
class ModelRequestMode:
    """Per-request generation policy, independent from model identity."""

    name: str
    enable_thinking: bool
    max_tokens: int
    temperature: float
    strategy: str = "direct"


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
    reasoning_budget_tokens: int | None = None
    reasoning_budget_message: str = ""
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
    service_name: str = "primary"
    request_modes: tuple[ModelRequestMode, ...] = ()

    def request_mode(self, name: str) -> ModelRequestMode:
        normalized = str(name).strip().casefold()
        for mode in self.request_modes:
            if mode.name == normalized:
                return mode
        available = ", ".join(mode.name for mode in self.request_modes) or "нет"
        raise ConfigError(
            f"Профиль {self.role} не содержит режим запроса {name}. Доступно: {available}."
        )


@dataclass(frozen=True)
class CapabilityRole:
    name: str
    label: str
    purpose: str
    primary_model: str | None
    candidate_model: str | None
    enabled: bool


@dataclass(frozen=True)
class UIDeliberationProfile:
    proposer_model: str
    reviewer_model: str
    proposal_mode: str
    review_mode: str
    max_revisions: int
    review_max_tokens: int


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
    def user_name(self) -> str:
        return str(self.raw.get("assistant", {}).get("user_name", "пользователь"))

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

    def model_service(self, name: str = "primary") -> ModelService:
        normalized = str(name).strip().casefold()
        configured = self.raw.get("model_services")
        if configured is None:
            if normalized != "primary":
                raise ConfigError(f"Неизвестный сервис модели: {name}")
            return ModelService(
                name="primary",
                host=self.host,
                port=self.port,
                state_file=self.state_file,
            )
        if not isinstance(configured, Mapping):
            raise ConfigError("Раздел model_services должен быть объектом.")
        try:
            item = configured[normalized]
        except KeyError as exc:
            raise ConfigError(f"Неизвестный сервис модели: {name}") from exc
        if not isinstance(item, Mapping):
            raise ConfigError(f"Сервис модели {normalized} повреждён.")
        host = str(item.get("host", self.host)).strip().casefold()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigError(
                f"Сервис модели {normalized} разрешено привязывать только к loopback-адресу."
            )
        try:
            port = int(item.get("port", self.port))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Порт сервиса модели {normalized} должен быть целым числом.") from exc
        if not 1 <= port <= 65535:
            raise ConfigError(
                f"Порт сервиса модели {normalized} должен находиться в диапазоне 1–65535."
            )
        raw_state_file = str(item.get("state_file", "")).strip()
        if not raw_state_file:
            raw_state_file = "state.json" if normalized == "primary" else f"state-{normalized}.json"
        relative_state_file = Path(raw_state_file)
        if relative_state_file.is_absolute():
            raise ConfigError(
                f"model_services.{normalized}.state_file должен быть относительным путём."
            )
        state_file = (self.runtime_dir / relative_state_file).resolve()
        try:
            state_file.relative_to(self.runtime_dir.resolve())
        except ValueError as exc:
            raise ConfigError(
                f"model_services.{normalized}.state_file выходит за runtime_dir."
            ) from exc
        return ModelService(
            name=normalized,
            host=host,
            port=port,
            state_file=state_file,
        )

    def model_service_names(self) -> tuple[str, ...]:
        configured = self.raw.get("model_services")
        if configured is None:
            return ("primary",)
        if not isinstance(configured, Mapping):
            raise ConfigError("Раздел model_services должен быть объектом.")
        return tuple(str(name).strip().casefold() for name in configured)

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
        service_name = str(item.get("service", "primary")).strip().casefold()
        if not service_name:
            raise ConfigError(f"Профиль {role} содержит пустой service.")
        self.model_service(service_name)
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
        request_modes_raw = item.get("request_modes", {})
        if not isinstance(request_modes_raw, Mapping):
            raise ConfigError(f"request_modes профиля {role} должен быть объектом.")
        request_modes: list[ModelRequestMode] = []
        for mode_name, mode_raw in request_modes_raw.items():
            normalized_mode = str(mode_name).strip().casefold()
            if not normalized_mode or not isinstance(mode_raw, Mapping):
                raise ConfigError(f"Режим запроса профиля {role} повреждён: {mode_name}.")
            enable_thinking = mode_raw.get("enable_thinking", False)
            if not isinstance(enable_thinking, bool):
                raise ConfigError(
                    f"models.{role}.request_modes.{normalized_mode}.enable_thinking "
                    "должен быть логическим."
                )
            try:
                mode_max_tokens = int(mode_raw.get("max_tokens", 512))
                mode_temperature = float(mode_raw.get("temperature", 0.1))
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"Числовые параметры режима {normalized_mode} профиля {role} повреждены."
                ) from exc
            if not 16 <= mode_max_tokens <= 32768:
                raise ConfigError(
                    f"max_tokens режима {normalized_mode} профиля {role} должен быть от 16 до 32768."
                )
            if not math.isfinite(mode_temperature) or not 0 <= mode_temperature <= 2:
                raise ConfigError(
                    f"temperature режима {normalized_mode} профиля {role} должен быть от 0 до 2."
                )
            strategy = str(mode_raw.get("strategy", "direct")).strip().casefold()
            if strategy not in {"direct", "cross_review"}:
                raise ConfigError(
                    f"Неизвестная strategy режима {normalized_mode} профиля {role}: {strategy}."
                )
            request_modes.append(
                ModelRequestMode(
                    name=normalized_mode,
                    enable_thinking=enable_thinking,
                    max_tokens=mode_max_tokens,
                    temperature=mode_temperature,
                    strategy=strategy,
                )
            )
        reasoning = str(item.get("reasoning", "off")).casefold()
        raw_reasoning_budget = item.get("reasoning_budget_tokens")
        try:
            reasoning_budget_tokens = (
                None if raw_reasoning_budget is None else int(raw_reasoning_budget)
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"reasoning_budget_tokens профиля {role} повреждён.") from exc
        if reasoning_budget_tokens is not None and not 1 <= reasoning_budget_tokens <= 32768:
            raise ConfigError(
                f"reasoning_budget_tokens профиля {role} должен быть от 1 до 32768."
            )
        reasoning_budget_message = str(item.get("reasoning_budget_message", "")).strip()
        if len(reasoning_budget_message) > 500 or any(
            character in reasoning_budget_message for character in ("\r", "\n", "\x00")
        ):
            raise ConfigError(
                f"reasoning_budget_message профиля {role} должен быть одной строкой до 500 символов."
            )
        reasoning_arguments(
            reasoning,
            budget_tokens=reasoning_budget_tokens,
            budget_message=reasoning_budget_message,
        )
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
            reasoning=reasoning,
            reasoning_budget_tokens=reasoning_budget_tokens,
            reasoning_budget_message=reasoning_budget_message,
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
            service_name=service_name,
            request_modes=tuple(request_modes),
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

    def resident_model_roles(self) -> tuple[str, ...]:
        runtime_routing = self.raw.get("runtime_routing", {})
        if not isinstance(runtime_routing, Mapping):
            raise ConfigError("Раздел runtime_routing должен быть объектом.")
        roles = runtime_routing.get("fast_resident_models", [])
        if not isinstance(roles, list) or not all(
            isinstance(role, str) and role.strip() for role in roles
        ):
            raise ConfigError("runtime_routing.fast_resident_models должен быть списком ролей.")
        normalized = tuple(role.strip() for role in roles)
        if len(set(normalized)) != len(normalized):
            raise ConfigError("runtime_routing.fast_resident_models содержит повторяющиеся роли.")
        known = set(self.model_roles())
        unknown = [role for role in normalized if role not in known]
        if unknown:
            raise ConfigError(
                "runtime_routing.fast_resident_models содержит неизвестные роли: "
                + ", ".join(unknown)
            )
        services: set[str] = set()
        for role in normalized:
            profile = self.model(role)
            if profile.service_name == "primary":
                raise ConfigError(
                    f"Резидентная модель {role} не должна использовать primary service."
                )
            if profile.service_name in services:
                raise ConfigError(
                    "Резидентные модели должны использовать разные сервисы; повтор: "
                    f"{profile.service_name}."
                )
            services.add(profile.service_name)
        return normalized

    def research_request_modes(
        self, model_role: str
    ) -> dict[str, ModelRequestMode]:
        """Resolve stage-specific research modes declared for one model profile."""

        runtime_routing = self.raw.get("runtime_routing", {})
        if not isinstance(runtime_routing, Mapping):
            raise ConfigError("Раздел runtime_routing должен быть объектом.")
        configured = runtime_routing.get("research_request_modes", {})
        if not isinstance(configured, Mapping):
            raise ConfigError(
                "runtime_routing.research_request_modes должен быть объектом."
            )
        raw_modes = configured.get(model_role)
        if raw_modes is None:
            return {}
        if not isinstance(raw_modes, Mapping):
            raise ConfigError(
                f"research_request_modes.{model_role} должен быть объектом."
            )
        allowed_stages = {
            "query",
            "synthesis_fast",
            "synthesis_normal",
            "synthesis_deep",
            "verification",
        }
        unknown_stages = sorted(set(map(str, raw_modes)) - allowed_stages)
        if unknown_stages:
            raise ConfigError(
                f"research_request_modes.{model_role} содержит неизвестные этапы: "
                + ", ".join(unknown_stages)
            )
        profile = self.model(model_role)
        resolved: dict[str, ModelRequestMode] = {}
        for stage, mode_name in raw_modes.items():
            normalized_stage = str(stage)
            if not isinstance(mode_name, str) or not mode_name.strip():
                raise ConfigError(
                    f"research_request_modes.{model_role}.{normalized_stage} "
                    "должен содержать имя режима."
                )
            resolved[normalized_stage] = profile.request_mode(mode_name)
        return resolved

    def assistant_request_mode(self, model_role: str) -> ModelRequestMode | None:
        """Resolve the configured text-conversation mode for an assistant profile."""

        runtime_routing = self.raw.get("runtime_routing", {})
        if not isinstance(runtime_routing, Mapping):
            raise ConfigError("Раздел runtime_routing должен быть объектом.")
        configured = runtime_routing.get("assistant_request_modes", {})
        if not isinstance(configured, Mapping):
            raise ConfigError(
                "runtime_routing.assistant_request_modes должен быть объектом."
            )
        raw_mode = configured.get(model_role)
        if raw_mode is None:
            return None
        if not isinstance(raw_mode, str) or not raw_mode.strip():
            raise ConfigError(
                f"assistant_request_modes.{model_role} должен содержать имя режима."
            )
        return self.model(model_role).request_mode(raw_mode)

    def fast_lookup_policy(self) -> tuple[tuple[str, ...], int]:
        routing = self.raw.get("routing", {})
        if not isinstance(routing, Mapping):
            raise ConfigError("Раздел routing должен быть объектом.")
        raw_signals = routing.get("fast_lookup_signals", [])
        if not isinstance(raw_signals, list) or not all(
            isinstance(signal, str) and signal.strip() for signal in raw_signals
        ):
            raise ConfigError("routing.fast_lookup_signals должен быть списком строк.")
        signals = tuple(signal.strip() for signal in raw_signals)
        if len(set(signal.casefold() for signal in signals)) != len(signals):
            raise ConfigError("routing.fast_lookup_signals содержит повторы.")
        try:
            max_chars = int(routing.get("fast_lookup_max_chars", 180))
        except (TypeError, ValueError) as exc:
            raise ConfigError("routing.fast_lookup_max_chars должен быть целым числом.") from exc
        if not 32 <= max_chars <= 2_000:
            raise ConfigError("routing.fast_lookup_max_chars должен быть от 32 до 2000.")
        return signals, max_chars

    def weather_signals(self) -> tuple[str, ...]:
        weather = self.raw.get("weather")
        if weather is None:
            return ()
        if not isinstance(weather, Mapping):
            raise ConfigError("Раздел weather должен быть объектом.")
        enabled = weather.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError("weather.enabled должен быть логическим значением.")
        provider = str(weather.get("provider", "")).strip().casefold()
        if enabled and provider != "open_meteo":
            raise ConfigError("Поддерживается только weather.provider=open_meteo.")
        for field in ("geocoding_url", "forecast_url"):
            value = weather.get(field, "")
            if enabled and (not isinstance(value, str) or not value.strip()):
                raise ConfigError(f"weather.{field} должен содержать HTTPS-адрес.")
        try:
            timeout = float(weather.get("timeout_seconds", 8))
            max_bytes = int(weather.get("max_response_bytes", 262_144))
        except (TypeError, ValueError) as exc:
            raise ConfigError("Числовые параметры weather повреждены.") from exc
        if not 1 <= timeout <= 30:
            raise ConfigError("weather.timeout_seconds должен быть от 1 до 30 секунд.")
        if not 16_384 <= max_bytes <= 1_048_576:
            raise ConfigError(
                "weather.max_response_bytes должен быть от 16384 до 1048576."
            )
        country_codes = weather.get("preferred_country_codes", [])
        if not isinstance(country_codes, list) or not all(
            isinstance(code, str)
            and len(code.strip()) == 2
            and code.strip().isalpha()
            for code in country_codes
        ):
            raise ConfigError(
                "weather.preferred_country_codes должен содержать двухбуквенные коды."
            )
        raw_signals = weather.get("signals", [])
        if not isinstance(raw_signals, list) or not all(
            isinstance(signal, str) and signal.strip() for signal in raw_signals
        ):
            raise ConfigError("weather.signals должен быть списком строк.")
        normalized = tuple(
            signal.strip().casefold().replace("ё", "е") for signal in raw_signals
        )
        if len(set(normalized)) != len(normalized):
            raise ConfigError("weather.signals содержит повторы.")
        return normalized

    def weather_enabled(self) -> bool:
        if "weather" not in self.raw:
            return False
        self.weather_signals()
        return bool(self.raw.get("weather", {}).get("enabled", True))

    def weather_current_blockers(self) -> tuple[str, ...]:
        weather = self.raw.get("weather")
        if weather is None:
            return ()
        raw_blockers = weather.get("current_lookup_blockers", [])
        if not isinstance(raw_blockers, list) or not all(
            isinstance(blocker, str) and blocker.strip() for blocker in raw_blockers
        ):
            raise ConfigError("weather.current_lookup_blockers должен быть списком строк.")
        normalized = tuple(
            blocker.strip().casefold().replace("ё", "е") for blocker in raw_blockers
        )
        if len(set(normalized)) != len(normalized):
            raise ConfigError("weather.current_lookup_blockers содержит повторы.")
        return normalized

    def ui_deliberation(self) -> UIDeliberationProfile | None:
        runtime_routing = self.raw.get("runtime_routing", {})
        if not isinstance(runtime_routing, Mapping):
            raise ConfigError("Раздел runtime_routing должен быть объектом.")
        raw = runtime_routing.get("ui_deliberation")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ConfigError("runtime_routing.ui_deliberation должен быть объектом.")
        proposer = str(raw.get("proposer_model", "")).strip()
        reviewer = str(raw.get("reviewer_model", "")).strip()
        proposal_mode = str(raw.get("proposal_mode", "fast")).strip().casefold()
        review_mode = str(raw.get("review_mode", "deliberate")).strip().casefold()
        if not proposer or not reviewer or proposer == reviewer:
            raise ConfigError(
                "UI deliberation требует разные proposer_model и reviewer_model."
            )
        proposer_profile = self.model(proposer)
        reviewer_profile = self.model(reviewer)
        selected_proposal_mode = proposer_profile.request_mode(proposal_mode)
        reviewer_profile.request_mode(review_mode)
        if selected_proposal_mode.strategy != "cross_review":
            raise ConfigError(
                "Режим proposer-а для UI deliberation должен иметь strategy=cross_review."
            )
        try:
            max_revisions = int(raw.get("max_revisions", 1))
            review_max_tokens = int(raw.get("review_max_tokens", 512))
        except (TypeError, ValueError) as exc:
            raise ConfigError("Числовые параметры UI deliberation повреждены.") from exc
        if not 0 <= max_revisions <= 2:
            raise ConfigError("ui_deliberation.max_revisions должен быть от 0 до 2.")
        if not 64 <= review_max_tokens <= 4096:
            raise ConfigError("ui_deliberation.review_max_tokens должен быть от 64 до 4096.")
        residents = set(self.resident_model_roles())
        if proposer not in residents or reviewer not in residents:
            raise ConfigError(
                "Обе модели UI deliberation должны входить в fast_resident_models."
            )
        return UIDeliberationProfile(
            proposer_model=proposer,
            reviewer_model=reviewer,
            proposal_mode=proposal_mode,
            review_mode=review_mode,
            max_revisions=max_revisions,
            review_max_tokens=review_max_tokens,
        )

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
    playback_backend = str(voice.get("playback_backend", "system")).strip().casefold()
    if playback_backend not in {"system", "pcm"}:
        raise ConfigError("voice.playback_backend должен быть system или pcm.")
    output_device = voice.get("output_device", "")
    if not isinstance(output_device, str):
        raise ConfigError("voice.output_device должен быть строкой.")
    try:
        capture_block_ms = int(voice.get("capture_block_ms", 40))
    except (TypeError, ValueError) as exc:
        raise ConfigError("voice.capture_block_ms должен быть целым числом.") from exc
    if not 10 <= capture_block_ms <= 250 or capture_block_ms % 10:
        raise ConfigError(
            "voice.capture_block_ms должен быть кратен 10 и находиться от 10 до 250 мс."
        )
    voice["playback_backend"] = playback_backend
    voice["output_device"] = output_device.strip()
    voice["capture_block_ms"] = capture_block_ms
    live = raw.get("live", {})
    if not isinstance(live, dict):
        raise ConfigError("Раздел live должен быть объектом.")
    live_enabled = live.get("enabled", False)
    if not isinstance(live_enabled, bool):
        raise ConfigError("Параметр live.enabled должен быть логическим значением.")
    semantic_endpointing = live.get("semantic_endpointing", True)
    speech_barge_in = live.get("speech_barge_in", False)
    if not isinstance(semantic_endpointing, bool) or not isinstance(
        speech_barge_in, bool
    ):
        raise ConfigError(
            "Параметры live.semantic_endpointing и live.speech_barge_in "
            "должны быть логическими значениями."
        )
    audio_processing = live.get("audio_processing", {})
    if not isinstance(audio_processing, dict):
        raise ConfigError("Раздел live.audio_processing должен быть объектом.")
    audio_processing_enabled = audio_processing.get("enabled", False)
    auto_gain_control = audio_processing.get("auto_gain_control", False)
    if not isinstance(audio_processing_enabled, bool) or not isinstance(
        auto_gain_control, bool
    ):
        raise ConfigError("Флаги live.audio_processing должны быть логическими.")
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
        stream_delay_ms = int(audio_processing.get("stream_delay_ms", 0))
        ns_level = int(audio_processing.get("ns_level", 1))
        barge_in_probe_seconds = float(live.get("barge_in_probe_seconds", 2.0))
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
    if not 0 <= stream_delay_ms <= 1_000:
        raise ConfigError("AEC stream delay должен быть от 0 до 1000 мс.")
    if not 0 <= ns_level <= 3:
        raise ConfigError("Уровень noise suppression должен быть от 0 до 3.")
    if not 0.5 <= barge_in_probe_seconds <= 5.0:
        raise ConfigError("Live barge-in probe должен быть от 0,5 до 5 секунд.")
    if audio_processing_enabled and (not live_enabled or playback_backend != "pcm"):
        raise ConfigError(
            "AEC разрешён только при live.enabled=true и voice.playback_backend=pcm."
        )
    if speech_barge_in and (
        not live_enabled
        or playback_backend != "pcm"
        or not audio_processing_enabled
    ):
        raise ConfigError(
            "Произвольный Live barge-in требует Live, PCM и включённый AEC."
        )
    if live:
        live["semantic_endpointing"] = semantic_endpointing
        live["speech_barge_in"] = speech_barge_in
        live["barge_in_probe_seconds"] = barge_in_probe_seconds
        live["turn_complete_silence_seconds"] = turn_silences[0]
        live["turn_ordinary_silence_seconds"] = turn_silences[1]
        live["turn_incomplete_silence_seconds"] = turn_silences[2]
        live["minimum_phrase_chars"] = minimum_phrase_chars
        live["maximum_phrase_chars"] = maximum_phrase_chars
        live["playback_timeout_seconds"] = playback_timeout_seconds
        audio_processing["enabled"] = audio_processing_enabled
        audio_processing["stream_delay_ms"] = stream_delay_ms
        audio_processing["ns_level"] = ns_level
        audio_processing["auto_gain_control"] = auto_gain_control
        live["audio_processing"] = audio_processing
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
    service_ports: set[tuple[str, int]] = set()
    service_states: set[Path] = set()
    for service_name in settings.model_service_names():
        service = settings.model_service(service_name)
        endpoint = (service.host, service.port)
        if endpoint in service_ports:
            raise ConfigError(
                f"Сервисы моделей не должны делить endpoint {service.host}:{service.port}."
            )
        if service.state_file in service_states:
            raise ConfigError(
                f"Сервисы моделей не должны делить state_file: {service.state_file}."
            )
        service_ports.add(endpoint)
        service_states.add(service.state_file)
    for profile_name in settings.model_roles():
        settings.model(profile_name)
    settings.resident_model_roles()
    settings.ui_deliberation()
    settings.fast_lookup_policy()
    settings.weather_signals()
    settings.weather_current_blockers()
    runtime_routing = settings.raw.get("runtime_routing", {})
    configured_research_modes = (
        runtime_routing.get("research_request_modes", {})
        if isinstance(runtime_routing, Mapping)
        else {}
    )
    if not isinstance(configured_research_modes, Mapping):
        raise ConfigError(
            "runtime_routing.research_request_modes должен быть объектом."
        )
    for model_role in configured_research_modes:
        settings.research_request_modes(str(model_role))
    configured_assistant_modes = (
        runtime_routing.get("assistant_request_modes", {})
        if isinstance(runtime_routing, Mapping)
        else {}
    )
    if not isinstance(configured_assistant_modes, Mapping):
        raise ConfigError(
            "runtime_routing.assistant_request_modes должен быть объектом."
        )
    for model_role in configured_assistant_modes:
        if str(model_role) not in settings.model_roles():
            raise ConfigError(
                "runtime_routing.assistant_request_modes содержит неизвестную роль: "
                f"{model_role}."
            )
        settings.assistant_request_mode(str(model_role))
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


def set_user_microphone(root: Path, selector: str) -> Path:
    """Persist a stable microphone name fragment without replacing voice settings."""
    target = root.resolve() / "config" / "user.json"
    selector = str(selector).strip()

    def edit(value: dict[str, Any]) -> None:
        voice = value.setdefault("voice", {})
        if not isinstance(voice, dict):
            raise ConfigError("Раздел voice в пользовательской конфигурации повреждён.")
        if selector:
            voice["wake_device"] = selector
        else:
            voice.pop("wake_device", None)
            if not voice:
                value.pop("voice", None)

    return _edit_user_settings(target, edit)
