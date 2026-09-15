"""Отслеживание исходов сигналов.

Без этого модуля бот был бы «генератором мнений»: выдал рекомендацию и забыл.
Здесь каждый сигнал доводится до конца — тейк, стоп или истечение срока, —
и именно эти исходы превращаются в winrate, который заказчик увидит в /stats.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from app.config import settings
from app.market.feed import feed
from app.storage import repo
from app.storage.settings_store import config

log = logging.getLogger("engine.tracker")

OutcomeCallback = Callable[[repo.Signal], Awaitable[None]]

# Как часто проверять активные сигналы
CHECK_INTERVAL = 30


def _cfg_for(signal: repo.Signal):
    """Настройки владельца сигнала.

    Срок жизни и рабочий таймфрейм теперь у каждого свои, поэтому закрывать
    чужой сигнал по своим правилам нельзя.
    """
    if getattr(signal, "owner_id", None):
        return config.view(signal.owner_id)
    return config


class Tracker:
    def __init__(self, on_outcome: OutcomeCallback | None = None) -> None:
        self.on_outcome = on_outcome
        self.running = False
        self.last_check_at: int | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="tracker")
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
        while self.running:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("Ошибка отслеживания: %s", exc)
            await asyncio.sleep(CHECK_INTERVAL)

    # ----------------------------------------------------------------

    async def check_once(self) -> list[repo.Signal]:
        """Проверяет все активные сигналы. Возвращает закрытые на этом проходе."""
        active = await repo.get_active_signals()
        self.last_check_at = int(time.time())
        if not active:
            return []

        closed: list[repo.Signal] = []

        # Группируем по паре «инструмент + таймфрейм»: у разных получателей
        # он может отличаться, а свечи качать дважды незачем
        by_key: dict[tuple[str, str], list[repo.Signal]] = {}
        for signal in active:
            tf = _cfg_for(signal).get("timeframe")
            by_key.setdefault((signal.symbol, tf), []).append(signal)

        for (symbol, timeframe), signals in by_key.items():
            try:
                candles = await feed.fetch_candles(
                    symbol, timeframe, limit=320, use_cache=False
                )
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Нет данных по %s для проверки сигналов: %s", symbol, exc)
                continue

            for signal in signals:
                result = await self._evaluate(signal, candles)
                if result is not None:
                    closed.append(result)

        return closed

    async def _evaluate(self, signal: repo.Signal, candles) -> repo.Signal | None:
        """Проверяет один сигнал по свечам, появившимся после его выдачи."""
        created_ms = signal.created_at * 1000
        relevant = [c for c in candles.items if c.ts >= created_ms]

        if relevant:
            await self._update_excursion(signal, relevant)

        # У бинарного опциона нет стопа и цели: он просто доживает до
        # экспирации, и там сравнивается цена с ценой входа.
        if signal.is_binary:
            return await self._evaluate_binary(signal, candles)

        outcome = self._detect_outcome(signal, relevant)
        if outcome is not None:
            status, exit_price = outcome
            closed = await repo.close_signal(signal.id, status, exit_price)
            if closed is not None:
                await self._announce(closed)
            return closed

        # Истечение срока жизни (только биржевые: у опционов свой срок)
        age_min = (time.time() - signal.created_at) / 60
        if age_min >= _cfg_for(signal).get("signal_ttl_min"):
            price = await feed.fetch_price(signal.symbol)
            if price is not None:
                closed = await repo.close_signal(signal.id, repo.EXPIRED, price)
                if closed is not None:
                    await self._announce(closed)
                return closed
        return None

    async def _evaluate_binary(self, signal: repo.Signal, candles) -> repo.Signal | None:
        """Опцион: ждём экспирацию, затем сравниваем цену с ценой входа.

        Выигрыш и проигрыш записываются теми же статусами, что и у биржевых
        сигналов (TP_HIT / SL_HIT) — тогда winrate, серии и разбивки
        считаются по всем сигналам одинаково, без отдельной ветки в отчётах.
        """
        if not signal.expiry_at:
            return None
        now = int(time.time())
        if now < signal.expiry_at:
            return None

        price = self._price_at(candles, signal.expiry_at)
        if price is None:
            price = await feed.fetch_price(signal.symbol)
        if price is None:
            log.warning(
                "Опцион #%d истёк, но цену на момент экспирации получить не удалось",
                signal.id,
            )
            return None

        moved = (price - signal.entry) * (1 if signal.is_long else -1)
        if moved > 0:
            status = repo.TP_HIT
        elif moved < 0:
            status = repo.SL_HIT
        else:
            # Цена вернулась ровно в точку входа: у брокеров это возврат ставки
            status = repo.EXPIRED

        closed = await repo.close_signal(signal.id, status, price)
        if closed is not None:
            await self._announce(closed)
        return closed

    @staticmethod
    def _price_at(candles, moment: int) -> float | None:
        """Цена закрытия свечи, накрывающей указанный момент."""
        target_ms = moment * 1000
        chosen = None
        for candle in candles.items:
            if candle.ts <= target_ms:
                chosen = candle
            else:
                break
        return chosen.close if chosen else None

    def _detect_outcome(self, signal: repo.Signal, candles: list) -> tuple[str, float] | None:
        """Определяет, задета ли цель или стоп.

        Если внутри ОДНОЙ свечи задеты и тейк, и стоп, засчитываем стоп.
        По минутным данным невозможно узнать, что произошло раньше, и честнее
        занизить статистику, чем показать заказчику завышенный winrate,
        который не повторится на реальных сделках.
        """
        for candle in candles:
            if signal.is_long:
                hit_tp = candle.high >= signal.take_profit
                hit_sl = candle.low <= signal.stop_loss
            else:
                hit_tp = candle.low <= signal.take_profit
                hit_sl = candle.high >= signal.stop_loss

            if hit_sl:
                return repo.SL_HIT, signal.stop_loss
            if hit_tp:
                return repo.TP_HIT, signal.take_profit
        return None

    async def _update_excursion(self, signal: repo.Signal, candles: list) -> None:
        """Максимальный ход в плюс (MFE) и в минус (MAE) за жизнь сигнала."""
        if not candles:
            return
        best = max(c.high for c in candles)
        worst = min(c.low for c in candles)

        if signal.is_long:
            mfe = (best - signal.entry) / signal.entry * 100
            mae = (worst - signal.entry) / signal.entry * 100
        else:
            mfe = (signal.entry - worst) / signal.entry * 100
            mae = (signal.entry - best) / signal.entry * 100

        await repo.update_excursion(signal.id, round(mfe, 3), round(min(mae, 0), 3))

    async def _announce(self, signal: repo.Signal) -> None:
        emoji = {
            repo.TP_HIT: "take",
            repo.SL_HIT: "stop",
            repo.EXPIRED: "expired",
        }.get(signal.status, signal.status)
        log.info(
            "Сигнал #%d закрыт: %s, результат %.2f%% (%.2fR)",
            signal.id,
            emoji,
            signal.pnl_pct or 0,
            signal.r_multiple or 0,
        )
        await repo.log_event(
            "outcome",
            f"#{signal.id} {signal.symbol} → {signal.status} ({signal.pnl_pct:+.2f}%)",
            {
                "signal_id": signal.id,
                "status": signal.status,
                "pnl_pct": signal.pnl_pct,
                "r_multiple": signal.r_multiple,
            },
        )
        if self.on_outcome is not None:
            try:
                await self.on_outcome(signal)
            except Exception as exc:
                log.error("Не удалось отправить исход #%d: %s", signal.id, exc)

    def state(self) -> dict:
        return {
            "running": self.running,
            "last_check_at": self.last_check_at,
            "last_error": self.last_error,
        }
