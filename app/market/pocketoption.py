"""Адаптер брокера бинарных опционов Pocket Option.

Зачем отдельный адаптер. Котировки OTC-пар («EURUSD_otc» и подобные)
брокер генерирует сам: они существуют только внутри его платформы, идут
и в выходные, и получить их со стороны нельзя. Считать индикаторы по
настоящему EUR/USD и выдавать сигнал по OTC-графику — значит показывать
трейдеру цифры, которых нет у него на экране. Поэтому данные берём
у самого брокера.

Работает поверх библиотеки `BinaryOptionsToolsV2` (обёртка на Rust).
Зависимость необязательная: если пакет не установлен или не задан SSID,
адаптер аккуратно выключается, а остальной бот продолжает работать.

Ограничения, о которых важно помнить:

* нужен **SSID** — токен сессии из браузера, где выполнен вход. Он живёт
  ограниченное время, и при истечении адаптер сообщит об этом;
* официального API у брокера нет, библиотека держится на реверс-инжиниринге
  веб-сокета и может ломаться после обновлений платформы;
* лицензия библиотеки разрешает свободно только личное использование —
  см. README проекта.

Бот **не торгует** и здесь: методы `buy`/`sell` библиотеки не используются,
берутся только котировки и справочник инструментов.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.market.base import (
    KIND_BINARY,
    TF_SECONDS,
    BrokerAdapter,
    BrokerError,
    Candle,
    Candles,
    SymbolInfo,
)

log = logging.getLogger("market.pocketoption")

# Таймфреймы, которые платформа отдаёт свечами
SUPPORTED_TF = ["5s", "15s", "30s", "1m", "5m", "15m", "30m", "1h"]

# Сколько свечей просить за раз, если не сказано иное
DEFAULT_LIMIT = 300

# Справочник инструментов меняется редко — держим его пять минут
ASSETS_TTL = 300


def library_available() -> bool:
    """Установлен ли пакет с клиентом брокера."""
    try:
        import BinaryOptionsToolsV2  # noqa: F401

        return True
    except Exception:
        return False


class PocketOptionBroker(BrokerAdapter):
    id = "po"
    title = "Pocket Option"
    kind = KIND_BINARY

    def __init__(self, ssid: str = "") -> None:
        self.ssid = ssid or ""
        self._api = None
        self._connected = False
        self._assets: list[SymbolInfo] = []
        self._assets_at: float = 0.0
        self._candles_cache: dict[tuple[str, str], tuple[float, Candles]] = {}
        self._lock = asyncio.Lock()
        self.last_error: str | None = None
        self._is_demo: bool | None = None

    # ----------------------------------------------------------------
    # Подключение
    # ----------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.ssid.strip())

    async def _ensure(self):
        """Ленивое подключение с понятными ошибками."""
        if self._api is not None and self._connected:
            return self._api

        if not self.configured:
            raise BrokerError(
                "Не задан SSID Pocket Option. Откройте платформу в браузере, "
                "войдите в аккаунт и скопируйте SSID сессии в настройках бота."
            )
        if not library_available():
            raise BrokerError(
                "Не установлен пакет BinaryOptionsToolsV2. "
                "Выполните: pip install BinaryOptionsToolsV2"
            )

        async with self._lock:
            if self._api is not None and self._connected:
                return self._api
            try:
                from BinaryOptionsToolsV2.config import Config
                from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync

                config = Config(connection_initialization_timeout_secs=25)
                api = PocketOptionAsync(self.ssid, config=config)
                await api.connect()
                self._api = api
                self._connected = True
                self.last_error = None
                try:
                    self._is_demo = bool(await api.is_demo())
                except Exception:
                    self._is_demo = None
                log.info(
                    "Pocket Option подключён (%s)",
                    "демо-счёт" if self._is_demo else "реальный счёт"
                    if self._is_demo is False
                    else "тип счёта неизвестен",
                )
            except BrokerError:
                raise
            except Exception as exc:
                self._api = None
                self._connected = False
                self.last_error = str(exc)
                raise BrokerError(
                    f"Не удалось подключиться к Pocket Option: {exc}. "
                    "Чаще всего это означает, что SSID устарел — "
                    "получите новый в браузере."
                ) from None
        return self._api

    async def close(self) -> None:
        if self._api is not None:
            try:
                await self._api.shutdown()
            except Exception:
                pass
            self._api = None
        self._connected = False

    async def _reconnect(self) -> None:
        """Сброс соединения — следующий запрос поднимет его заново."""
        log.info("Переподключаюсь к Pocket Option")
        await self.close()

    # ----------------------------------------------------------------
    # Инструменты
    # ----------------------------------------------------------------

    async def list_symbols(self) -> list[SymbolInfo]:
        now = time.time()
        if self._assets and now - self._assets_at < ASSETS_TTL:
            return self._assets

        api = await self._ensure()
        try:
            raw = await api.active_assets()
        except Exception as exc:
            self.last_error = str(exc)
            await self._reconnect()
            raise BrokerError(f"Не удалось получить список активов: {exc}") from None

        assets: list[SymbolInfo] = []
        for item in raw or []:
            try:
                assets.append(
                    SymbolInfo(
                        symbol=str(item.get("symbol") or "").strip(),
                        name=str(item.get("name") or "").strip(),
                        is_otc=bool(item.get("is_otc")),
                        payout=_as_float(item.get("payout")),
                        asset_type=str(item.get("asset_type") or "").strip(),
                        active=True,
                    )
                )
            except Exception:
                continue

        assets = [a for a in assets if a.symbol]
        self._assets = sorted(assets, key=lambda a: (not a.is_otc, a.symbol))
        self._assets_at = now
        log.info("Pocket Option: активов доступно %d", len(self._assets))
        return self._assets

    async def payout(self, symbol: str) -> float | None:
        """Выплата по инструменту в процентах — прямо влияет на ожидание."""
        for asset in await self.list_symbols():
            if asset.symbol == symbol:
                return asset.payout
        return None

    # ----------------------------------------------------------------
    # Котировки
    # ----------------------------------------------------------------

    def supported_timeframes(self) -> list[str]:
        return list(SUPPORTED_TF)

    async def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = DEFAULT_LIMIT,
        use_cache: bool = True,
    ) -> Candles:
        period = TF_SECONDS.get(timeframe)
        if period is None:
            raise BrokerError(
                f"Pocket Option не отдаёт свечи таймфрейма {timeframe}. "
                f"Доступны: {', '.join(SUPPORTED_TF)}"
            )

        key = (symbol, timeframe)
        now = time.time()
        if use_cache and key in self._candles_cache:
            expires_at, cached = self._candles_cache[key]
            if now < expires_at:
                return cached

        api = await self._ensure()
        # offset — насколько вглубь истории уходим, в секундах
        offset = period * max(limit, 50)
        try:
            raw = await api.get_candles(symbol, period, offset)
        except Exception as exc:
            self.last_error = str(exc)
            await self._reconnect()
            raise BrokerError(
                f"Не удалось получить свечи {symbol}: {exc}"
            ) from None

        items = _parse_candles(raw)
        candles = Candles(symbol=symbol, timeframe=timeframe, items=items)

        ttl = min(period, 30)
        self._candles_cache[key] = (now + ttl, candles)
        self.last_error = None
        return candles

    async def fetch_price(self, symbol: str) -> float | None:
        try:
            candles = await self.fetch_candles(symbol, "1m", limit=2)
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("Нет цены %s: %s", symbol, exc)
            return None
        last = candles.last
        return last.close if last else None

    # ----------------------------------------------------------------
    # Состояние
    # ----------------------------------------------------------------

    async def health(self) -> dict:
        started = time.time()
        base = {
            "broker": self.id,
            "title": self.title,
            "kind": self.kind,
            "configured": self.configured,
            "library": library_available(),
        }
        if not self.configured:
            return {
                **base,
                "connected": False,
                "latency_ms": 0,
                "error": "SSID не задан",
            }
        if not library_available():
            return {
                **base,
                "connected": False,
                "latency_ms": 0,
                "error": "Пакет BinaryOptionsToolsV2 не установлен",
            }
        try:
            api = await self._ensure()
            balance = None
            try:
                balance = await api.balance()
            except Exception:
                pass
            return {
                **base,
                "connected": True,
                "latency_ms": int((time.time() - started) * 1000),
                "error": None,
                "demo": self._is_demo,
                "balance": balance,
                "assets": len(self._assets),
            }
        except Exception as exc:
            return {
                **base,
                "connected": False,
                "latency_ms": int((time.time() - started) * 1000),
                "error": str(exc),
            }


# --------------------------------------------------------------------------
# Разбор ответов
# --------------------------------------------------------------------------


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_candles(raw) -> list[Candle]:
    """Приводит ответ библиотеки к нашим свечам.

    Формат у неё — список словарей, но имена ключей между версиями
    различались, поэтому принимаем несколько вариантов написания.
    """
    items: list[Candle] = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        ts = row.get("time", row.get("timestamp", row.get("ts")))
        if ts is None:
            continue
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        # Библиотека отдаёт секунды, у нас внутри миллисекунды
        ts_ms = int(ts * 1000) if ts < 1e12 else int(ts)

        close = _as_float(row.get("close", row.get("c")))
        if close is None:
            continue
        open_ = _as_float(row.get("open", row.get("o"))) or close
        high = _as_float(row.get("high", row.get("h"))) or max(open_, close)
        low = _as_float(row.get("low", row.get("l"))) or min(open_, close)
        volume = _as_float(row.get("volume", row.get("v"))) or 0.0

        items.append(
            Candle(ts=ts_ms, open=open_, high=high, low=low, close=close, volume=volume)
        )

    items.sort(key=lambda c: c.ts)
    # Защита от дублей по времени — брокер иногда повторяет последнюю свечу
    unique: dict[int, Candle] = {c.ts: c for c in items}
    return [unique[ts] for ts in sorted(unique)]
