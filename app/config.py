"""Конфигурация приложения. Все значения читаются из .env."""

from __future__ import annotations

from functools import lru_cache
from datetime import timezone as _dt_timezone
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
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
    webapp_url: str = ""
    domain: str = ""

    # --- Рынок ---
    exchange: str = "okx"
    symbols: str = "XAU/USDT:USDT"
    timeframe: str = "15m"
    htf_timeframe: str = "1h"
    scan_interval: int = 60
    exchange_proxy: str = ""

    # --- Pocket Option (бинарные опционы) ---
    po_ssid: str = ""
    po_expiry_min: int = 5
    po_min_payout: float = 70
    po_otc_only: bool = False

    # --- Стратегия ---
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
    max_signals_per_day: int = 0
    trade_hours: str = ""
    require_htf_agree: bool = True
    require_volume: bool = False
    signal_ttl_min: int = 240
    cooldown_min: int = 45

    # --- Новости ---
    news_filter_enabled: bool = True
    news_mute_before_min: int = 30
    news_mute_after_min: int = 15
    news_currencies: str = "USD"
    news_impact: str = "High"

    # --- Сервер ---
    webapp_dev_mode: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    timezone: str = "Europe/Moscow"
    daily_report_at: str = "21:00"
    log_level: str = "INFO"
    db_path: str = "data/signals.db"

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
    def news_currency_list(self) -> list[str]:
        return [c.upper() for c in _split(self.news_currencies)]

    @property
    def news_impact_list(self) -> list[str]:
        return [i.capitalize() for i in _split(self.news_impact)]

    @property
    def tz(self):
        """Часовой пояс отчётов. На системах без tzdata молча падаем в UTC."""
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return _dt_timezone.utc

    @property
    def webapp_enabled(self) -> bool:
        return self.webapp_url.startswith("https://")

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
        if not self.symbol_list:
            problems.append("SYMBOLS пуст — нечего анализировать")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
