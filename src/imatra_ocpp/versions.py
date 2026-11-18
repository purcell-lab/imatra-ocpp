# SPDX-License-Identifier: Apache-2.0
"""OCPP versions offered to the CSMS, in order of preference."""

OCPP21 = "ocpp2.1"
OCPP201 = "ocpp2.0.1"
SUBPROTOCOLS = (OCPP21, OCPP201)


def negotiated(subprotocol: str | None) -> str:
    """The version the CSMS chose. A CSMS that picks none is refused."""
    if subprotocol not in SUBPROTOCOLS:
        raise ValueError(f"CSMS chose no supported OCPP version (got {subprotocol!r})")
    return subprotocol
