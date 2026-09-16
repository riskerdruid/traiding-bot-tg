"""Конфигурация установки. Читается из .env один раз при старте.

Здесь только то, что задаёт инженер при развёртывании: токен бота, кому
слать сигналы, через какую биржу смотреть рынок. Всё, что пользователь
меняет сам из Telegram, лежит в app/storage/settings_store.py.
"""

from __future__ import annotations

from datetime import timezone as _dt_timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---
    bot_token: str = ""
    owner_ids: str = ""

    # --- Рынок ---
    exchange: str = "okx"
    symbols: str = "XAU/USDT:USDT"
    exchange_proxy: str = ""

    # --- Pocket Option (бинарные опционы) ---
    po_ssid: str = ""
    po_expiry_min: int = 5
    po_min_payout: float = 70

    # --- Стратегия ---
    # Пользователь не выставляет эти числа руками: их задаёт выбранный
    # режим работы (см. PRESETS). Здесь — стартовые значения на первый
    # запуск с пустой базой.
    timeframe: str = "15m"
    htf_timeframe: str = "1h"
    scan_interval: int = 60
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    ema_trend_slow: int = 200
    rsi_period: int = 14
    rsi_long_min: float = 45
    rsi_long_max: float = 72
    rsi_short_min: float = 28
    rsi_short_max: float = 55
    adx_period: int = 14
    adx_min: float = 20
    atr_period: int = 14
    atr_sl_mult: float = 1.5
    risk_reward: float = 1.8
    min_confidence: int = 60
    max_signals_per_day: int = 10
    require_htf_agree: bool = True
    require_volume: bool = False
    signal_ttl_min: int = 240
    cooldown_min: int = 45

    # --- Система ---
    timezone: str = "Europe/Moscow"
    daily_report_at: str = "21:00"
    log_level: str = "INFO"

    # Номер коммита, из которого собран образ. Проставляется сборкой
    # (--build-arg APP_VERSION=...), поэтому git внутри контейнера не нужен.
    app_version: str = "dev"
    db_path: str = "data/signals.db"
    # Файл-отметка «я жив»: по нему docker понимает, что процесс не завис.
    heartbeat_path: str = "data/heartbeat"

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # --- Производные значения ---

    @property
    def owner_id_list(self) -> list[int]:
        out: list[int] = []
        for raw in _split(self.owner_ids):
            try:
                out.append(int(raw))
            except ValueError:
                continue
        return out

    @property
    def symbol_list(self) -> list[str]:
        return _split(self.symbols)

    @property
    def tz(self):
        """Часовой пояс отчётов. На системах без tzdata молча падаем в UTC."""
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return _dt_timezone.utc

    def validate_runtime(self) -> list[str]:
        """Возвращает список проблем, из-за которых бот не сможет работать."""
        problems: list[str] = []
        if not self.bot_token or self.bot_token.startswith("123456789:"):
            problems.append(
                "BOT_TOKEN не задан. Создайте бота у @BotFather и впишите токен в .env"
            )
        if not self.owner_id_list:
            problems.append(
                "OWNER_IDS не задан. Узнайте свой id у @userinfobot и впишите его в .env"
            )
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
