"""Уведомления по цене — будильник, который человек ставит сам.

Сигналы бот придумывает сам: он решает, когда момент подходящий. Здесь
наоборот — уровень называет человек. «Сообщи, когда биткоин будет стоить
95 000» — и всё, никакой стратегии, никаких индикаторов.

Отдельный цикл нужен потому, что уведомление может стоять на инструменте,
за которым сканер вообще не следит: человек может заказать уровень по
чему угодно, не добавляя это себе в постоянный список.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from app.market.feed import feed
from app.storage import repo

log = logging.getLogger("engine.alerts")

AlertCallback = Callable[[repo.Alert, float], Awaitable[None]]

# Цену для будильника достаточно смотреть раз в полминуты: человек
# заказывает уровень, а не тик
CHECK_INTERVAL = 30


class AlertWatcher:
    def __init__(self, on_hit: AlertCallback | None = None) -> None:
        self.on_hit = on_hit
        self.running = False
        self.last_check_at: int | None = None
        self.last_error: str | None = None
        self.watching: int = 0
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="alerts")
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
                log.exception("Ошибка проверки уведомлений: %s", exc)
            await asyncio.sleep(CHECK_INTERVAL)

    async def check_once(self) -> list[repo.Alert]:
        """Один проход по всем заказанным уровням."""
        alerts = await repo.list_alerts(status=repo.ALERT_ACTIVE, limit=500)
        self.watching = len(alerts)
        self.last_check_at = int(time.time())
        if not alerts:
            return []

        # Цены тянем по одной на инструмент, даже если уровней на нём
        # заказано несколько
        symbols = sorted({a.symbol for a in alerts})
        try:
            prices = await feed.fetch_prices(symbols)
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("Не удалось получить цены для уведомлений: %s", exc)
            return []

        fired: list[repo.Alert] = []
        for alert in alerts:
            price = prices.get(alert.symbol)
            if price is None or not alert.reached(price):
                continue

            updated = await repo.trigger_alert(alert.id, price)
            if updated is None:
                continue
            fired.append(updated)
            log.info(
                "Уведомление #%d: %s дошёл до %s (заказано %s)",
                alert.id, alert.symbol, price, alert.price,
            )
            if self.on_hit is not None:
                try:
                    await self.on_hit(updated, price)
                except Exception as exc:
                    log.error("Не удалось отправить уведомление: %s", exc)

        return fired

    def state(self) -> dict:
        return {
            "running": self.running,
            "watching": self.watching,
            "last_check_at": self.last_check_at,
            "last_error": self.last_error,
        }
