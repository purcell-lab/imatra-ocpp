# Design

## Shape

- One process serves many Imatra devices. Each device is one OCPP charging
  station (identity = the Imatra device id) with one EVSE and one connector.
- Towards Imatra: an operator key with `read` and `control` rights, sent as
  `X-API-Key`. Reads `/v1/devices`, follows `/v1/events?cursor=` (at least
  once; the event id de-duplicates; the cursor is stored), sends
  `POST`/`DELETE /v1/devices/{id}/limit`.
- Towards the CSMS: one WebSocket per station, offering subprotocols
  `ocpp2.1` then `ocpp2.0.1`, with the `ocpp` library (The Mobility House,
  MIT) `v21` and `v201` modules over a shared core. Security Profile 1 or 2
  (Basic Auth, with TLS for profile 2).

## Events to OCPP (`events.py`)

| Imatra event | OCPP |
|---|---|
| `device.online` | StatusNotification Available (Occupied during a session) |
| `device.offline` | StatusNotification Unavailable |
| `session.started` (plug-in) | StatusNotification Occupied; TransactionEvent Started, CablePluggedIn, EVConnected |
| `charging.started` / `charging.stopped` | TransactionEvent Updated, ChargingStateChanged, Charging / SuspendedEV |
| `power.changed` | TransactionEvent Updated, MeterValuePeriodic, Power.Active.Import |
| `limit.changed` | TransactionEvent Updated, ChargingRateChanged |
| `session.ended` | TransactionEvent Ended (EVDisconnected for `ev_unplugged`), Energy.Active.Import.Register; StatusNotification Available |

One plug session is one OCPP transaction; V2G sessions inside it are not.
BootNotification is sent at connection, before the first event.

## Charging profiles to limits (`limits.py`)

| OCPP | Imatra |
|---|---|
| TxProfile, TxDefaultProfile, ChargingStationMaxProfile | accepted |
| other purposes, Recurring kind | Rejected |
| period in force now, A or W (W / (V x phases)) | `limit_a` |
| below 6 A, including 0 A | Rejected `BelowMinimum` |
| above the station limit | clamped |
| `duration`, `validTo`, next period, or the default | `duration_s`, capped |
| ClearChargingProfile | `DELETE /v1/devices/{id}/limit` |
| 2.1 `operationMode` other than ChargingOnly, `dischargeLimit` | Rejected |

## Confirmation

SetChargingProfile is answered only after Imatra confirms the device applied
the limit (`limit.applied`, or `.limit.confirmed_at` on the device). The lab
shows about 50 ms. If no confirmation arrives within a bound well inside the
CSMS response timeout, the answer is Rejected and the limit is cancelled.
Before a session, a TxDefaultProfile is kept and applied at plug-in.

## Not in scope yet

- Recurring and multi-period schedules (beyond the current period),
  stacking of several profiles.
- Authorisation (IdTokens, Plug & Charge); RequestStart/StopTransaction.
- ISO 15118-20, DC, V2G discharge (not in Imatra).
