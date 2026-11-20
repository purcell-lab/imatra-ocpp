# SPDX-License-Identifier: Apache-2.0
"""One Imatra device as one OCPP 2.1 / 2.0.1 charging station.

The OCPP side uses the `ocpp` library's v21 or v201 module, chosen by the
subprotocol the CSMS picked; the behaviour is shared. Charging profiles go
through limits.to_limit (the safety rules) and are answered Accepted only
when the device confirmed the limit, or, outside a session, when stored for
the next plug-in.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ocpp.charge_point import camel_to_snake_case, snake_to_camel_case
from ocpp.routing import after, on

from .events import Device
from .imatra import Imatra, ImatraError
from .limits import Limit, Rejected, Station as StationLimits, to_limit
from .versions import OCPP21

log = logging.getLogger("imatra_ocpp.station")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Profile:
    id: int
    purpose: str
    stack_level: int
    limit: Limit
    expires: float  # monotonic


@dataclass
class Settings:
    vendor: str = "purcell-lab"
    model: str = "imatra-ocpp"
    voltage_v: float = 230.0
    phases: int = 3
    default_duration_s: int = 3600
    max_duration_s: int = 86400
    confirm_timeout_s: float = 10.0


class StationCore:
    """State and rules for one device, independent of the OCPP version."""

    def __init__(self, device_id: str, station_id: str, imatra: Imatra, settings: Settings, station_max_a: float = 32.0):
        self.device_id, self.station_id = device_id, station_id
        self.imatra, self.settings = imatra, settings
        self.station_max_a = station_max_a
        self.mapper = Device(device_id)
        self.profiles: dict[str, Profile] = {}
        self.applied: tuple[float, float] | None = None  # (limit_a, until monotonic)
        self.version: str | None = None
        self.lock = asyncio.Lock()

    def limits(self) -> StationLimits:
        s = self.settings
        return StationLimits(max_a=self.station_max_a, voltage_v=s.voltage_v, phases=s.phases,
                             default_duration_s=s.default_duration_s, max_duration_s=s.max_duration_s)

    def _effective(self) -> Limit | None:
        now = time.monotonic()
        self.profiles = {k: p for k, p in self.profiles.items() if p.expires > now}
        tx = self.profiles.get("TxProfile") or self.profiles.get("TxDefaultProfile")
        mx = self.profiles.get("ChargingStationMaxProfile")
        chosen = [p for p in (tx, mx) if p]
        if not chosen:
            return None
        amps = min(p.limit.limit_a for p in chosen)
        remaining = round(min(p.expires for p in chosen) - now)
        return Limit(limit_a=amps, duration_s=max(1, min(remaining, self.settings.default_duration_s)))

    async def _apply(self, confirm: bool) -> tuple[bool, str]:
        """Send the effective limit to Imatra (or cancel it). (ok, info)."""
        limit = self._effective()
        try:
            if limit is None:
                if self.applied:
                    await self.imatra.clear_limit(self.device_id)
                    self.applied = None
                return True, "cleared"
            # The device confirms a limit by pushing a changed loop answer. A
            # limit equal to the one it already confirmed changes nothing, so it
            # is never confirmed again: treat it as in force and only renew it.
            current = {}
            if confirm:
                current = (await self.imatra.device(self.device_id)).get("limit") or {}
            same = (current.get("confirmed_at") and current.get("limit_a") is not None
                    and abs(float(current["limit_a"]) - limit.limit_a) < 0.05)
            cid = await self.imatra.set_limit(self.device_id, limit.limit_a, limit.duration_s)
            self.applied = (limit.limit_a, time.monotonic() + limit.duration_s)
            if not confirm:
                return True, f"sent {limit.limit_a} A"
            if same:
                return True, f"{limit.limit_a} A already in force on the device, renewed"
            confirmed = await self.imatra.wait_confirmed(self.device_id, cid, self.settings.confirm_timeout_s)
            if confirmed is None:
                return False, f"{limit.limit_a} A not confirmed by the device in {self.settings.confirm_timeout_s:g} s"
            return True, f"{limit.limit_a} A confirmed via {confirmed.get('confirmed_via')} in {confirmed.get('reaction_ms')} ms"
        except ImatraError as e:
            return False, f"Imatra refused: {e.status}"

    async def set_profile(self, evse_id: int, profile: dict) -> tuple[str, str | None, str]:
        """(status, reasonCode, info) for SetChargingProfile. profile in camelCase."""
        if evse_id not in (0, 1):
            return "Rejected", "UnknownEvse", f"evse {evse_id}"
        purpose = profile.get("chargingProfilePurpose")
        if purpose == "TxProfile":
            if not self.mapper.transaction_id:
                return "Rejected", "TxNotFound", "no transaction"
            if profile.get("transactionId") not in (None, self.mapper.transaction_id):
                return "Rejected", "TxNotFound", "another transaction"
        result = to_limit(profile, self.limits(), self.version or OCPP21)
        if isinstance(result, Rejected):
            return "Rejected", result.reason_code, result.info
        async with self.lock:
            previous = self.profiles.get(purpose)
            self.profiles[purpose] = Profile(int(profile.get("id", 0)), purpose, int(profile.get("stackLevel", 0)),
                                             result, time.monotonic() + result.duration_s)
            if not self.mapper.transaction_id:
                return "Accepted", None, f"stored for the next session ({result.limit_a} A)"
            ok, info = await self._apply(confirm=True)
            if not ok:
                if previous:
                    self.profiles[purpose] = previous
                else:
                    self.profiles.pop(purpose, None)
                await self._apply(confirm=False)
                return "Rejected", "NotConfirmed", info
            return "Accepted", None, info

    async def clear_profile(self, profile_id: int | None, criteria: dict | None) -> str:
        criteria = criteria or {}
        purpose, level = criteria.get("chargingProfilePurpose"), criteria.get("stackLevel")
        async with self.lock:
            hit = [k for k, p in self.profiles.items()
                   if (profile_id is None or p.id == profile_id)
                   and (purpose is None or p.purpose == purpose)
                   and (level is None or p.stack_level == level)]
            for k in hit:
                del self.profiles[k]
            if hit and self.mapper.transaction_id:
                await self._apply(confirm=False)
        return "Accepted" if hit else "Unknown"

    async def on_event(self, event: dict) -> list[tuple[str, dict]]:
        out = self.mapper.apply(event)
        kind = event["type"]
        if kind == "session.started" and self._effective():
            asyncio.create_task(self._apply_logged())
        elif kind == "session.ended":
            await self._release()
        return out

    async def _release(self) -> None:
        """End of session: drop the TxProfile and cancel the limit on the device.

        Without the cancel, Imatra keeps the last limit for its full duration
        and the next session starts limited. Profiles that are still valid
        (TxDefaultProfile, ChargingStationMaxProfile) are re-sent at the next
        session.started.
        """
        async with self.lock:
            self.profiles.pop("TxProfile", None)
            if not self.applied:
                return
            try:
                await self.imatra.clear_limit(self.device_id)
            except ImatraError as e:
                log.warning("%s: limit not cancelled at session end: Imatra refused: %s", self.station_id, e.status)
            self.applied = None

    async def _apply_logged(self) -> None:
        async with self.lock:
            ok, info = await self._apply(confirm=True)
        log.info("%s: stored profile at plug-in: %s", self.station_id, info if ok else "FAILED " + info)

    async def refresh(self) -> None:
        """Re-send the limit before Imatra's duration runs out, while profiles stay valid."""
        if not self.mapper.transaction_id or not self.applied:
            return
        if self.applied[1] - time.monotonic() < 120:
            async with self.lock:
                await self._apply(confirm=False)


