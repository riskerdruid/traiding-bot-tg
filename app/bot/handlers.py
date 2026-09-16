"""Обработчики команд и кнопок.

Экранов ровно четыре: сигналы, результаты, настройки, помощь. Всё, что
не помещается в эти четыре, боту не нужно — человек, который открыл его
впервые, не должен выбирать между десятью разделами.
"""

from __future__ import annotations

import contextlib
import logging
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import help as help_content
from app.bot import formatters as fmt
from app.bot import keyboards as kb
from app.config import settings
from app.market import catalog
from app.market.feed import feed
from app.storage import repo
from app.storage.settings_store import ValidationError, config

log = logging.getLogger("bot.handlers")

router = Router(name="main")

# Заполняется при старте из runner.py — ссылки на живые компоненты
runtime: dict = {"scanner": None, "tracker": None, "started_at": time.time()}


class Setup(StatesGroup):
    """Два случая, когда от человека нужен текст, а не нажатие."""

    deposit = State()
    pocket_key = State()


# --------------------------------------------------------------------------
# Доступ
# --------------------------------------------------------------------------


def is_owner(user_id: int | None) -> bool:
    """Бот отвечает только владельцу.

    Сигналы — приватная информация. Если id не в списке, бот вежливо
    отказывает, но не раскрывает, что это за бот.
    """
    if user_id is None:
        return False
    owners = settings.owner_id_list
    return not owners or user_id in owners


@router.message(~F.from_user.id.func(is_owner))
async def deny_message(message: Message) -> None:
    await message.answer(
        "Этот бот приватный и работает только для владельца.\n\n"
        f"<i>Ваш Telegram ID: <code>{message.from_user.id}</code></i>"
    )


@router.callback_query(~F.from_user.id.func(is_owner))
async def deny_callback(call: CallbackQuery) -> None:
    await call.answer("Нет доступа", show_alert=True)


# --------------------------------------------------------------------------
# Экраны
# --------------------------------------------------------------------------


async def screen_signals(uid: int) -> tuple[str, object]:
    active = await repo.get_active_signals(owner_id=uid)
    closed = [
        s
        for s in await repo.list_signals(limit=12, owner_id=uid)
        if s.status != repo.ACTIVE
    ][:3]

    prices: dict[str, float] = {}
    if active:
        with contextlib.suppress(Exception):
            prices = await feed.fetch_prices(sorted({s.symbol for s in active}))

    scanner = runtime.get("scanner")
    state = dict(scanner.state(uid)) if scanner else {"running": False}
    if not state.get("symbols"):
        # Первые секунды после запуска сканер ещё проверяет доступность
        # инструментов. Показать пустой список значило бы напугать зря.
        state["symbols"] = list(config.get("symbols", user_id=uid) or [])
    return fmt.signals_screen(active, closed, prices, state), kb.refresh_button()


async def screen_stats(uid: int) -> tuple[str, object]:
    data = await repo.stats(owner_id=uid)
    by_symbol = await repo.stats_by_symbol(owner_id=uid)
    return fmt.stats_card(data, by_symbol, config.view(uid).risk_money()), None


async def screen_settings(uid: int) -> tuple[str, object]:
    cfg = config.view(uid)
    return fmt.settings_card(cfg), kb.settings_screen(bool(cfg.get("quiet_night")))


async def screen_symbols(uid: int) -> tuple[str, object]:
    cfg = config.view(uid)
    choices = await catalog.available(list(cfg.get("symbols") or []))
    return (
        fmt.symbols_card(choices),
        kb.symbols_screen(choices, feed.pocket_broker.configured),
    )


async def screen_presets(uid: int) -> tuple[str, object]:
    cfg = config.view(uid)
    return fmt.presets_card(cfg.current_preset()), kb.presets_screen(cfg.current_preset())


async def screen_money(uid: int) -> tuple[str, object]:
    cfg = config.view(uid)
    return (
        fmt.money_card(cfg),
        kb.money_screen(
            float(cfg.get("deposit") or 0), float(cfg.get("risk_per_trade") or 0)
        ),
    )


async def screen_pocket(uid: int) -> tuple[str, object]:
    configured = feed.pocket_broker.configured
    return fmt.pocket_card(configured), kb.pocket_screen(configured)


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    cfg = config.view(uid)
    active = await repo.count_signals(status=repo.ACTIVE, owner_id=uid)
    data = await repo.stats(owner_id=uid)

    await message.answer(
        fmt.greeting(
            message.from_user.first_name or "трейдер",
            cfg,
            active,
            data["decided"],
            data["winrate"],
        ),
        reply_markup=kb.main_menu(),
    )


