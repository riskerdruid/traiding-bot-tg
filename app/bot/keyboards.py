"""Клавиатуры бота.

Внизу экрана — постоянные кнопки: из любого места видно, куда нажимать.
Внутри разделов — инлайн-кнопки, они перерисовывают то же сообщение,
а не засыпают чат новыми.
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
from app.storage.settings_store import PRESETS

# Подписи постоянных кнопок. Обработчики сверяются именно с ними.
BTN_SIGNALS = "🎯 Сигналы"
BTN_STATS = "📊 Результаты"
BTN_ALERTS = "🔔 Уведомления"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "❓ Помощь"
BTN_APP = "📱 Приложение"

# Суммы и проценты, которые предлагаются вместо ввода числа руками
DEPOSITS = (100, 500, 1000, 5000, 10000)
RISKS = (0.5, 1.0, 2.0)
QUICK_MOVES = (-1, -3, -5, 1, 3, 5)


def _webapp() -> WebAppInfo | None:
    """Telegram отклоняет кнопки приложения с пустым или http-адресом.

    При незаполненном WEBAPP_URL просто не показываем их — бот остаётся
    полностью рабочим, всё то же самое есть в сообщениях.
    """
    if not settings.webapp_enabled:
        return None
    return WebAppInfo(url=settings.webapp_url)


def open_app_button(text: str = "📱 Открыть приложение") -> InlineKeyboardButton | None:
    app = _webapp()
    return InlineKeyboardButton(text=text, web_app=app) if app else None


def main_menu() -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    app = _webapp()
    if app:
        rows.append([KeyboardButton(text=BTN_APP, web_app=app)])
    rows += [
        [KeyboardButton(text=BTN_SIGNALS), KeyboardButton(text=BTN_STATS)],
        [KeyboardButton(text=BTN_ALERTS), KeyboardButton(text=BTN_SETTINGS)],
        [KeyboardButton(text=BTN_HELP)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def signals_screen() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🔄 Обновить", callback_data="nav:signals")]]
    app = open_app_button("📱 Посмотреть в приложении")
    if app:
        rows.append([app])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def stats_screen() -> InlineKeyboardMarkup | None:
    app = open_app_button("📱 Подробнее в приложении")
    return InlineKeyboardMarkup(inline_keyboard=[[app]]) if app else None


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


# --------------------------------------------------------------------------
# Уведомления по цене
# --------------------------------------------------------------------------


def alerts_screen(alerts: list) -> InlineKeyboardMarkup:
    """Список заказанных уровней: каждый со своей кнопкой снятия."""
    from app.bot.formatters import money, short_symbol

    rows = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data="alert:add")]
    ]
    for alert in alerts[:20]:
        rows.append([InlineKeyboardButton(
            text=f"✖️ {short_symbol(alert.symbol)} · {money(alert.price)}",
            callback_data=f"alert:del:{alert.id}",
        )])
    if len(alerts) > 1:
        rows.append([InlineKeyboardButton(
            text="🧹 Убрать все", callback_data="alert:clear"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def alert_symbols(choices: list) -> InlineKeyboardMarkup:
    """Первый шаг мастера: по какому инструменту ставим будильник."""
    rows = [
        [InlineKeyboardButton(text=c.title, callback_data=f"alert:sym:{c.symbol}")]
        for c in choices[:12]
    ]
    rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="nav:alerts")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def alert_values(symbol: str) -> InlineKeyboardMarkup:
    """Второй шаг: готовые проценты движения — одно нажатие вместо ввода."""
    def button(value: int) -> InlineKeyboardButton:
        sign = "+" if value > 0 else "−"
        return InlineKeyboardButton(
            text=f"{sign}{abs(value)}%", callback_data=f"alert:pct:{value}"
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button(v) for v in QUICK_MOVES if v < 0],
            [button(v) for v in QUICK_MOVES if v > 0],
            [InlineKeyboardButton(
                text="✏️ Своя цена", callback_data="alert:custom"
            )],
            [InlineKeyboardButton(text="‹ Назад", callback_data="alert:add")],
        ]
    )


def alert_done() -> InlineKeyboardMarkup:
    """Кнопка под подтверждением и под сработавшим уведомлением."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🔔 Мои уведомления", callback_data="nav:alerts"
            )]
        ]
    )


# --------------------------------------------------------------------------
# Справка
# --------------------------------------------------------------------------


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


def cancel(to: str = "nav:settings") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=to)]]
    )
