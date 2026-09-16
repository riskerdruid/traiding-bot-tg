"""Маршрутизатор рыночных данных между площадками.

Остальной код (стратегия, сканер, трекер, API) обращается сюда и не знает,
откуда пришли свечи — с биржи или от брокера бинарных опционов.

Инструмент адресуется префиксом площадки:

    okx:XAU/USDT:USDT   — бессрочный фьючерс на золото на бирже
    po:EURUSD_otc       — OTC-пара у Pocket Option

Без префикса инструмент считается биржевым — так продолжают работать
конфигурации, написанные до появления второй площадки.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.market.base import (
    KIND_BINARY,
    KIND_EXCHANGE,
    TF_SECONDS,
    BrokerAdapter,
    BrokerError,
    Candle,
    Candles,
    SymbolInfo,
)
from app.market.ccxt_broker import CcxtBroker
from app.market.pocketoption import PocketOptionBroker

log = logging.getLogger("market.router")

__all__ = [
    "Candle",
    "Candles",
    "SymbolInfo",
    "BrokerError",
    "TF_SECONDS",
    "KIND_BINARY",
    "KIND_EXCHANGE",
    "MarketRouter",
    "feed",
    "split_symbol",
    "join_symbol",
]

# Префикс биржи меняется вместе с настройкой EXCHANGE, поэтому
# для неё зарезервировано устойчивое имя
EXCHANGE_PREFIX = "ex"
POCKET_PREFIX = "po"


def split_symbol(full: str) -> tuple[str, str]:
    """«po:EURUSD_otc» -> («po», «EURUSD_otc»).

    Осторожно: у биржевых символов двоеточие тоже встречается
    («XAU/USDT:USDT»), поэтому префиксом считается только известное имя
    площадки, стоящее перед первым двоеточием.
    """
    if ":" in full:
        head, _, rest = full.partition(":")
        if head in (POCKET_PREFIX, EXCHANGE_PREFIX):
            return head, rest
    return EXCHANGE_PREFIX, full


def join_symbol(broker_id: str, symbol: str) -> str:
    """Собирает адрес инструмента обратно."""
    if broker_id == EXCHANGE_PREFIX:
        return symbol
    return f"{broker_id}:{symbol}"


class MarketRouter:
    """Держит подключения ко всем площадкам и раздаёт запросы по адресу."""

    def __init__(self) -> None:
        self._exchange = CcxtBroker()
        # Ключ из .env — стартовый. Тот, что человек прислал боту, лежит
        # в базе и заменит этот при первом же чтении настроек.
        self._pocket = PocketOptionBroker(settings.po_ssid)
        self.last_error: str | None = None

    # ----------------------------------------------------------------
    # Доступ к площадкам
    # ----------------------------------------------------------------

    @property
    def exchange_broker(self) -> CcxtBroker:
        return self._exchange

    @property
    def pocket_broker(self) -> PocketOptionBroker:
        return self._pocket

    def broker_for(self, full_symbol: str) -> tuple[BrokerAdapter, str]:
        """Площадка и «чистое» имя инструмента."""
        prefix, symbol = split_symbol(full_symbol)
        if prefix == POCKET_PREFIX:
            return self._pocket, symbol
        return self._exchange, symbol

    def brokers(self) -> dict[str, BrokerAdapter]:
        return {EXCHANGE_PREFIX: self._exchange, POCKET_PREFIX: self._pocket}

    def apply_settings(self, pocket_ssid: str | None = None) -> None:
        """Подхватывает изменения настроек без перезапуска процесса."""
        if pocket_ssid is not None and pocket_ssid != self._pocket.ssid:
            self._pocket.ssid = pocket_ssid
            # Соединение со старым токеном больше не годится
            self._pocket._connected = False
            self._pocket._api = None
            log.info("SSID Pocket Option обновлён, соединение будет пересоздано")

    def is_binary(self, full_symbol: str) -> bool:
        broker, _ = self.broker_for(full_symbol)
        return broker.is_binary

    # ----------------------------------------------------------------
    # Данные
    # ----------------------------------------------------------------

    async def fetch_candles(
        self, full_symbol: str, timeframe: str, limit: int = 300,
        use_cache: bool = True,
    ) -> Candles:
        broker, symbol = self.broker_for(full_symbol)
        candles = await broker.fetch_candles(
            symbol, timeframe, limit=limit, use_cache=use_cache
        )
        # Наружу отдаём полное имя, чтобы журнал и интерфейс не путались
        return Candles(full_symbol, candles.timeframe, candles.items)

    async def fetch_history(
        self, full_symbol: str, timeframe: str, bars: int = 1500
    ) -> Candles:
        broker, symbol = self.broker_for(full_symbol)
        candles = await broker.fetch_history(symbol, timeframe, bars=bars)
        return Candles(full_symbol, candles.timeframe, candles.items)

    async def fetch_price(self, full_symbol: str) -> float | None:
        broker, symbol = self.broker_for(full_symbol)
        try:
            return await broker.fetch_price(symbol)
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("Нет цены %s: %s", full_symbol, exc)
            return None

    async def fetch_prices(self, full_symbols: list[str]) -> dict[str, float]:
        """Цены по списку — каждой площадке свой пакет запросов."""
        by_broker: dict[str, list[str]] = {}
        mapping: dict[tuple[str, str], str] = {}
        for full in full_symbols:
            prefix, symbol = split_symbol(full)
            by_broker.setdefault(prefix, []).append(symbol)
            mapping[(prefix, symbol)] = full

        out: dict[str, float] = {}
        for prefix, symbols in by_broker.items():
            broker = self._pocket if prefix == POCKET_PREFIX else self._exchange
            try:
                prices = await broker.fetch_prices(symbols)
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Нет цен для %s: %s", prefix, exc)
                continue
            for symbol, price in prices.items():
                out[mapping.get((prefix, symbol), symbol)] = price
        return out

    async def payout(self, full_symbol: str) -> float | None:
        broker, symbol = self.broker_for(full_symbol)
        try:
            return await broker.payout(symbol)
        except Exception:
            return None

    async def list_symbols(self, broker_id: str = EXCHANGE_PREFIX) -> list[SymbolInfo]:
        """Инструменты конкретной площадки, уже с полными адресами."""
        broker = self._pocket if broker_id == POCKET_PREFIX else self._exchange
        assets = await broker.list_symbols()
        return [
            SymbolInfo(
                symbol=join_symbol(broker_id, a.symbol),
                name=a.name,
                is_otc=a.is_otc,
                payout=a.payout,
                asset_type=a.asset_type,
                active=a.active,
            )
            for a in assets
        ]

    async def validate_symbols(
        self, full_symbols: list[str]
    ) -> tuple[list[str], list[str]]:
        """Делит список на доступные и отсутствующие."""
        ok: list[str] = []
        missing: list[str] = []

        exchange_symbols = [s for s in full_symbols if split_symbol(s)[0] == EXCHANGE_PREFIX]
        pocket_symbols = [s for s in full_symbols if split_symbol(s)[0] == POCKET_PREFIX]

        if exchange_symbols:
            try:
                good, bad = await self._exchange.validate_symbols(
                    [split_symbol(s)[1] for s in exchange_symbols]
                )
                good_set = set(good)
                for full in exchange_symbols:
                    (ok if split_symbol(full)[1] in good_set else missing).append(full)
            except Exception as exc:
                log.error("Биржа недоступна: %s", exc)
                missing.extend(exchange_symbols)

        if pocket_symbols:
            if not self._pocket.configured:
                log.warning(
                    "Инструменты Pocket Option заданы, но SSID не указан — пропускаю: %s",
                    ", ".join(pocket_symbols),
                )
                missing.extend(pocket_symbols)
            else:
                try:
                    available = {a.symbol for a in await self._pocket.list_symbols()}
                    for full in pocket_symbols:
                        (ok if split_symbol(full)[1] in available else missing).append(full)
                except Exception as exc:
                    log.error("Pocket Option недоступен: %s", exc)
                    missing.extend(pocket_symbols)

        return ok, missing

    # ----------------------------------------------------------------
    # Состояние
    # ----------------------------------------------------------------

    async def health(self) -> dict:
        """Состояние биржи — для обратной совместимости с /status."""
        return await self._exchange.health()

    async def health_all(self) -> list[dict]:
        """Состояние всех площадок."""
        out = [await self._exchange.health()]
        if self._pocket.configured or self._pocket._assets:
            out.append(await self._pocket.health())
        return out

    async def close(self) -> None:
        for broker in (self._exchange, self._pocket):
            try:
                await broker.close()
            except Exception:
                pass


# Один общий экземпляр на всё приложение
feed = MarketRouter()
