# SPDX-License-Identifier: Apache-2.0
"""python -m imatra_ocpp CONFIG.json"""
import asyncio
import logging
import os
import sys

from .bridge import Bridge, Config


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IMATRA_OCPP_CONFIG", "/data/options.json")
    asyncio.run(Bridge(Config.load(path)).run())


if __name__ == "__main__":
    main()
