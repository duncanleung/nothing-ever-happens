import logging
import os
import sys
from logging.handlers import RotatingFileHandler

try:
    from pythonjsonlogger.json import JsonFormatter
except ImportError:
    from pythonjsonlogger.jsonlogger import JsonFormatter  # type: ignore[no-redef]


def configure_logging(level: str) -> None:
    formatter = JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(stdout_handler)

    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "bot.jsonl"),
        maxBytes=50 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
