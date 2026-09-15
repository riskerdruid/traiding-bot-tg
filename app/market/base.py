"""Общий интерфейс источников рыночных данных.

Проект работает с двумя принципиально разными площадками:

* **биржи** (OKX, Binance…) — реальный рынок, позиция со стопом и целью,
  результат меряется движением цены;
* **брокеры бинарных опционов** (Pocket Option) — ставка на направление
  с фиксированным сроком и выплатой; стоп и цель там не существуют,
  а котировки OTC-пар брокер генерирует сам, и взять их можно только
  у него.

Чтобы остальной код не знал об этой разнице, оба вида спрятаны за одним
интерфейсом `BrokerAdapter`. Стратегия, журнал сигналов, статистика
и Mini App работают с ними одинаково.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# Длительность таймфрейма в секундах
TF_SECONDS = {
    "5s": 5,
    "15s": 15,
    "30s": 30,
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
}

# Вид площадки
KIND_EXCHANGE = "exchange"   # биржа: позиция, стоп, цель
KIND_BINARY = "binary"       # бинарные опционы: направление, срок, выплата


@dataclass(slots=True)
class Candle:
    ts: int  # время открытия, мс
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True)
class Candles:
    """Набор свечей с удобной выборкой колонок для индикаторов."""

    symbol: str
    timeframe: str
    items: list[Candle]

    def __len__(self) -> int:
        return len(self.items)

    @property
    def opens(self) -> list[float]:
        return [c.open for c in self.items]

    @property
    def highs(self) -> list[float]:
        return [c.high for c in self.items]

    @property
    def lows(self) -> list[float]:
        return [c.low for c in self.items]

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.items]

    @property
    def volumes(self) -> list[float]:
        return [c.volume for c in self.items]

    @property
    def last(self) -> Candle | None:
        return self.items[-1] if self.items else None

    def closed_only(self) -> Candles:
        """Без последней свечи — она ещё формируется и может перерисоваться.

        Сигналы принимаем только по закрытым свечам, иначе бот будет
        дёргаться на каждом тике и выдавать сигналы, которых потом нет
        на графике у трейдера.
        """
        return Candles(self.symbol, self.timeframe, self.items[:-1])


@dataclass(slots=True)
class SymbolInfo:
    """Инструмент в том виде, в каком его показывает приложение."""

    symbol: str            # внутреннее имя без префикса площадки
    name: str = ""         # человекочитаемое
    is_otc: bool = False   # синтетические котировки брокера
    payout: float | None = None   # выплата в % (только бинарные опционы)
    asset_type: str = ""   # currency / crypto / commodity …
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "is_otc": self.is_otc,
            "payout": self.payout,
            "asset_type": self.asset_type,
            "active": self.active,
        }


class BrokerError(Exception):
    """Ошибка площадки, понятная пользователю."""


class BrokerAdapter(ABC):
    """Единый интерфейс площадки.

    Реализации: `CcxtBroker` (биржи) и `PocketOptionBroker` (опционы).
    """

    #: короткий идентификатор, он же префикс символов: "okx:XAU/USDT:USDT"
    id: str = "broker"
    #: человекочитаемое название для интерфейса
    title: str = "Площадка"
    #: KIND_EXCHANGE или KIND_BINARY
    kind: str = KIND_EXCHANGE

    last_error: str | None = None

    @property
    def is_binary(self) -> bool:
        return self.kind == KIND_BINARY

    @abstractmethod
    async def close(self) -> None:
        """Закрыть соединения."""

    @abstractmethod
    async def list_symbols(self) -> list[SymbolInfo]:
        """Доступные инструменты."""

    @abstractmethod
    async def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = 300, use_cache: bool = True
    ) -> Candles:
        """Свечи по инструменту."""

    @abstractmethod
    async def fetch_price(self, symbol: str) -> float | None:
        """Текущая цена."""

    async def fetch_prices(self, symbols: list[str]) -> dict[str, float]:
        """Цены по нескольким инструментам. По умолчанию — по одному."""
        out: dict[str, float] = {}
        for symbol in symbols:
            price = await self.fetch_price(symbol)
            if price is not None:
                out[symbol] = price
        return out

    @abstractmethod
    async def health(self) -> dict:
        """Состояние подключения — для /status и Mini App."""

    async def payout(self, symbol: str) -> float | None:
        """Выплата по инструменту в процентах.

        Есть смысл только у бинарных опционов; биржи возвращают None.
        """
        return None

    async def fetch_history(
        self, symbol: str, timeframe: str, bars: int = 1500
    ) -> Candles:
        """Длинная история для бэктеста. По умолчанию — сколько отдаст площадка."""
        return await self.fetch_candles(symbol, timeframe, limit=bars, use_cache=False)

    def supported_timeframes(self) -> list[str]:
        """Какие таймфреймы площадка умеет отдавать."""
        return ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
