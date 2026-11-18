# SPDX-License-Identifier: Apache-2.0
"""The bridge: Imatra devices as OCPP stations towards one CSMS.

- Reads devices from Imatra (all, or the configured list) and opens one
  WebSocket per station to the CSMS (identity = prefix + device id).
- Follows Imatra's event feed by cursor (kept in the state directory) and
  sends each device's OCPP messages in order. Messages for a station whose
  CSMS connection is down are kept (bounded) and sent on reconnection.
- Re-sends limits before Imatra's duration runs out.
Never logged: the CSMS password, the Imatra key.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import ssl
from dataclasses import dataclass, field
from pathlib import Path

import websockets

from .imatra import Imatra
from .station import Settings, StationCore, build_charge_point
from .versions import SUBPROTOCOLS, negotiated

log = logging.getLogger("imatra_ocpp")


@dataclass
class Config:
    csms_url: str
    imatra_url: str
    imatra_api_key: str
    csms_password: str = ""
    csms_ca_file: str = ""
    devices: list[str] = field(default_factory=list)
    station_id_prefix: str = ""
    state_dir: str = "/data"
    poll_s: float = 1.0
    settings: Settings = field(default_factory=Settings)

    @classmethod
    def load(cls, path: str) -> "Config":
        raw = json.loads(Path(path).read_text())
        s = Settings(**{k: raw[k] for k in ("vendor", "model", "voltage_v", "phases", "default_duration_s",
                                             "max_duration_s", "confirm_timeout_s") if raw.get(k) not in (None, "")})
        return cls(csms_url=raw["csms_url"], imatra_url=raw["imatra_url"], imatra_api_key=raw["imatra_api_key"],
                   csms_password=raw.get("csms_password", ""), csms_ca_file=raw.get("csms_ca_file", ""),
                   devices=list(raw.get("devices") or []), station_id_prefix=raw.get("station_id_prefix", ""),
                   state_dir=raw.get("state_dir", "/data"), settings=s)


class Link:
    """One station's CSMS connection, with reconnection and an outbox."""

    def __init__(self, core: StationCore, cfg: Config):
        self.core, self.cfg = core, cfg
        self.outbox: collections.deque = collections.deque(maxlen=2000)
        self.wake = asyncio.Event()
        self.cp = None

    def url(self) -> str:
        return self.cfg.csms_url.rstrip("/") + "/" + self.core.station_id

    def headers(self) -> dict:
        if not self.cfg.csms_password:
            return {}
        token = base64.b64encode(f"{self.core.station_id}:{self.cfg.csms_password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def push(self, messages: list[tuple[str, dict]]) -> None:
        self.outbox.extend(messages)
        self.wake.set()

    async def run(self) -> None:
        backoff = 1
        tls = None
        if self.url().startswith("wss://"):
            tls = ssl.create_default_context(cafile=self.cfg.csms_ca_file or None)
        while True:
            try:
                async with websockets.connect(self.url(), subprotocols=list(SUBPROTOCOLS),
                                              additional_headers=self.headers(), ssl=tls,
                                              open_timeout=15) as ws:
                    version = negotiated(ws.subprotocol)
                    self.core.version = version
                    cp = build_charge_point(version)(self.core.station_id, ws)
                    cp.core = self.core
                    self.cp = cp
                    reader = asyncio.create_task(cp.start())
                    interval = await cp.boot()
                    log.info("%s: connected (%s), boot accepted, heartbeat %ss", self.core.station_id, version, interval)
                    backoff = 1
                    status = await self._initial_status()
                    await cp.send("StatusNotification", status)
                    await self._pump(cp, reader, interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # any failure: log, back off, reconnect
                log.warning("%s: CSMS connection: %s; retry in %ss", self.core.station_id, e.__class__.__name__ + ": " + str(e)[:200], backoff)
            self.cp = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _initial_status(self) -> dict:
        from .station import _now
        status = "Occupied" if self.core.mapper.transaction_id else ("Available" if self.core.mapper.online else "Unavailable")
        return {"timestamp": _now(), "connectorStatus": status, "evseId": 1, "connectorId": 1}

    async def _pump(self, cp, reader: asyncio.Task, interval: int) -> None:
        loop = asyncio.get_running_loop()
        next_beat = loop.time() + interval
        while not reader.done():
            while self.outbox:
                action, payload = self.outbox[0]
                await cp.send(action, payload)
                self.outbox.popleft()
            if loop.time() >= next_beat:
                await cp.heartbeat()
                next_beat = loop.time() + interval
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=max(0.1, min(5, next_beat - loop.time())))
            except asyncio.TimeoutError:
                pass
        reader.result()  # raises the connection error


class Bridge:
    def __init__(self, cfg: Config, imatra: Imatra | None = None):
        self.cfg = cfg
        self.imatra = imatra or Imatra(cfg.imatra_url, cfg.imatra_api_key)
        self.links: dict[str, Link] = {}
        self.tasks: list[asyncio.Task] = []
        self.cursor_file = Path(cfg.state_dir) / "cursor"

    async def _add(self, dev: dict) -> None:
        did = dev["device_id"]
        if did in self.links or (self.cfg.devices and did not in self.cfg.devices):
            return
        core = StationCore(did, self.cfg.station_id_prefix + did, self.imatra, self.cfg.settings,
                           float(dev.get("station_max_a") or 32))
        core.mapper.online = bool(dev.get("online"))
        link = Link(core, self.cfg)
        self.links[did] = link
        self.tasks.append(asyncio.create_task(link.run()))
        log.info("%s: station for Imatra device %s (max %s A)", core.station_id, did, core.station_max_a)

    async def discover(self) -> None:
        for dev in await self.imatra.devices():
            await self._add(dev)

    def _load_cursor(self) -> int | None:
        try:
            return int(self.cursor_file.read_text().strip())
        except (OSError, ValueError):
            return None

    def _save_cursor(self, cursor: int) -> None:
        tmp = self.cursor_file.with_suffix(".tmp")
        tmp.write_text(str(cursor))
        os.replace(tmp, self.cursor_file)

    async def follow(self) -> None:
        cursor = self._load_cursor()
        if cursor is None:
            cursor = await self.imatra.latest_cursor()
            log.info("first start: following the event feed from cursor %s (no history replayed)", cursor)
        seen: collections.OrderedDict = collections.OrderedDict()
        ticks = 0
        while True:
            events, nxt = await self.imatra.events(cursor)
            for e in events:
                if e["id"] in seen:
                    continue
                seen[e["id"]] = None
                if len(seen) > 10000:
                    seen.popitem(last=False)
                did = e.get("device_id")
                if did and did not in self.links and e["type"] in ("device.online", "device.bound"):
                    await self.discover()
                link = self.links.get(did)
                if link:
                    link.push(await link.core.on_event(e))
            if events:
                cursor = nxt
                self._save_cursor(cursor)
            ticks += 1
            if ticks % 30 == 0:
                for link in self.links.values():
                    await link.core.refresh()
            if not events:
                await asyncio.sleep(self.cfg.poll_s)

    async def run(self) -> None:
        Path(self.cfg.state_dir).mkdir(parents=True, exist_ok=True)
        await self.discover()
        if not self.links:
            log.warning("no Imatra devices yet; waiting for one to come online")
        await self.follow()
