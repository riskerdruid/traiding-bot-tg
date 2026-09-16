"""Адаптер биржи через ccxt.

Работает с любой биржей из списка ccxt — в .env меняется одна строка EXCHANGE.
Свечи кешируются на время текущей свечи, чтобы не долбить API на каждом цикле.

Это «обычный» рынок: есть позиция, стоп и цель, результат меряется
движением цены. Второй вид площадки — бинарные опционы — реализован
в pocketoption.py за тем же интерфейсом.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import ccxt.async_support as ccxt

from app.config import settings
from app.market.base import (
    KIND_EXCHANGE,
    TF_SECONDS,
    BrokerAdapter,
    Candle,
    Candles,
    SymbolInfo,
)

log = logging.getLogger("market.ccxt")


class CcxtBroker(BrokerAdapter):
    """Обёртка над биржей: свечи, цены, статус подключения."""

    kind = KIND_EXCHANGE

    @property
    def id(self) -> str:
        return settings.exchange.lower()

    @property
    def title(self) -> str:
        return settings.exchange.upper()

    def __init__(self) -> None:
        self._exchange: ccxt.Exchange | None = None
        self._cache: dict[tuple[str, str], tuple[float, Candles]] = {}
        self._markets_loaded = False
        self.last_error: str | None = None

    # --- жизненный цикл ---

    def _build(self) -> ccxt.Exchange:
        exchange_id = settings.exchange.lower()
        if not hasattr(ccxt, exchange_id):
            raise ValueError(
                f"Биржа '{exchange_id}' не поддерживается ccxt. "
                f"Проверьте EXCHANGE в .env"
            )
        params: dict = {
            "enableRateLimit": True,
            "timeout": 30_000,
            "options": {"defaultType": "swap"},
        }
        if settings.exchange_proxy:
            params["proxies"] = {
                "http": settings.exchange_proxy,
                "https": settings.exchange_proxy,
            }
        return getattr(ccxt, exchange_id)(params)

    @property
    def exchange(self) -> ccxt.Exchange:
        if self._exchange is None:
            self._exchange = self._build()
        return self._exchange

    async def ensure_markets(self) -> None:
        if not self._markets_loaded:
            await self.exchange.load_markets()
            self._markets_loaded = True
            log.info(
                "Биржа %s подключена, инструментов: %d",
                settings.exchange,
                len(self.exchange.symbols or []),
            )

    async def close(self) -> None:
        if self._exchange is not None:
            await self._exchange.close()
            self._exchange = None
            self._markets_loaded = False

    # --- данные ---

    async def validate_symbols(self, symbols: list[str]) -> tuple[list[str], list[str]]:
        """Делит список на доступные и отсутствующие на бирже."""
        await self.ensure_markets()
        available = set(self.exchange.symbols or [])
        ok = [s for s in symbols if s in available]
        missing = [s for s in symbols if s not in available]
        return ok, missing

    async def list_symbols(self, active_only: bool = True) -> list[SymbolInfo]:
        """Все торгуемые инструменты биржи — для выбора пар в боте."""
        await self.ensure_markets()
        markets = self.exchange.markets or {}
        out: list[SymbolInfo] = []
        for symbol, market in markets.items():
            active = market.get("active") is not False
            if active_only and not active:
                continue
            out.append(
                SymbolInfo(
                    symbol=symbol,
                    name=market.get("id") or symbol,
                    is_otc=False,
                    payout=None,
                    asset_type=market.get("type") or "",
                    active=active,
                )
            )
        return sorted(out, key=lambda a: a.symbol)

    async def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = 300, use_cache: bool = True
    ) -> Candles:
        """Свечи по инструменту. Кеш живёт до закрытия текущей свечи."""
        key = (symbol, timeframe)
        now = time.time()

        if use_cache and key in self._cache:
            expires_at, cached = self._cache[key]
            if now < expires_at:
                return cached

        await self.ensure_markets()
        raw = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        items = [
            Candle(
                ts=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5] or 0),
            )
            for r in raw
        ]
        items.sort(key=lambda c: c.ts)
        candles = Candles(symbol=symbol, timeframe=timeframe, items=items)

        # Кешируем до конца текущей свечи, но не дольше 60 секунд —
        # на дневном таймфрейме иначе цена зависла бы на сутки.
        tf_sec = TF_SECONDS.get(timeframe, 60)
        ttl = min(tf_sec, 60)
        self._cache[key] = (now + ttl, candles)
        self.last_error = None
        return candles

    async def fetch_history(
        self, symbol: str, timeframe: str, bars: int = 1500
    ) -> Candles:
        """Длинная история постранично.

        Биржи отдают ограниченное число свечей за один запрос (OKX — 300),
        поэтому для бэктеста собираем историю страницами, двигаясь вперёд
        от точки в прошлом. Используется только офлайн-инструментами:
        живому сканеру хватает одной страницы.
        """
        await self.ensure_markets()
        tf_ms = TF_SECONDS.get(timeframe, 900) * 1000
        page_size = 300
        cursor = int(time.time() * 1000) - tf_ms * bars
        now_ms = int(time.time() * 1000)

        collected: dict[int, Candle] = {}
        max_pages = max(1, bars // page_size + 2)

        for _ in range(max_pages):
            batch = await self.exchange.fetch_ohlcv(
                symbol, timeframe, since=cursor, limit=page_size
            )
            if not batch:
                break
            for r in batch:
                ts = int(r[0])
                collected[ts] = Candle(
                    ts=ts,
                    open=float(r[1]),
                    high=float(r[2]),
                    low=float(r[3]),
                    close=float(r[4]),
                    volume=float(r[5] or 0),
                )
            next_cursor = int(batch[-1][0]) + tf_ms
            if next_cursor <= cursor or next_cursor > now_ms:
                break
            cursor = next_cursor

        items = [collected[ts] for ts in sorted(collected)]
        log.info(
            "История %s %s: %d свечей", symbol, timeframe, len(items)
        )
        return Candles(symbol=symbol, timeframe=timeframe, items=items)

    async def fetch_price(self, symbol: str) -> float | None:
        """Текущая цена инструмента."""
        try:
            await self.ensure_markets()
            ticker = await self.exchange.fetch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            return float(price) if price is not None else None
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("Не удалось получить цену %s: %s", symbol, exc)
            return None

    async def fetch_prices(self, symbols: list[str]) -> dict[str, float]:
        """Цены по нескольким инструментам одним запросом, если биржа умеет."""
        out: dict[str, float] = {}
        try:
            await self.ensure_markets()
            if self.exchange.has.get("fetchTickers"):
                tickers = await self.exchange.fetch_tickers(symbols)
                for sym, t in tickers.items():
                    price = t.get("last") or t.get("close")
                    if price is not None:
                        out[sym] = float(price)
                if out:
                    return out
        except Exception as exc:
            log.debug("fetch_tickers недоступен (%s), опрашиваю по одному", exc)

        for sym in symbols:
            price = await self.fetch_price(sym)
            if price is not None:
                out[sym] = price
        return out

    async def health(self) -> dict:
        """Состояние подключения — для экрана сигналов."""
        started = time.time()
        try:
            await self.ensure_markets()
            symbol = settings.symbol_list[0] if settings.symbol_list else None
            price = await self.fetch_price(symbol) if symbol else None
            return {
                "connected": price is not None,
                "exchange": settings.exchange,
                "latency_ms": int((time.time() - started) * 1000),
                "error": self.last_error,
            }
        except Exception as exc:
            self.last_error = str(exc)
            return {
                "connected": False,
                "exchange": settings.exchange,
                "latency_ms": int((time.time() - started) * 1000),
                "error": str(exc),
            }


