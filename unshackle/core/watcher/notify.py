from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Optional

log = logging.getLogger("watcher.notify")


class Notifier:
    """Telegram/Discord notifications with secret-free messages and deduped errors."""

    def __init__(self, config: Optional[dict[str, Any]] = None):
        self.config = config or {}
        self.cooldown = int(self.config.get("error_cooldown", 900) or 900)

    def _post_json(self, url: str, payload: dict[str, Any]) -> None:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "unshackle-watcher/1"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - configured webhook/API URL
            response.read()

    def _telegram(self, message: str) -> None:
        cfg = self.config.get("telegram") or {}
        if not cfg.get("enabled", False):
            return
        token = os.environ.get(str(cfg.get("bot_token_env", "UNSHACKLE_TELEGRAM_BOT_TOKEN")))
        chat_id = cfg.get("chat_id")
        if not token or not chat_id:
            raise ValueError("Telegram notifications require a bot token environment variable and chat_id")
        self._post_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": str(chat_id), "text": message, "disable_web_page_preview": True},
        )

    def _discord(self, message: str) -> None:
        cfg = self.config.get("discord") or {}
        if not cfg.get("enabled", False):
            return
        webhook = os.environ.get(str(cfg.get("webhook_url_env", "UNSHACKLE_DISCORD_WEBHOOK_URL")))
        if not webhook:
            raise ValueError("Discord notifications require a webhook URL environment variable")
        self._post_json(webhook, {"content": message})

    def send(self, message: str) -> None:
        for name, sender in (("telegram", self._telegram), ("discord", self._discord)):
            try:
                sender(message)
            except Exception as exc:  # notifications must not change download state
                # Do not stringify URL-bearing HTTP exceptions: bot tokens and webhook
                # URLs can otherwise appear in logs.
                log.error("%s notification failed: %s", name.title(), type(exc).__name__)

    @staticmethod
    def _title(event: dict[str, Any]) -> str:
        return str(event.get("title") or event.get("title_ref") or "watcher target")

    def success(self, event: dict[str, Any]) -> None:
        files = event.get("output_files") or []
        lines = [
            "Unshackle watcher download successful",
            f"Service: {event.get('service')}",
            f"Title: {self._title(event)}",
        ]
        if event.get("selector"):
            lines.append(f"Episode: {event['selector']}")
        if files:
            lines.append("Files:")
            lines.extend(f"  {path}" for path in files)
        self.send("\n".join(lines))

    def error(self, event: dict[str, Any], state: Optional[dict[str, Any]] = None) -> bool:
        error = str(event.get("error") or "unknown error")
        fingerprint = hashlib.sha256(
            f"{event.get('service')}|{event.get('watcher_id')}|{event.get('selector')}|{error}".encode()
        ).hexdigest()
        previous = (state or {}).get("last_error") or {}
        now = datetime.now().astimezone()
        if previous.get("fingerprint") == fingerprint and previous.get("notified_at"):
            try:
                notified_at = datetime.fromisoformat(previous["notified_at"])
                if now - notified_at < timedelta(seconds=self.cooldown):
                    return False
            except ValueError:
                pass

        message = "\n".join(
            [
                "Unshackle watcher error",
                f"Service: {event.get('service')}",
                f"Title: {self._title(event)}",
                *( [f"Episode: {event['selector']}"] if event.get("selector") else [] ),
                f"Phase: {event.get('phase', 'unknown')}",
                f"Error: {error}",
                *( [f"Next retry: {event['next_retry_at']}"] if event.get("next_retry_at") else [] ),
            ]
        )
        self.send(message)
        event["fingerprint"] = fingerprint
        event["notified_at"] = now.isoformat(timespec="seconds")
        return True


__all__ = ("Notifier",)
