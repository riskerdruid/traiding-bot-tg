"""Обработчики команд и кнопок Telegram-бота."""

from __future__ import annotations

import logging
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot import formatters as fmt
from app.bot import keyboards as kb
from app.config import settings
from app.market.feed import feed
from app.news.calendar import calendar
from app.storage import repo
from app import help as help_content
from app.storage.settings_store import config

log = logging.getLogger("bot.handlers")

router = Router(name="main")

# Заполняется при старте из runner.py — ссылки на живые компоненты
runtime: dict = {"scanner": None, "tracker": None, "started_at": time.time()}

PERIODS = {
    "day": ("Сегодня", 86400),
    "week": ("Неделя", 86400 * 7),
    "month": ("Месяц", 86400 * 30),
    "all": ("За всё время", None),
}

# Быстрые переключатели, доступные прямо в боте.
# Полный набор параметров редактируется в Mini App.
QUICK_TOGGLES = ("notify_signals", "notify_outcomes", "daily_report")


# --------------------------------------------------------------------------
# Доступ
# --------------------------------------------------------------------------


def is_owner(user_id: int | None) -> bool:
    """Бот отвечает только владельцу.

    Сигналы — приватная информация заказчика. Если id не в списке,
    бот вежливо отказывает, но не раскрывает, что это за бот.
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
# Настройки пользователя
# --------------------------------------------------------------------------


async def get_prefs() -> dict:
    """Текущие значения быстрых переключателей."""
    await config.load()
    return {key: bool(config.get(key)) for key in QUICK_TOGGLES}


# --------------------------------------------------------------------------
# Экраны
# --------------------------------------------------------------------------


async def screen_signals() -> tuple[str, object]:
    active = await repo.get_active_signals()
    prices: dict[str, float] = {}
    if active:
        symbols = list({s.symbol for s in active})
        prices = await feed.fetch_prices(symbols)
    return fmt.active_list(active, prices), kb.signals_screen(bool(active))


async def screen_history() -> tuple[str, object]:
    signals = await repo.list_signals(limit=10)
    closed = [s for s in signals if s.status != repo.ACTIVE]
    return fmt.history_list(closed, "Последние завершённые"), kb.back_button()


async def screen_stats(period: str = "all") -> tuple[str, object]:
    label, window = PERIODS.get(period, PERIODS["all"])
    since = int(time.time() - window) if window else None
    data = await repo.stats(since=since)
    by_symbol = await repo.stats_by_symbol(since=since)
    return fmt.stats_card(data, label, by_symbol), kb.stats_periods(period)


async def screen_news() -> tuple[str, object]:
    await calendar.refresh()
    events = calendar.upcoming(limit=6)
    return fmt.news_card(events, calendar.mute_reason()), kb.back_button()


async def screen_status() -> tuple[str, object]:
    scanner = runtime.get("scanner")
    tracker = runtime.get("tracker")
    scanner_state = scanner.state() if scanner else {"running": False, "scans_done": 0}
    tracker_state = tracker.state() if tracker else {}
    health = await feed.health()
    prices = await feed.fetch_prices(config.get("symbols") or [])
    uptime = time.time() - runtime.get("started_at", time.time())
    return (
        fmt.status_card(scanner_state, tracker_state, health, prices, uptime),
        kb.back_button(),
    )


async def screen_settings() -> tuple[str, object]:
    prefs = await get_prefs()
    return fmt.settings_card(config.all()), kb.settings_screen(prefs)


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    name = message.from_user.first_name or "трейдер"
    active = await repo.count_signals(status=repo.ACTIVE)
    data = await repo.stats()

    symbols = ", ".join(
        fmt.short_symbol(s) for s in (config.get("symbols") or [])
    )

    greeting = [
        f"👋 Привет, <b>{name}</b>!",
        "",
        "Я круглосуточно слежу за графиком и пишу вам, когда вижу "
        "подходящий момент для сделки. В каждом сообщении будет:",
        "",
        "• <b>какой актив</b> и <b>в какую сторону</b> ждать движение",
        "• <b>где выходить</b> — при удаче и при неудаче",
        "• <b>почему</b> — что именно совпало",
        "",
        "Я <b>не торгую</b> за вас: доступа к вашим деньгам у меня нет, "
        "решение всегда остаётся за вами.",
        "",
        "━━━━━━━━━━━━━━━",
        "",
        f"📍 Сейчас слежу за: <b>{symbols or 'ничего не выбрано'}</b>",
        f"⏱ Свечи по <b>{config.get('timeframe')}</b>, "
        f"общая картина по <b>{config.get('htf_timeframe')}</b>",
    ]

    if data["decided"]:
        verdict = "неплохо" if data["winrate"] >= 50 else "пока слабо"
        greeting.append(
            f"📊 Из {data['decided']} завершённых сигналов удачных "
            f"<b>{data['winrate']}%</b> — {verdict}"
        )
    else:
        greeting.append(
            "📊 Статистики пока нет — она появится, когда первые сигналы "
            "дойдут до цели или стопа"
        )

    if active:
        greeting.append(f"🎯 Прямо сейчас в работе: <b>{active}</b>")

    greeting += [
        "",
        "━━━━━━━━━━━━━━━",
        "",
        "<b>Если торгуете впервые</b> — начните с кнопки «❓ Помощь». "
        "Там всё объяснено простым языком: что такое сигнал, что означает "
        "каждая строчка в сообщении и сколько вообще стоит рисковать.",
    ]

    if not settings.webapp_enabled:
        greeting.append("")
        greeting.append(
            "<i>Приложение выключено: чтобы включить, укажите "
            "WEBAPP_URL в файле .env</i>"
        )

    await message.answer(
        "\n".join(greeting), reply_markup=kb.persistent_menu()
    )
    await message.answer(
        "Чем займёмся?", reply_markup=kb.main_menu()
    )


@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def cmd_help(message: Message) -> None:
    await message.answer(fmt.help_menu(), reply_markup=kb.help_menu())


@router.callback_query(F.data == "nav:help")
async def nav_help(call: CallbackQuery) -> None:
    await _render(call, fmt.help_menu(), kb.help_menu())


@router.callback_query(F.data.startswith("help:"))
async def show_help_topic(call: CallbackQuery) -> None:
    topic_id = call.data.split(":", 1)[1]
    text = help_content.render_topic_text(topic_id)
    if text is None:
        await call.answer("Тема не найдена", show_alert=True)
        return
    await _render(call, text, kb.help_topic())


@router.message(Command("signals"))
@router.message(F.text == "🎯 Сигналы")
async def cmd_signals(message: Message) -> None:
    text, markup = await screen_signals()
    await message.answer(text, reply_markup=markup)


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    text, markup = await screen_history()
    await message.answer(text, reply_markup=markup)


@router.message(Command("stats"))
@router.message(F.text == "📊 Статистика")
async def cmd_stats(message: Message) -> None:
    text, markup = await screen_stats("all")
    await message.answer(text, reply_markup=markup)


@router.message(Command("news"))
@router.message(F.text == "📰 Новости")
async def cmd_news(message: Message) -> None:
    text, markup = await screen_news()
    await message.answer(text, reply_markup=markup)


@router.message(Command("status"))
@router.message(F.text == "ℹ️ Статус")
async def cmd_status(message: Message) -> None:
    text, markup = await screen_status()
    await message.answer(text, reply_markup=markup)


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    text, markup = await screen_settings()
    await message.answer(text, reply_markup=markup)


@router.message(Command("test"))
async def cmd_test(message: Message) -> None:
    """Проверка доставки: шлёт образец сигнала всем получателям."""
    notifier = runtime.get("notifier")
    if notifier is None:
        await message.answer(
            "Отправка недоступна: бот запущен в урезанном режиме."
        )
        return

    await message.answer("🧪 Отправляю тестовый сигнал…")
    report = await notifier.send_test()

    lines = [
        "<b>Результат проверки</b>",
        "",
        f"Инструмент: <b>{fmt.short_symbol(report['symbol'])}</b>",
        f"Цена сейчас: <b>{fmt.money(report['price'])}</b>",
        "",
    ]
    if report["sent"]:
        lines.append(f"✅ Доставлено: <b>{len(report['sent'])}</b>")
        for chat_id in report["sent"]:
            lines.append(f"   • <code>{chat_id}</code>")
    if report["failed"]:
        lines.append("")
        lines.append(f"❌ Не доставлено: <b>{len(report['failed'])}</b>")
        for chat_id, reason in report["failed"]:
            lines.append(f"   • <code>{chat_id}</code> — {reason}")
        lines.append("")
        lines.append(
            "<i>Получатель должен сам открыть бота и отправить /start — "
            "до этого Telegram не даёт ему писать.</i>"
        )

    lines.append("")
    lines.append("<i>Тестовый сигнал в журнал не записан "
                 "и на статистику не влияет.</i>")
    await message.answer("\n".join(lines))


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("Выберите раздел:", reply_markup=kb.main_menu())


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
        f"<i>Он указывается в OWNER_IDS в файле .env</i>"
    )


# --------------------------------------------------------------------------
# Кнопки
# --------------------------------------------------------------------------


async def _render(call: CallbackQuery, text: str, markup) -> None:
    """Перерисовывает текущее сообщение, не плодя новые."""
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        # Телеграм ругается, если текст не изменился — это не ошибка
        if "message is not modified" not in str(exc):
            await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data == "nav:menu")
async def nav_menu(call: CallbackQuery) -> None:
    await _render(call, "Выберите раздел:", kb.main_menu())


@router.callback_query(F.data == "nav:signals")
async def nav_signals(call: CallbackQuery) -> None:
    text, markup = await screen_signals()
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:history")
async def nav_history(call: CallbackQuery) -> None:
    text, markup = await screen_history()
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:stats")
async def nav_stats(call: CallbackQuery) -> None:
    text, markup = await screen_stats("all")
    await _render(call, text, markup)


@router.callback_query(F.data.startswith("stats:"))
async def nav_stats_period(call: CallbackQuery) -> None:
    period = call.data.split(":", 1)[1]
    text, markup = await screen_stats(period)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:news")
async def nav_news(call: CallbackQuery) -> None:
    text, markup = await screen_news()
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:status")
async def nav_status(call: CallbackQuery) -> None:
    text, markup = await screen_status()
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:settings")
async def nav_settings(call: CallbackQuery) -> None:
    text, markup = await screen_settings()
    await _render(call, text, markup)


@router.callback_query(F.data.startswith("toggle:"))
async def toggle_setting(call: CallbackQuery) -> None:
    key = call.data.split(":", 1)[1]
    if key not in QUICK_TOGGLES:
        await call.answer("Неизвестная настройка")
        return
    await config.set(key, not bool(config.get(key)))
    prefs = await get_prefs()
    await _render(call, fmt.settings_card(config.all()), kb.settings_screen(prefs))


@router.callback_query(F.data.startswith("signal:"))
async def show_signal(call: CallbackQuery) -> None:
    signal_id = int(call.data.split(":", 1)[1])
    signal = await repo.get_signal(signal_id)
    if signal is None:
        await call.answer("Сигнал не найден", show_alert=True)
        return

    if signal.status == repo.ACTIVE:
        price = await feed.fetch_price(signal.symbol)
        text = fmt.signal_card(signal)
        if price is not None:
            text += (
                f"\n\n<b>Сейчас:</b> {fmt.money(price)}  "
                f"({fmt.pct(signal.unrealized_pct(price))})"
            )
    else:
        text = fmt.outcome_card(signal)

    await call.message.answer(text, reply_markup=kb.back_button())
    await call.answer()


# Любой другой текст — показываем меню, чтобы пользователь не терялся
@router.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer(
        "Не понял команду. Вот что я умею:", reply_markup=kb.main_menu()
    )
