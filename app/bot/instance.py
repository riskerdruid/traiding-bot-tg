"""Создание экземпляра бота и диспетчера."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonCommands

from app.bot.handlers import router
from app.config import settings

log = logging.getLogger("bot.instance")

COMMANDS = [
    BotCommand(command="start", description="Начать сначала"),
    BotCommand(command="signals", description="Что сейчас в работе"),
    BotCommand(command="stats", description="Угадываю я или нет"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="test", description="Проверить, что сообщения доходят"),
]


def create_bot() -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def setup_bot_profile(bot: Bot) -> None:
    """Список команд рядом с полем ввода."""
    try:
        await bot.set_my_commands(COMMANDS)
    except Exception as exc:
        log.warning("Не удалось установить список команд: %s", exc)

    # Кнопка меню хранится на стороне Telegram и переживает перезапуск.
    # Ставим её явно: иначе у установок, где раньше было мини-приложение,
    # осталась бы ссылка в никуда.
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as exc:
        log.warning("Не удалось настроить кнопку меню: %s", exc)
