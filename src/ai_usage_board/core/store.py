"""Thread-safe snapshot store with an on-disk cache.

Every provider thread writes here; the Tk thread only ever reads
:meth:`UsageStore.snapshot`, which hands back a deep copy. That keeps the UI
free of locks without risking a torn read.
"""
from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import atomic_write
from .models import SERVICE_ORDER, SnapshotError, validate_snapshot


class UsageStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self.reveal_requested = threading.Event()
        #: Set by the bridge when another launch asks this instance to quit.
        #: The widget owns the shutdown, so the request is only a flag here.
        self.shutdown_requested = threading.Event()
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(stored, dict):
            return
        for key in SERVICE_ORDER:
            item = stored.get(key)
            if not isinstance(item, dict):
                continue
            try:
                snapshot = validate_snapshot(item, source=str(item.get("source") or "cache"))
            except SnapshotError:
                continue
            # Keep the original timestamp so stale data is visibly stale.
            with contextlib.suppress(Exception):
                snapshot["updatedAt"] = float(item.get("updatedAt") or 0.0)
            self._data[key] = snapshot

    def _persist_locked(self) -> None:
        with contextlib.suppress(OSError):
            atomic_write(self.path, json.dumps(self._data, ensure_ascii=False))

    # -- writes -----------------------------------------------------------
    def update(self, snapshot: Dict[str, Any]) -> None:
        """Store a validated snapshot."""
        with self._lock:
            self._data[snapshot["provider"]] = snapshot
            self._persist_locked()

    def fail(self, provider: str, message: str, *, source: str = "cli", error_type: str = "unknown") -> None:
        """Mark a provider as failing without discarding the last good numbers."""
        with self._lock:
            item = dict(self._data.get(provider) or {"provider": provider, "windows": []})
            item["error"] = message
            item["errorType"] = error_type
            item["source"] = source
            item.setdefault("note", "")
            item.setdefault("updatedAt", 0.0)
            self._data[provider] = item
            self._persist_locked()

    def clear_error(self, provider: str) -> None:
        with self._lock:
            item = self._data.get(provider)
            if item:
                item.pop("error", None)
                item.pop("errorType", None)

    # -- reads ------------------------------------------------------------
    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def get(self, provider: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._data.get(provider)
            return json.loads(json.dumps(item)) if item else None

