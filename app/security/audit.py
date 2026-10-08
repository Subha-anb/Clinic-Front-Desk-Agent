"""Append-only audit log of guardrail decisions. Message text is redacted before it is written."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .pii import redact


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def record(self, event: str, patient_ref: str, **fields) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "patient_ref": patient_ref,
            **{k: redact(v) if isinstance(v, str) else v for k, v in fields.items()},
        }
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class RateLimiter:
    """Sliding-window limit per patient, to blunt abuse and runaway LLM spend."""

    def __init__(self, max_messages: int = 20, window_seconds: int = 600):
        self.max_messages = max_messages
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            allowed = len(hits) < self.max_messages
            if allowed:
                hits.append(now)
            self._hits[key] = hits
            return allowed
