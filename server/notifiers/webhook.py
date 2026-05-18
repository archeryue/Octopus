"""Webhook notifier — POST JSON to a URL.

Minimal: no retries, no auth headers, just a fire-and-forget POST with
a 5s timeout. Errors are logged and swallowed (the manager logs at a
higher level too).
"""

from __future__ import annotations

import logging

import httpx

from .base import NotifierBase, NotifierEvent

logger = logging.getLogger(__name__)


class WebhookNotifier(NotifierBase):
    type = "webhook"

    TIMEOUT_SECONDS = 5.0

    async def send(self, event: NotifierEvent) -> None:
        url = (self.config.get("url") or "").strip()
        if not url:
            logger.warning("Webhook notifier %s has no URL configured", self.id)
            return
        payload = event.to_payload()
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload)
        except Exception as e:
            logger.warning(
                "Webhook %s POST to %s failed: %s", self.id, url, e
            )
            return
        if resp.status_code >= 400:
            logger.warning(
                "Webhook %s POST to %s returned %s",
                self.id,
                url,
                resp.status_code,
            )
