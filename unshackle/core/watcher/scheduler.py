from __future__ import annotations

import copy
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import yaml

from unshackle.core.config import config
from unshackle.core.titles import Episode
from unshackle.core.watcher.notify import Notifier
from unshackle.core.watcher.runtime import ServiceRuntime
from unshackle.core.watcher.selectors import (
    episode_key,
    episode_selector,
    matches_wanted,
    parse_wanted,
    title_identity,
)
from unshackle.core.watcher.state import Timeout, WatcherState, utc_now

log = logging.getLogger("watcher")


WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _parse_seconds(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> dtime:
    if isinstance(value, dtime):
        return value
    text = str(value or "00:00").strip()
    hour, minute = text.split(":", 1)
    return dtime(int(hour), int(minute))


def _parse_weekdays(value: Any) -> list[int]:
    result: list[int] = []
    for item in _as_list(value or ["monday"]):
        if isinstance(item, int) and 0 <= item <= 6:
            result.append(item)
            continue
        key = str(item).strip().lower()
        if key not in WEEKDAYS:
            raise ValueError(f"Unknown watcher weekday: {item!r}")
        result.append(WEEKDAYS[key])
    return sorted(set(result))


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed.astimezone() if parsed.tzinfo else parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


@dataclass
class WatchTarget:
    id: str
    service: str
    title_ref: str
    profile: Optional[str] = None
    mode: str = "sequential"
    wanted: Any = None
    service_params: dict[str, Any] = field(default_factory=dict)
    proxy: dict[str, Any] = field(default_factory=dict)
    schedule: dict[str, Any] = field(default_factory=dict)
    availability: dict[str, Any] = field(default_factory=dict)
    download: dict[str, Any] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)

    @property
    def service_proxy(self) -> Any:
        return self.proxy.get("service", self.proxy.get("availability"))

    @property
    def metadata_proxy(self) -> Any:
        return self.proxy.get("metadata", "direct")

    @property
    def download_proxy(self) -> Any:
        value = self.proxy.get("download", "inherit")
        return self.service_proxy if value in (None, "inherit") else value

    @property
    def no_cache(self) -> bool:
        return bool(self.availability.get("no_cache", True))

    @property
    def daily(self) -> bool:
        return bool(self.availability.get("daily", False))

    @property
    def wanted_keys(self) -> set[str]:
        return parse_wanted(self.wanted)

    @property
    def stability_delay(self) -> int:
        return _parse_seconds(self.availability.get("stability_delay", 0), 0)

    @property
    def poll_interval(self) -> int:
        return _parse_seconds(self.schedule.get("poll_interval", 300), 300)

    @property
    def initial_policy(self) -> str:
        return str(self.recovery.get("initial_policy", "latest")).lower()

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], index: int) -> "WatchTarget":
        if not isinstance(raw, dict):
            raise TypeError(f"watchers[{index}] must be a mapping")
        watcher_id = str(raw.get("id") or f"watcher-{index + 1}")
        service = raw.get("service")
        title_ref = raw.get("title_ref", raw.get("title"))
        if not service or not title_ref:
            raise ValueError(f"watcher {watcher_id!r} needs service and title_ref")
        proxy = raw.get("proxy") or {}
        if isinstance(proxy, str):
            proxy = {"service": proxy, "download": proxy}
        mode = str(raw.get("mode", "sequential")).lower()
        if mode not in {"sequential", "latest", "explicit"}:
            raise ValueError(f"watcher {watcher_id!r} has unsupported mode {mode!r}")
        if mode == "latest" and raw.get("wanted"):
            raise ValueError(f"watcher {watcher_id!r} cannot combine latest mode with wanted selectors")
        if mode == "explicit" and not raw.get("wanted"):
            raise ValueError(f"watcher {watcher_id!r} explicit mode needs wanted selectors")
        return cls(
            id=watcher_id,
            service=str(service),
            title_ref=str(title_ref),
            profile=raw.get("profile"),
            mode=mode,
            wanted=raw.get("wanted"),
            service_params=dict(raw.get("service_params") or {}),
            proxy=dict(proxy),
            schedule=dict(raw.get("schedule") or {}),
            availability=dict(raw.get("availability") or {}),
            download=copy.deepcopy(dict(raw.get("download") or {})),
            recovery=dict(raw.get("recovery") or {}),
        )

    def state_target(self) -> dict[str, Any]:
        """Non-secret target information kept with state for recovery diagnostics."""
        return {
            "service": self.service,
            "title_ref": self.title_ref,
            "profile": self.profile,
            "mode": self.mode,
            "wanted": self.wanted,
        }

    def scheduled_release(self, now: datetime) -> Optional[datetime]:
        weekdays = _parse_weekdays(self.schedule.get("weekdays", self.schedule.get("weekday")))
        release_time = _parse_time(self.schedule.get("release_time", "00:00"))
        timezone_name = str(self.schedule.get("timezone") or "UTC")
        try:
            timezone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ValueError(f"Invalid timezone for watcher {self.id}: {timezone_name}") from exc
        local_now = now.astimezone(timezone)
        candidates: list[datetime] = []
        for offset in range(-8, 3):
            day = local_now.date() + timedelta(days=offset)
            if day.weekday() in weekdays:
                candidates.append(datetime.combine(day, release_time, tzinfo=timezone))
        return max((candidate for candidate in candidates if candidate <= local_now), default=None)

    def current_window(self, now: datetime) -> Optional[tuple[datetime, datetime, datetime]]:
        timezone_name = str(self.schedule.get("timezone") or "UTC")
        timezone = ZoneInfo(timezone_name)
        local_now = now.astimezone(timezone)
        preflight = _parse_seconds(self.schedule.get("preflight_minutes", 5), 5) * 60
        burst_window = _parse_seconds(self.schedule.get("burst_window_minutes", 30), 30) * 60
        checks = self.schedule.get("checks_after_release", [0, 5, 15, 30])
        offsets = [int(x) * 60 for x in _as_list(checks)] if checks is not None else [0]
        end_offset = max([burst_window, *offsets], default=burst_window)
        weekdays = _parse_weekdays(self.schedule.get("weekdays", self.schedule.get("weekday")))
        release_time = _parse_time(self.schedule.get("release_time", "00:00"))
        for offset in range(-2, 9):
            day = local_now.date() + timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            release = datetime.combine(day, release_time, tzinfo=timezone)
            start = release - timedelta(seconds=preflight)
            end = release + timedelta(seconds=end_offset)
            if start <= local_now <= end:
                return release, start, end
        return None


