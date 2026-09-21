from __future__ import annotations

import copy
import inspect
import logging
import sys
from types import SimpleNamespace
from typing import Any, Optional

import click
import yaml

from unshackle.core import providers
from unshackle.core.auth import get_cookie_jar, get_cookie_path, get_credentials, save_cookies
from unshackle.core.config import config
from unshackle.core.proxies.resolve import initialize_proxy_providers, resolve_proxy
from unshackle.core.service import Service
from unshackle.core.services import Services
from unshackle.core.title_cacher import get_account_hash
from unshackle.core.utils.collections import merge_dict

log = logging.getLogger("watcher.runtime")


DIRECT_VALUES = {"", "none", "direct", "off", "false", "null"}


def _proxy_is_direct(value: Any) -> bool:
    return value is None or str(value).strip().lower() in DIRECT_VALUES


def _resolve_proxy(value: Any, proxy_providers: list[Any]) -> Optional[str]:
    if _proxy_is_direct(value):
        return None
    value = str(value).strip()
    if value.lower().startswith(("http://", "https://", "socks4://", "socks5://")):
        return value
    return resolve_proxy(value, proxy_providers)


def _list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _loaded_video_enum(name: str, values: list[Any]) -> list[Any]:
    """Normalize track options only if the selected service already loaded Video."""
    module = sys.modules.get("unshackle.core.tracks.video")
    enum_cls = getattr(getattr(module, "Video", None), name, None) if module else None
    if enum_cls is None:
        return values
    normalized: list[Any] = []
    for value in values:
        if isinstance(value, enum_cls):
            normalized.append(value)
            continue
        raw = str(value).strip().lower()
        normalized.append(
            next(
                (member for member in enum_cls if raw in (member.name.lower(), str(member.value).lower())),
                value,
            )
        )
    return normalized


def _load_service_config(tag: str) -> dict[str, Any]:
    merged = copy.deepcopy(config.services.get(tag, {}) or {})
    try:
        path = Services.get_path(tag) / config.filenames.config
    except KeyError:
        path = None
    if path and path.exists():
        service_file = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(service_file, dict):
            raise ValueError(f"Service config {path} must contain a mapping")
        merge_dict(service_file, merged)
    return merged


def _service_constructor_kwargs(service_class: type[Service], target: Any) -> dict[str, Any]:
    parameters = inspect.signature(service_class.__init__).parameters
    supplied = dict(getattr(target, "service_params", {}) or {})
    kwargs: dict[str, Any] = {}

    if "title" in parameters:
        kwargs["title"] = getattr(target, "title_ref")
    elif "title_id" in parameters:
        kwargs["title_id"] = getattr(target, "title_ref")

    for name, value in supplied.items():
        if name in parameters and name not in ("self", "ctx"):
            kwargs[name] = value

    profile = getattr(target, "profile", None)
    if "profile" in parameters and "profile" not in kwargs:
        kwargs["profile"] = profile

    for name, parameter in parameters.items():
        if name in ("self", "ctx") or name in kwargs:
            continue
        if parameter.default is not inspect.Parameter.empty:
            continue
        if name == "movie":
            kwargs[name] = "/movies/" in str(getattr(target, "title_ref", "")).lower()
        elif name == "meta_lang":
            kwargs[name] = None
        else:
            raise TypeError(
                f"Watcher target {getattr(target, 'id', '?')} must provide required service parameter '{name}'"
            )
    return kwargs


