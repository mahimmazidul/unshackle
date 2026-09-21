"""Shared cookie and credential helpers used by lightweight service callers.

This module deliberately does not import the download pipeline.  A title watcher can
load the same cookie/credential material as ``dl`` and still stop after authentication
and ``Service.get_titles_cached()``.
"""

from __future__ import annotations

import html
from http.cookiejar import CookieJar, MozillaCookieJar
from pathlib import Path
from typing import Optional
from io import StringIO

from unshackle.core.config import config
from unshackle.core.credential import Credential


def get_cookie_path(service: str, profile: Optional[str]) -> Optional[Path]:
    """Return the configured cookie path for *service* and *profile*."""
    direct_cookie_file = config.directories.cookies / f"{service}.txt"
    profile_cookie_file = config.directories.cookies / service / f"{profile}.txt"
    default_cookie_file = config.directories.cookies / service / "default.txt"

    if direct_cookie_file.exists():
        return direct_cookie_file
    if profile and profile_cookie_file.exists():
        return profile_cookie_file
    if default_cookie_file.exists():
        return default_cookie_file
    return None


def load_cookie_file(cookie_file: Path) -> MozillaCookieJar:
    """Load a Netscape cookie file using the same validation as ``dl``."""
    cookie_jar = MozillaCookieJar(cookie_file)
    cookie_data = html.unescape(cookie_file.read_text("utf-8-sig")).splitlines(keepends=False)
    head = next((line.lstrip() for line in cookie_data if line.strip()), "")
    if head[:1] in ("[", "{"):
        raise ValueError(f"{cookie_file} is a JSON export; export it again in Netscape format")

    for index, line in enumerate(cookie_data):
        body = line.lstrip()
        prefix = "#HttpOnly_" if body.startswith("#HttpOnly_") else ""
        body = body[len(prefix) :]
        if not body or body.startswith("#"):
            continue
        line_data = body.split("\t") if "\t" in body else body.split(None, 6)
        if len(line_data) == 6:
            line_data.append("")
        if len(line_data) != 7:
            raise ValueError(f"{cookie_file} line {index + 1} is not a Netscape cookie row: {line!r}")
        # Keep cookies usable when a browser exported an old expiry.
        line_data[4] = ""
        line_data[1] = str(line_data[0].startswith(".")).upper()
        cookie_data[index] = prefix + "\t".join(line_data)

    cookie_data.insert(0, "# Netscape HTTP Cookie File")
    cookie_jar._really_load(  # type: ignore[attr-defined]
        StringIO("\n".join(cookie_data)), str(cookie_file), True, True
    )
    if not len(cookie_jar):
        raise ValueError(f"{cookie_file} holds no cookies; export it again in Netscape format")
    return cookie_jar


def get_cookie_jar(service: str, profile: Optional[str]) -> Optional[CookieJar]:
    """Load Firefox cookies when configured, otherwise the profile cookie file."""
    ff_settings = getattr(config, "firefox_cookies", {}).get(service)
    if ff_settings:
        try:
            from unshackle.core.utils.firefox_cookie_extractor import get_firefox_cookies

            extracted = get_firefox_cookies(ff_settings)
            if extracted:
                return extracted
        except Exception:
            # Match dl's behavior: fall back silently to the exported cookie file.
            pass

    cookie_file = get_cookie_path(service, profile)
    return load_cookie_file(cookie_file) if cookie_file else None


def save_cookies(path: Path, cookies: CookieJar) -> None:
    """Merge a service session's cookies into its existing Netscape file."""
    if hasattr(cookies, "jar"):
        cookies = cookies.jar

    cookie_jar = load_cookie_file(path) if path.exists() else MozillaCookieJar(path)
    for cookie in cookies:
        if cookie.name and cookie.domain:
            cookie_jar.set_cookie(cookie)
    cookie_jar.save(ignore_discard=True, ignore_expires=True)


def get_credentials(service: str, profile: Optional[str]) -> Optional[Credential]:
    """Return the configured credential for a service/profile."""
    credentials = config.credentials.get(service)
    if not credentials:
        return None

    if isinstance(credentials, dict):
        credentials = credentials.get(profile) if profile else credentials.get("default")
        if credentials is None:
            credentials = config.credentials.get(service, {}).get("default")

    if isinstance(credentials, list):
        return Credential(*credentials)
    if credentials:
        return Credential.loads(credentials)  # type: ignore[arg-type]
    return None


__all__ = (
    "get_cookie_path",
    "get_cookie_jar",
    "load_cookie_file",
    "save_cookies",
    "get_credentials",
)
