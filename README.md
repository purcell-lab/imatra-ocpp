# imatra-ocpp

An OCPP 2.1 / 2.0.1 charging-station client for
[Imatra](https://github.com/Chargetronic/imatra.iso15118_server) ISO 15118-2
devices. It puts OCPP in the control path: a CSMS such as CitrineOS sees each
Imatra device as one charging station, and its charging profiles become Imatra
operator limits.

```
CSMS (CitrineOS) <-- OCPP 2.1 / 2.0.1 --> imatra-ocpp <-- /v1 API + event feed --> Imatra <-- MQTT/mTLS --> station <-- ISO 15118-2 --> EV
```

Status: early. The mapping core (charging profiles to limits, Imatra events
to OCPP transactions) is written and tested. The runtime (WebSocket
connections, the event-feed reader, confirmation handling) is next. See
[docs/design.md](docs/design.md).

## Why a separate project

Imatra's own [adapters guide](https://github.com/Chargetronic/imatra.iso15118_server/blob/main/adapters/README.md)
says OCPP clients are out of its scope and are built as separate processes on
its published interfaces: the operator API `/v1` and the event feed
`/v1/events?cursor=`. This project changes nothing in Imatra and links none of
its code.

## Safety rules

- Limits below 6 A, including 0 A, are rejected, never rounded up.
- Limits above the station limit are clamped to it.
- Every limit has a duration. A profile with later periods is applied only to
  the end of the current period.
- OCPP 2.1: `operationMode` `ChargingOnly` only. No discharge.
- A SetChargingProfile is answered `Accepted` only after Imatra reports that
  the device applied the limit.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests
```

## Licence and patents

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Chargetronic's
[PATENTS.md](https://github.com/Chargetronic/imatra.iso15118_server/blob/main/PATENTS.md)
says communication adapters and other equipment are not covered by Imatra's
licence and, where they fall under its claims (FI 20246111 A1, EP 4711188 A1),
need a licence from Salute Solutions Oy. This project is lab software. Confirm
with Chargetronic (cto@chargetronic.fi) before production or commercial use.
