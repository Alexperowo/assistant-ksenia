from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ConversationMemory:
    """Durable conversation state with atomic writes and conservative recovery."""

    def __init__(self, runtime_dir: Path, *, max_messages: int = 80) -> None:
        self.path = runtime_dir / "memory" / "session.json"
        self.max_messages = max(8, max_messages)

    def load(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        messages = raw.get("messages", []) if isinstance(raw, dict) else []
        if not isinstance(messages, list):
            return []
        result: list[dict[str, Any]] = []
        for message in messages[-self.max_messages :]:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", ""))
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            result.append(dict(message))
        while result and result[0].get("role") not in {"system", "user"}:
            result.pop(0)
        return result

    def save(self, messages: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "messages": messages[-self.max_messages :],
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
