"""Клавиатуры бота.

Внизу экрана — четыре постоянные кнопки: из любого места видно, куда
нажимать. Внутри разделов — инлайн-кнопки, они перерисовывают то же
сообщение, а не засыпают чат новыми.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.storage.settings_store import PRESETS

# Подписи постоянных кнопок. Обработчики сверяются именно с ними.
BTN_SIGNALS = "🎯 Сигналы"
BTN_STATS = "📊 Результаты"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "❓ Помощь"

# Суммы, которые предлагаются вместо ввода числа руками
DEPOSITS = (100, 500, 1000, 5000, 10000)
RISKS = (0.5, 1.0, 2.0)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SIGNALS), KeyboardButton(text=BTN_STATS)],
            [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def refresh_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="nav:signals")]
        ]
    )


def settings_screen(night_on: bool) -> InlineKeyboardMarkup:
    night = "🌙 Ночью не беспокоить: вкл" if night_on else "🌙 Ночью не беспокоить: выкл"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📈 За чем следить", callback_data="nav:symbols")],
            [InlineKeyboardButton(
                text="🎚 Как часто сигналы", callback_data="nav:presets"
            )],
            [InlineKeyboardButton(text="💰 Деньги", callback_data="nav:money")],
            [InlineKeyboardButton(text=night, callback_data="night:toggle")],
        ]
    )


def symbols_screen(choices: list, pocket_configured: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if c.chosen else '▫️'} {c.title}",
                callback_data=f"sym:{c.symbol}",
            )
        ]
        for c in choices
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "🔑 Ключ Pocket Option"
                    if pocket_configured
                    else "🔑 Подключить опционы Pocket Option"
                ),
                callback_data="nav:pocket",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="‹ Настройки", callback_data="nav:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def presets_screen(current: str | None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{p.emoji} {p.name}" + (" ✓" if current == p.key else ""),
                callback_data=f"preset:{p.key}",
            )
        ]
        for p in PRESETS
    ]
    rows.append([InlineKeyboardButton(text="‹ Настройки", callback_data="nav:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def money_screen(deposit: float, risk: float) -> InlineKeyboardMarkup:
    def mark(value: float, current: float, text: str) -> str:
        return f"• {text} •" if abs(value - current) < 1e-9 else text

    amounts = [
        InlineKeyboardButton(
            text=mark(value, deposit, f"{value:,}".replace(",", " ")),
            callback_data=f"money:dep:{value}",
        )
        for value in DEPOSITS
    ]
    risks = [
        InlineKeyboardButton(
            text=mark(value, risk, f"{value:g}%"),
            callback_data=f"money:risk:{value}",
        )
        for value in RISKS
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            amounts[:3],
            amounts[3:],
            [InlineKeyboardButton(
                text="✏️ Другая сумма", callback_data="money:custom"
            )],
            risks,
            [InlineKeyboardButton(text="‹ Настройки", callback_data="nav:settings")],
        ]
    )


def pocket_screen(configured: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text="🔑 Прислать ключ" if not configured else "🔑 Прислать новый ключ",
            callback_data="pocket:set",
        )]
    ]
    if configured:
        rows.append(
            [InlineKeyboardButton(text="🚫 Отключить", callback_data="pocket:clear")]
        )
    rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="nav:symbols")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def help_menu() -> InlineKeyboardMarkup:
    from app import help as help_content

    rows = [
        [InlineKeyboardButton(
            text=f"{topic['icon']} {topic['title']}",
            callback_data=f"help:{topic['id']}",
        )]
        for topic in help_content.topic_list()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def help_topic() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="‹ Другие вопросы", callback_data="nav:help")]
        ]
    )


def cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="nav:settings")]
        ]
    )
