"""Availability watcher support for Unshackle.

The watcher package intentionally keeps the download pipeline out of its import path.
It loads service/auth/title modules for metadata checks and imports the normal download
executor only when an item is ready to trigger.
"""

from .scheduler import WatchTarget, WatcherScheduler, load_targets, load_watchers

__all__ = ("WatchTarget", "WatcherScheduler", "load_targets", "load_watchers")