class WatcherScheduler:
    def __init__(
        self,
        targets: list[WatchTarget],
        *,
        once: bool = False,
        force: bool = False,
        recover: bool = False,
        from_date: Optional[date] = None,
        only: Optional[set[str]] = None,
        notifications: Optional[dict[str, Any]] = None,
    ):
        self.targets = [target for target in targets if not only or target.id in only]
        self.once = once
        self.force = force
        self.recover = recover
        self.from_date = from_date
        self.notifications: dict[str, Any] = notifications or {}
        self.notifier = Notifier(self.notifications)
        self.runtimes: dict[str, ServiceRuntime] = {}
        watchers_dir = getattr(config.directories, "watchers", None) or (config.directories.data / "watchers")
        self.state_root = Path(watchers_dir).expanduser()
        self._processed_recovery: dict[str, int] = {}

    def _state(self, target: WatchTarget) -> WatcherState:
        return WatcherState(self.state_root, target.id)

    def _runtime(self, target: WatchTarget) -> ServiceRuntime:
        runtime = self.runtimes.get(target.id)
        if runtime is None:
            runtime = ServiceRuntime(target)
            self.runtimes[target.id] = runtime
        return runtime

    def _daily_for(self, target: WatchTarget, item: Any = None) -> bool:
        if target.daily:
            return True
        runtime = self.runtimes.get(target.id)
        service_daily = bool(getattr(getattr(runtime, "service_class", None), "DAILY", False))
        per_title = getattr(item, "daily", None) if item is not None else None
        return service_daily if per_title is None else bool(per_title)

    def _due(self, target: WatchTarget, state: dict[str, Any], now: datetime) -> tuple[bool, Optional[str]]:
        if self.force:
            return True, "forced"
        pending = state.get("pending")
        if isinstance(pending, dict) and pending:
            last = _parse_iso((state.get("window") or {}).get("last_poll_at"))
            pending_delay = _parse_seconds(target.availability.get("pending_poll_seconds", 30), 30)
            can_poll = last is None or (now - last).total_seconds() >= pending_delay
            if can_poll:
                for record in pending.values():
                    if not isinstance(record, dict):
                        continue
                    threshold = _parse_iso(record.get("next_retry_at") or record.get("eligible_at"))
                    if threshold is None or now >= threshold:
                        # A pending manifest remains retryable outside a weekly
                        # release window; it must not wait until next week's slot.
                        return True, "pending"
        if state.get("recovery_mode"):
            last = _parse_iso((state.get("window") or {}).get("last_poll_at"))
            delay = _parse_seconds(target.recovery.get("delay_between_downloads", 30), 30)
            if last is None or (now - last).total_seconds() >= delay:
                return True, "recovery"
            return False, None
        window = target.current_window(now)
        if target.schedule.get("weekday") is None and target.schedule.get("weekdays") is None:
            last = _parse_iso((state.get("window") or {}).get("last_poll_at"))
            if last is None or (now - last).total_seconds() >= target.poll_interval:
                return True, "interval"
            return False, None
        if window is None:
            return False, None
        release, _start, _end = window
        slot = release.isoformat()
        stored = state.setdefault("window", {"slot": None, "checks": [], "last_poll_at": None})
        if stored.get("slot") != slot:
            stored["slot"] = slot
            stored["checks"] = []
            stored["last_poll_at"] = None
        local_now = now.astimezone(release.tzinfo)
        if local_now < release:
            return ("preflight" not in stored["checks"]), "preflight"
        burst_seconds = _parse_seconds(target.schedule.get("burst_poll_seconds", 0), 0)
        burst_end = release + timedelta(minutes=_parse_seconds(target.schedule.get("burst_window_minutes", 30), 30))
        if burst_seconds and local_now <= burst_end:
            last = _parse_iso(stored.get("last_poll_at"))
            if last is None or (now - last).total_seconds() >= burst_seconds:
                return True, "burst"
            return False, None
        offsets = [int(x) for x in _as_list(target.schedule.get("checks_after_release", [0, 5, 15, 30]))]
        for offset in offsets:
            marker = f"after:{offset}"
            if marker not in stored["checks"] and local_now >= release + timedelta(minutes=offset):
                return True, marker
        return False, None

    @staticmethod
    def _mark_poll(state: dict[str, Any], marker: Optional[str], now: datetime) -> None:
        window = state.setdefault("window", {"slot": None, "checks": [], "last_poll_at": None})
        window["last_poll_at"] = now.isoformat(timespec="seconds")
        if marker and marker not in ("burst", "interval", "forced", "recovery", "pending"):
            window.setdefault("checks", []).append(marker)

    @staticmethod
    def _items(titles: Any) -> list[Any]:
        if titles is None:
            return []
        return list(titles) if hasattr(titles, "__iter__") and not isinstance(titles, (str, bytes)) else [titles]

    def _candidate_items(self, target: WatchTarget, titles: Any, state: dict[str, Any]) -> list[Any]:
        items = self._items(titles)
        wanted = target.wanted_keys
        if wanted:
            items = [item for item in items if isinstance(item, Episode) and matches_wanted(item, wanted)]
        if not items:
            return []

        if isinstance(items[0], Episode):
            items.sort(key=episode_key)
            processed = state.setdefault("processed", {})
            if target.mode == "latest":
                return [items[-1]] if not self._already_processed(items[-1], target, state) else []
            if processed or state.get("last_processed"):
                last_key = self._last_episode_key(state)
                if not wanted:
                    items = [item for item in items if episode_key(item) > last_key]
                else:
                    items = [item for item in items if not self._already_processed(item, target, state)]
            elif wanted or state.get("recovery_mode") or target.initial_policy == "all":
                pass
            elif target.initial_policy == "latest":
                # Safe default on first use: do not unexpectedly download an entire back catalogue.
                latest = items[-1]
                state["last_processed"] = self._item_record(latest, target, skipped=True)
                return []
            return items

        movie = items[0]
        return [] if self._already_processed(movie, target, state) else [movie]

    @staticmethod
    def _last_episode_key(state: dict[str, Any]) -> tuple[int, int, int, int]:
        record = state.get("last_processed") or {}
        key = record.get("ordering_key") or [0, 0, 0, 0]
        try:
            return tuple(int(value) for value in key)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return (0, 0, 0, 0)

    def _item_record(self, item: Any, target: WatchTarget, skipped: bool = False) -> dict[str, Any]:
        kind, key, ordering = title_identity(item, daily=self._daily_for(target, item))
        record = {
            "kind": kind,
            "selector": key if kind == "episode" else None,
            "item_id": str(item.id),
            "ordering_key": list(ordering) if ordering else None,
            "completed_at": utc_now(),
        }
        if skipped:
            record["bootstrap_skipped"] = True
        return record

    def _already_processed(self, item: Any, target: WatchTarget, state: dict[str, Any]) -> bool:
        kind, key, _ordering = title_identity(item, daily=self._daily_for(target, item))
        if kind == "movie":
            return (state.get("last_processed") or {}).get("item_id") == str(item.id)
        return key in (state.get("processed") or {}) or key == (state.get("last_processed") or {}).get("selector")

    def _apply_from_date(self, items: list[Any]) -> list[Any]:
        if self.from_date is None:
            return items
        result: list[Any] = []
        for item in items:
            air_date = getattr(item, "air_date", None)
            if air_date is None or not isinstance(air_date, date) or air_date >= self.from_date:
                result.append(item)
        return result

    def _error_event(self, target: WatchTarget, phase: str, exc: BaseException, selector: Optional[str] = None) -> dict[str, Any]:
        return {
            "event": "watcher_error",
            "watcher_id": target.id,
            "service": target.service,
            "title_ref": target.title_ref,
            "selector": selector,
            "phase": phase,
            "error": f"{type(exc).__name__}: {exc}",
        }

    def _notify_error(self, target: WatchTarget, state: dict[str, Any], event: dict[str, Any]) -> None:
        if self.notifier.error(event, state):
            state["last_error"] = {
                "fingerprint": event.get("fingerprint"),
                "notified_at": event.get("notified_at"),
            }

    def _trigger(self, target: WatchTarget, item: Any, state: dict[str, Any]) -> None:
        selector: Optional[str] = None
        runtime = self._runtime(target)
        if isinstance(item, Episode):
            selector = episode_selector(item, daily=self._daily_for(target, item))
        download_params = copy.deepcopy(target.download)
        if not download_params.get("output_dir"):
            download_params["output_dir"] = str(Path(config.directories.downloads).expanduser())
        if isinstance(download_params.get("output_dir"), str):
            download_params["output_dir"] = str(Path(download_params["output_dir"]).expanduser())
        download_params["profile"] = target.profile
        download_params["no_cache"] = True
        download_params["service_params"] = target.service_params
        download_params["proxy"] = runtime.resolve_download_proxy(target.download_proxy)
        if target.download_proxy is not None and str(target.download_proxy).strip().lower() in {"direct", "none", "off"}:
            download_params["no_proxy"] = True
        if target.mode == "latest" and not target.wanted:
            download_params["latest_episode"] = True
            download_params.pop("wanted", None)
        elif selector:
            download_params["wanted"] = selector

        job_id = "watch-" + hashlib.sha256(f"{target.id}|{selector}|{time.time_ns()}".encode()).hexdigest()[:20]
        try:
            # Heavy downloader/CDM/vault/mux imports happen inside this trigger only.
            from unshackle.core.api.download_manager import perform_download

            files = perform_download(
                job_id=job_id,
                service=runtime.tag,
                title_id=target.title_ref,
                params=download_params,
            )
        except Exception as exc:  # normal API helper surfaces worker and pipeline errors
            pending = state.setdefault("pending", {})
            if not isinstance(pending, dict):
                pending = state["pending"] = {}
            pending_record = pending.get(selector or str(item.id), {})
            attempts = int(pending_record.get("attempts", 0)) + 1
            retry_delays = target.availability.get("retry_delays", [30, 60, 180, 300])
            delay = int(retry_delays[min(attempts - 1, len(retry_delays) - 1)]) if retry_delays else 300
            next_retry = datetime.now().astimezone() + timedelta(seconds=delay)
            pending[selector or str(item.id)] = {
                "selector": selector,
                "item_id": str(item.id),
                "first_seen_at": pending_record.get("first_seen_at", utc_now()),
                "eligible_at": pending_record.get("eligible_at", utc_now()),
                "attempts": attempts,
                "next_retry_at": next_retry.isoformat(timespec="seconds"),
            }
            event = self._error_event(target, "download_trigger", exc, selector)
            event["next_retry_at"] = next_retry.isoformat(timespec="seconds")
            self._notify_error(target, state, event)
            self._state(target).event(event)
            return

        record = self._item_record(item, target)
        if record["kind"] == "episode":
            state.setdefault("processed", {})[record["selector"]] = record
        state["last_processed"] = record
        state["pending"] = None
        state["last_error"] = None
        event = {
            "event": "download_success",
            "watcher_id": target.id,
            "service": target.service,
            "title": getattr(item, "title", None) or getattr(item, "name", None) or str(item),
            "title_ref": target.title_ref,
            "selector": selector,
            "item_id": str(item.id),
            "output_files": files,
        }
        self._state(target).event(event)
        self.notifier.success(event)

    def process_target(self, target: WatchTarget, now: Optional[datetime] = None, marker: Optional[str] = None) -> None:
        now = now or datetime.now().astimezone()
        state_store = self._state(target)
        try:
            lock = state_store.lock(timeout=0)
        except Timeout:
            log.debug("Watcher %s is already running; skipping overlapping invocation", target.id)
            return
        runtime: Optional[ServiceRuntime] = None
        try:
            state, existed = state_store.load(target.state_target())
            if self.recover:
                state["recovery_mode"] = True
                state.setdefault("recovery", {})["requested_at"] = utc_now()
            if not existed and self.recover:
                state["recovery_mode"] = True
            self._mark_poll(state, marker, now)
            try:
                runtime = self._runtime(target)
            except Exception as exc:
                event = self._error_event(target, "runtime_setup", exc)
                self._notify_error(target, state, event)
                state_store.event(event)
                state_store.save(state)
                return
            try:
                titles = runtime.fresh_titles()
            except Exception as exc:
                event = self._error_event(target, "availability_check", exc)
                self._notify_error(target, state, event)
                state_store.event(event)
                state_store.save(state)
                return

            try:
                metadata = runtime.resolve_metadata(titles)
                if metadata:
                    state["metadata"] = {
                        "title": metadata.title,
                        "year": metadata.year,
                        "source": metadata.source,
                        "external_ids": vars(metadata.external_ids),
                    }
            except Exception as exc:
                # Metadata is enrichment; title availability itself remains usable.
                log.warning("Metadata resolution failed for %s: %s", target.id, exc)

            candidates = self._apply_from_date(self._candidate_items(target, titles, state))
            if not candidates:
                if state.get("recovery_mode"):
                    state["recovery_mode"] = False
                    state["recovery_completed_at"] = utc_now()
                state_store.save(state)
                return

            item = candidates[0]
            selector = episode_selector(item, daily=self._daily_for(target, item)) if isinstance(item, Episode) else None
            pending_key = selector or str(item.id)
            pending = state.setdefault("pending", {})
            if not isinstance(pending, dict):
                pending = state["pending"] = {}
            existing = pending.get(pending_key) or {}
            now_iso = utc_now()
            eligible_at = existing.get("eligible_at")
            if not existing:
                eligible = now + timedelta(seconds=target.stability_delay)
                pending[pending_key] = {
                    "selector": selector,
                    "item_id": str(item.id),
                    "first_seen_at": now_iso,
                    "eligible_at": eligible.isoformat(timespec="seconds"),
                    "attempts": 0,
                }
                state_store.event({
                    "event": "candidate_seen",
                    "watcher_id": target.id,
                    "service": target.service,
                    "selector": selector,
                    "item_id": str(item.id),
                })
                # Persist before the trigger as well as after it. If the process is
                # interrupted during a zero-delay download, recovery sees the pending
                # item instead of treating it as a brand-new candidate.
                state_store.save(state)
                if target.stability_delay:
                    return
            elif existing.get("next_retry_at"):
                retry_at = _parse_iso(existing.get("next_retry_at"))
                if retry_at and now < retry_at:
                    state_store.save(state)
                    return

            if eligible_at:
                eligible = _parse_iso(eligible_at)
                if eligible and now < eligible:
                    state_store.save(state)
                    return
            self._trigger(target, item, state)
            state_store.save(state)
        finally:
            if runtime and marker in ("preflight", "forced"):
                # Keep the authenticated runtime alive for the release burst; it is
                # discarded at process exit or after a poll failure.
                pass
            lock.release()

    def close(self) -> None:
        for runtime in self.runtimes.values():
            runtime.close()
        self.runtimes.clear()

    def run_once(self) -> None:
        now = datetime.now().astimezone()
        for target in self.targets:
            state, _ = self._state(target).load(target.state_target())
            due, marker = self._due(target, state, now)
            if due:
                self.process_target(target, now=now, marker=marker)

    def run_forever(self) -> None:
        log.info("Watcher scheduler started for %d target(s)", len(self.targets))
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Watcher scheduler loop failed")
            # One-second scheduler resolution enables a true release burst without
            # contacting services when no target is due.
            time.sleep(1)


