"""Создание экземпляра бота и диспетчера."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    MenuButtonCommands,
    MenuButtonWebApp,
    WebAppInfo,
)

from app.bot.handlers import router
from app.config import settings

log = logging.getLogger("bot.instance")

COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="signals", description="Активные сигналы"),
    BotCommand(command="stats", description="Статистика и winrate"),
    BotCommand(command="history", description="Завершённые сигналы"),
    BotCommand(command="news", description="Экономический календарь"),
    BotCommand(command="status", description="Состояние бота"),
    BotCommand(command="settings", description="Уведомления"),
    BotCommand(command="help", description="Справка"),
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
    """Меню команд и кнопка Mini App рядом с полем ввода."""
    try:
        await bot.set_my_commands(COMMANDS)
    except Exception as exc:
        log.warning("Не удалось установить список команд: %s", exc)

    # Кнопка меню хранится на стороне Telegram и переживает перезапуск бота.
    # Поэтому при пустом WEBAPP_URL её нужно ЯВНО снять: иначе останется
    # ссылка от прошлого запуска, и заказчик получит ошибку вместо приложения.
    if settings.webapp_enabled:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Приложение",
                    web_app=WebAppInfo(url=settings.webapp_url),
                )
            )
            log.info("Кнопка Mini App установлена: %s", settings.webapp_url)
        except Exception as exc:
            log.warning("Не удалось установить кнопку Mini App: %s", exc)
    else:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            log.info(
                "WEBAPP_URL не задан — кнопка Mini App снята, "
                "бот работает через сообщения"
            )
        except Exception as exc:
            log.warning("Не удалось снять кнопку Mini App: %s", exc)
