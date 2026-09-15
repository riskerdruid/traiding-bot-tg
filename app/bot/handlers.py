"""Обработчики команд и кнопок Telegram-бота."""

from __future__ import annotations

import contextlib
import logging
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot import alerts_input
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


async def get_prefs(uid: int) -> dict:
    """Текущие значения быстрых переключателей этого получателя."""
    await config.load()
    return {key: bool(config.get(key, user_id=uid)) for key in QUICK_TOGGLES}


# --------------------------------------------------------------------------
# Экраны
# --------------------------------------------------------------------------


async def screen_signals(uid: int) -> tuple[str, object]:
    active = await repo.get_active_signals(owner_id=uid)
    prices: dict[str, float] = {}
    if active:
        symbols = list({s.symbol for s in active})
        prices = await feed.fetch_prices(symbols)
    return fmt.active_list(active, prices), kb.signals_screen(bool(active))


async def screen_history(uid: int) -> tuple[str, object]:
    signals = await repo.list_signals(limit=10, owner_id=uid)
    closed = [s for s in signals if s.status != repo.ACTIVE]
    return fmt.history_list(closed, "Последние завершённые"), kb.back_button()


async def screen_stats(uid: int, period: str = "all") -> tuple[str, object]:
    label, window = PERIODS.get(period, PERIODS["all"])
    since = int(time.time() - window) if window else None
    data = await repo.stats(since=since, owner_id=uid)
    by_symbol = await repo.stats_by_symbol(since=since, owner_id=uid)
    return fmt.stats_card(data, label, by_symbol), kb.stats_periods(period)


async def screen_news(uid: int) -> tuple[str, object]:
    cfg = config.view(uid)
    await calendar.refresh()
    events = calendar.upcoming(limit=6, cfg=cfg)
    return fmt.news_card(events, calendar.mute_reason(cfg)), kb.back_button()


async def screen_status(uid: int) -> tuple[str, object]:
    scanner = runtime.get("scanner")
    tracker = runtime.get("tracker")
    scanner_state = scanner.state(uid) if scanner else {"running": False, "scans_done": 0}
    tracker_state = tracker.state() if tracker else {}
    health = await feed.health()
    prices = await feed.fetch_prices(config.get("symbols", user_id=uid) or [])
    uptime = time.time() - runtime.get("started_at", time.time())
    return (
        fmt.status_card(scanner_state, tracker_state, health, prices, uptime),
        kb.back_button(),
    )


async def screen_settings(uid: int) -> tuple[str, object]:
    prefs = await get_prefs(uid)
    return (
        fmt.settings_card(config.all(user_id=uid), config.explain(user_id=uid)),
        kb.settings_screen(prefs),
    )


async def screen_alerts(uid: int) -> tuple[str, object]:
    alerts = await repo.list_alerts(owner_id=uid)
    prices = {}
    if alerts:
        with contextlib.suppress(Exception):
            prices = await feed.fetch_prices(sorted({a.symbol for a in alerts}))
    return fmt.alerts_list_card(alerts, prices), kb.alerts_screen(alerts)


async def create_alert_from_text(uid: int, text: str) -> str | None:
    """Пробует понять «биткоин 95000» и завести уведомление.

    Возвращает готовый ответ человеку или None, если в сообщении вообще
    не было похоже на заказ уровня — тогда отвечает общий обработчик.
    """
    parsed = alerts_input.parse_request(text)
    if parsed is None:
        return None
    names, price, note = parsed

    known = list(config.get("symbols", user_id=uid) or [])
    symbol = alerts_input.resolve_any(names, known)
    if symbol is None:
        return (
            "🤔 Цену я понял, а вот инструмент — нет.\n\n"
            "Напишите название в начале: <code>биткоин 95000</code>, "
            "<code>золото 4400</code>, <code>BTC 95000</code>.\n\n"
            "Работают и обычные названия, и тикеры."
        )

    current = await feed.fetch_price(symbol)
    if current is None:
        return (
            f"😕 Не удалось узнать текущую цену "
            f"<b>{fmt.short_symbol(symbol)}</b> — уведомление не поставил.\n\n"
            "Похоже, инструмент сейчас недоступен. Попробуйте позже "
            "или выберите другой."
        )

    alert = await repo.create_alert(
        owner_id=uid, symbol=symbol, price=price,
        start_price=current, note=note,
    )

    direction = "вырастет до" if alert.direction == repo.UP else "опустится до"
    distance = abs(current - price) / current * 100 if current else 0
    answer = [
        f"🔔 <b>Принято.</b> Сообщу, когда "
        f"{fmt.short_symbol(symbol)} {direction} <b>{fmt.money(price)}</b>.",
        "",
        f"Сейчас: <b>{fmt.money(current)}</b> — идти "
        f"{distance:.2f}% {'вверх' if alert.direction == repo.UP else 'вниз'}.",
    ]
    if note:
        answer.append(f"📝 Заметка: <i>{note}</i>")
    answer += [
        "",
        "<i>Это просто будильник по цене, не совет на сделку. "
        "Все заказанные уровни — в разделе «Уведомления по цене».</i>",
    ]
    return "\n".join(answer)


