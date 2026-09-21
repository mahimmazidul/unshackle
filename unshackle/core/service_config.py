from __future__ import annotations

from pathlib import Path
from typing import Optional

import click
import yaml

from unshackle.core.config import config, restore_bool_languages
from unshackle.core.utils.collections import ci_get, merge_dict

# Service YAML that the matching service module indexes with a top-level key.
REQUIRED_SERVICE_KEYS = {
    "AMZN": ("regions",),
}


def unwrap_service_root(data: dict, tag: str) -> dict:
    """If the YAML is wrapped as ``TAG: {...}``, return the inner mapping."""
    if not isinstance(data, dict) or len(data) != 1:
        return data
    key = next(iter(data))
    if isinstance(key, str) and key.lower() == tag.lower() and isinstance(data[key], dict):
        return data[key]
    return data


def read_service_yaml(path: Path) -> dict:
    """Load a service config.yaml as a mapping. Empty files become {}."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise click.ClickException(
            f"Service config {path} must be a YAML mapping, not {type(raw).__name__}."
        )
    return restore_bool_languages(raw)


def parse_service_config_file(path: Path, tag: str) -> dict:
    """Read ``path`` and unwrap a single ``TAG:`` root if present."""
    return unwrap_service_root(read_service_yaml(path), tag)


def merge_service_overlay(data: dict, tag: str, services_map: Optional[dict] = None) -> dict:
    """Merge ``services.<TAG>`` from unshackle.yaml into the service YAML in-place."""
    overlay = ci_get(services_map if services_map is not None else config.services, tag) or {}
    if overlay and isinstance(overlay, dict):
        merge_dict(overlay, data)
    return data


def missing_service_config_key_message(tag: str, key: str, data: dict, path: Optional[Path]) -> str:
    where = str(path) if path else f"{tag}/{config.filenames.config}"
    loaded = ", ".join(sorted(map(str, data))) or "(none)"
    return (
        f"{tag} config is missing {key!r}. Expected a top-level {key!r} in {where} "
        f"(or services.{tag}.{key} in unshackle.yaml). Loaded keys: {loaded}."
    )


def check_service_config_keys(tag: str, data: dict, path: Optional[Path]) -> None:
    """Raise if a known service YAML is missing keys that module indexes directly."""
    for key in REQUIRED_SERVICE_KEYS.get(tag.upper(), ()):
        if key not in data:
            raise click.ClickException(missing_service_config_key_message(tag, key, data, path))
