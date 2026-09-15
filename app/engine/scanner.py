"""Сканер рынка: ищет точки входа и заводит сигналы в журнал."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from app.config import settings
from app.market.feed import feed, split_symbol
from app.news.calendar import calendar
from app.storage import repo
from app.storage.settings_store import config
from app.strategy.trend_momentum import strategy

log = logging.getLogger("engine.scanner")

SignalCallback = Callable[[repo.Signal], Awaitable[None]]


class Scanner:
    """Периодически опрашивает рынок и создаёт сигналы.

    Намеренные ограничения, каждое из которых лечит конкретную болезнь:

    * анализ только по ЗАКРЫТЫМ свечам — иначе сигнал «мигает» внутри свечи
      и не совпадает с тем, что трейдер видит на графике;
    * один активный сигнал на инструмент — чтобы не было каши из
      противоречивых рекомендаций;
    * кулдаун после сигнала — защита от серии входов на одном движении;
    * одна свеча — максимум один сигнал.
    """

    def __init__(self, on_signal: SignalCallback | None = None) -> None:
        self.on_signal = on_signal
        self.running = False
        self.last_scan_at: int | None = None
        self.scans_done = 0
        self.last_error: str | None = None
        self._last_candle_ts: dict[str, int] = {}
        self._active_symbols: list[str] = []
        self._task: asyncio.Task | None = None

    # ----------------------------------------------------------------
    # Запуск
    # ----------------------------------------------------------------

    async def prepare(self) -> list[str]:
        """Проверяет, какие инструменты реально доступны на бирже."""
        wanted = config.get("symbols")
        try:
            ok, missing = await feed.validate_symbols(wanted)
        except Exception as exc:
            self.last_error = str(exc)
            log.error("Не удалось загрузить список инструментов: %s", exc)
            return []

        if missing:
            log.warning(
                "Эти инструменты не найдены на бирже %s и будут пропущены: %s",
                settings.exchange,
                ", ".join(missing),
            )
            await repo.log_event(
                "warning",
                f"Инструменты недоступны на {settings.exchange}: {', '.join(missing)}",
                {"missing": missing},
            )
        if ok:
            log.info("В работе инструменты: %s", ", ".join(ok))
        self._active_symbols = ok
        return ok

    def start(self) -> asyncio.Task:
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="scanner")
        return self._task

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        await self.prepare()
        await calendar.refresh(force=True)

        while self.running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("Ошибка цикла сканирования: %s", exc)
                await repo.log_event("error", f"Сбой сканера: {exc}")
            await asyncio.sleep(config.get("scan_interval"))

    # ----------------------------------------------------------------
    # Один проход
    # ----------------------------------------------------------------

    async def scan_once(self) -> list[repo.Signal]:
        """Один полный проход по всем инструментам."""
        # Перечитываем настройки — заказчик мог поменять их из приложения
        # минуту назад, и они должны примениться без перезапуска.
        previous_symbols = list(config.get("symbols") or [])
        await config.load()
        # SSID брокера мог измениться в приложении — подхватываем без перезапуска
        feed.apply_settings(pocket_ssid=str(config.get("po_ssid") or ""))
        if list(config.get("symbols") or []) != previous_symbols:
            log.info("Список инструментов изменён, перепроверяю на бирже")
            self._active_symbols = []

        await calendar.refresh()

        if not self._active_symbols:
            await self.prepare()
            if not self._active_symbols:
                return []

        # Вне рабочих часов не ищем входы вовсе
        if not self._within_trade_hours():
            self.last_scan_at = int(time.time())
            self.scans_done += 1
            return []

        # Дневной лимит сигналов
        limit = int(config.get("max_signals_per_day") or 0)
        if limit > 0:
            since = int(time.time()) - 86400
            today = len(await repo.list_signals(limit=limit + 1, since=since))
            if today >= limit:
                log.info("Дневной лимит сигналов исчерпан (%d), пауза до завтра", limit)
                self.last_scan_at = int(time.time())
                self.scans_done += 1
                return []

        # Если идёт важная новость — молчим по всем инструментам разом
        muted_by = calendar.mute_reason()
        if muted_by is not None:
            minutes = muted_by.minutes_from()
            when = f"через {minutes:.0f} мин" if minutes > 0 else f"{-minutes:.0f} мин назад"
            log.info("Тишина из-за новости: %s (%s)", muted_by.title, when)
            self.last_scan_at = int(time.time())
            self.scans_done += 1
            return []

        created: list[repo.Signal] = []
        for symbol in self._active_symbols:
            try:
                signal = await self._scan_symbol(symbol)
                if signal is not None:
                    created.append(signal)
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Ошибка анализа %s: %s", symbol, exc)

        self.last_scan_at = int(time.time())
        self.scans_done += 1
        return created

    async def _scan_symbol(self, symbol: str) -> repo.Signal | None:
        # Уже есть активный сигнал — новый не нужен
        active = await repo.get_active_signals(symbol)
        if active:
            return None

        # Кулдаун после предыдущего сигнала
        last_at = await repo.last_signal_at(symbol)
        if last_at and (time.time() - last_at) < config.get("cooldown_min") * 60:
            return None

        raw = await feed.fetch_candles(
            symbol, config.get("timeframe"), limit=max(strategy.min_candles() + 20, 320)
        )
        candles = raw.closed_only()
        if len(candles) < strategy.min_candles():
            log.debug(
                "%s: свечей %d, нужно %d — жду накопления",
                symbol,
                len(candles),
                strategy.min_candles(),
            )
            return None

        # Одна свеча — один сигнал
        last_candle = candles.last
        if last_candle is None:
            return None
        if self._last_candle_ts.get(symbol) == last_candle.ts:
            return None

        htf_raw = await feed.fetch_candles(symbol, config.get("htf_timeframe"), limit=320)
        htf = htf_raw.closed_only()

        result = strategy.analyze(candles, htf)
        if result is None:
            return None

        self._last_candle_ts[symbol] = last_candle.ts

        if result.confidence < config.get("min_confidence"):
            log.debug(
                "%s: сигнал %s отброшен, уверенность %d < %d",
                symbol,
                result.side,
                result.confidence,
                config.get("min_confidence"),
            )
            return None

        broker_id, _ = split_symbol(symbol)
        is_binary = feed.is_binary(symbol)

        expiry_at: int | None = None
        payout: float | None = None
        reasons = list(result.reasons)

        if is_binary:
            expiry_min = int(config.get("po_expiry_min") or 5)
            expiry_at = int(time.time()) + expiry_min * 60
            payout = await feed.payout(symbol)

            min_payout = float(config.get("po_min_payout") or 0)
            if payout is not None and payout < min_payout:
                log.info(
                    "%s: сигнал отброшен, выплата %.0f%% ниже порога %.0f%%",
                    symbol, payout, min_payout,
                )
                return None

            reasons.append(f"Экспирация через {expiry_min} мин")
            if payout is not None:
                reasons.append(f"Выплата брокера {payout:.0f}%")

        signal = await repo.create_signal(
            symbol=symbol,
            side=result.side,
            timeframe=config.get("timeframe"),
            entry=result.entry,
            stop_loss=result.stop_loss,
            take_profit=result.take_profit,
            confidence=result.confidence,
            reasons=reasons,
            indicators=result.indicators,
            broker=broker_id,
            kind="binary" if is_binary else "exchange",
            expiry_at=expiry_at,
            payout=payout,
        )
        log.info(
            "Сигнал #%d %s %s @ %.4f (уверенность %d%%)",
            signal.id,
            signal.side,
            signal.symbol,
            signal.entry,
            signal.confidence,
        )
        await repo.log_event(
            "signal",
            f"{signal.side} {signal.symbol} @ {signal.entry:.4f}",
            {"signal_id": signal.id, "confidence": signal.confidence},
        )

        if self.on_signal is not None:
            try:
                await self.on_signal(signal)
            except Exception as exc:
                log.error("Не удалось отправить сигнал #%d: %s", signal.id, exc)

        return signal

    def _within_trade_hours(self) -> bool:
        """Попадает ли текущее время в заданное окно работы."""
        window = str(config.get("trade_hours") or "").strip()
        if not window or "-" not in window:
            return True
        try:
            start, end = (p.strip() for p in window.split("-", 1))
            now_hm = datetime.now(settings.tz).strftime("%H:%M")
            if start == end:
                return True
            if start < end:
                return start <= now_hm < end
            # Окно через полночь, например 22:00-06:00
            return now_hm >= start or now_hm < end
        except Exception:
            return True

    # ----------------------------------------------------------------
    # Состояние для /status и Mini App
    # ----------------------------------------------------------------

    def state(self) -> dict:
        muted = calendar.mute_reason()
        return {
            "running": self.running,
            "symbols": self._active_symbols,
            "last_scan_at": self.last_scan_at,
            "scans_done": self.scans_done,
            "last_error": self.last_error,
            "muted_by_news": muted.to_dict() if muted else None,
            "within_hours": self._within_trade_hours(),
            "trade_hours": str(config.get("trade_hours") or ""),
            "news_loaded": calendar.loaded,
        }