class ServiceRuntime:
    """A lightweight authenticated service session for title-only checks."""

    def __init__(self, target: Any):
        self.target = target
        self.tag = Services.get_tag(target.service)
        self.service_class = Services.load(self.tag)
        self.proxy_providers = initialize_proxy_providers(quiet=True)
        self.service_proxy = _resolve_proxy(target.service_proxy, self.proxy_providers)
        self.metadata_proxy = _resolve_proxy(target.metadata_proxy, self.proxy_providers)
        self.service: Optional[Service] = None
        self.authenticated = False
        self.service_config = _load_service_config(self.tag)

    def _build(self) -> Service:
        # Keep the service-specific config available through both the lightweight context
        # and config.services, matching the normal dl path.
        config.services[self.tag] = copy.deepcopy(self.service_config)
        requested_proxy = self.target.service_proxy
        explicit_no_proxy = _proxy_is_direct(requested_proxy) and requested_proxy is not None

        parent = click.Context(click.Command(name=f"watch-{self.tag}"))
        parent.invoked_subcommand = self.tag
        parent.params = {
            "proxy": self.service_proxy,
            "proxy_query": None if _proxy_is_direct(requested_proxy) else str(requested_proxy),
            "proxy_provider": None,
            "no_proxy": explicit_no_proxy,
            "profile": self.target.profile,
            "no_cache": bool(self.target.no_cache),
            "reset_cache": False,
            "quality": _list_value(self.target.download.get("quality")),
            "vcodec": _loaded_video_enum("Codec", _list_value(self.target.download.get("vcodec"))),
            "range_": _loaded_video_enum(
                "Range", _list_value(self.target.download.get("range")) or ["sdr"]
            ),
            "best_available": bool(self.target.download.get("best_available", False)),
            "watcher": True,
        }
        parent.obj = SimpleNamespace(
            config=self.service_config,
            cdm=None,
            proxy_providers=self.proxy_providers,
            profile=self.target.profile,
        )

        service_ctx = click.Context(click.Command(name=self.tag))
        service_ctx.parent = parent
        service_ctx.obj = parent.obj
        kwargs = _service_constructor_kwargs(self.service_class, self.target)
        return self.service_class(service_ctx, **kwargs)

    def ensure_authenticated(self) -> Service:
        if self.service is None:
            self.service = self._build()
            self.authenticated = False
        if not self.authenticated:
            cookies = get_cookie_jar(self.tag, self.target.profile)
            credential = get_credentials(self.tag, self.target.profile)
            self.service.authenticate(cookies, credential)
            self.authenticated = True
            cookie_path = get_cookie_path(self.tag, self.target.profile)
            if cookie_path:
                try:
                    save_cookies(cookie_path, self.service.session.cookies)
                except Exception as exc:  # cookie persistence must not hide a successful check
                    log.debug("Could not save cookies for %s: %s", self.tag, exc)
        return self.service

    def resolve_download_proxy(self, value: Any) -> Optional[str]:
        """Resolve a download-role URI/provider name using the same configured providers."""
        return _resolve_proxy(value, self.proxy_providers)

    def invalidate(self) -> None:
        self.authenticated = False
        self.service = None

    def fresh_titles(self) -> Any:
        service = self.ensure_authenticated()
        try:
            # The wrapper is intentional: no_cache=True makes it call the actual service
            # get_titles() while still applying title_map and normal collection behavior.
            return service.get_titles_cached()
        except Exception:
            self.invalidate()
            raise

    def resolve_metadata(self, titles: Any) -> Optional[Any]:
        """Resolve the show/movie through existing IMDb/SIMKL/TMDB logic."""
        if not titles:
            return None
        sample = titles[0] if hasattr(titles, "__getitem__") else titles
        from unshackle.core.titles import Episode, Movie

        if isinstance(sample, Episode):
            title, year, kind = sample.title, sample.year, "tv"
            per_title = getattr(sample, "anime", None)
            anime = bool(getattr(self.service_class, "ANIME", False) if per_title is None else per_title)
        elif isinstance(sample, Movie):
            title, year, kind = sample.name, sample.year, "movie"
            per_title = getattr(sample, "anime", None)
            anime = bool(getattr(self.service_class, "ANIME", False) if per_title is None else per_title)
        else:
            return None

        service = self.ensure_authenticated()
        cacher = getattr(service, "title_cache", None)
        cache_title_id = getattr(service, "title", None) or getattr(service, "title_id", None)
        with providers.metadata_proxy(self.metadata_proxy):
            return providers.resolve_by_ids(
                title=title,
                year=year,
                kind=kind,
                title_cacher=cacher,
                cache_title_id=str(cache_title_id) if cache_title_id else None,
                cache_region=getattr(service, "current_region", None),
                cache_account_hash=get_account_hash(getattr(service, "credential", None)),
                anime=anime,
            )

    def close(self) -> None:
        if self.service is not None and hasattr(self.service, "close"):
            try:
                self.service.close()
            except Exception:
                log.debug("Service %s close failed", self.tag, exc_info=True)
        self.service = None
        self.authenticated = False


__all__ = ("ServiceRuntime",)
