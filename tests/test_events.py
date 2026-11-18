# SPDX-License-Identifier: Apache-2.0
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

    def test_offline_unavailable(self):
        self.assertEqual(Device("dev-01").apply(ev("device.offline"))[0][1]["connectorStatus"], "Unavailable")


if __name__ == "__main__":
    unittest.main()