async def screen_presets(uid: int) -> tuple[str, object]:
    current = config.current_preset(user_id=uid)
    return fmt.presets_card(current), kb.presets_screen(current)


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    name = message.from_user.first_name or "трейдер"
    uid = message.from_user.id
    active = await repo.count_signals(status=repo.ACTIVE, owner_id=uid)
    data = await repo.stats(owner_id=uid)

    symbols = ", ".join(
        fmt.short_symbol(s) for s in (config.get("symbols", user_id=uid) or [])
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
        f"⏱ Свечи по <b>{config.get('timeframe', user_id=uid)}</b>, "
        f"общая картина по <b>{config.get('htf_timeframe', user_id=uid)}</b>",
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
    text, markup = await screen_signals(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    text, markup = await screen_history(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("stats"))
@router.message(F.text == "📊 Статистика")
async def cmd_stats(message: Message) -> None:
    text, markup = await screen_stats(message.from_user.id, "all")
    await message.answer(text, reply_markup=markup)


@router.message(Command("news"))
@router.message(F.text == "📰 Новости")
async def cmd_news(message: Message) -> None:
    text, markup = await screen_news(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("status"))
@router.message(F.text == "ℹ️ Статус")
async def cmd_status(message: Message) -> None:
    text, markup = await screen_status(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    text, markup = await screen_settings(message.from_user.id)
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
    text, markup = await screen_signals(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:history")
async def nav_history(call: CallbackQuery) -> None:
    text, markup = await screen_history(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:stats")
async def nav_stats(call: CallbackQuery) -> None:
    text, markup = await screen_stats(call.from_user.id, "all")
    await _render(call, text, markup)


@router.callback_query(F.data.startswith("stats:"))
async def nav_stats_period(call: CallbackQuery) -> None:
    period = call.data.split(":", 1)[1]
    text, markup = await screen_stats(call.from_user.id, period)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:news")
async def nav_news(call: CallbackQuery) -> None:
    text, markup = await screen_news(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:status")
async def nav_status(call: CallbackQuery) -> None:
    text, markup = await screen_status(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:settings")
async def nav_settings(call: CallbackQuery) -> None:
    text, markup = await screen_settings(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "nav:presets")
async def nav_presets(call: CallbackQuery) -> None:
    text, markup = await screen_presets(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data.startswith("preset:"))
async def choose_preset(call: CallbackQuery) -> None:
    """Применяет готовый режим целиком."""
    key = call.data.split(":", 1)[1]
    uid = call.from_user.id
    try:
        preset = await config.apply_preset(key, user_id=uid)
    except Exception as exc:
        await call.answer(str(exc), show_alert=True)
        return

    await call.answer(f"{preset.emoji} {preset.name}: {preset.expect}")
    text, markup = await screen_presets(uid)
    await _render(call, text, markup)


@router.message(Command("alerts"))
@router.message(F.text == "🔔 Уведомления")
async def cmd_alerts(message: Message) -> None:
    text, markup = await screen_alerts(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("alert"))
async def cmd_alert(message: Message) -> None:
    """/alert биткоин 95000 — то же самое, что написать это словами."""
    payload = (message.text or "").partition(" ")[2].strip()
    if not payload:
        text, markup = await screen_alerts(message.from_user.id)
        await message.answer(text, reply_markup=markup)
        return
    answer = await create_alert_from_text(message.from_user.id, payload)
    await message.answer(
        answer or (
            "Напишите инструмент и цену: <code>/alert биткоин 95000</code>"
        )
    )


@router.callback_query(F.data == "nav:alerts")
async def nav_alerts(call: CallbackQuery) -> None:
    text, markup = await screen_alerts(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data.startswith("alert:del:"))
async def drop_alert(call: CallbackQuery) -> None:
    alert_id = int(call.data.rsplit(":", 1)[1])
    ok = await repo.cancel_alert(alert_id, owner_id=call.from_user.id)
    await call.answer("Убрал" if ok else "Уже снято")
    text, markup = await screen_alerts(call.from_user.id)
    await _render(call, text, markup)


@router.callback_query(F.data == "alert:clear")
async def drop_all_alerts(call: CallbackQuery) -> None:
    count = await repo.cancel_all_alerts(call.from_user.id)
    await call.answer(f"Убрано: {count}")
    text, markup = await screen_alerts(call.from_user.id)
    await _render(call, text, markup)


@router.message(Command("setup"))
async def cmd_setup(message: Message) -> None:
    """Быстрая настройка одной командой."""
    text, markup = await screen_presets(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("toggle:"))
async def toggle_setting(call: CallbackQuery) -> None:
    key = call.data.split(":", 1)[1]
    if key not in QUICK_TOGGLES:
        await call.answer("Неизвестная настройка")
        return
    uid = call.from_user.id
    await config.set(key, not bool(config.get(key, user_id=uid)), user_id=uid)
    prefs = await get_prefs(uid)
    await _render(call, fmt.settings_card(config.all(user_id=uid)), kb.settings_screen(prefs))


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
    """Обычный текст.

    Сначала пробуем прочитать его как заказ уведомления: «биткоин 95000».
    Это самый естественный способ поставить будильник по цене — человек
    пишет то, что хочет, а не ищет команду.
    """
    answer = await create_alert_from_text(message.from_user.id, message.text)
    if answer:
        await message.answer(answer, reply_markup=kb.alert_actions(0))
        return

    await message.answer(
        "Не понял. Вот что можно сделать:\n\n"
        "• <b>Заказать уведомление по цене</b> — просто напишите, "
        "например: <code>биткоин 95000</code> или <code>золото 4400</code>. "
        "Я сообщу, когда рынок дойдёт до этой цены.\n"
        "• Открыть разделы кнопками ниже.\n"
        "• Спросить у справки: /help",
        reply_markup=kb.main_menu(),
    )
