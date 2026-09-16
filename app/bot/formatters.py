"""Тексты сообщений.

Вся вёрстка собрана здесь, чтобы формулировки можно было править, не трогая
логику. Разметка — HTML (в Markdown пришлось бы экранировать символы из
названий пар).

Главное правило этих текстов: их читает человек, который открыл первый
в жизни торговый бот. Поэтому вместо «Стоп 4290» написано «если упадёт
до 4290 — выйти», а вместо «-1.2R» — сумма в деньгах.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.market.catalog import base_ticker, pretty
from app.storage import repo
from app.storage.settings_store import PRESETS, QUIET_FROM, QUIET_TO, config

LONG_EMOJI = "🟢"
SHORT_EMOJI = "🔴"

STATUS_VIEW = {
    repo.ACTIVE: ("⏳", "В работе"),
    repo.TP_HIT: ("✅", "Дошёл до цели"),
    repo.SL_HIT: ("❌", "Пришлось выйти"),
    repo.EXPIRED: ("⌛", "Не дождались"),
    repo.CANCELLED: ("🚫", "Отменён"),
}

# У опциона те же статусы, но называются иначе: не «цель» и «стоп»,
# а «угадал» и «не угадал».
BINARY_STATUS_VIEW = {
    repo.ACTIVE: ("⏳", "Ждём результата"),
    repo.TP_HIT: ("✅", "Угадали"),
    repo.SL_HIT: ("❌", "Не угадали"),
    repo.EXPIRED: ("➖", "Возврат ставки"),
    repo.CANCELLED: ("🚫", "Отменён"),
}


# --------------------------------------------------------------------------
# Примитивы
# --------------------------------------------------------------------------


def money(value: float | None) -> str:
    """Цена с разделителями разрядов и разумным числом знаков."""
    if value is None:
        return "—"
    decimals = 2 if abs(value) >= 100 else (4 if abs(value) >= 1 else 6)
    return f"{value:,.{decimals}f}".replace(",", " ")


def amount(value: float | None) -> str:
    """Денежная сумма. Круглые суммы — без копеек: «1 000», а не «1 000.00»."""
    if value is None:
        return "—"
    text = f"{value:,.2f}".replace(",", " ")
    return text[:-3] if text.endswith(".00") else text


def signed_amount(value: float | None) -> str:
    """Сумма со знаком: «+18 $», «-10 $»."""
    if value is None:
        return "—"
    return f"{'+' if value >= 0 else '-'}{amount(abs(value))} $"


def pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def signals_word(count: int) -> str:
    """«1 из 1 сигнала», но «6 из 9 сигналов»."""
    return "сигнала" if count % 10 == 1 and count % 100 != 11 else "сигналов"


def bar(value: float, width: int = 10) -> str:
    """Шкала из блоков — уверенность видно, не читая числа."""
    filled = max(0, min(width, round(value / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def local_time(ts: int | None, with_date: bool = False) -> str:
    from app.config import settings

    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(settings.tz)
    return dt.strftime("%d.%m %H:%M") if with_date else dt.strftime("%H:%M")


def ago(ts: int | None) -> str:
    """Человеческое «сколько времени назад»."""
    if not ts:
        return "ещё ни разу"
    seconds = int(datetime.now(tz=timezone.utc).timestamp()) - ts
    if seconds < 90:
        return "только что"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч назад"
    return f"{seconds // 86400} дн назад"


def short_symbol(symbol: str) -> str:
    """Понятное имя инструмента: XAU/USDT:USDT -> Золото."""
    return pretty(symbol)


def symbols_line(symbols: list[str] | None) -> str:
    return ", ".join(short_symbol(s) for s in (symbols or [])) or "ничего не выбрано"


# --------------------------------------------------------------------------
# Сигналы
# --------------------------------------------------------------------------


def _cfg_for(signal: repo.Signal):
    """Настройки владельца сигнала: деньги у каждого свои."""
    if getattr(signal, "owner_id", None):
        return config.view(signal.owner_id)
    return config


def position_size(signal: repo.Signal) -> str | None:
    """Сколько брать, чтобы на стопе потерять ровно заданную сумму.

    Это тот самый расчёт, который новичок не делает никогда — и поэтому
    теряет депозит за неделю. Бот делает его за него.
    """
    cfg = _cfg_for(signal)
    risk_money = cfg.risk_money()
    distance = abs(signal.entry - signal.stop_loss)
    if risk_money <= 0 or distance <= 0:
        return None

    units = risk_money / distance
    if units >= 1000:
        units_text = f"{units:,.1f}".replace(",", " ")
    else:
        units_text = f"{units:.4f}".rstrip("0").rstrip(".") or "0"

    deposit = float(cfg.get("deposit") or 0)
    return (
        f"взять примерно <b>{units_text} {base_ticker(signal.symbol)}</b> — "
        f"тогда при неудаче потеряете {amount(risk_money)} $ "
        f"из {amount(deposit)} $"
    )


def binary_stake(signal: repo.Signal) -> str | None:
    """Размер ставки по опциону."""
    cfg = _cfg_for(signal)
    risk_money = cfg.risk_money()
    if risk_money <= 0:
        return None
    deposit = float(cfg.get("deposit") or 0)
    return (
        f"поставить <b>{amount(risk_money)} $</b> — "
        f"это {float(cfg.get('risk_per_trade') or 0):g}% от {amount(deposit)} $"
    )


def signal_card(signal: repo.Signal) -> str:
    """Карточка нового сигнала — главное сообщение продукта."""
    if signal.is_binary:
        return _binary_card(signal)

    emoji = LONG_EMOJI if signal.is_long else SHORT_EMOJI
    side = "ПОКУПКА" if signal.is_long else "ПРОДАЖА"
    risk_pct = abs(signal.entry - signal.stop_loss) / signal.entry * 100
    reward_pct = abs(signal.take_profit - signal.entry) / signal.entry * 100

    down = "упадёт" if signal.is_long else "вырастет"
    up = "дойдёт" if signal.is_long else "опустится"

    lines = [
        f"{emoji} <b>{side}</b> · <b>{short_symbol(signal.symbol)}</b>",
        "",
        f"Входить по <b>{money(signal.entry)}</b>",
        f"❌ Если {down} до <b>{money(signal.stop_loss)}</b> — выходить, "
        f"идея не сработала <i>({pct(-risk_pct)})</i>",
        f"✅ Если {up} до <b>{money(signal.take_profit)}</b> — забирать "
        f"прибыль <i>({pct(reward_pct)})</i>",
        "",
        f"Уверенность <b>{signal.confidence}%</b>  <code>{bar(signal.confidence)}</code>",
    ]

    size = position_size(signal)
    if size:
        lines.append(f"💰 Объём: {size}")

    lines += _reasons_block(signal)
    lines.append("")
    lines.append(f"<i>{_tail(signal)}</i>")
    return "\n".join(lines)


def _binary_card(signal: repo.Signal) -> str:
    """Карточка опциона: направление и срок вместо стопа и цели."""
    emoji = LONG_EMOJI if signal.is_long else SHORT_EMOJI
    side = "ВВЕРХ" if signal.is_long else "ВНИЗ"
    arrow = "▲" if signal.is_long else "▼"
    minutes = int(signal.expiry_minutes or 0)

    lines = [
        f"{emoji} <b>{arrow} {side}</b> · <b>{short_symbol(signal.symbol)}</b>",
        "",
        f"Ставить на <b>{'рост' if signal.is_long else 'падение'}</b> "
        f"по цене <b>{money(signal.entry)}</b>",
        f"⏱ Итог через <b>{minutes} мин</b>"
        + (f" — в {local_time(signal.expiry_at)}" if signal.expiry_at else ""),
    ]
    if signal.payout is not None:
        breakeven = 100 / (1 + signal.payout / 100)
        lines.append(
            f"💵 Выплата брокера <b>{signal.payout:.0f}%</b> — чтобы выйти "
            f"в плюс, угадывать надо чаще, чем {breakeven:.0f} раз из 100"
        )

    lines += [
        "",
        f"Уверенность <b>{signal.confidence}%</b>  <code>{bar(signal.confidence)}</code>",
    ]

    stake = binary_stake(signal)
    if stake:
        lines.append(f"💰 Сколько: {stake}")

    lines += _reasons_block(signal)
    lines.append("")
    lines.append(f"<i>{_tail(signal)}</i>")
    return "\n".join(lines)


def _reasons_block(signal: repo.Signal) -> list[str]:
    if not signal.reasons:
        return []
    lines = ["", "<b>Почему:</b>"]
    lines += [f"• {reason}" for reason in signal.reasons[:6]]
    return lines


def _tail(signal: repo.Signal) -> str:
    # У образца для проверки доставки номера в журнале нет — не пишем «#0»
    tail = local_time(signal.created_at)
    if signal.id:
        tail += f" · сигнал #{signal.id}"
    return tail


def outcome_card(signal: repo.Signal) -> str:
    """Сообщение о том, чем всё закончилось."""
    view = BINARY_STATUS_VIEW if signal.is_binary else STATUS_VIEW
    icon, title = view.get(signal.status, ("•", signal.status))

    if signal.is_binary:
        side = "вверх" if signal.is_long else "вниз"
    else:
        side = "покупка" if signal.is_long else "продажа"

    lines = [
        f"{icon} <b>{title}</b> · сигнал #{signal.id}",
        "",
        f"{short_symbol(signal.symbol)}, {side} по {money(signal.entry)} → "
        f"вышли по {money(signal.exit_price)}",
    ]

    if signal.pnl_pct is not None:
        line = f"Результат <b>{pct(signal.pnl_pct)}</b>"
        gain = _money_result(signal)
        if gain:
            line += f" — это примерно <b>{gain}</b>"
        lines.append(line)

    if signal.closed_at and signal.created_at:
        minutes = (signal.closed_at - signal.created_at) / 60
        held = f"{minutes / 60:.1f} ч".replace(".0 ", " ") if minutes >= 60             else f"{minutes:.0f} мин"
        lines.append(f"В работе был {held}")

    if signal.status == repo.SL_HIT:
        lines += [
            "",
            "<i>Так бывает: часть сигналов всегда уходит в минус. "
            "Смысл в том, что удачные приносят больше, чем забирают неудачные.</i>",
        ]
    return "\n".join(lines)


def _money_result(signal: repo.Signal) -> str | None:
    """Результат сигнала в деньгах — по риску владельца."""
    if signal.r_multiple is None:
        return None
    risk_money = _cfg_for(signal).risk_money()
    if risk_money <= 0:
        return None
    return signed_amount(signal.r_multiple * risk_money)


def signal_row(signal: repo.Signal, price: float | None = None) -> str:
    """Компактная строка для списков."""
    emoji = LONG_EMOJI if signal.is_long else SHORT_EMOJI
    view = BINARY_STATUS_VIEW if signal.is_binary else STATUS_VIEW
    icon, _ = view.get(signal.status, ("•", ""))
    name = short_symbol(signal.symbol)
    side = "покупка" if signal.is_long else "продажа"

    if signal.status == repo.ACTIVE:
        now = f" · сейчас {pct(signal.unrealized_pct(price))}" if price else ""
        return (
            f"{emoji} <b>{name}</b> · {side} по {money(signal.entry)}{now}\n"
            f"   <i>#{signal.id} · {local_time(signal.created_at, True)}</i>"
        )

    result = f" <b>{pct(signal.pnl_pct)}</b>" if signal.pnl_pct is not None else ""
    return (
        f"{icon} <b>{name}</b> · {side} по {money(signal.entry)}{result}\n"
        f"   <i>#{signal.id} · {local_time(signal.created_at, True)}</i>"
    )


def signals_screen(
    active: list[repo.Signal],
    recent: list[repo.Signal],
    prices: dict[str, float],
    scanner_state: dict,
) -> str:
    """Экран «Сигналы»: что в работе, что закончилось, жив ли бот."""
    lines = []

    if active:
        lines.append(f"🎯 <b>Сейчас в работе: {len(active)}</b>")
        lines.append("")
        for signal in active:
            lines.append(signal_row(signal, prices.get(signal.symbol)))
            lines.append("")
    else:
        lines += [
            "🎯 <b>Сейчас сигналов нет</b>",
            "",
            "Это нормально: бот пишет только тогда, когда видит подходящий "
            "момент, а такие моменты бывают не каждый час.",
            "",
        ]

    if recent:
        lines.append("<b>Последние завершённые:</b>")
        lines.append("")
        for signal in recent:
            lines.append(signal_row(signal))
            lines.append("")

    lines.append(_pulse(scanner_state))
    return "\n".join(lines).strip()


def _pulse(state: dict) -> str:
    """Одна строка о том, что бот жив. Заменяет собой целый экран статуса."""
    if not state or not state.get("running"):
        return "<i>⚠️ Бот сейчас не следит за рынком — идёт запуск или перезагрузка.</i>"
    watching = symbols_line(state.get("symbols"))
    muted = state.get("muted_by_news")
    if muted:
        return (
            f"<i>🔇 Слежу за: {watching}. Сейчас молчу из-за важной новости "
            f"({muted}) — в такие минуты цену двигает не график.</i>"
        )
    return (
        f"<i>✅ Слежу за: {watching}. "
        f"Рынок проверен {ago(state.get('last_scan_at'))}.</i>"
    )


# --------------------------------------------------------------------------
# Результаты
# --------------------------------------------------------------------------


def stats_card(data: dict, by_symbol: list[dict], risk_money: float) -> str:
    """Главный отчёт: угадывает бот или нет и что это в деньгах."""
    if not data["decided"] and not data["active"]:
        return (
            "📊 <b>Результаты</b>\n\n"
            "Пока пусто. Результат появится, когда первые сигналы дойдут "
            "до цели или до стопа.\n\n"
            "<i>Обычно на это уходит от нескольких часов до пары дней.</i>"
        )

    winrate = data["winrate"]
    mood = "🟢" if winrate >= 55 else ("🟡" if winrate >= 45 else "🔴")

    lines = ["📊 <b>Результаты</b>", ""]
    if data["decided"]:
        lines += [
            f"{mood} <b>Угадано {winrate:.0f}%</b> — {data['wins']} "
            f"из {data['decided']} {signals_word(data['decided'])}",
            f"<code>{bar(winrate)}</code>",
            "",
        ]
    lines += [
        f"✅ Дошли до цели    <b>{data['wins']}</b>",
        f"❌ Пришлось выйти   <b>{data['losses']}</b>",
    ]
    if data["expired"]:
        lines.append(f"⌛ Не дождались     <b>{data['expired']}</b>")
    if data["active"]:
        lines.append(f"⏳ Сейчас в работе  <b>{data['active']}</b>")

    if data["decided"] and risk_money > 0:
        total = data["total_r"] * risk_money
        lines += [
            "",
            f"💰 Если бы вы торговали по всем сигналам с риском "
            f"{amount(risk_money)} $ на сделку, сейчас было бы "
            f"<b>{signed_amount(total)}</b>",
        ]

    if by_symbol:
        rows = [r for r in by_symbol if r["wins"] + r["losses"] > 0]
        if rows:
            lines += ["", "<b>По инструментам:</b>"]
            for row in rows[:6]:
                total = row["wins"] + row["losses"]
                lines.append(
                    f"• {short_symbol(row['symbol'])} — {row['winrate']:.0f}% "
                    f"({row['wins']} из {total})"
                )

    if data["decided"] < 20:
        lines += [
            "",
            "<i>⚠️ Сигналов пока мало, чтобы судить о боте. "
            "Ориентир появляется после 20–30 завершённых.</i>",
        ]
    return "\n".join(lines)


def daily_report(data: dict, signals_today: list[repo.Signal]) -> str:
    """Вечерняя сводка. Приходит, только если за день что-то было."""
    lines = ["🌙 <b>Итоги дня</b>", "", f"Сигналов было: <b>{len(signals_today)}</b>"]
    if data["decided"]:
        lines.append(
            f"Из закрытых сегодня угадано: <b>{data['winrate']:.0f}%</b> "
            f"({data['wins']} из {data['decided']})"
        )
    else:
        lines.append("<i>Ни один сигнал сегодня ещё не закрылся.</i>")

    if signals_today:
        lines.append("")
        for signal in signals_today[:8]:
            view = BINARY_STATUS_VIEW if signal.is_binary else STATUS_VIEW
            icon, _ = view.get(signal.status, ("•", ""))
            side = "▲" if signal.is_long else "▼"
            result = f"  {pct(signal.pnl_pct)}" if signal.pnl_pct is not None else ""
            lines.append(
                f"{icon} {side} {short_symbol(signal.symbol)} "
                f"{local_time(signal.created_at)}{result}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------


def mode_line(cfg) -> str:
    """Подпись режима.

    Если значения не совпадают ни с одним готовым режимом (так бывает,
    когда их правили в .env руками), честно говорим об этом: иначе
    человек прочитает «1–3 сигнала в день» и будет ждать их напрасно.
    """
    preset = cfg.preset()
    if cfg.current_preset() is None:
        return f"<b>{preset.emoji} {preset.name}</b> <i>(с ручными правками)</i>"
    return f"<b>{preset.emoji} {preset.name}</b> — {preset.expect}"


def settings_card(cfg) -> str:
    deposit = float(cfg.get("deposit") or 0)
    risk = float(cfg.get("risk_per_trade") or 0)
    night = "не беспокою" if cfg.get("quiet_night") else "пишу как обычно"

    return "\n".join(
        [
            "⚙️ <b>Настройки</b>",
            "",
            f"📈 Слежу за: <b>{symbols_line(cfg.get('symbols'))}</b>",
            f"🎚 Режим: {mode_line(cfg)}",
            f"💰 Счёт: <b>{amount(deposit)} $</b>, рискую {risk:g}% — "
            f"около <b>{amount(cfg.risk_money())} $</b> на сделку",
            f"🌙 Ночью ({QUIET_FROM}–{QUIET_TO}): <b>{night}</b>",
            "",
            "<i>Больше настраивать нечего — остальное бот делает сам.</i>",
        ]
    )


def symbols_card(choices: list) -> str:
    lines = [
        "📈 <b>За чем следить</b>",
        "",
        "Нажмите на строку, чтобы включить или выключить. "
        "Начните с одного-двух: так проще понять, как бот работает.",
        "",
    ]
    for choice in choices:
        mark = "✅" if choice.chosen else "▫️"
        lines.append(f"{mark} <b>{choice.title}</b> — <i>{choice.note}</i>")
    if not choices:
        lines.append("<i>Ни один инструмент сейчас недоступен. "
                     "Похоже, нет связи с биржей — попробуйте позже.</i>")
    return "\n".join(lines)


def presets_card(current: str | None) -> str:
    lines = [
        "🎚 <b>Как часто присылать сигналы</b>",
        "",
        "Чем строже бот отбирает моменты, тем реже пишет — и тем надёжнее "
        "то, что напишет.",
        "",
    ]
    for preset in PRESETS:
        mark = " — <b>сейчас выбран</b>" if current == preset.key else ""
        lines += [
            f"{preset.emoji} <b>{preset.name}</b>{mark}",
            f"{preset.summary}.",
            f"Ожидайте <b>{preset.expect}</b>.",
            "",
        ]
    return "\n".join(lines).strip()


def money_card(cfg) -> str:
    deposit = float(cfg.get("deposit") or 0)
    risk = float(cfg.get("risk_per_trade") or 0)
    return "\n".join(
        [
            "💰 <b>Деньги</b>",
            "",
            f"На счёте: <b>{amount(deposit)} $</b>",
            f"Рискую на одной сделке: <b>{risk:g}%</b> — "
            f"это <b>{amount(cfg.risk_money())} $</b>",
            "",
            "Бот считает от этих чисел, сколько брать в сделку. "
            "Сами деньги он не видит и никуда их не передаёт: "
            "это просто подсказка в сообщении.",
            "",
            "<i>1% — то, с чего начинают все. При таком риске даже десять "
            "неудач подряд заберут около десятой части счёта. При 10% те же "
            "десять заберут почти всё.</i>",
        ]
    )


def pocket_card(configured: bool) -> str:
    """Экран подключения брокера опционов."""
    if configured:
        head = "🔑 <b>Pocket Option подключён</b>\n\nПары брокера доступны в списке «За чем следить»."
    else:
        head = "🔑 <b>Опционы Pocket Option</b>\n\nПока не подключены."
    return "\n".join(
        [
            head,
            "",
            "Чтобы бот видел котировки брокера, нужен ключ сессии. "
            "Пароль от счёта вводить не нужно.",
            "",
            "<b>Как его взять:</b>",
            "1. Откройте pocketoption.com на компьютере и войдите в счёт",
            "2. Нажмите F12 — откроется панель разработчика",
            "3. Вкладка <b>Network</b>, фильтр <b>WS</b>",
            "4. Обновите страницу и откройте единственное соединение",
            "5. Во вкладке <b>Messages</b> найдите строку, начинающуюся "
            "с <code>42[&quot;auth&quot;</code>",
            "6. Скопируйте её целиком и пришлите мне сообщением",
            "",
            "<i>Ключ живёт несколько дней, потом его нужно прислать заново — "
            "бот сам напишет, когда перестанет видеть брокера.</i>",
        ]
    )


# --------------------------------------------------------------------------
# Прочее
# --------------------------------------------------------------------------


def greeting(name: str, cfg, active: int, decided: int, winrate: float) -> str:
    lines = [
        f"👋 <b>Здравствуйте, {name}!</b>",
        "",
        "Я смотрю на рынок круглые сутки и пишу, когда вижу подходящий "
        "момент для сделки. В каждом сообщении будет:",
        "",
        "• <b>что</b> покупать или продавать",
        "• <b>по какой цене</b> входить",
        "• <b>когда выйти</b>, если рынок пошёл не туда",
        "• <b>когда забрать прибыль</b>",
        "• <b>почему</b> я так решил — простыми словами",
        "",
        "Торговать за вас я не могу: доступа к вашим деньгам у меня нет. "
        "Решение всегда остаётся за вами.",
        "",
        "━━━━━━━━━━━━━━━",
        "",
        f"📈 Слежу за: <b>{symbols_line(cfg.get('symbols'))}</b>",
        f"🎚 Режим: {mode_line(cfg)}",
    ]
    if active:
        lines.append(f"🎯 Сейчас в работе: <b>{active}</b>")
    if decided:
        lines.append(
            f"📊 Угадано: <b>{winrate:.0f}%</b> из {decided} завершённых"
        )

    lines += [
        "",
        "Настраивать ничего не нужно — просто ждите сообщений. "
        "Если что-то непонятно, нажмите <b>❓ Помощь</b>: там всё объяснено "
        "обычными словами.",
    ]
    return "\n".join(lines)


def test_signal_card(symbol: str, price: float, is_binary: bool, cfg) -> str:
    """Образец сигнала по текущей цене — проверка доставки.

    Выглядит как настоящий сигнал, но с явной пометкой, чтобы никто
    не принял его за рекомендацию. В журнал не записывается.
    """
    from app.config import settings

    now = int(datetime.now(tz=timezone.utc).timestamp())
    reasons = [
        "Цена развернулась вверх",
        "Движение уверенное, а не топтание на месте",
        "На крупном графике рынок идёт туда же",
    ]

    if is_binary:
        sample = repo.Signal(
            id=0, symbol=symbol, side="LONG", timeframe=cfg.get("timeframe"),
            entry=price, stop_loss=price, take_profit=price, confidence=74,
            reasons=reasons, indicators={}, status=repo.ACTIVE, created_at=now,
            kind="binary", broker="po", payout=92.0,
            expiry_at=now + int(cfg.get("po_expiry_min") or 5) * 60,
            owner_id=getattr(cfg, "user_id", None),
        )
    else:
        risk = price * 0.0015 * float(cfg.get("atr_sl_mult") or 1.5)
        sample = repo.Signal(
            id=0, symbol=symbol, side="LONG", timeframe=cfg.get("timeframe"),
            entry=price, stop_loss=price - risk,
            take_profit=price + risk * float(cfg.get("risk_reward") or 1.8),
            confidence=74, reasons=reasons, indicators={},
            status=repo.ACTIVE, created_at=now,
            owner_id=getattr(cfg, "user_id", None),
        )

    return (
        "🧪 <b>Проверка связи</b>\n"
        "<i>Это не сигнал, делать ничего не нужно. "
        "Так будет выглядеть настоящий:</i>\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        + signal_card(sample)
        + f"\n\n━━━━━━━━━━━━━━━\n\n✅ Сообщения доходят. "
        f"<i>Версия {settings.app_version}</i>"
    )


# --------------------------------------------------------------------------
# Уведомления по цене
#
# Это не сигнал: бот ничего не советует, а просто сообщает, что рынок
# дошёл до числа, которое человек назвал сам. Разница принципиальная,
# поэтому она проговаривается в каждом сообщении.
# --------------------------------------------------------------------------


def alerts_card(alerts: list, prices: dict[str, float] | None = None) -> str:
    prices = prices or {}
    if not alerts:
        return (
            "🔔 <b>Уведомления по цене</b>\n\n"
            "Пока ни одного.\n\n"
            "Это будильник: вы называете цену — я пишу, когда рынок до неё "
            "дошёл. Например: «сообщи, когда биткоин будет стоить 95 000» "
            "или «если золото упадёт на 1%».\n\n"
            "Нажмите <b>➕ Добавить</b> — я всё спрошу сам."
        )

    lines = ["🔔 <b>Уведомления по цене</b>", "", "Жду вот этого:", ""]
    for alert in alerts:
        name = short_symbol(alert.symbol)
        if alert.percent is not None:
            move = "вырастет" if alert.direction == repo.UP else "упадёт"
            head = f"• <b>{name}</b> {move} на {alert.percent:g}%"
            tail = f" — это {money(alert.price)}"
        else:
            move = "поднимется до" if alert.direction == repo.UP else "опустится до"
            head = f"• <b>{name}</b> {move} <b>{money(alert.price)}</b>"
            tail = ""
        lines.append(head + tail)

        now = prices.get(alert.symbol)
        if now:
            distance = abs(now - alert.price) / now * 100
            where = "вверх" if alert.price > now else "вниз"
            lines.append(
                f"   <i>сейчас {money(now)} — идти {distance:.2f}% {where}</i>"
            )
        if alert.note:
            lines.append(f"   📝 <i>{alert.note}</i>")

    lines += [
        "",
        "<i>Сработавшее уведомление гаснет само. Это не совет на сделку — "
        "просто напоминание о цене.</i>",
    ]
    return "\n".join(lines)


def alert_created(alert, current: float | None) -> str:
    """Подтверждение: что именно бот теперь ждёт."""
    name = short_symbol(alert.symbol)
    if alert.percent is not None:
        move = "вырастет" if alert.direction == repo.UP else "упадёт"
        head = (
            f"🔔 <b>Принято.</b> Сообщу, если {name} {move} "
            f"на <b>{alert.percent:g}%</b>."
        )
    else:
        move = "поднимется до" if alert.direction == repo.UP else "опустится до"
        head = f"🔔 <b>Принято.</b> Сообщу, когда {name} {move} <b>{money(alert.price)}</b>."

    lines = [head, ""]
    if current:
        distance = abs(current - alert.price) / current * 100
        where = "вверх" if alert.price > current else "вниз"
        lines.append(
            f"Сейчас <b>{money(current)}</b>, сработает при "
            f"<b>{money(alert.price)}</b> — идти {distance:.2f}% {where}."
        )
    if alert.note:
        lines.append(f"📝 Заметка: <i>{alert.note}</i>")
    lines += [
        "",
        "<i>Это будильник по цене, а не сигнал на сделку.</i>",
    ]
    return "\n".join(lines)


def alert_pair_created(up, down, current: float | None, percent: float) -> str:
    """Человек попросил движение «на N%», не сказав куда — ждём обе стороны."""
    name = short_symbol(up.symbol)
    return "\n".join(
        [
            f"🔔 <b>Принято.</b> Сообщу, если {name} сдвинется "
            f"на <b>{percent:g}%</b> в любую сторону.",
            "",
            f"Сейчас <b>{money(current)}</b>",
            f"▲ вверх — при {money(up.price)}",
            f"▼ вниз — при {money(down.price)}",
            "",
            "<i>Это будильник по цене, а не сигнал на сделку.</i>",
        ]
    )


def alert_fired(alert, price: float) -> str:
    """Сообщение в момент, когда цена дошла до заказанного уровня."""
    name = short_symbol(alert.symbol)
    if alert.percent is not None:
        move = "вырос" if alert.direction == repo.UP else "упал"
        head = f"🔔 <b>{name} {move} на {alert.percent:g}%</b>"
    else:
        move = "поднялся до" if alert.direction == repo.UP else "опустился до"
        head = f"🔔 <b>{name} {move} {money(alert.price)}</b>"

    lines = [head, "", f"Сейчас: <b>{money(price)}</b>"]

    if alert.start_price:
        delta = price - alert.start_price
        arrow = "▲" if delta >= 0 else "▼"
        delta_pct = delta / alert.start_price * 100
        lines.append(
            f"С того момента, как вы попросили: {arrow} {pct(delta_pct)} "
            f"(было {money(alert.start_price)})"
        )
    lines.append(f"Заказано {local_time(alert.created_at, with_date=True)}")

    if alert.note:
        lines.append(f"📝 Ваша заметка: <i>{alert.note}</i>")

    lines += [
        "",
        "<i>Это не сигнал: я сообщил ровно о том, о чём вы просили. "
        "Решение — за вами.</i>",
    ]
    return "\n".join(lines)


def alert_ask_value(symbol: str, price: float | None) -> str:
    """Второй шаг мастера: что именно сообщить по выбранному инструменту."""
    name = short_symbol(symbol)
    now = f"Сейчас <b>{money(price)}</b>.\n\n" if price else ""
    return (
        f"🔔 <b>{name}</b>\n\n"
        f"{now}"
        "Выберите кнопкой или напишите числом:\n"
        f"<code>{money(price) if price else '95000'}</code> — сообщу при этой цене\n"
        "<code>-1%</code> — если упадёт на столько\n"
        "<code>+2%</code> — если вырастет\n\n"
        "<i>После числа можно дописать заметку для себя — "
        "она вернётся вместе с уведомлением.</i>"
    )
