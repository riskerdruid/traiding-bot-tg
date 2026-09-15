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
                InlineKeyboardButton(
                    text="🔔 Уведомления по цене", callback_data="nav:alerts"
                ),
            ],
            [
                InlineKeyboardButton(text="ℹ️ Статус", callback_data="nav:status"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="nav:settings"),
            ],
            [InlineKeyboardButton(text="❓ Помощь", callback_data="nav:help")],
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
            [InlineKeyboardButton(
                text="🎚 Выбрать режим работы", callback_data="nav:presets"
            )],
            [toggle("notify_signals", "Новые сигналы")],
            [toggle("notify_outcomes", "Исходы сигналов")],
            [toggle("daily_report", "Сводка за день")],
            [InlineKeyboardButton(text="‹ Меню", callback_data="nav:menu")],
        ]
    )


def presets_screen(current: str | None) -> InlineKeyboardMarkup:
    """Три режима одной кнопкой каждый.

    Выбор цели человеку по силам, а выставление двух десятков чисел — нет.
    Отметка показывает, какой режим сейчас работает.
    """
    from app.storage.settings_store import PRESETS

    rows = []
    for preset in PRESETS:
        mark = " ✓" if current == preset.key else ""
        rows.append([InlineKeyboardButton(
            text=f"{preset.emoji} {preset.name}{mark}",
            callback_data=f"preset:{preset.key}",
        )])
    rows.append([InlineKeyboardButton(
        text="‹ Настройки", callback_data="nav:settings"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def alerts_screen(alerts: list) -> InlineKeyboardMarkup:
    """Список заказанных уровней: каждый со своей кнопкой снятия."""
    from app.bot.formatters import money, short_symbol

    rows = []
    for alert in alerts[:20]:
        rows.append([InlineKeyboardButton(
            text=f"✖️ {short_symbol(alert.symbol)} · {money(alert.price)}",
            callback_data=f"alert:del:{alert.id}",
        )])
    if len(alerts) > 1:
        rows.append([InlineKeyboardButton(
            text="🧹 Убрать все", callback_data="alert:clear"
        )])
    rows.append([InlineKeyboardButton(text="‹ Меню", callback_data="nav:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def alert_actions(alert_id: int) -> InlineKeyboardMarkup:
    """Кнопки под самим уведомлением."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔔 Мои уведомления", callback_data="nav:alerts"
        ),
    ]])


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


def help_menu() -> InlineKeyboardMarkup:
    """Список тем справки — по две в ряд, чтобы влезли заголовки."""
    from app import help as help_content

    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for topic in help_content.topic_list():
        pair.append(
            InlineKeyboardButton(
                text=f"{topic['icon']} {topic['title']}",
                callback_data=f"help:{topic['id']}",
            )
        )
        if len(pair) == 1:  # заголовки длинные, кладём по одной в ряд
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    webapp = _webapp_button("📱 Открыть приложение")
    if webapp:
        rows.append([webapp])
    rows.append([InlineKeyboardButton(text="‹ Меню", callback_data="nav:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def help_topic() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="‹ Все темы", callback_data="nav:help")],
            [InlineKeyboardButton(text="Меню", callback_data="nav:menu")],
        ]
    )


def persistent_menu() -> ReplyKeyboardMarkup:
    """Нижняя клавиатура — быстрый доступ без прокрутки истории чата."""
    rows = [
        [KeyboardButton(text="🎯 Сигналы"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="📰 Новости"), KeyboardButton(text="ℹ️ Статус")],
        [KeyboardButton(text="❓ Помощь")],
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
