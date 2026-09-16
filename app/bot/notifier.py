"""Отправка сигналов и исходов владельцу."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from app.bot import formatters as fmt
from app.bot import keyboards as kb
from app.config import settings
from app.storage import repo
from app.storage.settings_store import config

log = logging.getLogger("bot.notifier")


class Notifier:
    """Рассылает сообщения получателям с учётом их настроек."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def _send(
        self,
        text: str,
        to_owner: int | None = None,
        respect_quiet: bool = False,
        markup=None,
    ) -> int:
        """Отправляет сообщение. Возвращает, скольким дошло.

        `to_owner` — адресовать одному получателю. Сигнал принадлежит тому,
        по чьим настройкам он найден, и другим он не нужен: у них другие
        инструменты и пороги.
        """
        targets = [to_owner] if to_owner else list(settings.owner_id_list)
        sent = 0
        for chat_id in targets:
            if respect_quiet and self._is_quiet_now(chat_id):
                log.info("Получатель %s: ночной режим, сообщение придержано", chat_id)
                continue
            if await self._deliver(chat_id, text, markup):
                sent += 1
        return sent

    def _is_quiet_now(self, owner_id: int | None = None) -> bool:
        now_hm = datetime.now(settings.tz).strftime("%H:%M")
        return config.in_quiet_hours(now_hm, user_id=owner_id)

    async def _deliver(self, chat_id: int, text: str, markup=None) -> bool:
        try:
            await self.bot.send_message(chat_id, text, reply_markup=markup)
            return True
        except TelegramRetryAfter as exc:
            # Телеграм просит подождать — ждём и пробуем ещё раз
            log.warning("Лимит Telegram, пауза %s с", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
            try:
                await self.bot.send_message(chat_id, text, reply_markup=markup)
                return True
            except Exception as retry_exc:
                log.error("Повтор не удался для %s: %s", chat_id, retry_exc)
        except TelegramForbiddenError:
            log.error(
                "Пользователь %s заблокировал бота — сообщение не доставлено.", chat_id
            )
        except TelegramBadRequest as exc:
            # Самый частый случай на первом запуске: владелец ещё не открывал
            # чат с ботом, поэтому Telegram не знает, куда слать
            if "chat not found" in str(exc).lower():
                log.warning(
                    "Чат с %s ещё не создан. Откройте бота в Telegram и отправьте "
                    "/start — после этого сигналы будут приходить.",
                    chat_id,
                )
            else:
                log.error("Не удалось отправить сообщение %s: %s", chat_id, exc)
        except Exception as exc:
            log.error("Не удалось отправить сообщение %s: %s", chat_id, exc)
        return False

    # ----------------------------------------------------------------

    async def send_signal(self, signal: repo.Signal) -> None:
        sent = await self._send(
            fmt.signal_card(signal), to_owner=signal.owner_id, respect_quiet=True
        )
        if sent:
            await repo.mark_notified(signal.id)

    async def send_outcome(self, signal: repo.Signal) -> None:
        await self._send(
            fmt.outcome_card(signal), to_owner=signal.owner_id, respect_quiet=True
        )

    async def send_daily_report(self) -> None:
        """Сводка у каждого своя — по его сигналам.

        Если за день ничего не было, сводка не приходит вовсе: пустое
        сообщение каждый вечер быстро превращается в шум, который
        перестают открывать.
        """
        since = int(time.time()) - 86400
        for owner_id in settings.owner_id_list:
            data = await repo.stats(since=since, owner_id=owner_id)
            signals_today = await repo.list_signals(
                limit=50, since=since, owner_id=owner_id
            )
            if not signals_today and not data["decided"]:
                continue
            await self._send(fmt.daily_report(data, signals_today), to_owner=owner_id)

    async def send_startup(self) -> None:
        """Короткое «я на месте» после перезапуска."""
        for owner_id in settings.owner_id_list:
            cfg = config.view(owner_id)
            await self._send(
                "🚀 <b>Бот на связи и следит за рынком</b>\n\n"
                f"Слежу за: <b>{fmt.symbols_line(cfg.get('symbols'))}</b>",
                to_owner=owner_id,
                markup=kb.main_menu(),
            )

    async def send_test(self, owner_id: int) -> dict:
        """Шлёт образец сигнала. Возвращает, получилось ли."""
        from app.market.feed import feed

        cfg = config.view(owner_id)
        symbols = list(cfg.get("symbols") or [])
        if not symbols:
            return {
                "ok": False,
                "error": "Не выбран ни один инструмент: «Настройки» → «За чем следить».",
            }

        symbol = symbols[0]
        price = await feed.fetch_price(symbol)
        if price is None:
            return {
                "ok": False,
                "error": (
                    f"Не удалось узнать цену {fmt.short_symbol(symbol)} — "
                    "похоже, нет связи с биржей. Попробуйте позже."
                ),
            }

        text = fmt.test_signal_card(symbol, price, feed.is_binary(symbol), cfg)
        ok = await self._deliver(owner_id, text)
        return {"ok": ok, "error": "" if ok else "Telegram не принял сообщение."}

    async def send_price_alert(self, alert: repo.Alert, price: float) -> None:
        """Цена дошла до уровня, который человек заказал сам.

        Ночной режим здесь не действует: человек назвал уровень и ждёт
        сообщения именно в этот момент. Не хочет ночных — не заказывает
        ночной уровень.
        """
        await self._send(
            fmt.alert_fired(alert, price),
            to_owner=alert.owner_id,
            markup=kb.alert_done(),
        )

    async def send_alert(self, message: str) -> None:
        """Техническая тревога — всем владельцам, без оглядки на ночь."""
        await self._send(f"⚠️ <b>Внимание</b>\n\n{message}")
