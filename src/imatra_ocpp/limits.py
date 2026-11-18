# SPDX-License-Identifier: Apache-2.0
"""SetChargingProfile (OCPP 2.1 / 2.0.1) to an Imatra operator limit.

Pure functions, no I/O. The rules, safety first:

- Imatra applies one current per device for a bounded time. Only the period
  in force now is used; a profile whose later periods differ is accepted only
  if it is refreshed by the CSMS (a later SetChargingProfile).
- Below 6 A (IEC 61851 minimum), including 0 A, is rejected, never rounded up.
- Above the station limit is clamped to it (an OCPP limit is a maximum).
- Every Imatra limit needs a duration: from the schedule duration or validTo,
  else the configured default; never longer than the configured maximum.
- OCPP 2.1: only operationMode ChargingOnly (or absent); no discharge.
- Recurring profiles are not supported yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .versions import OCPP21

MIN_A = 6.0
PURPOSES = {"TxProfile", "TxDefaultProfile", "ChargingStationMaxProfile"}


@dataclass(frozen=True)
class Station:
    max_a: float = 32.0
    voltage_v: float = 230.0
    phases: int = 3
    default_duration_s: int = 3600
    max_duration_s: int = 86400


@dataclass(frozen=True)
class Limit:
    limit_a: float
    duration_s: int

    def body(self) -> dict:
        """The body of POST /v1/devices/{id}/limit."""
        return {"limit_a": self.limit_a, "duration_s": self.duration_s}


@dataclass(frozen=True)
class Rejected:
    reason_code: str  # OCPP StatusInfo.reasonCode
    info: str


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_limit(profile: dict, station: Station, version: str, now: datetime | None = None) -> Limit | Rejected:
    now = now or datetime.now(timezone.utc)
    if profile.get("chargingProfilePurpose") not in PURPOSES:
        return Rejected("UnsupportedPurpose", f"purpose {profile.get('chargingProfilePurpose')!r}")
    kind = profile.get("chargingProfileKind")
    if kind not in ("Absolute", "Relative"):
        return Rejected("UnsupportedKind", f"kind {kind!r}")

    schedules = profile.get("chargingSchedule") or []
    if isinstance(schedules, dict):  # tolerate a single schedule object
        schedules = [schedules]
    if not schedules:
        return Rejected("InvalidSchedule", "no chargingSchedule")
    schedule = schedules[0]
    unit = schedule.get("chargingRateUnit")
    periods = sorted(schedule.get("chargingSchedulePeriod") or [], key=lambda p: p.get("startPeriod", 0))
    if not periods:
        return Rejected("InvalidSchedule", "no chargingSchedulePeriod")

    start = schedule.get("startSchedule")
    elapsed = 0.0
    if kind == "Absolute" and start:
        elapsed = (now - _parse_time(start)).total_seconds()
        if elapsed < 0:
            return Rejected("NotYetSupported", "a schedule starting later")
    current = [p for p in periods if p.get("startPeriod", 0) <= elapsed]
    if not current:
        return Rejected("InvalidSchedule", "no period in force now")
    period = current[-1]

    if version == OCPP21:
        mode = period.get("operationMode")
        if mode not in (None, "ChargingOnly"):
            return Rejected("UnsupportedOperationMode", f"operationMode {mode!r}")
        if period.get("dischargeLimit") not in (None, 0):
            return Rejected("UnsupportedOperationMode", "dischargeLimit")

    value = float(period.get("limit", -1))
    if value < 0:
        return Rejected("InvalidValue", "negative or missing limit")
    phases = int(period.get("numberPhases") or station.phases)
    if unit == "A":
        amps = value
    elif unit == "W":
        amps = value / (station.voltage_v * phases)
    else:
        return Rejected("InvalidValue", f"chargingRateUnit {unit!r}")
    amps = round(amps, 1)
    if amps < MIN_A:
        return Rejected("BelowMinimum", f"{amps} A is below {MIN_A:g} A")
    amps = min(amps, station.max_a)

    duration = None
    if schedule.get("duration"):
        duration = int(schedule["duration"]) - int(max(elapsed, 0))
    elif profile.get("validTo"):
        duration = int((_parse_time(profile["validTo"]) - now).total_seconds())
    later = [p for p in periods if p.get("startPeriod", 0) > elapsed]
    if later:  # stop at the next period; the CSMS must send it again
        until_next = int(later[0]["startPeriod"] - elapsed)
        duration = until_next if duration is None else min(duration, until_next)
    if duration is None:
        duration = station.default_duration_s
    if duration <= 0:
        return Rejected("InvalidSchedule", "the schedule has already ended")
    return Limit(limit_a=amps, duration_s=min(duration, station.max_duration_s))
