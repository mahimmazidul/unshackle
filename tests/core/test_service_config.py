from __future__ import annotations

from pathlib import Path

import click
import pytest

from unshackle.core.service_config import (
    check_service_config_keys,
    merge_service_overlay,
    missing_service_config_key_message,
    parse_service_config_file,
    unwrap_service_root,
)


def test_unwrap_service_root_strips_tag_wrapper() -> None:
    inner = {"regions": {"us": {"base": "https://example.invalid"}}}
    assert unwrap_service_root({"AMZN": inner}, "AMZN") == inner
    assert unwrap_service_root({"amzn": inner}, "AMZN") == inner
    assert unwrap_service_root(inner, "AMZN") == inner
    assert unwrap_service_root({"regions": {}, "device": "web"}, "AMZN")["device"] == "web"


def test_amzn_missing_regions_names_key_and_path() -> None:
    path = Path("/home/beuz/.local/unshackle/services/AMZN/config.yaml")
    check_service_config_keys("EXAMPLE", {"endpoints": {}}, path)
    with pytest.raises(click.ClickException) as exc:
        check_service_config_keys("AMZN", {"endpoints": {}}, path)
    message = str(exc.value)
    assert "regions" in message
    assert str(path) in message
    assert "endpoints" in message
    assert "unshackle.yaml" in message


def test_missing_key_message_when_file_absent() -> None:
    message = missing_service_config_key_message("AMZN", "regions", {}, None)
    assert "AMZN/config.yaml" in message
    assert "(none)" in message


def test_parse_service_config_unwraps_tag_root(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("AMZN:\n  regions:\n    us: {}\n  device: web\n", encoding="utf-8")
    data = parse_service_config_file(path, "AMZN")
    assert data["regions"] == {"us": {}}
    assert data["device"] == "web"


def test_parse_service_config_empty_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("# comments only\n", encoding="utf-8")
    assert parse_service_config_file(path, "EXAMPLE") == {}


def test_merge_service_overlay_is_case_insensitive() -> None:
    data = {"regions": {"us": {}}, "device": "web"}
    merge_service_overlay(data, "AMZN", {"amzn": {"device": "tv"}})
    assert data["regions"] == {"us": {}}
    assert data["device"] == "tv"
