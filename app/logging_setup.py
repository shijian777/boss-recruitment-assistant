from __future__ import annotations

import logging
import queue
from logging.handlers import RotatingFileHandler
from pathlib import Path


class UiQueueHandler(logging.Handler):
    def __init__(self, event_queue: "queue.Queue[dict[str, object]]") -> None:
        super().__init__()
        self.event_queue = event_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.event_queue.put({"type": "log", "message": self.format(record)})
        except Exception:
            self.handleError(record)


def setup_logging(log_dir: Path, event_queue: "queue.Queue[dict[str, object]]") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("boss_inviter")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(
        log_dir / "boss_inviter.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    ui_handler = UiQueueHandler(event_queue)
    ui_handler.setFormatter(formatter)
    logger.addHandler(ui_handler)
    return logger
