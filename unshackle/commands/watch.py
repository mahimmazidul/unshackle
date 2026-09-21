from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

import click

from unshackle.core.constants import context_settings
from unshackle.core.watcher.scheduler import WatcherScheduler, load_watchers

log = logging.getLogger("watch")


@click.command(
    name="watch",
    short_help="Watch service title catalogs and trigger normal downloads when items appear.",
    context_settings=context_settings,
)
@click.option(
    "--config",
    "watcher_config",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Watcher YAML file. Default path comes from unshackle.yaml (watch.config, or an inline watchers list).",
)
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Run due checks once and exit. Useful with cron and other user-level schedulers.",
)
@click.option(
    "--now",
    "force_now",
    is_flag=True,
    default=False,
    help="Run every selected watcher immediately, ignoring its schedule, then exit.",
)
@click.option(
    "--cache",
    "--recover-state",
    "recover_state",
    is_flag=True,
    default=False,
    help="Recover missing watcher state from its journal/current catalog, processing items one at a time.",
)
@click.option(
    "--from",
    "from_date",
    type=str,
    default=None,
    help="During recovery, ignore episodes with an air date before YYYY-MM-DD when the service provides one.",
)
@click.option(
    "--only",
    multiple=True,
    metavar="WATCHER_ID",
    help="Run only this watcher ID. Repeat the option for multiple IDs.",
)
@click.pass_context
def watch(
    ctx: click.Context,
    watcher_config: Optional[Path],
    once: bool,
    force_now: bool,
    recover_state: bool,
    from_date: Optional[str],
    only: tuple[str, ...],
) -> None:
    """Poll configured services and hand new items to Unshackle's normal download path.

    Availability checks are title-only and uncached. The downloader, CDM, vault, DRM,
    muxing and media-processing imports happen only after an item is ready to trigger.
    """
    try:
        targets, notifications, source = load_watchers(watcher_config)
        parsed_from = date.fromisoformat(from_date) if from_date else None
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc

    if not targets:
        raise click.ClickException(f"No watchers found in {source}")

    scheduler = WatcherScheduler(
        targets,
        once=once or force_now,
        force=force_now,
        recover=recover_state,
        from_date=parsed_from,
        only=set(only) or None,
        notifications=notifications,
    )

    try:
        if recover_state:
            log.info("Watcher recovery enabled; state will be checkpointed after each successful item")
        if force_now:
            scheduler.run_once()
        elif once:
            scheduler.run_once()
        else:
            scheduler.run_forever()
    finally:
        scheduler.close()


__all__ = ("watch",)
