from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from unshackle.core.config import config
from unshackle.core.titles import Episode
from unshackle.core.watcher.scheduler import WatchTarget, WatcherScheduler
from unshackle.core.watcher.selectors import parse_wanted
from unshackle.core.watcher.state import WatcherState

pytestmark = pytest.mark.unit


class FakeService:
    pass


def episode(number: int, part: int | None = None) -> Episode:
    return Episode(str(number) * 4, FakeService, "Example", 2, number, part=part)


def test_wanted_reuses_season_range_and_keeps_parts() -> None:
    assert parse_wanted("S02E07,S02E07.2") == {"2x7", "2x7.2"}

    target = WatchTarget.from_mapping(
        {
            "id": "explicit",
            "service": "example",
            "title_ref": "https://example.test/show/opaque",
            "mode": "explicit",
            "wanted": ["S02E01", "S02E03"],
        },
        0,
    )
    scheduler = WatcherScheduler([target], force=True)
    state = {"processed": {}, "last_processed": None, "pending": None}
    candidates = scheduler._candidate_items(target, [episode(1), episode(2), episode(3)], state)
    assert [item.number for item in candidates] == [1, 3]


def test_sequential_first_run_is_safe_and_recovery_is_staged() -> None:
    target = WatchTarget.from_mapping(
        {
            "id": "sequential",
            "service": "example",
            "title_ref": "full-url-is-kept",
            "mode": "sequential",
        },
        0,
    )
    scheduler = WatcherScheduler([target], force=True)
    state = {"processed": {}, "last_processed": None, "pending": None}
    assert scheduler._candidate_items(target, [episode(1), episode(2)], state) == []
    assert state["last_processed"]["selector"] == "S02E02"

    state = {"processed": {}, "last_processed": None, "pending": None, "recovery_mode": True}
    candidates = scheduler._candidate_items(target, [episode(1), episode(2)], state)
    assert [item.number for item in candidates] == [1, 2]


def test_pending_retry_remains_due_after_a_release_window() -> None:
    target = WatchTarget.from_mapping(
        {
            "id": "pending",
            "service": "example",
            "title_ref": "url",
            "schedule": {"weekday": "thursday", "release_time": "20:00"},
        },
        0,
    )
    scheduler = WatcherScheduler([target])
    state = {
        "pending": {"S01E01": {"next_retry_at": "2026-09-18T00:00:00+00:00"}},
        "window": {"last_poll_at": "2026-09-17T23:00:00+00:00"},
    }
    due, marker = scheduler._due(target, state, datetime.fromisoformat("2026-09-18T01:00:00+00:00"))
    assert due and marker == "pending"


def test_release_window_includes_preflight_and_bounded_burst() -> None:
    target = WatchTarget.from_mapping(
        {
            "id": "release",
            "service": "example",
            "title_ref": "url",
            "schedule": {
                "timezone": "Asia/Dhaka",
                "weekday": "thursday",
                "release_time": "20:00",
                "preflight_minutes": 5,
                "burst_poll_seconds": 3,
                "burst_window_minutes": 30,
            },
        },
        0,
    )
    before = datetime.fromisoformat("2026-09-17T19:56:00+06:00")
    after = datetime.fromisoformat("2026-09-17T20:31:00+06:00")
    assert target.current_window(before) is not None
    assert target.current_window(after) is None


def test_state_journal_rebuilds_successes(tmp_path: Path) -> None:
    store = WatcherState(tmp_path, "journal test")
    state, existed = store.load({"title_ref": "exact-url"})
    assert not existed
    state["processed"]["S01E01"] = {"selector": "S01E01", "item_id": "1"}
    store.save(state)
    store.event({"event": "download_success", "selector": "S01E01", "item_id": "1"})
    store.path.unlink()

    recovered, existed = store.load()
    assert not existed
    assert recovered["processed"]["S01E01"]["item_id"] == "1"


def test_latest_and_explicit_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="cannot combine"):
        WatchTarget.from_mapping(
            {
                "service": "example",
                "title_ref": "url",
                "mode": "latest",
                "wanted": "S01",
            },
            0,
        )


