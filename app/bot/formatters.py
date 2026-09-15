"""Форматирование сообщений для Telegram.

Вся вёрстка собрана здесь, чтобы тексты можно было править, не трогая логику.
Разметка — HTML (в Markdown пришлось бы экранировать символы из названий пар).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from app.config import settings
from app.news.calendar import NewsEvent
from app.storage import repo
from app.storage.settings_store import FIELDS, GROUPS, config

LONG_EMOJI = "🟢"
SHORT_EMOJI = "🔴"

# У опциона те же статусы, но называются иначе: не «цель» и «стоп»,
# а «угадал» и «не угадал».
BINARY_STATUS_VIEW = {
    repo.ACTIVE: ("⏳", "Ждём экспирации"),
    repo.TP_HIT: ("✅", "Опцион в плюс"),
    repo.SL_HIT: ("❌", "Опцион в минус"),
    repo.EXPIRED: ("➖", "Возврат ставки"),
    repo.CANCELLED: ("🚫", "Отменён"),
}

STATUS_VIEW = {
    repo.ACTIVE: ("⏳", "В работе"),
    repo.TP_HIT: ("✅", "Цель достигнута"),
    repo.SL_HIT: ("❌", "Сработал стоп"),
    repo.EXPIRED: ("⌛", "Истёк по времени"),
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
    text = f"{value:,.{decimals}f}".replace(",", " ")
    return text


def pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def bar(value: float, width: int = 10) -> str:
    """Шкала из блоков для наглядной уверенности."""
    filled = max(0, min(width, round(value / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def local_time(ts: int | None, with_date: bool = False) -> str:
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(settings.tz)
    return dt.strftime("%d.%m %H:%M") if with_date else dt.strftime("%H:%M")


def ago(ts: int | None) -> str:
    """Человеческое «сколько времени назад»."""
    if not ts:
        return "никогда"
    seconds = int(datetime.now(tz=timezone.utc).timestamp()) - ts
    if seconds < 60:
        return "только что"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч назад"
    return f"{seconds // 86400} дн назад"


def short_symbol(symbol: str) -> str:
    """Читаемое имя инструмента.

    Сначала снимаем префикс площадки, потом хвост расчётной валюты:
        po:EURUSD_otc   -> EURUSD OTC
        XAU/USDT:USDT   -> XAU/USDT
    """
    from app.market.feed import split_symbol

    prefix, name = split_symbol(symbol)
    if prefix == "po":
        # У брокера имена вида EURUSD_otc — показываем без подчёркивания
        return name.replace("_otc", " OTC").replace("_", " ")
    return name.split(":")[0]


def amount(value: float | None) -> str:
    """Денежная сумма: два знака, без ценовой точности."""
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ")


def _cfg_for(signal: repo.Signal):
    """Настройки владельца сигнала.

    Депозит и допустимый риск у каждого свои, поэтому объём позиции нельзя
    считать по общим значениям. У старых сигналов владельца нет — для них
    берём общие настройки.
    """
    if getattr(signal, "owner_id", None):
        return config.view(signal.owner_id)
    return config


# --------------------------------------------------------------------------
# Сигналы
# --------------------------------------------------------------------------


def position_sizing(signal: repo.Signal) -> dict | None:
    """Сколько брать, чтобы потерять на стопе ровно заданный процент депозита.

    Это ответ на вопрос, который трейдер задаёт себе каждый раз руками:
    «стоп в 12 пунктах, депозит 1000, рискую 1% — какой объём?»
    Считается от расстояния до стопа, а не от цены входа, потому что
    рискуем мы именно этим расстоянием.
    """
    cfg = _cfg_for(signal)
    deposit = float(cfg.get("deposit") or 0)
    risk_pct = float(cfg.get("risk_per_trade") or 0)
    distance = abs(signal.entry - signal.stop_loss)
    if deposit <= 0 or risk_pct <= 0 or distance <= 0:
        return None

    risk_money = deposit * risk_pct / 100
    units = risk_money / distance
    notional = units * signal.entry

    base = short_symbol(signal.symbol).split("/")[0]
    # Имя намеренно не `amount` — так называется функция форматирования суммы,
    # объявленная выше, и затенять её здесь нельзя
    if units >= 1000:
        units_text = f"{units:,.1f}".replace(",", " ")
    else:
        # Обрезаем только незначащие нули самого числа, не трогая тикер
        units_text = f"{units:.4f}".rstrip("0").rstrip(".") or "0"

    return {
        "units": f"{units_text} {base}",
        "units_raw": round(units, 6),
        "notional": money(notional),
        "risk_money": f"{amount(risk_money)} ({risk_pct:g}%)",
        "deposit": amount(deposit),
    }


def signal_card(signal: repo.Signal) -> str:
    """Карточка нового сигнала — главное сообщение продукта."""
    if signal.is_binary:
        return binary_signal_card(signal)

    emoji = LONG_EMOJI if signal.is_long else SHORT_EMOJI
    side = "ПОКУПКА" if signal.is_long else "ПРОДАЖА"

    risk_pct = abs(signal.entry - signal.stop_loss) / signal.entry * 100
    reward_pct = abs(signal.take_profit - signal.entry) / signal.entry * 100
    rr = reward_pct / risk_pct if risk_pct else 0

    lines = [
        f"{emoji} <b>{side}</b> · <b>{short_symbol(signal.symbol)}</b>",
        "",
        f"<code>Вход   {money(signal.entry):>12}</code>",
        f"<code>Стоп   {money(signal.stop_loss):>12}</code>  <i>{pct(-risk_pct)}</i>",
        f"<code>Цель   {money(signal.take_profit):>12}</code>  <i>{pct(reward_pct)}</i>",
        "",
        f"Уверенность  <code>{bar(signal.confidence)}</code> {signal.confidence}%",
        f"Риск/прибыль  <b>1:{rr:.1f}</b>",
    ]

    if signal.reasons:
        lines.append("")
        lines.append("<b>Основания:</b>")
        for reason in signal.reasons[:6]:
            lines.append(f"• {reason}")

    sizing = position_sizing(signal)
    if sizing:
        lines.append("")
        lines.append(
            f"<b>Объём:</b> {sizing['units']} "
            f"<i>(риск {sizing['risk_money']} из {sizing['deposit']})</i>"
        )

    ind = signal.indicators or {}
    chips = []
    if ind.get("rsi") is not None:
        chips.append(f"RSI {ind['rsi']:.0f}")
    if ind.get("adx") is not None:
        chips.append(f"ADX {ind['adx']:.0f}")
    if ind.get("atr") is not None:
        chips.append(f"ATR {money(ind['atr'])}")
    if chips:
        lines.append("")
        lines.append("<i>" + " · ".join(chips) + "</i>")

    # У образца для проверки доставки номера в журнале нет — не показываем «#0»
    tail = f"{signal.timeframe} · {local_time(signal.created_at)}"
    if signal.id:
        tail += f" · сигнал #{signal.id}"
    lines.append(f"<i>{tail}</i>")
    return "\n".join(lines)


def binary_signal_card(signal: repo.Signal) -> str:
    """Карточка опциона: направление, срок и выплата вместо стопа и цели."""
    emoji = LONG_EMOJI if signal.is_long else SHORT_EMOJI
    side = "ВВЕРХ" if signal.is_long else "ВНИЗ"
    arrow = "▲" if signal.is_long else "▼"

    expiry_min = signal.expiry_minutes or 0
    lines = [
        f"{emoji} <b>{arrow} {side}</b> · <b>{short_symbol(signal.symbol)}</b>",
        "",
        f"<code>Цена входа  {money(signal.entry):>12}</code>",
        f"<code>Экспирация  {expiry_min:>9.0f} мин</code>",
    ]
    if signal.payout is not None:
        lines.append(f"<code>Выплата     {signal.payout:>10.0f} %</code>")
    if signal.expiry_at:
        lines.append(f"<code>Закрытие    {local_time(signal.expiry_at):>12}</code>")

    lines += [
        "",
        f"Уверенность  <code>{bar(signal.confidence)}</code> {signal.confidence}%",
    ]

    if signal.payout is not None:
        breakeven = 100 / (1 + signal.payout / 100)
        lines.append(
            f"Порог безубытка  <b>{breakeven:.0f}%</b> "
            f"<i>— столько нужно угадывать</i>"
        )

    if signal.reasons:
        lines.append("")
        lines.append("<b>Основания:</b>")
        for reason in signal.reasons[:6]:
            lines.append(f"• {reason}")

    stake = binary_stake(signal)
    if stake:
        lines.append("")
        lines.append(f"<b>Ставка:</b> {stake}")

    ind = signal.indicators or {}
    chips = []
    if ind.get("rsi") is not None:
        chips.append(f"RSI {ind['rsi']:.0f}")
    if ind.get("adx") is not None:
        chips.append(f"ADX {ind['adx']:.0f}")
    if chips:
        lines.append("")
        lines.append("<i>" + " · ".join(chips) + "</i>")

    # У образца для проверки доставки номера в журнале нет — не показываем «#0»
    tail = f"{signal.timeframe} · {local_time(signal.created_at)}"
    if signal.id:
        tail += f" · сигнал #{signal.id}"
    lines.append(f"<i>{tail}</i>")
    return "\n".join(lines)


def binary_stake(signal: repo.Signal) -> str | None:
    """Размер ставки по опциону: процент депозита, заданный заказчиком."""
    cfg = _cfg_for(signal)
    deposit = float(cfg.get("deposit") or 0)
    risk_pct = float(cfg.get("risk_per_trade") or 0)
    if deposit <= 0 or risk_pct <= 0:
        return None
    stake = deposit * risk_pct / 100
    return f"{amount(stake)} ({risk_pct:g}% от {amount(deposit)})"


def test_signal_card(
    symbol: str, price: float, is_binary: bool = False, cfg=None
) -> str:
    """Карточка-образец по текущей цене.

    Выглядит как настоящий сигнал, но с явной пометкой, чтобы никто
    не принял её за рекомендацию. В журнал не записывается.
    """
    cfg = cfg or config
    header = [
        "🧪 <b>ТЕСТОВОЕ СООБЩЕНИЕ</b>",
        "<i>Это проверка доставки, а не сигнал. "
        "Ничего делать не нужно.</i>",
        "",
        "Так будет выглядеть настоящий сигнал:",
        "",
        "━━━━━━━━━━━━━━━",
        "",
    ]

    now = int(datetime.now(tz=timezone.utc).timestamp())
    if is_binary:
        sample = repo.Signal(
            id=0, symbol=symbol, side="LONG", timeframe=cfg.get("timeframe"),
            entry=price, stop_loss=price, take_profit=price, confidence=74,
            reasons=[
                "EMA9 пересекла EMA21 снизу вверх",
                "ADX 24 — тренд подтверждён",
                "Старший ТФ в том же направлении",
            ],
            indicators={"rsi": 58.0, "adx": 24.0},
            status=repo.ACTIVE, created_at=now, kind="binary", broker="po",
            expiry_at=now + int(cfg.get("po_expiry_min") or 5) * 60,
            payout=92.0,
        )
    else:
        atr = price * 0.0015
        risk = atr * float(cfg.get("atr_sl_mult") or 1.5)
        sample = repo.Signal(
            id=0, symbol=symbol, side="LONG", timeframe=cfg.get("timeframe"),
            entry=price, stop_loss=price - risk,
            take_profit=price + risk * float(cfg.get("risk_reward") or 1.8),
            confidence=74,
            reasons=[
                "EMA9 пересекла EMA21 снизу вверх",
                "ADX 24 — тренд подтверждён",
                "Цена выше EMA50",
            ],
            indicators={"rsi": 58.0, "adx": 24.0, "atr": round(atr, 4)},
            status=repo.ACTIVE, created_at=now,
        )

    footer = [
        "",
        "━━━━━━━━━━━━━━━",
        "",
        "✅ Доставка работает. Настоящие сигналы придут так же, "
        "но без этой пометки.",
    ]
    return "\n".join(header) + signal_card(sample) + "\n".join(footer)


def outcome_card(signal: repo.Signal) -> str:
    """Сообщение о закрытии сигнала."""
    if signal.is_binary:
        icon, title = BINARY_STATUS_VIEW.get(
            signal.status, STATUS_VIEW.get(signal.status, ("•", signal.status))
        )
        side = "ВВЕРХ" if signal.is_long else "ВНИЗ"
    else:
        icon, title = STATUS_VIEW.get(signal.status, ("•", signal.status))
        side = "ПОКУПКА" if signal.is_long else "ПРОДАЖА"

    held = ""
    if signal.closed_at and signal.created_at:
        minutes = (signal.closed_at - signal.created_at) / 60
        held = f"{minutes / 60:.1f} ч" if minutes >= 60 else f"{minutes:.0f} мин"

    result_line = f"Результат  <b>{pct(signal.pnl_pct)}</b>"
    if signal.r_multiple is not None:
        result_line += f"  ({signal.r_multiple:+.2f}R)"

    lines = [
        f"{icon} <b>{title}</b> · сигнал #{signal.id}",
        "",
        f"{side} <b>{short_symbol(signal.symbol)}</b>",
        f"<code>Вход   {money(signal.entry):>12}</code>",
        f"<code>Выход  {money(signal.exit_price):>12}</code>",
        "",
        result_line,
    ]
    if held:
        lines.append(f"В работе  {held}")
    return "\n".join(lines)


def signal_row(signal: repo.Signal, price: float | None = None) -> str:
    """Компактная строка для списков."""
    emoji = LONG_EMOJI if signal.is_long else SHORT_EMOJI
    icon, _ = STATUS_VIEW.get(signal.status, ("•", ""))

    if signal.status == repo.ACTIVE:
        now = f" · сейчас {pct(signal.unrealized_pct(price))}" if price else ""
        return (
            f"{emoji} <b>{short_symbol(signal.symbol)}</b> "
            f"@ {money(signal.entry)} {icon}{now}\n"
            f"   <i>#{signal.id} · {local_time(signal.created_at, True)} · "
            f"уверенность {signal.confidence}%</i>"
        )

    head = (
        f"{emoji} <b>{short_symbol(signal.symbol)}</b> "
        f"@ {money(signal.entry)} {icon} <b>{pct(signal.pnl_pct)}</b>"
    )
    meta = f"#{signal.id} · {local_time(signal.created_at, True)}"
    if signal.r_multiple is not None:
        meta += f" · {signal.r_multiple:+.2f}R"
    return f"{head}\n   <i>{meta}</i>"


def active_list(signals: list[repo.Signal], prices: dict[str, float]) -> str:
    if not signals:
        return (
            "🎯 <b>Активные сигналы</b>\n\n"
            "<i>Сейчас активных сигналов нет.</i>\n\n"
            "Бот следит за рынком и пришлёт сообщение, "
            "как только появится подходящая точка входа."
        )
    lines = [f"🎯 <b>Активные сигналы</b> ({len(signals)})", ""]
    for s in signals:
        lines.append(signal_row(s, prices.get(s.symbol)))
        lines.append("")
    return "\n".join(lines).strip()


def history_list(signals: list[repo.Signal], title: str = "История сигналов") -> str:
    if not signals:
        return (
            f"📜 <b>{title}</b>\n\n"
            "<i>Пока пусто.</i> Здесь появятся завершённые сигналы "
            "с результатом по каждому."
        )
    lines = [f"📜 <b>{title}</b>", ""]
    for s in signals:
        lines.append(signal_row(s))
        lines.append("")
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Статистика
# --------------------------------------------------------------------------


def stats_card(data: dict, period_name: str, by_symbol: list[dict] | None = None) -> str:
    """Главный отчёт. Именно его заказчик открывает чаще всего."""
    if not data["total"] and not data["active"]:
        return (
            f"📊 <b>Статистика · {period_name}</b>\n\n"
            "<i>Пока нет завершённых сигналов.</i>\n\n"
            "Статистика появится, как только первые сигналы дойдут "
            "до цели или стопа."
        )

    winrate = data["winrate"]
    verdict = "🟢" if winrate >= 55 else ("🟡" if winrate >= 45 else "🔴")

    lines = [
        f"📊 <b>Статистика · {period_name}</b>",
        "",
        f"{verdict} <b>Точность {winrate}%</b>  <code>{bar(winrate)}</code>",
        f"<i>по {data['decided']} завершённым сигналам</i>",
        "",
        f"✅ Цель      <b>{data['wins']}</b>",
        f"❌ Стоп      <b>{data['losses']}</b>",
        f"⌛ Истекли   <b>{data['expired']}</b>",
        f"⏳ В работе  <b>{data['active']}</b>",
        "",
        f"Итог            <b>{data['total_r']:+.2f}R</b>  <i>в размерах риска</i>",
        f"В среднем       <b>{data['avg_r']:+.2f}R</b>  <i>за один сигнал</i>",
        f"Прибыль / убыток <b>{data['profit_factor']}</b>  <i>во сколько раз больше</i>",
    ]

    if data["wins"]:
        lines.append(f"Средняя прибыль <b>{pct(data['avg_win_pct'])}</b>")
    if data["losses"]:
        lines.append(f"Средний убыток  <b>{pct(data['avg_loss_pct'])}</b>")
    if data["max_loss_streak"]:
        lines.append("")
        lines.append(
            f"Лучшая серия  {data['max_win_streak']} подряд · "
            f"худшая  {data['max_loss_streak']} подряд"
        )

    if by_symbol:
        lines.append("")
        lines.append("<b>По инструментам:</b>")
        for row in by_symbol[:6]:
            lines.append(
                f"• {short_symbol(row['symbol'])} — "
                f"{row['winrate']}% ({row['wins']}/{row['wins'] + row['losses']})"
            )

    if data["decided"] < 20:
        lines.append("")
        lines.append(
            "<i>⚠️ Выборка мала — по такому числу сигналов "
            "делать выводы о стратегии рано.</i>"
        )

    return "\n".join(lines)


def daily_report(data: dict, signals_today: list[repo.Signal]) -> str:
    """Вечерняя сводка."""
    lines = [
        "🌙 <b>Итоги дня</b>",
        "",
        f"Сигналов выдано  <b>{len(signals_today)}</b>",
    ]
    if data["decided"]:
        lines += [
            f"Закрыто          <b>{data['decided']}</b>",
            f"Точность         <b>{data['winrate']}%</b>",
            f"Результат        <b>{data['total_r']:+.2f}R</b>",
        ]
    else:
        lines.append("<i>Завершённых сигналов сегодня не было.</i>")

    if signals_today:
        lines.append("")
        for s in signals_today[:8]:
            icon, _ = STATUS_VIEW.get(s.status, ("•", ""))
            side = "▲" if s.is_long else "▼"
            result = f" {pct(s.pnl_pct)}" if s.pnl_pct is not None else ""
            lines.append(
                f"{icon} {side} {short_symbol(s.symbol)} "
                f"{local_time(s.created_at)}{result}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Статус и новости
# --------------------------------------------------------------------------


def status_card(
    scanner_state: dict,
    tracker_state: dict,
    health: dict,
    prices: dict[str, float],
    uptime_sec: float,
) -> str:
    online = "🟢 в работе" if scanner_state["running"] else "🔴 остановлен"
    connected = "🟢 подключена" if health.get("connected") else "🔴 нет связи"

    hours = int(uptime_sec // 3600)
    minutes = int((uptime_sec % 3600) // 60)
    uptime = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"

    lines = [
        "ℹ️ <b>Состояние бота</b>",
        "",
        f"Сканер       {online}",
        f"Биржа        {connected} <i>({health.get('exchange', '?')}, "
        f"{health.get('latency_ms', 0)} мс)</i>",
        f"Работает     {uptime} без перерыва",
        f"Проверок     {scanner_state['scans_done']}",
        f"Последняя    {ago(scanner_state.get('last_scan_at'))}",
        "",
        f"Свечи по     {config.get('timeframe')} "
        f"(общая картина: {config.get('htf_timeframe')})",
        f"Порог        {config.get('min_confidence')}% совпавших признаков",
        f"Версия       <code>{settings.app_version}</code>",
    ]

    if prices:
        lines.append("")
        lines.append("<b>Текущие цены:</b>")
        for symbol, price in prices.items():
            lines.append(f"• {short_symbol(symbol)}  <b>{money(price)}</b>")

    muted = scanner_state.get("muted_by_news")
    if muted:
        lines.append("")
        lines.append(
            f"🔇 <b>Пауза из-за новости</b>\n"
            f"<i>{muted['title']} ({muted['currency']}), "
            f"через {muted['minutes_until']:.0f} мин</i>"
        )

    errors = scanner_state.get("last_error") or tracker_state.get("last_error")
    if errors:
        lines.append("")
        lines.append(f"⚠️ <i>Последняя ошибка: {errors[:150]}</i>")

    return "\n".join(lines)


def news_card(events: list[NewsEvent], muted_by: NewsEvent | None) -> str:
    if muted_by is not None:
        header = (
            f"🔇 <b>Сейчас пауза</b>\n"
            f"<i>{muted_by.title} ({muted_by.currency}) — "
            f"через {muted_by.minutes_from():.0f} мин</i>\n\n"
        )
    else:
        header = "📰 <b>Экономический календарь</b>\n\n"

    if not events:
        return header + "<i>Ближайших важных событий нет.</i>"

    lines = [header.rstrip(), ""]
    for e in events:
        minutes = e.minutes_from()
        when = (
            f"через {minutes:.0f} мин"
            if minutes < 120
            else local_time(e.event_at, with_date=True)
        )
        lines.append(f"🔸 <b>{e.title}</b>")
        lines.append(f"   <i>{e.currency} · {when}</i>")
    lines.append("")
    lines.append(
        f"<i>В окне −{config.get('news_mute_before_min')}/"
        f"+{config.get('news_mute_after_min')} мин вокруг таких событий "
        f"бот не выдаёт сигналы.</i>"
    )
    return "\n".join(lines)


def presets_card(current: str | None) -> str:
    """Экран выбора режима работы."""
    from app.storage.settings_store import PRESETS

    lines = [
        "🎚 <b>Режим работы</b>",
        "",
        "Это готовый набор настроек под одну цель. Нажмите — и все "
        "значения выставятся сами. Отдельные из них потом можно "
        "поправить вручную.",
        "",
    ]
    for preset in PRESETS:
        mark = " — <b>включён сейчас</b>" if current == preset.key else ""
        lines += [
            f"{preset.emoji} <b>{preset.name}</b>{mark}",
            f"{preset.summary}.",
            f"<i>{preset.detail}</i>",
            f"Ожидайте: <b>{preset.expect}</b>.",
            "",
        ]

    if current is None:
        lines.append(
            "<i>Сейчас работают ваши собственные значения — ни один "
            "готовый режим им не соответствует. Это нормально: "
            "выбирайте режим, только если хотите начать заново.</i>"
        )
    return "\n".join(lines)


def settings_card(values: dict | None = None, summary=None) -> str:
    """Показывает текущие настройки, сгруппированные как в приложении.

    Перед списком параметров идёт описание обычными словами: человеку
    важнее понять, что бот делает, чем прочитать сорок строк со
    значениями.
    """
    values = values if values is not None else config.all()

    def render(value, kind: str, unit: str) -> str:
        if kind == "bool":
            return "включено ✅" if value else "выключено ❌"
        if kind == "list":
            return ", ".join(short_symbol(v) for v in (value or [])) or "—"
        text = f"{value:g}" if isinstance(value, float) else str(value)
        return f"{text} {unit}".strip() if unit else (text or "—")

    lines = ["⚙️ <b>Настройки</b>", ""]
    if summary:
        lines.append("<b>Что происходит сейчас</b>")
        lines += [f"• {line}" for line in summary]
        lines.append("")
    for group_key, group_label in GROUPS.items():
        group_fields = [f for f in FIELDS if f.group == group_key and not f.advanced]
        if not group_fields:
            continue
        lines.append(f"<b>{group_label}</b>")
        for f in group_fields:
            shown = render(values.get(f.key), f.kind, f.unit)
            lines.append(f"  {f.label}: <code>{shown}</code>")
        lines.append("")

    lines.append(
        "<i>Не знаете, что выставить, — нажмите «Выбрать режим работы»: "
        "всё настроится одним нажатием. Отдельные значения удобнее "
        "менять в приложении, там у каждой строки есть пояснение. "
        "Применяется сразу, перезапуск не нужен.</i>"
    )
    return "\n".join(lines)


def help_menu() -> str:
    """Стартовый экран справки со списком тем."""
    from app import help as help_content

    lines = [
        "❓ <b>Помощь</b>",
        "",
        "Если вы здесь впервые, читайте первые четыре темы подряд — "
        "это пять минут, и станет понятно, что вообще происходит.",
        "",
        "Каждая тема открывается отдельным сообщением. "
        "Та же справка есть в приложении.",
        "",
    ]
    for topic in help_content.topic_list():
        lines.append(f"{topic['icon']} <b>{topic['title']}</b>")
        lines.append(f"    <i>{topic['summary']}</i>")
    return "\n".join(lines)


def help_card() -> str:
    return "\n".join(
        [
            "🤖 <b>Бот торговых сигналов</b>",
            "",
            "Анализирует рынок и присылает точки входа со стопом, "
            "целью и объяснением, почему сигнал возник.",
            "",
            "<b>Команды:</b>",
            "/signals — активные сигналы",
            "/history — последние завершённые",
            "/stats — статистика и winrate",
            "/news — экономический календарь",
            "/status — состояние бота и цены",
            "/settings — уведомления",
            "/help — эта справка",
            "",
            "<b>Как читать сигнал:</b>",
            "• <b>Вход</b> — цена на момент сигнала",
            "• <b>Стоп</b> — где признать идею неверной",
            "• <b>Цель</b> — куда фиксировать прибыль",
            "• <b>Уверенность</b> — сколько подтверждений совпало",
            "",
            "<i>Бот не торгует и не имеет доступа к вашим счетам. "
            "Он только присылает информацию — все решения ваши.</i>",
        ]
    )


# --------------------------------------------------------------------------
# Уведомления по цене
# --------------------------------------------------------------------------


async def market_snapshot(symbol: str) -> dict:
    """Короткая сводка по инструменту: что с ценой за сутки.

    Нужна, чтобы уведомление не состояло из одного числа. Человек,
    которому написали «дошло до 95 000», сразу хочет знать: это рывок
    или оно тут весь день топчется.
    """
    from app.market.feed import feed

    out: dict = {}
    try:
        candles = await feed.fetch_candles(symbol, "1h", limit=25)
    except Exception:
        return out
    if len(candles) < 2:
        return out

    closes = candles.closes
    highs = candles.highs
    lows = candles.lows
    out["day_open"] = closes[0]
    out["day_high"] = max(highs)
    out["day_low"] = min(lows)
    out["last"] = closes[-1]
    if closes[0]:
        out["day_change_pct"] = (closes[-1] - closes[0]) / closes[0] * 100

    # Где сейчас цена внутри суточного размаха: 0% — на дне, 100% — на пике
    span = out["day_high"] - out["day_low"]
    if span > 0:
        out["day_position"] = (closes[-1] - out["day_low"]) / span * 100
    return out


def _waited(seconds: int) -> str:
    """Сколько уведомление прождало своего часа."""
    if seconds < 90:
        return "меньше минуты"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 24:
        rest = minutes % 60
        return f"{hours} ч" + (f" {rest} мин" if rest else "")
    days = hours // 24
    return f"{days} дн" + (f" {hours % 24} ч" if hours % 24 else "")


def alert_card(alert: repo.Alert, price: float, snapshot: dict | None = None) -> str:
    """Сообщение о том, что цена дошла до заказанного уровня."""
    snapshot = snapshot or {}
    name = short_symbol(alert.symbol)

    if alert.percent is not None:
        # Человек заказывал движение, а не цену: так ему и говорим,
        # иначе он увидит незнакомое число и будет гадать, откуда оно
        move = "вырос" if alert.direction == repo.UP else "упал"
        head = f"🔔 <b>{name} {move} на {alert.percent:g}%</b>"
    else:
        side = "поднялся до" if alert.direction == repo.UP else "опустился до"
        head = f"🔔 <b>{name} {side} {money(alert.price)}</b>"

    lines = [
        head,
        "",
        f"Сейчас: <b>{money(price)}</b>",
    ]

    if alert.start_price:
        delta = price - alert.start_price
        delta_pct = delta / alert.start_price * 100 if alert.start_price else 0
        arrow = "▲" if delta >= 0 else "▼"
        lines.append(
            f"С момента заказа: {arrow} {money(abs(delta))} "
            f"({pct(delta_pct, True)}) — было {money(alert.start_price)}"
        )

    if "day_change_pct" in snapshot:
        change = snapshot["day_change_pct"]
        mood = "растёт" if change > 0.15 else ("падает" if change < -0.15 else "стоит на месте")
        lines.append(f"За сутки {mood}: {pct(change, True)}")

    if "day_high" in snapshot and "day_low" in snapshot:
        lines.append(
            f"Размах за сутки: {money(snapshot['day_low'])} — "
            f"{money(snapshot['day_high'])}"
        )
        position = snapshot.get("day_position")
        if position is not None:
            if position >= 80:
                where = "у самого верха дневного диапазона"
            elif position <= 20:
                where = "у самого низа дневного диапазона"
            else:
                where = "в середине дневного диапазона"
            lines.append(f"Цена {where}")

    waited = int(time.time()) - alert.created_at
    lines += [
        "",
        f"⏱ Уведомление ждало {_waited(waited)} — вы заказали его "
        f"{local_time(alert.created_at, True)}.",
    ]
    if alert.note:
        lines.append(f"📝 Ваша заметка: <i>{alert.note}</i>")

    lines += [
        "",
        "<i>Это не сигнал на сделку: бот сообщил ровно о том, "
        "о чём вы просили. Решение — за вами.</i>",
    ]
    if alert.repeat:
        lines.append(
            "<i>Уведомление повторяющееся: сообщу снова, когда цена "
            "вернётся к этому уровню с другой стороны.</i>"
        )
    return "\n".join(lines)


def alerts_list_card(alerts: list[repo.Alert], prices: dict | None = None) -> str:
    """Список заказанных уровней."""
    prices = prices or {}
    if not alerts:
        return (
            "🔔 <b>Уведомления по цене</b>\n\n"
            "Пока ни одного. Это простая вещь: вы называете цену — "
            "бот пишет, когда рынок до неё дошёл.\n\n"
            "Просто отправьте сообщение вида:\n"
            "<code>биткоин 95000</code> — сообщу при этой цене\n"
            "<code>биткоин -1.5%</code> — если упадёт на столько\n"
            "<code>биткоин +2%</code> — если вырастет\n"
            "<code>биткоин 2%</code> — если сдвинется в любую сторону\n"
            "<code>BTC 95000 продать половину</code>\n\n"
            "Всё, что напишете после числа, станет заметкой для себя — "
            "она вернётся вместе с уведомлением."
        )

    lines = ["🔔 <b>Уведомления по цене</b>", ""]
    for alert in alerts:
        name = short_symbol(alert.symbol)
        now = prices.get(alert.symbol)
        arrow = "выше" if alert.direction == repo.UP else "ниже"
        if alert.percent is not None:
            move = "рост" if alert.direction == repo.UP else "падение"
            line = (
                f"• <b>{name}</b> — {move} на {alert.percent:g}% "
                f"(это {money(alert.price)})"
            )
        else:
            line = f"• <b>{name}</b> — сообщить при {money(alert.price)} ({arrow})"
        if now:
            distance = abs(now - alert.price) / now * 100 if now else 0
            line += f"\n  сейчас {money(now)}, осталось {distance:.2f}%"
        if alert.note:
            line += f"\n  📝 {alert.note}"
        lines.append(line)

    lines += [
        "",
        "<i>Чтобы добавить ещё — отправьте сообщение вида "
        "«биткоин 95000» или «биткоин -1.5%». "
        "Чтобы убрать — кнопкой ниже.</i>",
    ]
    return "\n".join(lines)
