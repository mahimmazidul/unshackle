from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from filelock import FileLock, Timeout


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_state_name(watcher_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", watcher_id).strip("._") or "watcher"
    return cleaned[:120]


def default_state(watcher_id: str, target: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "version": 1,
        "watcher_id": watcher_id,
        "target": target or {},
        "last_processed": None,
        "processed": {},
        "pending": None,
        "recovery_mode": False,
        "window": {"slot": None, "checks": [], "last_poll_at": None},
        "last_error": None,
        "updated_at": None,
    }


class WatcherState:
    """Atomic JSON state plus a small append-only event journal.

    The lock is held by the scheduler for one target while it performs a poll/download,
    so cron invocations cannot trigger the same episode twice.
    """

    def __init__(self, root: Path, watcher_id: str):
        self.root = root
        self.watcher_id = watcher_id
        name = safe_state_name(watcher_id)
        self.path = root / f"{name}.json"
        self.backup_path = root / f"{name}.json.bak"
        self.lock_path = root / f"{name}.lock"
        self.events_path = root / f"{name}.events.jsonl"

    def lock(self, timeout: float = 0) -> FileLock:
        self.root.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.lock_path), timeout=timeout)
        lock.acquire()
        return lock

    def _read(self, path: Path) -> Optional[dict[str, Any]]:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _journal_state(self) -> Optional[dict[str, Any]]:
        """Rebuild the success portion when the main JSON state was deleted."""
        if not self.events_path.is_file():
            return None
        state = default_state(self.watcher_id)
        found = False
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") != "download_success":
                continue
            found = True
            selector = event.get("selector")
            item_id = event.get("item_id")
            if selector:
                state["processed"][selector] = {
                    "selector": selector,
                    "item_id": item_id,
                    "completed_at": event.get("time"),
                }
                state["last_processed"] = state["processed"][selector]
        return state if found else None

    def load(self, target: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], bool]:
        """Return (state, existed).

        A backup or event journal counts as recovery material, but not as a complete
        current JSON file. The caller can decide whether to backfill missing items.
        """
        state = self._read(self.path)
        existed = state is not None
        if state is None:
            state = self._read(self.backup_path) or self._journal_state() or default_state(self.watcher_id, target)
        state.setdefault("version", 1)
        state.setdefault("watcher_id", self.watcher_id)
        state.setdefault("target", target or {})
        state.setdefault("processed", {})
        state.setdefault("pending", None)
        state.setdefault("window", {"slot": None, "checks": [], "last_poll_at": None})
        state.setdefault("last_error", None)
        state.setdefault("recovery_mode", False)
        return state, existed

    def save(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = utc_now()
        payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
        if self.path.exists():
            try:
                os.replace(self.path, self.backup_path)
            except OSError:
                pass
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def event(self, event: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        entry = {"time": utc_now(), **event}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @contextmanager
    def acquired(self, timeout: float = 0) -> Iterator[FileLock]:
        lock = self.lock(timeout=timeout)
        try:
            yield lock
        finally:
            lock.release()


__all__ = ("WatcherState", "default_state", "safe_state_name", "utc_now", "Timeout")
