from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Optional

from unshackle.core.titles import Episode
from unshackle.core.utils.click_types import SeasonRange


def selector_tokens(value: Any) -> list[str]:
    """Turn watcher ``wanted`` config into the same tokens accepted by ``-w``."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(selector_tokens(item))
        return result
    raise TypeError(f"wanted selectors must be strings or lists, not {type(value).__name__}")


def parse_wanted(value: Any) -> set[str]:
    """Parse selectors with Unshackle's existing ``SeasonRange`` parser."""
    tokens = selector_tokens(value)
    return set(SeasonRange().parse_tokens(*tokens)) if tokens else set()


def episode_selector(episode: Episode, daily: bool = False) -> str:
    """Return the normal selector that identifies an episode for ``-w``."""
    if daily and isinstance(episode.air_date, date):
        return episode.air_date.isoformat()
    selector = f"S{int(episode.season):02}E{int(episode.number):02}"
    if episode.part is not None:
        selector += f".{int(episode.part)}"
    return selector


def episode_key(episode: Episode) -> tuple[int, int, int, int]:
    """Numeric ordering key; avoids lexicographic S01E10/S01E09 mistakes."""
    return (
        int(episode.season),
        int(episode.number),
        int(episode.part or 0),
        int(episode.year or 0),
    )


def item_key(episode: Episode, daily: bool = False) -> str:
    """Stable state key for one service episode."""
    return episode_selector(episode, daily=daily)


def matches_wanted(episode: Episode, wanted: set[str]) -> bool:
    """Use the existing Episode matching semantics, including parts and dates."""
    return not wanted or episode.matches_wanted(wanted)


def title_identity(title: Any, daily: bool = False) -> tuple[str, str, Optional[tuple[int, int, int, int]]]:
    """Return (kind, state key, numeric ordering key) for a Movie/Episode."""
    if isinstance(title, Episode):
        return "episode", item_key(title, daily=daily), episode_key(title)
    return "movie", str(title.id), None


def sort_episodes(items: Iterable[Episode]) -> list[Episode]:
    return sorted(items, key=episode_key)
