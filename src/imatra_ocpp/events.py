# SPDX-License-Identifier: Apache-2.0
"""Imatra feed events (api/schemas/events.v1.json) to OCPP 2.x messages.

One Imatra device is one OCPP charging station with one EVSE and one
connector. Pure and stateful per device; no I/O. Each call returns the OCPP
calls (action, payload) the station sends to the CSMS, in order.

Plug-in to unplug is one OCPP transaction (Imatra's plug session); the
digital V2G sessions inside it are not separate transactions.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone



def utc(at: str) -> str:
    """Imatra event time as OCPP wants it: UTC, milliseconds, "Z".

    Imatra may report local offsets and microseconds (e.g.
    2026-11-20T18:06:35.753255+10:00). CitrineOS rejects those with
    FormatViolation, so normalise every timestamp at the edge.
    """
    t = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


EVSE = {"id": 1, "connectorId": 1}
STOPPED_REASON = {"ev_unplugged": "EVDisconnected", "blocked": "DeAuthorized"}


@dataclass
class Device:
    device_id: str
    seq_no: int = 0
    transaction_id: str | None = None
    energy_wh: float = 0.0
    charging: bool = False
    online: bool = False
    soc: int | None = None

    def _tx(self, event_type: str, trigger: str, at: str, extra: dict | None = None) -> tuple[str, dict]:
        payload = {
            "eventType": event_type,
            "timestamp": at,
            "triggerReason": trigger,
            "seqNo": self.seq_no,
            "transactionInfo": {"transactionId": self.transaction_id},
            "evse": EVSE,
        }
        self.seq_no += 1
        for key, value in (extra or {}).items():
            if key == "transactionInfo":
                payload["transactionInfo"].update(value)
            else:
                payload[key] = value
        return "TransactionEvent", payload

    @staticmethod
    def _status(status: str, at: str) -> tuple[str, dict]:
        return "StatusNotification", {"timestamp": at, "connectorStatus": status, "evseId": 1, "connectorId": 1}

    @staticmethod
    def _meter(at: str, measurand: str, value: float, unit: str) -> list[dict]:
        return [{"timestamp": at, "sampledValue": [{"value": value, "measurand": measurand, "unitOfMeasure": {"unit": unit}}]}]

    @staticmethod
    def _soc(data: dict) -> int | None:
        """The EV's SoC in %, or None. Imatra reads it only over DIN 70121."""
        soc = data.get("soc")
        if isinstance(soc, bool) or not isinstance(soc, (int, float)) or not 0 <= soc <= 100:
            return None
        return int(soc)

    def apply(self, event: dict) -> list[tuple[str, dict]]:
        kind, at, data = event["type"], utc(event["time"]), event.get("data") or {}
        out: list[tuple[str, dict]] = []
        if kind == "device.online":
            self.online = True
            out.append(self._status("Occupied" if self.transaction_id else "Available", at))
        elif kind == "device.offline":
            self.online = False
            out.append(self._status("Unavailable", at))
        elif kind == "session.started":
            self.transaction_id = str(uuid.uuid4())
            self.seq_no, self.energy_wh, self.charging, self.soc = 0, 0.0, False, None
            out.append(self._status("Occupied", at))
            out.append(self._tx("Started", "CablePluggedIn", at, {"transactionInfo": {"chargingState": "EVConnected"}}))
        elif kind == "charging.started" and self.transaction_id:
            self.charging = True
            out.append(self._tx("Updated", "ChargingStateChanged", at, {"transactionInfo": {"chargingState": "Charging"}}))
        elif kind == "charging.stopped" and self.transaction_id:
            self.charging = False
            out.append(self._tx("Updated", "ChargingStateChanged", at, {"transactionInfo": {"chargingState": "SuspendedEV"}}))
        elif kind == "power.changed" and self.transaction_id:
            out.append(self._tx("Updated", "MeterValuePeriodic", at,
                                {"meterValue": self._meter(at, "Power.Active.Import", data["power_w"], "W")}))
        elif kind == "ev.parameters" and self.transaction_id and (soc := self._soc(data)) is not None:
            self.soc = soc
            out.append(self._tx("Updated", "MeterValuePeriodic", at, {"meterValue": [{"timestamp": at, "sampledValue": [
                {"value": soc, "measurand": "SoC", "unitOfMeasure": {"unit": "Percent"}, "location": "EV",
                 "context": "Other"}]}]}))
        elif kind == "limit.changed" and self.transaction_id:
            out.append(self._tx("Updated", "ChargingRateChanged", at))
        elif kind == "session.ended" and self.transaction_id:
            self.energy_wh = float(data.get("energy_wh") or 0.0)
            reason = STOPPED_REASON.get(data.get("reason", ""), "Other")
            out.append(self._tx("Ended", "EVCommunicationLost" if reason == "Other" else "EVDeparted", at, {
                "transactionInfo": {"chargingState": "Idle", "stoppedReason": reason},
                "meterValue": self._meter(at, "Energy.Active.Import.Register", self.energy_wh, "Wh"),
            }))
            out.append(self._status("Available", at))
            self.transaction_id, self.charging = None, False
        return out
