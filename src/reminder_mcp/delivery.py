from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

import httpx


class TransientDeliveryError(RuntimeError):
    """A delivery failed before a definitive application-level result and may be retried."""


class FeishuDelivery:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=15)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def _required_env(name: str | None) -> str:
        if not name:
            raise ValueError(
                "missing environment variable name in target configuration"
            )
        value = os.getenv(name)
        if not value:
            raw_map = os.getenv("FEISHU_TARGET_SECRETS_JSON", "{}")
            try:
                secret_map = json.loads(raw_map)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "FEISHU_TARGET_SECRETS_JSON is not valid JSON"
                ) from exc
            if not isinstance(secret_map, dict):
                raise ValueError("FEISHU_TARGET_SECRETS_JSON must be a JSON object")
            candidate = secret_map.get(name)
            value = candidate if isinstance(candidate, str) else None
        if not value:
            raise ValueError(f"required environment variable is not set: {name}")
        return value

    @classmethod
    def _optional_env(cls, name: str | None) -> str | None:
        if not name:
            return None
        try:
            return cls._required_env(name)
        except ValueError as exc:
            if "required environment variable is not set" in str(exc):
                return None
            raise

    @staticmethod
    def webhook_payload(
        text: str, secret: str | None = None, timestamp: int | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
        if secret:
            ts = timestamp or int(time.time())
            string_to_sign = f"{ts}\n{secret}".encode()
            digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
            payload["timestamp"] = str(ts)
            payload["sign"] = base64.b64encode(digest).decode()
        return payload

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.client.post(url, **kwargs)
            response.raise_for_status()
            return response
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise TransientDeliveryError(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429 or status >= 500:
                raise TransientDeliveryError(f"HTTP {status}") from exc
            raise

    async def send(
        self,
        target: dict[str, Any],
        text: str,
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if target["kind"] == "feishu_webhook":
            endpoint = self._required_env(target["endpoint_env"])
            secret = self._optional_env(target.get("secret_env"))
            payload = self.webhook_payload(text, secret)
            if dry_run:
                return {"dry_run": True, "kind": target["kind"], "payload": payload}
            response = await self._post(endpoint, json=payload)
            data = response.json()
            if data.get("code", data.get("StatusCode", 0)) not in (0, None):
                raise RuntimeError(f"Feishu webhook rejected message: {data}")
            return data

        if target["kind"] == "feishu_chat":
            app_id = self._required_env("FEISHU_APP_ID")
            app_secret = self._required_env("FEISHU_APP_SECRET")
            if dry_run:
                return {
                    "dry_run": True,
                    "kind": target["kind"],
                    "recipient": target["recipient"],
                    "text": text,
                }
            token_response = await self._post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            token_data = token_response.json()
            if token_data.get("code") != 0:
                raise RuntimeError(f"failed to obtain tenant token: {token_data}")
            message_body = {
                "receive_id": target["recipient"],
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            }
            if idempotency_key:
                message_body["uuid"] = idempotency_key
            response = await self._post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={
                    "Authorization": f"Bearer {token_data['tenant_access_token']}"
                },
                json=message_body,
            )
            data = response.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Feishu API rejected message: {data}")
            return data

        raise ValueError(f"unsupported delivery kind: {target['kind']}")
