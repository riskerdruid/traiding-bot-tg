"""Отправка сигналов и исходов владельцу."""

from __future__ import annotations

import asyncio
import logging
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
    """Рассылает сообщения владельцам с учётом их настроек."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    def _is_quiet_now(self, owner_id: int | None = None) -> bool:
        """Действует ли сейчас режим «не беспокоить» у этого получателя."""
        now_hm = datetime.now(settings.tz).strftime("%H:%M")
        return config.in_quiet_hours(now_hm, user_id=owner_id)

    async def _send(
        self,
        text: str,
        markup=None,
        respect_quiet: bool = False,
        to_owner: int | None = None,
    ) -> int:
        """Отправляет сообщение.

        `to_owner` — адресовать одному получателю. Сигнал принадлежит тому,
        по чьим настройкам он найден, и другим он не нужен: у них другие
        инструменты и пороги.
        """
        targets = [to_owner] if to_owner else list(settings.owner_id_list)
        sent = 0
        for chat_id in targets:
            if respect_quiet and self._is_quiet_now(chat_id):
                log.info("Получатель %s: режим «не беспокоить»", chat_id)
                continue
            try:
                await self.bot.send_message(
                    chat_id, text, reply_markup=markup, disable_web_page_preview=True
                )
                sent += 1
            except TelegramRetryAfter as exc:
                # Телеграм просит подождать — ждём и пробуем ещё раз
                log.warning("Лимит Telegram, пауза %s с", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
                try:
                    await self.bot.send_message(chat_id, text, reply_markup=markup)
                    sent += 1
                except Exception as retry_exc:
                    log.error("Повтор не удался для %s: %s", chat_id, retry_exc)
            except TelegramForbiddenError:
                log.error(
                    "Пользователь %s заблокировал бота — сообщение не доставлено. "
                    "Разблокируйте бота в Telegram.",
                    chat_id,
                )
            except TelegramBadRequest as exc:
                # Самый частый случай на первом запуске: владелец ещё не
                # открывал чат с ботом, поэтому Telegram не знает, куда слать
                if "chat not found" in str(exc).lower():
                    log.warning(
                        "Чат с %s ещё не создан. Откройте бота в Telegram "
                        "и отправьте /start — после этого сигналы будут приходить.",
                        chat_id,
                    )
                else:
                    log.error("Не удалось отправить сообщение %s: %s", chat_id, exc)
            except Exception as exc:
                log.error("Не удалось отправить сообщение %s: %s", chat_id, exc)
        return sent

    # ----------------------------------------------------------------

    async def send_signal(self, signal: repo.Signal) -> None:
        owner = signal.owner_id
        if not config.get("notify_signals", user_id=owner):
            log.info("Сигнал #%d не отправлен: уведомления выключены", signal.id)
            return

        sent = await self._send(
            fmt.signal_card(signal),
            kb.signal_actions(signal.id),
            respect_quiet=True,
            to_owner=owner,
        )
        if sent:
            await repo.mark_notified(signal.id)

    async def send_outcome(self, signal: repo.Signal) -> None:
        owner = signal.owner_id
        if not config.get("notify_outcomes", user_id=owner):
            return
        await self._send(
            fmt.outcome_card(signal), respect_quiet=True, to_owner=owner
        )

    async def send_daily_report(self) -> None:
        """Сводка у каждого своя — по его сигналам."""
        import time

        since = int(time.time()) - 86400
        for owner_id in settings.owner_id_list:
            if not config.get("daily_report", user_id=owner_id):
                continue
            data = await repo.stats(since=since, owner_id=owner_id)
            signals_today = await repo.list_signals(
                limit=50, since=since, owner_id=owner_id
            )
            if not signals_today and not data["decided"]:
                continue
            await self._send(
                fmt.daily_report(data, signals_today), to_owner=owner_id
            )

    async def send_test(self) -> dict:
        """Отправляет всем получателям образец сигнала.

        Возвращает отчёт: кому дошло, кому нет и почему. Нужен для ответа
        на команду — иначе непонятно, сработало ли.
        """
        from app.market.feed import feed

        report = {"symbol": "", "price": 0.0, "sent": [], "failed": []}

        for chat_id in settings.owner_id_list:
            cfg = config.view(chat_id)
            symbols = list(cfg.get("symbols") or [])
            symbol = symbols[0] if symbols else "XAU/USDT:USDT"
            price = await feed.fetch_price(symbol)
            if price is None:
                price = 1.0
            if not report["symbol"]:
                report["symbol"], report["price"] = symbol, price

            text = fmt.test_signal_card(
                symbol, price, feed.is_binary(symbol), cfg=cfg
            )
            try:
                await self.bot.send_message(
                    chat_id, text, disable_web_page_preview=True
                )
                report["sent"].append(chat_id)
            except TelegramBadRequest as exc:
                reason = (
                    "не нажимал /start"
                    if "chat not found" in str(exc).lower()
                    else str(exc)[:60]
                )
                report["failed"].append((chat_id, reason))
            except TelegramForbiddenError:
                report["failed"].append((chat_id, "заблокировал бота"))
            except Exception as exc:
                report["failed"].append((chat_id, str(exc)[:60]))
        return report

    async def send_startup(self) -> None:
        """Сообщение о запуске — каждому со своими настройками."""
        for owner_id in settings.owner_id_list:
            cfg = config.view(owner_id)
            symbols = ", ".join(
                fmt.short_symbol(s) for s in (cfg.get("symbols") or [])
            )
            text = (
                "🚀 <b>Бот запущен и следит за рынком</b>\n\n"
                f"Слежу за: <b>{symbols or 'ничего не выбрано'}</b>\n"
                f"Свечи по <b>{cfg.get('timeframe')}</b>, "
                f"общая картина по <b>{cfg.get('htf_timeframe')}</b>"
            )
            await self._send(text, kb.main_menu(), to_owner=owner_id)

    async def send_alert(self, message: str) -> None:
        await self._send(f"⚠️ <b>Внимание</b>\n\n{message}")

    async def send_price_alert(self, alert: repo.Alert, price: float) -> None:
        """Цена дошла до уровня, который человек заказал сам.

        Режим «не беспокоить» здесь не действует: человек назвал уровень
        и ждёт сообщения именно в этот момент. Не хочет ночных звонков —
        не заказывает ночной уровень.
        """
        snapshot = await fmt.market_snapshot(alert.symbol)
        await self._send(
            fmt.alert_card(alert, price, snapshot),
            kb.alert_actions(alert.id),
            to_owner=alert.owner_id,
        )
