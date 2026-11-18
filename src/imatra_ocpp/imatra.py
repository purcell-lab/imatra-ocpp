# SPDX-License-Identifier: Apache-2.0
"""Client for Imatra's published interfaces: the operator API /v1 and the event feed."""
from __future__ import annotations

import asyncio
import time

import httpx


class ImatraError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Imatra {status}: {detail}")
        self.status, self.detail = status, detail


class Imatra:
    def __init__(self, base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/v1", transport=transport, timeout=10,
                                       headers={"X-API-Key": api_key})

    async def close(self) -> None:
        await self._http.aclose()

    async def _json(self, method: str, path: str, **kw) -> dict:
        r = await self._http.request(method, path, **kw)
        if r.status_code >= 400:
            raise ImatraError(r.status_code, r.text[:300])
        return r.json() if r.content else {}

    async def devices(self) -> list[dict]:
        out, cursor = [], None
        while True:
            page = await self._json("GET", "/devices", params={"cursor": cursor} if cursor else None)
            out += page.get("devices", [])
            cursor = page.get("next_cursor")
            if not cursor:
                return out

    async def device(self, device_id: str) -> dict:
        return await self._json("GET", f"/devices/{device_id}")

    async def events(self, cursor: int, limit: int = 500) -> tuple[list[dict], int]:
        page = await self._json("GET", "/events", params={"cursor": cursor, "limit": limit})
        return page.get("events", []), int(page.get("next_cursor", cursor))

    async def latest_cursor(self) -> int:
        cursor = 0
        while True:
            events, nxt = await self.events(cursor, 1000)
            if not events:
                return cursor
            cursor = nxt

    async def set_limit(self, device_id: str, limit_a: float, duration_s: int) -> int:
        r = await self._json("POST", f"/devices/{device_id}/limit", json={"limit_a": limit_a, "duration_s": duration_s})
        return int(r["command_id"])

    async def clear_limit(self, device_id: str) -> None:
        await self._json("DELETE", f"/devices/{device_id}/limit")

    async def wait_confirmed(self, device_id: str, command_id: int, timeout_s: float) -> dict | None:
        """The device's limit once the device confirmed this command, or None."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            limit = (await self.device(device_id)).get("limit") or {}
            if limit.get("command_id") == command_id and limit.get("confirmed_at"):
                return limit
            await asyncio.sleep(0.1)
        return None
