"""Подключение к SQLite и схема базы.

Схема создаётся при старте и обновляется идемпотентно — отдельные миграции
не нужны, достаточно дописывать CREATE TABLE IF NOT EXISTS и ALTER-патчи
в _MIGRATIONS.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from app.config import settings

log = logging.getLogger("storage.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,           -- LONG | SHORT
    timeframe     TEXT    NOT NULL,
    entry         REAL    NOT NULL,
    stop_loss     REAL    NOT NULL,
    take_profit   REAL    NOT NULL,
    confidence    INTEGER NOT NULL,
    reasons       TEXT    NOT NULL DEFAULT '[]',  -- JSON-массив причин входа
    indicators    TEXT    NOT NULL DEFAULT '{}',  -- JSON-снимок индикаторов
    status        TEXT    NOT NULL DEFAULT 'ACTIVE',
                                              -- ACTIVE|TP_HIT|SL_HIT|EXPIRED|CANCELLED
    created_at    INTEGER NOT NULL,           -- unix seconds
    closed_at     INTEGER,
    exit_price    REAL,
    pnl_pct       REAL,                       -- результат в % от цены входа
    r_multiple    REAL,                       -- результат в единицах риска
    mfe_pct       REAL DEFAULT 0,             -- максимум в плюс за жизнь сигнала
    mae_pct       REAL DEFAULT 0,             -- максимум в минус
    notified      INTEGER NOT NULL DEFAULT 0,

    -- Поля бинарных опционов. У биржевых сигналов пустые.
    broker        TEXT    NOT NULL DEFAULT 'ex',  -- ex | po
    kind          TEXT    NOT NULL DEFAULT 'exchange',  -- exchange | binary
    expiry_at     INTEGER,                    -- когда истекает опцион, unix
    payout        REAL                        -- выплата брокера в % на момент сигнала
);

CREATE INDEX IF NOT EXISTS idx_signals_status  ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_symbol  ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,      -- info | warning | error | signal | outcome
    message    TEXT    NOT NULL,
    payload    TEXT    NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);

CREATE TABLE IF NOT EXISTS news_cache (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    currency   TEXT    NOT NULL,
    impact     TEXT    NOT NULL,
    event_at   INTEGER NOT NULL,
    forecast   TEXT    DEFAULT '',
    previous   TEXT    DEFAULT '',
    UNIQUE(title, event_at)
);

CREATE INDEX IF NOT EXISTS idx_news_at ON news_cache(event_at);
"""

# Дополнения к уже существующим базам. Выполняются по одному,
# ошибка "duplicate column" считается нормой.
_MIGRATIONS: list[str] = [
    # Поля появились вместе с поддержкой бинарных опционов.
    # На уже существующих базах добавляются здесь.
    "ALTER TABLE signals ADD COLUMN broker TEXT NOT NULL DEFAULT 'ex'",
    "ALTER TABLE signals ADD COLUMN kind TEXT NOT NULL DEFAULT 'exchange'",
    "ALTER TABLE signals ADD COLUMN expiry_at INTEGER",
    "ALTER TABLE signals ADD COLUMN payout REAL",
]


class Database:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or settings.db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        if self._conn is None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.path)
            self._conn.row_factory = aiosqlite.Row
            # WAL позволяет читать из Mini App, пока сканер пишет
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA synchronous=NORMAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.commit()
        return self._conn

    async def init(self) -> None:
        conn = await self.connect()
        await conn.executescript(_SCHEMA)
        for statement in _MIGRATIONS:
            try:
                await conn.execute(statement)
            except Exception as exc:
                if "duplicate column" not in str(exc).lower():
                    log.warning("Миграция пропущена: %s (%s)", statement[:60], exc)
        await conn.commit()
        log.info("База готова: %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


db = Database()