def test_trigger_passes_exact_reference_and_normal_pipeline_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import ModuleType, SimpleNamespace

    import unshackle.core.watcher.scheduler as scheduler_module

    calls: list[dict] = []

    class Runtime:
        tag = "CANONICAL"
        service_class = SimpleNamespace(DAILY=False)

        def fresh_titles(self):
            return [episode(1)]

        def resolve_metadata(self, titles):
            return None

        def resolve_download_proxy(self, value):
            return value

        def close(self):
            pass

    def perform_download(**kwargs):
        calls.append(kwargs)
        return ["/tmp/output.mkv"]

    fake_manager = ModuleType("unshackle.core.api.download_manager")
    fake_manager.perform_download = perform_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "unshackle.core.api.download_manager", fake_manager)
    monkeypatch.setattr(scheduler_module, "ServiceRuntime", lambda target: Runtime())
    monkeypatch.setattr(config.directories, "data", tmp_path)
    monkeypatch.setattr(config.directories, "watchers", tmp_path / "watchers", raising=False)

    target = WatchTarget.from_mapping(
        {
            "id": "trigger",
            "service": "alias",
            "title_ref": "https://service.example/full/path?id=opaque",
            "mode": "explicit",
            "wanted": "S02E01",
            "proxy": {"service": "direct", "download": "http://download-proxy:8080"},
            "availability": {"no_cache": True, "stability_delay": 0},
        },
        0,
    )
    scheduler = WatcherScheduler([target], force=True)
    scheduler.process_target(target, now=datetime.now().astimezone(), marker="forced")
    scheduler.close()

    assert calls[0]["service"] == "CANONICAL"
    assert calls[0]["title_id"] == "https://service.example/full/path?id=opaque"
    assert calls[0]["params"]["wanted"] == "S02E01"
    assert calls[0]["params"]["no_cache"] is True
    assert calls[0]["params"]["proxy"] == "http://download-proxy:8080"
    assert calls[0]["params"]["output_dir"] == str(config.directories.downloads)


def test_watchers_load_from_unshackle_yaml_and_download_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unshackle.core.watcher.scheduler import load_targets_from_mapping, load_watchers

    targets, notifications = load_targets_from_mapping(
        {
            "notifications": {"error_cooldown": 30},
            "watchers": [
                {
                    "id": "from-unshackle-yaml",
                    "service": "example",
                    "title_ref": "https://service.example/shows/inline",
                    "mode": "sequential",
                    "download": {},
                }
            ],
        }
    )
    assert [target.id for target in targets] == ["from-unshackle-yaml"]
    assert notifications["error_cooldown"] == 30

    monkeypatch.setattr(config, "watchers", [{"id": "inline", "service": "example", "title_ref": "url"}], raising=False)
    monkeypatch.setattr(config, "watch", {"notifications": {"error_cooldown": 12}}, raising=False)
    loaded, notes, source = load_watchers(None)
    assert [target.id for target in loaded] == ["inline"]
    assert notes["error_cooldown"] == 12
    assert source.endswith("unshackle.yaml") or source == "unshackle.yaml"

    explicit = tmp_path / "watchers.yaml"
    explicit.write_text("watchers:\n  - id: file\n    service: example\n    title_ref: url\n", encoding="utf-8")
    loaded, _notes, source = load_watchers(explicit)
    assert loaded[0].id == "file"
    assert source == str(explicit)

    monkeypatch.setattr(config.directories, "downloads", tmp_path / "downloads")
    monkeypatch.setattr(config.directories, "data", tmp_path)
    monkeypatch.setattr(config.directories, "watchers", tmp_path / "watcher-state")
    scheduler = WatcherScheduler(targets)
    assert scheduler.state_root == tmp_path / "watcher-state"
    download_params: dict = {}
    if not download_params.get("output_dir"):
        download_params["output_dir"] = str(config.directories.downloads)
    assert download_params["output_dir"] == str(tmp_path / "downloads")


def test_notifier_messages_have_no_emoji() -> None:
    from unshackle.core.watcher.notify import Notifier

    sent: list[str] = []
    notifier = Notifier({"error_cooldown": 0})
    notifier.send = sent.append  # type: ignore[method-assign]
    notifier.success({"service": "example", "title": "Show", "selector": "S01E01", "output_files": []})
    notifier.error({"service": "example", "title": "Show", "phase": "availability_check", "error": "boom"})
    assert sent and all("✅" not in message and "❌" not in message for message in sent)
    assert sent[0].startswith("Unshackle watcher download successful")
    assert sent[1].startswith("Unshackle watcher error")
