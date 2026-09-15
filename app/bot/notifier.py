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

    def _is_quiet_now(self) -> bool:
        """Действует ли сейчас режим «не беспокоить»."""
        now_hm = datetime.now(settings.tz).strftime("%H:%M")
        return config.in_quiet_hours(now_hm)

    async def _send(self, text: str, markup=None, respect_quiet: bool = False) -> int:
        """Отправляет всем владельцам. Возвращает число успешных отправок."""
        if respect_quiet and self._is_quiet_now():
            log.info("Режим «не беспокоить» — сообщение не отправлено")
            return 0
        sent = 0
        for chat_id in settings.owner_id_list:
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
        if not config.get("notify_signals"):
            log.info("Сигнал #%d не отправлен: уведомления выключены", signal.id)
            return

        sent = await self._send(
            fmt.signal_card(signal),
            kb.signal_actions(signal.id),
            respect_quiet=True,
        )
        if sent:
            await repo.mark_notified(signal.id)

    async def send_outcome(self, signal: repo.Signal) -> None:
        if not config.get("notify_outcomes"):
            return
        await self._send(fmt.outcome_card(signal), respect_quiet=True)

    async def send_daily_report(self) -> None:
        if not config.get("daily_report"):
            return

        import time

        since = int(time.time()) - 86400
        data = await repo.stats(since=since)
        signals_today = await repo.list_signals(limit=50, since=since)
        if not signals_today and not data["decided"]:
            log.info("Сводка не отправлена: за сутки не было активности")
            return
        await self._send(fmt.daily_report(data, signals_today))

    async def send_startup(self) -> None:
        """Сообщение о том, что бот поднялся — заметно, если он падал."""
        symbols = ", ".join(fmt.short_symbol(s) for s in (config.get("symbols") or []))
        text = (
            "🚀 <b>Бот запущен</b>\n\n"
            f"Инструменты: <b>{symbols}</b>\n"
            f"Таймфрейм: <b>{config.get('timeframe')}</b> "
            f"(тренд по {config.get('htf_timeframe')})\n"
            f"Порог уверенности: <b>{config.get('min_confidence')}%</b>"
        )
        await self._send(text, kb.main_menu())

    async def send_alert(self, message: str) -> None:
        await self._send(f"⚠️ <b>Внимание</b>\n\n{message}")
