from __future__ import annotations

import copy
import logging

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from .config import HOST, PORT


def main() -> None:
    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config["disable_existing_loggers"] = False
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("backend.main:app", host=HOST, port=PORT, log_config=log_config)


if __name__ == "__main__":
    main()
