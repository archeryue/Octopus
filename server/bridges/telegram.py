from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from .base import Bridge
from .manager import BridgeManager

logger = logging.getLogger(__name__)

DEFAULT_API_BASE_URL = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096


class TelegramBridge(Bridge):
    """Telegram Bot integration via long-polling.

    Uses long-polling (getUpdates) for simplicity — no public URL or SSL
    certificate required. Works behind NAT/firewalls.
    """

    name = "telegram"

    def __init__(
        self,
        manager: BridgeManager,
        token: str,
        allowed_chat_ids: list[str] | None = None,
        api_base_url: str = DEFAULT_API_BASE_URL,
    ) -> None:
        super().__init__(manager)
        self.token = token
        self.allowed_chat_ids = set(allowed_chat_ids) if allowed_chat_ids else None
        self._base_url = f"{api_base_url}/bot{token}"
        self._client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None
        self._offset: int = 0
        self._last_poll_ok: float = 0.0

    @property
    def healthy(self) -> bool:
        if self._last_poll_ok == 0:
            return True  # hasn't polled yet
        return (time.time() - self._last_poll_ok) < 120

    @property
    def max_message_length(self) -> int:
        return MAX_MESSAGE_LENGTH

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0)
        )
        resp = await self._client.get(f"{self._base_url}/getMe")
        resp.raise_for_status()
        bot_info = resp.json()["result"]
        logger.info("Telegram bot connected: @%s", bot_info["username"])
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    # --- Polling ---

    async def _poll_loop(self) -> None:
        backoff = 5
        while True:
            try:
                resp = await self._client.get(
                    f"{self._base_url}/getUpdates",
                    params={
                        "offset": self._offset,
                        "timeout": 30,
                        "allowed_updates": json.dumps(
                            ["message", "callback_query"]
                        ),
                    },
                )
                if resp.status_code != 200:
                    logger.error("Telegram poll error: %s", resp.status_code)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 300)
                    continue

                data = resp.json()
                if not data.get("ok"):
                    logger.error("Telegram API error: %s", data)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 300)
                    continue

                backoff = 5  # reset on success
                self._last_poll_ok = time.time()
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    asyncio.create_task(self._handle_update(update))

            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException:
                self._last_poll_ok = time.time()
                continue  # Normal long-poll timeout
            except Exception:
                logger.exception("Telegram poll error")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._handle_callback_query(update["callback_query"])
            return

        message = update.get("message")
        if not message or "text" not in message:
            return

        chat_id = str(message["chat"]["id"])

        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            logger.warning("Rejecting message from unauthorized chat: %s", chat_id)
            return

        await self.manager.handle_incoming("telegram", chat_id, message["text"], self)

    async def _handle_callback_query(self, query: dict) -> None:
        chat_id = str(query["message"]["chat"]["id"])
        data = query.get("data", "")

        # Answer callback to remove loading indicator
        await self._api_call(
            "answerCallbackQuery",
            {"callback_query_id": query["id"]},
        )

        if ":" not in data:
            return

        action, tool_use_id = data.split(":", 1)
        approved = action == "approve"

        await self.manager.handle_tool_decision(
            "telegram", chat_id, tool_use_id, approved
        )

        # Remove inline keyboard after decision
        try:
            await self._api_call(
                "editMessageReplyMarkup",
                {
                    "chat_id": int(chat_id),
                    "message_id": query["message"]["message_id"],
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        except Exception:
            pass

    # --- Send methods ---

    async def send_text(self, chat_id: str, text: str) -> None:
        chunks = self._split_text(text, MAX_MESSAGE_LENGTH)
        for chunk in chunks:
            await self._api_call(
                "sendMessage",
                {"chat_id": int(chat_id), "text": chunk, "parse_mode": "Markdown"},
            )

    async def send_tool_approval_request(
        self,
        chat_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        input_str = json.dumps(tool_input, indent=2)
        if len(input_str) > 3000:
            input_str = input_str[:3000] + "\n..."

        text = f"*{tool_name}* wants to execute:\n```\n{input_str}\n```"
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "Allow",
                        "callback_data": f"approve:{tool_use_id}",
                    },
                    {
                        "text": "Deny",
                        "callback_data": f"deny:{tool_use_id}",
                    },
                ]
            ]
        }
        await self._api_call(
            "sendMessage",
            {
                "chat_id": int(chat_id),
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            },
        )

    async def send_tool_use(
        self, chat_id: str, tool_name: str, tool_input: dict[str, Any]
    ) -> None:
        preview = ""
        if isinstance(tool_input, dict):
            if "command" in tool_input:
                preview = f": `{str(tool_input['command'])[:80]}`"
            elif "file_path" in tool_input:
                preview = f": `{tool_input['file_path']}`"
        await self._api_call(
            "sendMessage",
            {
                "chat_id": int(chat_id),
                "text": f"*{tool_name}*{preview}",
                "parse_mode": "Markdown",
            },
        )

    async def send_tool_result(
        self, chat_id: str, output: str, is_error: bool
    ) -> None:
        prefix = "Error" if is_error else "Result"
        truncated = output[:3500] if len(output) > 3500 else output
        await self._api_call(
            "sendMessage",
            {
                "chat_id": int(chat_id),
                "text": f"*{prefix}:*\n```\n{truncated}\n```",
                "parse_mode": "Markdown",
            },
        )

    async def send_status(self, chat_id: str, status: str) -> None:
        if status == "running":
            await self._api_call(
                "sendChatAction",
                {"chat_id": int(chat_id), "action": "typing"},
            )

    async def send_result(
        self, chat_id: str, cost: float | None, is_error: bool
    ) -> None:
        cost_str = f" (${cost:.4f})" if cost is not None else ""
        label = "Error" if is_error else "Done"
        await self._api_call(
            "sendMessage",
            {
                "chat_id": int(chat_id),
                "text": f"*{label}*{cost_str}",
                "parse_mode": "Markdown",
            },
        )

    async def send_error(self, chat_id: str, message: str) -> None:
        await self._api_call(
            "sendMessage",
            {"chat_id": int(chat_id), "text": f"Error: {message}"},
        )

    # --- Helpers ---

    async def _api_call(self, method: str, data: dict) -> dict | None:
        for attempt in range(3):
            try:
                resp = await self._client.post(
                    f"{self._base_url}/{method}", json=data
                )
                result = resp.json()
                if result.get("ok"):
                    return result.get("result")

                if resp.status_code == 429:
                    retry_after = result.get("parameters", {}).get(
                        "retry_after", 5
                    )
                    logger.warning(
                        "Telegram rate limited, waiting %ds", retry_after
                    )
                    await asyncio.sleep(retry_after)
                    continue

                logger.error("Telegram API error: %s %s", method, result)
                return None

            except Exception:
                logger.exception("Telegram API call failed: %s", method)
                if attempt < 2:
                    await asyncio.sleep(1)
        return None

    @staticmethod
    def _split_text(text: str, max_len: int) -> list[str]:
        if len(text) <= max_len:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            split_at = text.rfind("\n", 0, max_len)
            if split_at == -1 or split_at < max_len // 2:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks
