# SPDX-License-Identifier: Apache-2.0
"""The bridge end to end in one process: a mock CSMS (the ocpp library's
central-system side) and a fake Imatra API, for OCPP 2.1 and 2.0.1."""
import asyncio
import importlib
import tempfile
import unittest

import httpx
import websockets
from ocpp.routing import on

from imatra_ocpp.bridge import Bridge, Config
from imatra_ocpp.imatra import Imatra
from imatra_ocpp.station import Settings


class FakeImatra:
    def __init__(self, confirm=True):
        self.events, self.calls, self.confirm = [], [], confirm
        self.limit, self.cid = None, 0

    def add(self, kind, **data):
        self.events.append({"seq": len(self.events) + 1, "id": f"e{len(self.events) + 1}", "type": kind,
                            "schema_ver": 1, "device_id": "dev-01", "time": "2026-11-19T01:00:00+00:00", "data": data})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        assert request.headers["X-API-Key"] == "k" * 24
        if path == "/v1/devices":
            return httpx.Response(200, json={"devices": [{"device_id": "dev-01", "online": True, "station_max_a": 32}],
                                             "next_cursor": None})
        if path == "/v1/events":
            cursor = int(request.url.params["cursor"])
            out = [e for e in self.events if e["seq"] > cursor]
            return httpx.Response(200, json={"events": out, "next_cursor": out[-1]["seq"] if out else cursor})
        if path == "/v1/devices/dev-01/limit" and method == "POST":
            body = __import__("json").loads(request.content)
            self.calls.append(("POST", body))
            self.cid += 1
            # Like the device: only a changed limit pushes a new loop answer and is confirmed.
            changed = not (self.limit and self.limit["confirmed_at"] and self.limit["limit_a"] == body["limit_a"])
            self.limit = {"command_id": self.cid, "limit_a": body["limit_a"],
                          "confirmed_at": "2026-11-19T01:00:01Z" if self.confirm and changed else None,
                          "confirmed_via": "loop", "reaction_ms": 50}
            return httpx.Response(202, json={"command_id": self.cid})
        if path == "/v1/devices/dev-01/limit" and method == "DELETE":
            self.calls.append(("DELETE", None))
            self.limit = None
            return httpx.Response(204)
        if path == "/v1/devices/dev-01":
            return httpx.Response(200, json={"device_id": "dev-01", "online": True, "limit": self.limit})
        return httpx.Response(404)


def csms_class(version):
    mod = importlib.import_module("ocpp.v21" if version == "ocpp2.1" else "ocpp.v201")
    A, R = mod.enums.Action, mod.call_result

    class CSMS(mod.ChargePoint):
        received = []

        @on(A.boot_notification)
        async def boot(self, charging_station, reason, **_):
            self.received.append(("BootNotification", charging_station))
            return R.BootNotification(current_time="2026-11-19T01:00:00Z", interval=300, status="Accepted")

        @on(A.status_notification)
        async def status(self, connector_status, **_):
            self.received.append(("StatusNotification", connector_status))
            return R.StatusNotification()

        @on(A.transaction_event)
        async def tx(self, event_type, transaction_info, **kw):
            self.received.append(("TransactionEvent", (event_type, transaction_info, kw)))
            return R.TransactionEvent()

        @on(A.heartbeat)
        async def hb(self, **_):
            return R.Heartbeat(current_time="2026-11-19T01:00:00Z")

    return mod, CSMS


def profile(limit, tx_id, **period):
    return {"id": 7, "stack_level": 0, "charging_profile_purpose": "TxProfile", "charging_profile_kind": "Relative",
            "transaction_id": tx_id,
            "charging_schedule": [{"id": 1, "charging_rate_unit": "A",
                                   "charging_schedule_period": [{"start_period": 0, "limit": limit, **period}]}]}


class BridgeEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def run_version(self, version, confirm=True):
        mod, CSMS = csms_class(version)
        CSMS.received = []
        holder = {}

        async def serve(ws):
            cp = CSMS(ws.request.path.strip("/"), ws)
            holder["cp"] = cp
            await cp.start()

        server = await websockets.serve(serve, "127.0.0.1", 0, subprotocols=[version])
        port = server.sockets[0].getsockname()[1]
        fake = FakeImatra(confirm)
        tmp = tempfile.mkdtemp()
        cfg = Config(csms_url=f"ws://127.0.0.1:{port}/ocpp", imatra_url="http://imatra", imatra_api_key="k" * 24,
                     station_id_prefix="IMATRA-", state_dir=tmp, poll_s=0.05,
                     settings=Settings(confirm_timeout_s=0.5))
        bridge = Bridge(cfg, Imatra(cfg.imatra_url, cfg.imatra_api_key, transport=httpx.MockTransport(fake.handler)))
        task = asyncio.create_task(bridge.run())

        async def until(pred, what, secs=5):
            for _ in range(int(secs / 0.05)):
                if pred():
                    return
                await asyncio.sleep(0.05)
            self.fail(f"{version}: {what}; received {CSMS.received}")

        try:
            await until(lambda: ("StatusNotification", "Available") in CSMS.received, "boot and Available")
            self.assertEqual(CSMS.received[0][0], "BootNotification")
            fake.add("session.started", cp="B")
            await until(lambda: any(m[0] == "TransactionEvent" and m[1][0] == "Started" for m in CSMS.received), "Started")
            tx_id = next(m[1][1]["transaction_id"] for m in CSMS.received if m[0] == "TransactionEvent")
            cp = holder["cp"]
            self.assertEqual(cp._connection.subprotocol, version)

            r = await cp.call(mod.call.SetChargingProfile(evse_id=1, charging_profile=profile(10, tx_id)))
            if not confirm:
                self.assertEqual((r.status, r.status_info["reason_code"]), ("Rejected", "NotConfirmed"))
                return
            self.assertEqual(r.status, "Accepted")
            self.assertEqual(fake.calls[-1], ("POST", {"limit_a": 10.0, "duration_s": 3600}))

            # The same limit again changes nothing on the device, so it is never
            # confirmed again: accepted as already in force, and renewed.
            r = await cp.call(mod.call.SetChargingProfile(evse_id=1, charging_profile=profile(10, tx_id)))
            self.assertEqual(r.status, "Accepted")
            self.assertEqual(fake.calls[-1], ("POST", {"limit_a": 10.0, "duration_s": 3600}))

            r = await cp.call(mod.call.SetChargingProfile(evse_id=1, charging_profile=profile(3, tx_id)))
            self.assertEqual((r.status, r.status_info["reason_code"]), ("Rejected", "BelowMinimum"))
            r = await cp.call(mod.call.SetChargingProfile(evse_id=1, charging_profile=profile(10, "other")))
            self.assertEqual(r.status_info["reason_code"], "TxNotFound")
            n = len(fake.calls)

            r = await cp.call(mod.call.ClearChargingProfile(charging_profile_id=7))
            self.assertEqual(r.status, "Accepted")
            self.assertEqual(fake.calls[n:], [("DELETE", None)])

            # A TxProfile still in force at session end is cancelled on the device,
            # so the next session does not start limited.
            r = await cp.call(mod.call.SetChargingProfile(evse_id=1, charging_profile=profile(10, tx_id)))
            self.assertEqual(r.status, "Accepted")
            n = len(fake.calls)
            fake.add("session.ended", reason="ev_unplugged", energy_wh=1.1)
            await until(lambda: any(m[0] == "TransactionEvent" and m[1][0] == "Ended" for m in CSMS.received), "Ended")
            ended = [m for m in CSMS.received if m[0] == "TransactionEvent"][-1][1]
            self.assertEqual(ended[1]["stopped_reason"], "EVDisconnected")
            self.assertEqual(fake.calls[n:], [("DELETE", None)])
            self.assertIsNone(fake.limit)
        finally:
            task.cancel()
            for t in bridge.tasks:
                t.cancel()
            server.close()
            await bridge.imatra.close()

    async def test_ocpp21(self):
        await self.run_version("ocpp2.1")

    async def test_ocpp201(self):
        await self.run_version("ocpp2.0.1")

    async def test_unconfirmed_limit_rejected(self):
        await self.run_version("ocpp2.0.1", confirm=False)


if __name__ == "__main__":
    unittest.main()
