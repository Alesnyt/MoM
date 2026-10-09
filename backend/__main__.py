from __future__ import annotations

import copy
import logging

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from . import logctx  # noqa: F401 — meeting id on every log record
from .config import HOST, PORT

_LOG_FORMAT = "%(levelname)s %(name)s [%(meeting)s] %(message)s"


def main() -> None:
    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config["disable_existing_loggers"] = False
    log_config["formatters"]["mom"] = {"format": _LOG_FORMAT}
    log_config["handlers"]["mom"] = {
        "formatter": "mom",
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stderr",
    }
    log_config["loggers"]["mom"] = {"handlers": ["mom"], "level": "INFO", "propagate": False}
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    uvicorn.run("backend.main:app", host=HOST, port=PORT, log_config=log_config)


if __name__ == "__main__":
    main()
