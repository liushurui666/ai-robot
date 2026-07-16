from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any
from urllib.parse import unquote

import httpx


class FeishuMessagePermissionError(RuntimeError):
    """The Feishu app cannot read message bodies or their resources."""


class FeishuDirectory:
    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        cache_ttl_seconds: int = 600,
    ):
        self.client = client or httpx.AsyncClient(timeout=20)
        self._owns_client = client is None
        self.cache_ttl_seconds = cache_ttl_seconds
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._users: list[dict[str, Any]] = []
        self._users_expires_at = 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def _required_env(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise ValueError(f"required environment variable is not set: {name}")
        return value

    async def _tenant_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        response = await self.client.post(
            f"{self.BASE_URL}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self._required_env("FEISHU_APP_ID"),
                "app_secret": self._required_env("FEISHU_APP_SECRET"),
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(f"Feishu token request failed: {data.get('msg')}")
        self._token = str(data["tenant_access_token"])
        self._token_expires_at = now + max(int(data.get("expire", 7200)) - 60, 60)
        return self._token

    async def _get_page(
        self,
        path: str,
        *,
        params: dict[str, Any],
        token: str,
    ) -> dict[str, Any]:
        response = await self.client.get(
            f"{self.BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu directory request failed: {data.get('msg')}")
        return data.get("data") or {}

    async def _department_ids(self, token: str) -> list[str]:
        department_ids = ["0"]
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "department_id_type": "department_id",
                "fetch_child": "true",
                "page_size": 50,
            }
            if page_token:
                params["page_token"] = page_token
            page = await self._get_page(
                "/contact/v3/departments/0/children",
                params=params,
                token=token,
            )
            department_ids.extend(
                str(item["department_id"])
                for item in page.get("items") or []
                if item.get("department_id")
            )
            if not page.get("has_more"):
                return list(dict.fromkeys(department_ids))
            page_token = str(page.get("page_token") or "")

    async def _department_users(
        self,
        department_id: str,
        *,
        token: str,
        semaphore: asyncio.Semaphore,
    ) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "department_id": department_id,
                "department_id_type": "department_id",
                "user_id_type": "open_id",
                "page_size": 50,
            }
            if page_token:
                params["page_token"] = page_token
            async with semaphore:
                page = await self._get_page(
                    "/contact/v3/users/find_by_department",
                    params=params,
                    token=token,
                )
            users.extend(page.get("items") or [])
            if not page.get("has_more"):
                return users
            page_token = str(page.get("page_token") or "")

    async def list_users(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._users and now < self._users_expires_at:
            return self._users
        token = await self._tenant_token()
        department_ids = await self._department_ids(token)
        semaphore = asyncio.Semaphore(10)
        pages = await asyncio.gather(
            *(
                self._department_users(
                    department_id,
                    token=token,
                    semaphore=semaphore,
                )
                for department_id in department_ids
            )
        )
        unique: dict[str, dict[str, Any]] = {}
        for users in pages:
            for user in users:
                open_id = str(user.get("open_id") or "")
                if open_id:
                    unique[open_id] = user
        self._users = list(unique.values())
        self._users_expires_at = now + self.cache_ttl_seconds
        return self._users

    @staticmethod
    def _attachment_name_from_headers(response: httpx.Response) -> str | None:
        disposition = response.headers.get("content-disposition", "")
        encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
        if encoded:
            return unquote(encoded.group(1)).strip()
        plain = re.search(r'filename="?([^";]+)', disposition, re.IGNORECASE)
        return plain.group(1).strip() if plain else None

    @staticmethod
    def _walk_attachment_keys(value: Any, inherited_name: str | None = None):
        if isinstance(value, dict):
            name = str(
                value.get("file_name") or value.get("name") or inherited_name or ""
            ).strip() or None
            image_key = value.get("image_key")
            if isinstance(image_key, str) and image_key:
                yield {"kind": "image", "file_key": image_key, "filename": name}
            file_key = value.get("file_key")
            if isinstance(file_key, str) and file_key:
                yield {"kind": "file", "file_key": file_key, "filename": name}
            for child in value.values():
                yield from FeishuDirectory._walk_attachment_keys(child, name)
        elif isinstance(value, list):
            for child in value:
                yield from FeishuDirectory._walk_attachment_keys(child, inherited_name)

    async def message_attachments(self, message_id: str) -> list[dict[str, str | None]]:
        """Return attachment locators from one Feishu message without mutating it."""

        if re.fullmatch(r"om_[A-Za-z0-9]+", message_id) is None:
            raise ValueError("invalid Feishu message id")
        token = await self._tenant_token()
        response = await self.client.get(
            f"{self.BASE_URL}/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            payload = response.json()
        except json.JSONDecodeError:
            response.raise_for_status()
            raise RuntimeError("Feishu message read returned an invalid response")
        code = payload.get("code")
        if code == 99991672:
            raise FeishuMessagePermissionError(
                "Feishu app requires im:message:readonly or im:message"
            )
        if code != 0:
            raise RuntimeError(f"Feishu message read failed: {payload.get('msg')}")
        response.raise_for_status()

        attachments: list[dict[str, str | None]] = []
        seen: set[tuple[str, str]] = set()
        for item in (payload.get("data") or {}).get("items") or []:
            if not isinstance(item, dict):
                continue
            body = item.get("body") or {}
            raw_content = body.get("content") if isinstance(body, dict) else None
            try:
                content = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
            except json.JSONDecodeError:
                continue
            for attachment in self._walk_attachment_keys(content):
                key = str(attachment["file_key"])
                kind = str(attachment["kind"])
                if (kind, key) in seen:
                    continue
                seen.add((kind, key))
                attachments.append(attachment)
        return attachments

    async def download_message_attachment(
        self,
        message_id: str,
        file_key: str,
        kind: str,
        *,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> tuple[bytes, str | None]:
        """Download an original Feishu message resource with a bounded size."""

        if re.fullmatch(r"om_[A-Za-z0-9]+", message_id) is None:
            raise ValueError("invalid Feishu message id")
        if not file_key or len(file_key) > 512:
            raise ValueError("invalid Feishu file key")
        resource_type = "image" if kind == "image" else "file"
        token = await self._tenant_token()
        response = await self.client.get(
            f"{self.BASE_URL}/im/v1/messages/{message_id}/resources/{file_key}",
            params={"type": resource_type},
            headers={"Authorization": f"Bearer {token}"},
        )
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            payload = response.json()
            if payload.get("code") == 99991672:
                raise FeishuMessagePermissionError(
                    "Feishu app requires im:message:readonly or im:message"
                )
            raise RuntimeError(f"Feishu resource download failed: {payload.get('msg')}")
        response.raise_for_status()
        if len(response.content) > max_bytes:
            raise RuntimeError("Feishu attachment exceeds the local 100 MB limit")
        return response.content, self._attachment_name_from_headers(response)

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().split())

    @classmethod
    def _user_names(cls, user: dict[str, Any]) -> list[str]:
        return [
            str(user.get(key) or "").strip()
            for key in ("name", "en_name", "nickname")
            if str(user.get(key) or "").strip()
        ]

    @classmethod
    def _display_name(cls, user: dict[str, Any]) -> str:
        names = cls._user_names(user)
        return names[0] if names else "未知用户"

    @classmethod
    def match_users(
        cls, users: list[dict[str, Any]], query: str
    ) -> list[dict[str, Any]]:
        needle = cls._normalize(query)
        if not needle:
            return []
        exact = [
            user
            for user in users
            if needle in {cls._normalize(name) for name in cls._user_names(user)}
        ]
        if exact:
            return exact
        return [
            user
            for user in users
            if any(needle in cls._normalize(name) for name in cls._user_names(user))
        ]

    async def send_to_user(self, recipient: str, content: str) -> dict[str, Any]:
        recipient = recipient.strip()
        content = content.strip()
        if not recipient or not content:
            raise ValueError("recipient and content must not be empty")
        matches = self.match_users(await self.list_users(), recipient)
        if not matches:
            return {
                "sent": False,
                "reason": "no_match",
                "message": f"通讯录中未找到“{recipient}”",
            }
        if len(matches) > 1:
            return {
                "sent": False,
                "reason": "ambiguous",
                "message": f"通讯录中有多个用户匹配“{recipient}”",
                "matches": [self._display_name(user) for user in matches[:10]],
            }

        user = matches[0]
        token = await self._tenant_token()
        response = await self.client.post(
            f"{self.BASE_URL}/im/v1/messages",
            params={"receive_id_type": "open_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": user["open_id"],
                "msg_type": "text",
                "content": json.dumps({"text": content}, ensure_ascii=False),
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu message send failed: {data.get('msg')}")
        return {
            "sent": True,
            "recipient": self._display_name(user),
            "message_id": (data.get("data") or {}).get("message_id"),
        }