def load_targets_from_mapping(raw: dict[str, Any]) -> tuple[list[WatchTarget], dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("Watcher config must contain a mapping")
    watcher_rows = raw.get("watchers") or []
    if isinstance(watcher_rows, dict):
        watcher_rows = [dict(value, id=key) for key, value in watcher_rows.items()]
    if not isinstance(watcher_rows, list):
        raise ValueError("watchers must be a list or mapping")
    targets = [WatchTarget.from_mapping(row, index) for index, row in enumerate(watcher_rows)]
    notifications = dict(raw.get("notifications") or {})
    return targets, notifications


def load_targets(path: Path) -> tuple[list[WatchTarget], dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Watcher config was not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Watcher config must contain a mapping")
    return load_targets_from_mapping(raw)


def default_watcher_config_path() -> Path:
    """Path of the dedicated watcher YAML, taken from unshackle.yaml when set."""
    watch = getattr(config, "watch", None) or {}
    if isinstance(watch, dict) and watch.get("config"):
        return Path(str(watch["config"])).expanduser()
    filename = getattr(config.filenames, "watchers", "watchers.yaml")
    return Path(config.directories.user_configs) / filename


def configured_watchers_inline() -> Optional[tuple[list[WatchTarget], dict[str, Any]]]:
    """Load watcher targets defined in unshackle.yaml, if any."""
    watch = getattr(config, "watch", None) or {}
    inline = getattr(config, "watchers", None)
    notifications: dict[str, Any] = {}
    if isinstance(watch, dict):
        if inline is None:
            inline = watch.get("watchers")
        notifications = dict(watch.get("notifications") or {})
        if watch.get("notifications") is None and isinstance(watch.get("notify"), dict):
            notifications = dict(watch.get("notify") or {})
    if not inline:
        return None
    return load_targets_from_mapping({"watchers": inline, "notifications": notifications})


def load_watchers(path: Optional[Path] = None) -> tuple[list[WatchTarget], dict[str, Any], str]:
    """Resolve watcher targets from --config, then unshackle.yaml, then the default YAML."""
    if path is not None:
        targets, notifications = load_targets(path)
        return targets, notifications, str(path)
    inline = configured_watchers_inline()
    if inline is not None:
        from unshackle.core.config import config_path

        source = str(config_path) if config_path else "unshackle.yaml"
        return inline[0], inline[1], source
    default_path = default_watcher_config_path()
    targets, notifications = load_targets(default_path)
    return targets, notifications, str(default_path)


__all__ = (
    "WatchTarget",
    "WatcherScheduler",
    "default_watcher_config_path",
    "load_targets",
    "load_targets_from_mapping",
    "load_watchers",
)
