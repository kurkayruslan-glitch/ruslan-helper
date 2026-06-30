import logging
import os
import sys


def setup_logging(name: str = "ruslan-helper") -> logging.Logger:
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        ))
        root.addHandler(handler)
    root.setLevel(level)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    return logger
