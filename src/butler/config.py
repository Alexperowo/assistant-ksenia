from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


def _resolve(root: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


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
    def default_role(self) -> str:
        return str(self.raw["assistant"]["default_role"])

    @property
    def state_file(self) -> Path:
        return self.runtime_dir / "state.json"

    def model(self, role: str) -> ModelProfile:
        try:
            item = self.raw["models"][role]
        except KeyError as exc:
            raise ConfigError(f"Неизвестная роль модели: {role}") from exc
        model_path_raw = item.get("path")
        model_path = (
            _resolve(self.root, str(model_path_raw))
            if model_path_raw
            else self.models_dir / str(item["filename"])
        )
        return ModelProfile(
            role=role,
            label=str(item["label"]),
            model_path=model_path.resolve(),
            context_size=int(item["context_size"]),
            gpu_layers=(
                int(item["gpu_layers"])
                if isinstance(item["gpu_layers"], int)
                else str(item["gpu_layers"])
            ),
            enabled=bool(item.get("enabled", True)),
            experimental=bool(item.get("experimental", False)),
            extra_args=tuple(str(arg) for arg in item.get("extra_args", [])),
            reasoning=str(item.get("reasoning", "off")).casefold(),
            expected_size_bytes=max(0, int(item.get("expected_size_bytes", 0) or 0)),
            sha256=str(item.get("sha256", "")).strip().casefold(),
        )

    def model_roles(self) -> tuple[str, ...]:
        return tuple(self.raw.get("models", {}).keys())

    def capability_role(self, name: str) -> CapabilityRole:
        try:
            item = self.raw["capability_roles"][name]
        except KeyError as exc:
            raise ConfigError(f"Неизвестная функциональная роль: {name}") from exc
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
            enabled=bool(item.get("enabled", True)),
        )

    def capability_role_names(self) -> tuple[str, ...]:
        return tuple(self.raw.get("capability_roles", {}).keys())


def load_settings(root: Path | None = None) -> Settings:
    root = (root or project_root()).resolve()
    default_path = root / "config" / "default.json"
    user_path = root / "config" / "user.json"
    raw = _read_json(default_path)
    if user_path.exists():
        raw = _deep_merge(raw, _read_json(user_path))

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
    return Settings(
        root=root,
        raw=raw,
        llama_server=_resolve(root, str(paths["llama_server"])),
        models_dir=_resolve(root, str(paths["models_dir"])),
        runtime_dir=_resolve(root, str(paths["runtime_dir"])),
        host=host,
        port=port,
        startup_timeout_seconds=int(server.get("startup_timeout_seconds", 120)),
    )


def write_user_settings(root: Path, llama_server: Path, models_dir: Path) -> Path:
    target = root.resolve() / "config" / "user.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "paths": {
            "llama_server": str(llama_server.resolve()),
            "models_dir": str(models_dir.resolve()),
        }
    }
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def set_user_model(root: Path, role: str, model_path: Path, *, enabled: bool = True) -> Path:
    target = root.resolve() / "config" / "user.json"
    value = _read_json(target) if target.exists() else {}
    models = value.setdefault("models", {})
    if not isinstance(models, dict):
        raise ConfigError("Раздел models в пользовательской конфигурации повреждён.")
    profile = models.setdefault(role, {})
    if not isinstance(profile, dict):
        raise ConfigError(f"Профиль модели {role} повреждён.")
    profile["path"] = str(model_path.resolve())
    profile["enabled"] = enabled
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def set_user_reasoning(root: Path, role: str, level: str) -> Path:
    reasoning_arguments(level)
    target = root.resolve() / "config" / "user.json"
    value = _read_json(target) if target.exists() else {}
    models = value.setdefault("models", {})
    if not isinstance(models, dict):
        raise ConfigError("Раздел models в пользовательской конфигурации повреждён.")
    profile = models.setdefault(role, {})
    if not isinstance(profile, dict):
        raise ConfigError(f"Профиль модели {role} повреждён.")
    profile["reasoning"] = level
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def set_user_response_budget(root: Path, max_tokens: int) -> Path:
    try:
        _label, research_max_tokens, plan_max_tokens = RESPONSE_BUDGETS[int(max_tokens)]
    except (KeyError, TypeError, ValueError) as exc:
        available = ", ".join(str(value) for value in RESPONSE_BUDGETS)
        raise ConfigError(f"Неизвестный лимит ответа: {max_tokens}. Доступно: {available}.") from exc
    target = root.resolve() / "config" / "user.json"
    value = _read_json(target) if target.exists() else {}
    generation = value.setdefault("generation", {})
    routing = value.setdefault("routing", {})
    if not isinstance(generation, dict) or not isinstance(routing, dict):
        raise ConfigError("Раздел генерации или маршрутизации повреждён.")
    generation["max_tokens"] = int(max_tokens)
    routing["research_turn_max_tokens"] = research_max_tokens
    routing["plan_max_tokens"] = plan_max_tokens
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def set_user_headset_control(
    root: Path,
    button: str,
    *,
    enabled: bool = True,
    consume: bool = True,
) -> Path:
    target = root.resolve() / "config" / "user.json"
    value = _read_json(target) if target.exists() else {}
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
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target