@router.message(Command("signals"))
@router.message(F.text == kb.BTN_SIGNALS)
async def cmd_signals(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = await screen_signals(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("stats"))
@router.message(F.text == kb.BTN_STATS)
async def cmd_stats(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = await screen_stats(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("settings"))
@router.message(F.text == kb.BTN_SETTINGS)
async def cmd_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, markup = await screen_settings(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("help"))
@router.message(F.text == kb.BTN_HELP)
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(help_content.menu_text(), reply_markup=kb.help_menu())


@router.message(Command("test"))
async def cmd_test(message: Message, state: FSMContext) -> None:
    """Проверка доставки: шлёт образец сигнала."""
    await state.clear()
    notifier = runtime.get("notifier")
    if notifier is None:
        await message.answer("Отправка недоступна: бот запущен в урезанном режиме.")
        return

    report = await notifier.send_test(message.from_user.id)
    if report["ok"]:
        return  # образец уже ушёл отдельным сообщением
    await message.answer(
        "😕 Не получилось отправить образец.\n\n"
        f"<i>{report['error']}</i>"
    )


# --------------------------------------------------------------------------
# Переходы между экранами
# --------------------------------------------------------------------------


async def _render(call: CallbackQuery, text: str, markup) -> None:
    """Перерисовывает текущее сообщение, не плодя новые."""
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        # Телеграм ругается, если текст не изменился — это не ошибка
        if "message is not modified" not in str(exc):
            await call.message.answer(text, reply_markup=markup)
    # На часть нажатий обработчик отвечает сам — всплывающей подсказкой.
    # Повторный ответ Telegram отвергает, и это не ошибка.
    with contextlib.suppress(Exception):
        await call.answer()


@router.callback_query(F.data == "nav:signals")
async def nav_signals(call: CallbackQuery) -> None:
    text, markup = await screen_signals(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:settings")
async def nav_settings(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = await screen_settings(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:symbols")
async def nav_symbols(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    text, markup = await screen_symbols(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:presets")
async def nav_presets(call: CallbackQuery) -> None:
    text, markup = await screen_presets(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:money")
async def nav_money(call: CallbackQuery) -> None:
    text, markup = await screen_money(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:pocket")
async def nav_pocket(call: CallbackQuery) -> None:
    text, markup = await screen_pocket(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:help")
async def nav_help(call: CallbackQuery) -> None:
    await _render(call, help_content.menu_text(), kb.help_menu())


@router.callback_query(F.data.startswith("help:"))
async def show_help_topic(call: CallbackQuery) -> None:
    text = help_content.render_topic_text(call.data.split(":", 1)[1])
    if text is None:
        await call.answer("Тема не найдена", show_alert=True)
        return
    await _render(call, text, kb.help_topic())


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------


def _refresh_scanner() -> None:
    """Просит сканер заново проверить, какие инструменты доступны.

    Без этого изменение вступало бы в силу не сразу, а человек, нажав
    кнопку, ждёт результата немедленно.
    """
    scanner = runtime.get("scanner")
    if scanner is not None:
        scanner.invalidate()


@router.callback_query(F.data.startswith("sym:"))
async def toggle_symbol(call: CallbackQuery) -> None:
    symbol = call.data.split(":", 1)[1]
    cfg = config.view(call.from_user.id)
    chosen = list(cfg.get("symbols") or [])

    adding = symbol not in chosen
    if adding:
        chosen.append(symbol)
    else:
        chosen.remove(symbol)

    try:
        await cfg.set("symbols", chosen)
    except ValidationError as exc:
        await call.answer(str(exc), show_alert=True)
        return

    _refresh_scanner()
    await call.answer(
        f"Слежу за {catalog.pretty(symbol)}" if adding
        else f"{catalog.pretty(symbol)} — больше не слежу"
    )
    text, markup = await screen_symbols(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data.startswith("preset:"))
async def choose_preset(call: CallbackQuery) -> None:
    cfg = config.view(call.from_user.id)
    try:
        preset = await cfg.apply_preset(call.data.split(":", 1)[1])
    except ValidationError as exc:
        await call.answer(str(exc), show_alert=True)
        return

    await call.answer(f"{preset.emoji} {preset.name}: {preset.expect}")
    text, markup = await screen_presets(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "night:toggle")
async def toggle_night(call: CallbackQuery) -> None:
    cfg = config.view(call.from_user.id)
    value = not bool(cfg.get("quiet_night"))
    await cfg.set("quiet_night", value)
    await call.answer("Ночью не побеспокою" if value else "Буду писать и ночью")
    text, markup = await screen_settings(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data.startswith("money:dep:"))
async def set_deposit(call: CallbackQuery) -> None:
    value = float(call.data.rsplit(":", 1)[1])
    await config.view(call.from_user.id).set("deposit", value)
    await call.answer(f"Считаю от {fmt.amount(value)} $")
    text, markup = await screen_money(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data.startswith("money:risk:"))
async def set_risk(call: CallbackQuery) -> None:
    value = float(call.data.rsplit(":", 1)[1])
    cfg = config.view(call.from_user.id)
    await cfg.set("risk_per_trade", value)
    await call.answer(f"Риск {value:g}% — это {fmt.amount(cfg.risk_money())} $")
    text, markup = await screen_money(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "money:custom")
async def ask_deposit(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Setup.deposit)
    await _render(
        call,
        "💰 <b>Сколько у вас на счёте?</b>\n\n"
        "Напишите сумму числом — например <code>2500</code>.\n\n"
        "<i>Нужно только для подсказки «сколько брать в сделку». "
        "К вашим деньгам у бота доступа нет.</i>",
        kb.cancel(),
    )


@router.message(Setup.deposit)
async def got_deposit(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(" ", "").replace(",", ".")
    raw = "".join(c for c in raw if c.isdigit() or c == ".")
    try:
        value = float(raw)
    except ValueError:
        await message.answer(
            "Не понял сумму. Напишите одним числом, например <code>2500</code>.",
            reply_markup=kb.cancel(),
        )
        return

    try:
        await config.view(message.from_user.id).set("deposit", value)
    except ValidationError as exc:
        await message.answer(str(exc), reply_markup=kb.cancel())
        return

    await state.clear()
    text, markup = await screen_money(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "pocket:set")
async def ask_pocket_key(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Setup.pocket_key)
    await _render(
        call,
        "🔑 <b>Пришлите ключ</b>\n\n"
        "Отправьте следующим сообщением строку, которая начинается "
        "с <code>42[&quot;auth&quot;</code> — целиком, вместе со скобками.\n\n"
        "<i>Это ключ сессии брокера. Пароль от счёта присылать не нужно "
        "и нельзя.</i>",
        kb.cancel(),
    )


@router.message(Setup.pocket_key)
async def got_pocket_key(message: Message, state: FSMContext) -> None:
    key = (message.text or "").strip()
    # Ключ — секрет, поэтому убираем сообщение сразу, ещё до проверки:
    # даже негодная строка может содержать кусок сессии
    with contextlib.suppress(Exception):
        await message.delete()
    try:
        # Ключ общий для всей установки: брокер один и подключение к нему одно,
        # поэтому пишем его в общий слой, а не в личный
        await config.set("po_ssid", key)
    except ValidationError as exc:
        await message.answer(
            f"😕 Этот ключ не подходит.\n\n<i>{exc}</i>\n\nПопробуйте ещё раз.",
            reply_markup=kb.cancel(),
        )
        return

    feed.apply_settings(pocket_ssid=key)
    _refresh_scanner()

    await state.clear()
    await message.answer(
        "✅ <b>Ключ принят</b>\n\n"
        "Сообщение с ключом я удалил, чтобы оно не лежало в переписке.\n"
        "Пары брокера появятся в списке «За чем следить» через минуту."
    )
    text, markup = await screen_symbols(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "pocket:clear")
async def clear_pocket_key(call: CallbackQuery) -> None:
    await config.set("po_ssid", "")
    feed.apply_settings(pocket_ssid="")
    _refresh_scanner()
    await call.answer("Отключил")
    text, markup = await screen_pocket(call.from_user.id)
    await _render(call, text, markup)


# --------------------------------------------------------------------------
# Всё остальное
# --------------------------------------------------------------------------


@router.message()
async def fallback(message: Message) -> None:
    """Любое другое сообщение. Не ругаемся, а показываем, куда нажимать."""
    await message.answer(
        "Я понимаю только кнопки внизу экрана 👇\n\n"
        "🎯 <b>Сигналы</b> — что сейчас в работе\n"
        "📊 <b>Результаты</b> — угадываю я или нет\n"
        "⚙️ <b>Настройки</b> — за чем следить и как часто писать\n"
        "❓ <b>Помощь</b> — если что-то непонятно",
        reply_markup=kb.main_menu(),
    )
