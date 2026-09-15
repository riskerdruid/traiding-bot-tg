"""Настройка логирования: цветная консоль + ротация в файл."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = _COLORS.get(record.levelname, "")
        record.levelname_c = f"{color}{record.levelname:<8}{_RESET}"
        return super().format(record)


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        _ColorFormatter(
            "%(asctime)s %(levelname_c)s %(name)-22s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        Path(log_dir) / "bot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)-22s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)

    # Сторонние библиотеки слишком разговорчивы на DEBUG
    for noisy in ("aiogram.event", "httpx", "ccxt", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
