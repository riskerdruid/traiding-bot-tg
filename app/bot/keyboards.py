"""Клавиатуры бота.

Основная навигация — инлайн-кнопки под сообщением: они не занимают место
внизу экрана и позволяют перерисовывать один и тот же экран, а не засорять
чат новыми сообщениями.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from app.config import settings


def _webapp_button(text: str = "📱 Открыть приложение") -> InlineKeyboardButton | None:
    """Кнопка Mini App появляется только при корректном HTTPS-адресе.

    Telegram отклоняет WebApp-кнопки с http:// или пустым URL, поэтому
    при незаполненном WEBAPP_URL просто не показываем её — бот остаётся
    полностью рабочим.
    """
    if not settings.webapp_enabled:
        return None
    return InlineKeyboardButton(
        text=text, web_app=WebAppInfo(url=settings.webapp_url)
    )


def main_menu() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    webapp = _webapp_button()
    if webapp:
        rows.append([webapp])

    rows.extend(
        [
            [
                InlineKeyboardButton(text="🎯 Сигналы", callback_data="nav:signals"),
                InlineKeyboardButton(text="📊 Статистика", callback_data="nav:stats"),
            ],
            [
                InlineKeyboardButton(text="📜 История", callback_data="nav:history"),
                InlineKeyboardButton(text="📰 Новости", callback_data="nav:news"),
            ],
            [
                InlineKeyboardButton(text="ℹ️ Статус", callback_data="nav:status"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="nav:settings"),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_button(to: str = "nav:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="‹ Назад", callback_data=to)]]
    )


def stats_periods(active: str = "all") -> InlineKeyboardMarkup:
    periods = [("day", "Сегодня"), ("week", "Неделя"), ("month", "Месяц"), ("all", "Всё")]
    row = [
        InlineKeyboardButton(
            text=f"• {label} •" if key == active else label,
            callback_data=f"stats:{key}",
        )
        for key, label in periods
    ]
    rows = [row]
    webapp = _webapp_button("📱 Подробный отчёт")
    if webapp:
        rows.append([webapp])
    rows.append([InlineKeyboardButton(text="‹ Меню", callback_data="nav:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def signals_screen(has_active: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_active:
        rows.append(
            [InlineKeyboardButton(text="🔄 Обновить цены", callback_data="nav:signals")]
        )
    webapp = _webapp_button("📱 Открыть в приложении")
    if webapp:
        rows.append([webapp])
    rows.append([InlineKeyboardButton(text="‹ Меню", callback_data="nav:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_screen(prefs: dict) -> InlineKeyboardMarkup:
    def toggle(key: str, label: str) -> InlineKeyboardButton:
        mark = "✅" if prefs.get(key) else "❌"
        return InlineKeyboardButton(
            text=f"{mark} {label}", callback_data=f"toggle:{key}"
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [toggle("notify_signals", "Новые сигналы")],
            [toggle("notify_outcomes", "Исходы сигналов")],
            [toggle("daily_report", "Сводка за день")],
            [InlineKeyboardButton(text="‹ Меню", callback_data="nav:menu")],
        ]
    )


def signal_actions(signal_id: int) -> InlineKeyboardMarkup:
    """Кнопки под карточкой сигнала."""
    rows = [
        [
            InlineKeyboardButton(
                text="📈 Подробнее", callback_data=f"signal:{signal_id}"
            )
        ]
    ]
    webapp = _webapp_button()
    if webapp:
        rows.append([webapp])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def persistent_menu() -> ReplyKeyboardMarkup:
    """Нижняя клавиатура — быстрый доступ без прокрутки истории чата."""
    rows = [
        [KeyboardButton(text="🎯 Сигналы"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="📰 Новости"), KeyboardButton(text="ℹ️ Статус")],
    ]
    if settings.webapp_enabled:
        rows.insert(
            0,
            [
                KeyboardButton(
                    text="📱 Приложение",
                    web_app=WebAppInfo(url=settings.webapp_url),
                )
            ],
        )
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, is_persistent=True
    )
