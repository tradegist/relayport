"""Webhook notifier — HMAC-SHA256 signed POST to a target URL."""

import hashlib
import hmac
import logging
import os

import httpx
from pydantic import BaseModel

from .base import BaseNotifier

log = logging.getLogger("notifier.webhook")


class WebhookNotifier(BaseNotifier):
    """Send JSON payloads to a webhook URL with HMAC-SHA256 signature."""

    name = "webhook"

    def __init__(self, suffix: str = "") -> None:
        self._url = os.environ.get(f"TARGET_WEBHOOK_URL{suffix}", "")
        self._secret = os.environ.get(f"WEBHOOK_SECRET{suffix}", "")
        self._header_name = os.environ.get(f"WEBHOOK_HEADER_NAME{suffix}", "")
        self._header_value = os.environ.get(f"WEBHOOK_HEADER_VALUE{suffix}", "")

    @staticmethod
    def required_env_vars() -> list[str]:
        return ["TARGET_WEBHOOK_URL", "WEBHOOK_SECRET"]

    def send(self, payload: BaseModel) -> None:
        body = payload.model_dump_json(indent=2)

        if not self._url:
            log.info("Webhook payload (dry-run):\n%s", body)
            return

        signature = hmac.new(
            self._secret.encode(), body.encode(), hashlib.sha256
        ).hexdigest()

        try:
            headers: dict[str, str] = {
                "Content-Type": "application/json",
                "X-Signature-256": f"sha256={signature}",
            }
            if self._header_name:
                headers[self._header_name] = self._header_value

            resp = httpx.post(
                self._url,
                content=body,
                headers=headers,
                timeout=10.0,
            )
            log.info("Webhook sent — status %d", resp.status_code)
        except httpx.HTTPError as exc:
            log.error("Webhook delivery failed: %s", exc)