def build_charge_point(version: str):
    """The ocpp ChargePoint class for this version, with the shared handlers."""
    mod = importlib.import_module("ocpp.v21" if version == OCPP21 else "ocpp.v201")
    call, call_result = mod.call, mod.call_result
    Action = mod.enums.Action

    class ChargePoint(mod.ChargePoint):
        core: StationCore

        async def send(self, action: str, payload: dict):
            return await self.call(getattr(call, action)(**camel_to_snake_case(payload)))

        async def boot(self) -> int:
            r = await self.call(call.BootNotification(
                charging_station={"vendor_name": self.core.settings.vendor, "model": self.core.settings.model,
                                  "serial_number": self.core.device_id},
                reason="PowerUp"))
            if r.status != "Accepted":
                raise RuntimeError(f"BootNotification {r.status}")
            return int(r.interval or 300)

        async def heartbeat(self):
            return await self.call(call.Heartbeat())

        @on(Action.set_charging_profile)
        async def on_set_charging_profile(self, evse_id, charging_profile, **_):
            status, reason, info = await self.core.set_profile(evse_id, snake_to_camel_case(charging_profile))
            log.info("%s: SetChargingProfile %s %s", self.core.station_id, status, info)
            return call_result.SetChargingProfile(
                status=status, status_info={"reason_code": reason, "additional_info": info[:512]} if reason else None)

        @on(Action.clear_charging_profile)
        async def on_clear_charging_profile(self, charging_profile_id=None, charging_profile_criteria=None, **_):
            status = await self.core.clear_profile(charging_profile_id, snake_to_camel_case(charging_profile_criteria or {}))
            return call_result.ClearChargingProfile(status=status)

        @on(Action.get_variables)
        async def on_get_variables(self, get_variable_data, **_):
            return call_result.GetVariables(get_variable_result=[
                {"attribute_status": "UnknownComponent", "component": d["component"], "variable": d["variable"]}
                for d in get_variable_data])

        @on(Action.set_variables)
        async def on_set_variables(self, set_variable_data, **_):
            return call_result.SetVariables(set_variable_result=[
                {"attribute_status": "Rejected", "component": d["component"], "variable": d["variable"]}
                for d in set_variable_data])

        @on(Action.get_base_report)
        async def on_get_base_report(self, request_id, report_base, **_):
            return call_result.GetBaseReport(status="Accepted")

        @after(Action.get_base_report)
        async def after_get_base_report(self, request_id, report_base, **_):
            await self.call(call.NotifyReport(request_id=request_id, generated_at=_now(), seq_no=0, tbc=False, report_data=[
                {"component": {"name": "ChargingStation"}, "variable": {"name": "Model"},
                 "variable_attribute": [{"type": "Actual", "value": self.core.settings.model, "mutability": "ReadOnly"}]},
                {"component": {"name": "SmartChargingCtrlr"}, "variable": {"name": "Enabled"},
                 "variable_attribute": [{"type": "Actual", "value": "true", "mutability": "ReadOnly"}]},
            ]))

        @on(Action.trigger_message)
        async def on_trigger_message(self, requested_message, **_):
            ok = requested_message in ("BootNotification", "Heartbeat", "StatusNotification")
            return call_result.TriggerMessage(status="Accepted" if ok else "NotImplemented")

        @after(Action.trigger_message)
        async def after_trigger_message(self, requested_message, **_):
            if requested_message == "Heartbeat":
                await self.heartbeat()
            elif requested_message == "StatusNotification":
                status = "Occupied" if self.core.mapper.transaction_id else (
                    "Available" if self.core.mapper.online else "Unavailable")
                await self.send("StatusNotification", {"timestamp": _now(), "connectorStatus": status,
                                                       "evseId": 1, "connectorId": 1})
            elif requested_message == "BootNotification":
                await self.boot()

        @on(Action.change_availability)
        async def on_change_availability(self, operational_status, **_):
            return call_result.ChangeAvailability(status="Accepted" if operational_status == "Operative" else "Rejected")

        @on(Action.reset)
        async def on_reset(self, type, **_):  # noqa: A002 (the OCPP field name)
            return call_result.Reset(status="Rejected")

        @on(Action.request_start_transaction)
        async def on_request_start(self, id_token, remote_start_id, **_):
            # Imatra starts on plug-in (EIM); no remote start.
            return call_result.RequestStartTransaction(status="Rejected")

        @on(Action.request_stop_transaction)
        async def on_request_stop(self, transaction_id, **_):
            return call_result.RequestStopTransaction(status="Rejected")

    return ChargePoint
