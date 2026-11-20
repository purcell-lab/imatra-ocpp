# SPDX-License-Identifier: Apache-2.0
import re
import unittest

from imatra_ocpp.events import Device


def ev(kind, **data):
    return {"seq": 1, "id": "x", "type": kind, "schema_ver": 1, "time": "2026-11-19T01:00:00Z",
            "device_id": "dev-01", "data": data}


class Events(unittest.TestCase):
    def test_plug_session_is_one_transaction(self):
        d = Device("dev-01")
        self.assertEqual(d.apply(ev("device.online"))[0][1]["connectorStatus"], "Available")
        started = d.apply(ev("session.started", cp="B"))
        self.assertEqual([a for a, _ in started], ["StatusNotification", "TransactionEvent"])
        tx = started[1][1]
        self.assertEqual((tx["eventType"], tx["triggerReason"], tx["seqNo"]), ("Started", "CablePluggedIn", 0))
        tid = tx["transactionInfo"]["transactionId"]

        charging = d.apply(ev("charging.started"))[0][1]
        self.assertEqual(charging["transactionInfo"], {"transactionId": tid, "chargingState": "Charging"})
        power = d.apply(ev("power.changed", power_w=6900, quality="measured"))[0][1]
        self.assertEqual(power["meterValue"][0]["sampledValue"][0]["value"], 6900)

        ended = d.apply(ev("session.ended", reason="ev_unplugged", energy_wh=1.1))
        tx = ended[0][1]
        self.assertEqual((tx["eventType"], tx["seqNo"]), ("Ended", 3))
        self.assertEqual(tx["transactionInfo"]["stoppedReason"], "EVDisconnected")
        self.assertEqual(tx["meterValue"][0]["sampledValue"][0]["measurand"], "Energy.Active.Import.Register")
        self.assertEqual(ended[1][1]["connectorStatus"], "Available")
        self.assertIsNone(d.transaction_id)

    def test_no_transaction_events_outside_a_session(self):
        d = Device("dev-01")
        for kind, data in (("charging.started", {}), ("power.changed", {"power_w": 1, "quality": "estimated"}),
                           ("session.ended", {"reason": "ev_unplugged"})):
            self.assertEqual(d.apply(ev(kind, **data)), [])

    def test_soc_is_an_ev_measurand(self):
        d = Device("dev-01")
        d.apply(ev("session.started", cp="B"))
        tx = d.apply(ev("ev.parameters", source="din70121", soc=47))[0][1]
        self.assertEqual((tx["eventType"], tx["triggerReason"]), ("Updated", "MeterValuePeriodic"))
        self.assertEqual(tx["meterValue"][0]["sampledValue"][0], {
            "value": 47, "measurand": "SoC", "unitOfMeasure": {"unit": "Percent"}, "location": "EV", "context": "Other"})
        self.assertEqual(d.soc, 47)
        # ISO 15118-2 gives no SoC; bad values and readings outside a session send nothing.
        for data in ({"source": "iso15118-2", "e_amount_wh": 9000}, {"source": "din70121", "soc": 101},
                     {"source": "din70121", "soc": True}):
            self.assertEqual(d.apply(ev("ev.parameters", **data)), [])
        self.assertEqual(Device("dev-02").apply(ev("ev.parameters", source="din70121", soc=50)), [])

    def test_offline_unavailable(self):
        self.assertEqual(Device("dev-01").apply(ev("device.offline"))[0][1]["connectorStatus"], "Unavailable")


if __name__ == "__main__":
    unittest.main()


class Timestamps(unittest.TestCase):
    def test_local_offset_and_microseconds_become_utc_ms(self):
        from imatra_ocpp.events import utc
        self.assertEqual(utc("2026-11-20T18:06:35.753255+10:00"), "2026-11-20T08:06:35.753Z")
        self.assertEqual(utc("2026-11-19T01:00:00Z"), "2026-11-19T01:00:00.000Z")

    def test_every_message_timestamp_is_utc(self):
        d = Device("dev-01")
        msgs = []
        for kind, data in (("device.online", {}), ("session.started", {"cp": "B"}),
                           ("power.changed", {"power_w": 6900}), ("session.ended", {"reason": "ev_unplugged", "energy_wh": 1.1})):
            e = ev(kind, **data); e["time"] = "2026-11-20T18:06:35.753255+10:00"
            msgs += d.apply(e)
        stamps = re.findall(r"'timestamp': '([^']+)'", str(msgs))
        self.assertTrue(stamps)
        for s in stamps:
            self.assertRegex(s, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
