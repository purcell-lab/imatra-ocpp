# SPDX-License-Identifier: Apache-2.0
import unittest
from datetime import datetime, timezone

from imatra_ocpp.limits import Limit, Rejected, Station, to_limit
from imatra_ocpp.versions import OCPP21, OCPP201

NOW = datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc)
ST = Station(max_a=32, voltage_v=230, phases=3, default_duration_s=3600, max_duration_s=86400)


def profile(limit, unit="A", purpose="TxProfile", kind="Relative", **period):
    return {
        "id": 1, "stackLevel": 0, "chargingProfilePurpose": purpose, "chargingProfileKind": kind,
        "chargingSchedule": [{"id": 1, "chargingRateUnit": unit,
                              "chargingSchedulePeriod": [{"startPeriod": 0, "limit": limit, **period}]}],
    }


class Limits(unittest.TestCase):
    def test_amps_with_default_duration(self):
        self.assertEqual(to_limit(profile(10), ST, OCPP201, NOW), Limit(10.0, 3600))

    def test_watts_to_amps_three_phase(self):
        self.assertEqual(to_limit(profile(6900, "W"), ST, OCPP201, NOW).limit_a, 10.0)

    def test_watts_single_phase_override(self):
        self.assertEqual(to_limit(profile(2300, "W", numberPhases=1), ST, OCPP201, NOW).limit_a, 10.0)

    def test_below_six_amps_and_zero_rejected(self):
        for value in (5.9, 2, 0):
            r = to_limit(profile(value), ST, OCPP201, NOW)
            self.assertIsInstance(r, Rejected, value)
            self.assertEqual(r.reason_code, "BelowMinimum")

    def test_above_station_clamped(self):
        self.assertEqual(to_limit(profile(63), ST, OCPP201, NOW).limit_a, 32)

    def test_duration_from_schedule_and_capped(self):
        p = profile(16); p["chargingSchedule"][0]["duration"] = 600
        self.assertEqual(to_limit(p, ST, OCPP201, NOW).duration_s, 600)
        p["chargingSchedule"][0]["duration"] = 10 ** 7
        self.assertEqual(to_limit(p, ST, OCPP201, NOW).duration_s, 86400)

    def test_stops_at_next_period(self):
        p = profile(16)
        p["chargingSchedule"][0]["chargingSchedulePeriod"].append({"startPeriod": 900, "limit": 8})
        self.assertEqual(to_limit(p, ST, OCPP201, NOW), Limit(16.0, 900))

    def test_absolute_picks_period_in_force(self):
        p = profile(16, kind="Absolute")
        s = p["chargingSchedule"][0]
        s["startSchedule"] = "2026-11-19T00:50:00Z"
        s["chargingSchedulePeriod"].append({"startPeriod": 300, "limit": 8})
        self.assertEqual(to_limit(p, ST, OCPP201, NOW).limit_a, 8.0)

    def test_ocpp21_charging_only(self):
        self.assertIsInstance(to_limit(profile(10, operationMode="ChargingOnly"), ST, OCPP21, NOW), Limit)
        for extra in ({"operationMode": "CentralSetpoint"}, {"dischargeLimit": -5}):
            r = to_limit(profile(10, **extra), ST, OCPP21, NOW)
            self.assertEqual(r.reason_code, "UnsupportedOperationMode", extra)

    def test_unsupported_shapes_rejected(self):
        self.assertEqual(to_limit(profile(10, kind="Recurring"), ST, OCPP201, NOW).reason_code, "UnsupportedKind")
        self.assertEqual(to_limit(profile(10, purpose="ChargingStationExternalConstraints"), ST, OCPP201, NOW).reason_code,
                         "UnsupportedPurpose")
        self.assertEqual(to_limit(profile(-1), ST, OCPP201, NOW).reason_code, "InvalidValue")


if __name__ == "__main__":
    unittest.main()
