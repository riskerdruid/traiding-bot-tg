"""Сквозная проверка приложения без Telegram и без сети.

Поднимает базу во временном файле, создаёт сигналы, прогоняет их через
трекер, считает статистику, рендерит каждое сообщение бота и проверяет,
что все кнопки ведут в существующие обработчики.

Этот файл — то, что автодеплой гоняет в новом образе перед тем, как
подменить работающего бота. Падает он — обновление не устанавливается.

Запуск:  python -m tests.test_smoke
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
import tempfile
import time

# База для тестов — до импорта настроек
_TMP_DB = os.path.join(tempfile.gettempdir(), f"signals_test_{os.getpid()}.db")
os.environ["DB_PATH"] = _TMP_DB
os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_IDS"] = "42"
os.environ["HEARTBEAT_PATH"] = os.path.join(
    tempfile.gettempdir(), f"heartbeat_test_{os.getpid()}"
)
# Приложение проверяем без подписи Telegram — её саму проверяем отдельно
os.environ["WEBAPP_DEV_MODE"] = "true"

from app import help as help_content  # noqa: E402
from app.bot import alerts_input  # noqa: E402
from app.bot import formatters as fmt  # noqa: E402
from app.bot import keyboards as kb  # noqa: E402
from app.engine.tracker import Tracker  # noqa: E402
from app.market import catalog  # noqa: E402
from app.market.feed import Candle, Candles  # noqa: E402
from app.storage import repo  # noqa: E402
from app.storage.db import db  # noqa: E402
from app.storage.settings_store import (  # noqa: E402
    FIELDS,
    PRESETS,
    ValidationError,
    config,
)
from app.strategy.trend_momentum import strategy  # noqa: E402

failures: list[str] = []

# Слова, которых человек, впервые открывший бота, знать не обязан.
# Если они всплыли в тексте сообщения — текст надо переписать.
JARGON = re.compile(
    r"\b(EMA\d*|ADX|ATR|RSI|MACD|winrate|профит-фактор|стохастик|таймфрейм|"
    r"экспирация|SSID|drawdown|R-multiple)\b",
    re.IGNORECASE,
)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def check_text(name: str, text: str) -> None:
    """Общие требования к любому сообщению бота."""
    check(f"{name}: не пусто", len(text) > 30, text[:60])
    check(f"{name}: теги сбалансированы", text.count("<b>") == text.count("</b>"))
    check(f"{name}: курсив закрыт", text.count("<i>") == text.count("</i>"))
    found = JARGON.findall(text)
    check(f"{name}: без жаргона", not found, str(found[:4]))


def sample_signal(**kwargs) -> repo.Signal:
    base = dict(
        id=1, symbol="XAU/USDT:USDT", side="LONG", timeframe="15m",
        entry=4300.0, stop_loss=4290.0, take_profit=4318.0, confidence=78,
        reasons=[
            "Цена развернулась вверх",
            "Движение уверенное, а не топтание на месте",
        ],
        indicators={"rsi": 56.2, "adx": 27.1},
        status=repo.ACTIVE, created_at=int(time.time()), owner_id=42,
    )
    base.update(kwargs)
    return repo.Signal(**base)


# --------------------------------------------------------------------------


async def test_database() -> None:
    print("База данных")
    await db.init()
    check("схема создана", True)

    signal = await repo.create_signal(
        symbol="XAU/USDT:USDT", side="LONG", timeframe="15m",
        entry=4300.0, stop_loss=4290.0, take_profit=4318.0, confidence=78,
        reasons=["Цена развернулась вверх"], indicators={"rsi": 56.2},
        owner_id=42,
    )
    check("сигнал создан", signal.id > 0, f"id={signal.id}")
    check("причины сохранились", signal.reasons == ["Цена развернулась вверх"])
    check("индикаторы сохранились", signal.indicators["rsi"] == 56.2)
    check("статус ACTIVE", signal.status == repo.ACTIVE)
    check("риск посчитан", abs(signal.risk_abs - 10.0) < 1e-9)

    check("активные читаются", len(await repo.get_active_signals()) == 1)
    fetched = await repo.get_signal(signal.id)
    check("сигнал читается по id", fetched is not None and fetched.id == signal.id)
    check("время последнего сигнала", await repo.last_signal_at("XAU/USDT:USDT"))

    check(
        "текущий результат для покупки",
        abs(signal.unrealized_pct(4343.0) - 1.0) < 0.01,
    )
    short = sample_signal(side="SHORT", entry=100.0, stop_loss=102.0, take_profit=96.0)
    check("текущий результат для продажи", abs(short.unrealized_pct(99.0) - 1.0) < 1e-9)


async def test_outcomes_and_stats() -> None:
    print("Исходы и статистика")

    signals = await repo.get_active_signals()
    won = await repo.close_signal(signals[0].id, repo.TP_HIT, 4318.0)
    check("сигнал закрыт", won.status == repo.TP_HIT)
    check("результат в процентах", won.pnl_pct > 0, f"={won.pnl_pct}")
    check("результат в рисках = 1.8", abs(won.r_multiple - 1.8) < 0.01)

    again = await repo.close_signal(won.id, repo.SL_HIT, 4290.0)
    check("повторное закрытие игнорируется", again.status == repo.TP_HIT)

    # 3 победы и 2 поражения. Риск везде 5, прибыль 9 — ровно 1.8R.
    for i in range(5):
        is_long = i % 2 == 0
        s = await repo.create_signal(
            symbol="XAU/USDT:USDT" if is_long else "BTC/USDT:USDT",
            side="LONG" if is_long else "SHORT",
            timeframe="15m", entry=100.0,
            stop_loss=95.0 if is_long else 105.0,
            take_profit=109.0 if is_long else 91.0,
            confidence=70 + i, reasons=["тест"], indicators={}, owner_id=42,
        )
        await repo.close_signal(
            s.id,
            repo.TP_HIT if i < 3 else repo.SL_HIT,
            (109.0 if is_long else 91.0) if i < 3 else (95.0 if is_long else 105.0),
        )

    data = await repo.stats()
    check("всего закрытых = 6", data["total"] == 6, f"={data['total']}")
    check("побед 4", data["wins"] == 4, f"={data['wins']}")
    check("поражений 2", data["losses"] == 2, f"={data['losses']}")
    check("угадано 66.7%", abs(data["winrate"] - 66.7) < 0.1, f"={data['winrate']}")
    check("итог в рисках = 5.2", abs(data["total_r"] - 5.2) < 0.05, f"={data['total_r']}")
    check(
        "прибыль/убыток = 3.6",
        abs(data["profit_factor"] - 3.6) < 0.05,
        f"={data['profit_factor']}",
    )

    by_symbol = await repo.stats_by_symbol()
    check("разбивка по инструментам", len(by_symbol) == 2, f"={len(by_symbol)}")

    # Чужие сигналы в статистику не попадают
    other = await repo.create_signal(
        symbol="ETH/USDT:USDT", side="LONG", timeframe="15m", entry=10.0,
        stop_loss=9.0, take_profit=12.0, confidence=60, reasons=[],
        indicators={}, owner_id=999,
    )
    await repo.close_signal(other.id, repo.TP_HIT, 12.0)
    mine = await repo.stats(owner_id=42)
    check("статистика только по своим", mine["wins"] == 4, f"={mine['wins']}")


async def test_tracker_logic() -> None:
    print("Логика трекера")
    tracker = Tracker()
    signal = sample_signal(id=999, entry=100.0, stop_loss=95.0, take_profit=110.0)

    def candle(high: float, low: float) -> Candle:
        return Candle(ts=0, open=100, high=high, low=low, close=100, volume=1)

    check(
        "цель засчитана",
        tracker._detect_outcome(signal, [candle(111, 99)]) == (repo.TP_HIT, 110.0),
    )
    check(
        "стоп засчитан",
        tracker._detect_outcome(signal, [candle(101, 94)]) == (repo.SL_HIT, 95.0),
    )
    check("ничего не задето", tracker._detect_outcome(signal, [candle(105, 97)]) is None)
    # Обе цели в одной свече — засчитываем стоп (пессимистично и честно)
    check(
        "при двойном касании — стоп",
        tracker._detect_outcome(signal, [candle(111, 94)]) == (repo.SL_HIT, 95.0),
    )

    short = sample_signal(
        id=998, side="SHORT", entry=100.0, stop_loss=105.0, take_profit=90.0
    )
    check(
        "продажа: цель засчитана",
        tracker._detect_outcome(short, [candle(101, 89)]) == (repo.TP_HIT, 90.0),
    )
    check(
        "продажа: стоп засчитан",
        tracker._detect_outcome(short, [candle(106, 99)]) == (repo.SL_HIT, 105.0),
    )


async def test_strategy() -> None:
    print("Стратегия")
    await config.load()

    needed = strategy.min_candles()
    check(
        "прогрев укладывается в 150 свечей брокера опционов",
        needed <= 150,
        f"нужно {needed}",
    )
    check("прогрев не выродился в ноль", needed >= 50, f"={needed}")

    items = []
    price = 100.0
    for i in range(400):
        price *= 1.002 if i % 9 else 0.998
        items.append(
            Candle(ts=i * 900_000, open=price, high=price * 1.001,
                   low=price * 0.999, close=price, volume=1000)
        )
    candles = Candles("TEST/USDT", "15m", items)
    result = strategy.analyze(candles, candles)
    check("стратегия не падает на синтетике", True)
    if result:
        check("сторона определена", result.side in ("LONG", "SHORT"))
        check("уверенность в 0..100", 0 <= result.confidence <= 100)
        check(
            "стоп с правильной стороны",
            (result.stop_loss < result.entry) if result.side == "LONG"
            else (result.stop_loss > result.entry),
        )
        check(
            "цель с правильной стороны",
            (result.take_profit > result.entry) if result.side == "LONG"
            else (result.take_profit < result.entry),
        )
        check("соотношение риск/прибыль выдержано", abs(result.rr - 1.8) < 0.01)
        jargon = [r for r in result.reasons if JARGON.search(r)]
        check("причины написаны словами, а не формулами", not jargon, str(jargon))

    tiny = Candles("TEST/USDT", "15m", items[:10])
    check("короткий ряд не ломает стратегию", strategy.analyze(tiny, tiny) is None)


async def test_settings() -> None:
    print("Настройки")
    await config.load()

    check("значения по умолчанию есть", config.get("deposit") == 1000.0)
    check("инструменты списком", isinstance(config.get("symbols"), list))
    check("ночной режим включён по умолчанию", config.get("quiet_night") is True)

    await config.set("min_confidence", 75)
    check("число сохраняется", config.get("min_confidence") == 75)

    for bad in (("min_confidence", 500), ("min_confidence", "много"), ("symbols", [])):
        try:
            await config.set(*bad)
            check(f"негодное значение {bad[0]}={bad[1]!r} отклонено", False)
        except ValidationError:
            check(f"негодное значение {bad[0]}={bad[1]!r} отклонено", True)

    try:
        await config.set("htf_timeframe", "5m")
        check("общая картина не может быть мельче рабочей", False)
    except ValidationError:
        check("общая картина не может быть мельче рабочей", True)

    try:
        await config.set("выдуманный", 1)
        check("неизвестный параметр отклонён", False)
    except ValidationError:
        check("неизвестный параметр отклонён", True)

    # Ночная тишина
    await config.set("quiet_night", True)
    check("ночью молчим", config.in_quiet_hours("02:30"))
    check("вечером до 23:00 пишем", not config.in_quiet_hours("22:59"))
    check("днём пишем", not config.in_quiet_hours("14:00"))
    await config.set("quiet_night", False)
    check("выключенный ночной режим не глушит", not config.in_quiet_hours("03:00"))
    await config.set("quiet_night", True)

    # Деньги
    await config.set("deposit", 2000.0)
    await config.set("risk_per_trade", 1.0)
    check("риск в деньгах", abs(config.risk_money() - 20.0) < 1e-9)

    # У каждого свои настройки
    alice, bob = config.view(777001), config.view(777002)
    await alice.set("deposit", 5000)
    await bob.set("deposit", 300)
    check(
        "настройки не смешиваются",
        (alice.get("deposit"), bob.get("deposit")) == (5000.0, 300.0),
    )
    await config.load()
    check(
        "и переживают перезагрузку",
        (config.view(777001).get("deposit"), config.view(777002).get("deposit"))
        == (5000.0, 300.0),
    )
    check(
        "нетронутые параметры берутся из общих",
        config.view(777001).get("min_confidence") == config.get("min_confidence"),
    )

    # Режимы
    for preset in PRESETS:
        unknown = [k for k in preset.values if k not in FIELDS]
        check(f"режим «{preset.name}»: параметры существуют", not unknown, str(unknown))
        applied = await alice.apply_preset(preset.key)
        check(f"режим «{preset.name}» применяется", applied.key == preset.key)
        check(
            f"режим «{preset.name}» распознаётся обратно",
            alice.current_preset() == preset.key,
            str(alice.current_preset()),
        )
    check("режим всегда есть, даже если значения свои", config.preset() is not None)

    try:
        await alice.apply_preset("выдуманный")
        check("неизвестный режим отклонён", False)
    except ValidationError:
        check("неизвестный режим отклонён", True)


async def test_messages() -> None:
    print("Сообщения бота")
    cfg = config.view(42)
    await cfg.set("deposit", 1000.0)
    await cfg.set("risk_per_trade", 1.0)

    active = sample_signal()
    card = fmt.signal_card(active)
    check_text("сигнал", card)
    check("сигнал: есть цена входа", "4 300" in card, card[:80])
    check("сигнал: сказано, когда выходить", "выходить" in card)
    check("сигнал: сказано, когда забирать", "забирать" in card)
    check("сигнал: посчитан объём", "потеряете" in card, card)

    binary = sample_signal(
        id=2, symbol="po:EURUSD_otc", kind="binary", broker="po",
        entry=1.0812, stop_loss=1.0812, take_profit=1.0812, payout=92.0,
        expiry_at=int(time.time()) + 300,
    )
    binary_card = fmt.signal_card(binary)
    check_text("сигнал по опциону", binary_card)
    check("опцион: сказано про срок", "Итог через" in binary_card)
    check("опцион: сказано про ставку", "поставить" in binary_card.lower())
    check("опцион: имя пары читаемое", "EUR/USD" in binary_card, binary_card[:60])

    closed = sample_signal(
        status=repo.TP_HIT, exit_price=4318.0, pnl_pct=0.42, r_multiple=1.8,
        closed_at=int(time.time()) + 3600,
    )
    outcome = fmt.outcome_card(closed)
    check_text("исход", outcome)
    check("исход: результат в деньгах", "$" in outcome, outcome)

    lost = sample_signal(
        id=3, status=repo.SL_HIT, exit_price=4290.0, pnl_pct=-0.23, r_multiple=-1.0,
        closed_at=int(time.time()) + 600,
    )
    check_text("исход в минус", fmt.outcome_card(lost))

    state = {
        "running": True, "symbols": ["XAU/USDT:USDT"],
        "last_scan_at": int(time.time()) - 30, "muted_by_news": None,
    }
    check_text("экран сигналов", fmt.signals_screen([active], [closed], {}, state))
    empty = fmt.signals_screen([], [], {}, state)
    check_text("экран сигналов без сигналов", empty)
    check("пустой экран объясняет молчание", "нормально" in empty)
    check(
        "виден признак жизни",
        "проверен" in fmt.signals_screen([], [], {}, state),
    )
    muted = dict(state, muted_by_news="Ставка ФРС")
    check("видно паузу из-за новости", "молчу" in fmt.signals_screen([], [], {}, muted))
    stopped = fmt.signals_screen([], [], {}, {"running": False})
    check("видно, что бот не работает", "не следит" in stopped)

    data = await repo.stats(owner_id=42)
    by_symbol = await repo.stats_by_symbol(owner_id=42)
    stats_text = fmt.stats_card(data, by_symbol, 10.0)
    check_text("результаты", stats_text)
    check("результаты: сказано, сколько угадано", "Угадано" in stats_text)
    check("результаты: есть пересчёт в деньги", "$" in stats_text)
    check_text("пустые результаты", fmt.stats_card(
        {"decided": 0, "active": 0, "wins": 0, "losses": 0, "expired": 0,
         "winrate": 0.0, "total_r": 0.0}, [], 10.0))

    signals = await repo.list_signals(limit=5, owner_id=42)
    check_text("итоги дня", fmt.daily_report(data, signals))

    check_text("настройки", fmt.settings_card(cfg))
    check_text("выбор режима", fmt.presets_card("balanced"))
    check_text("деньги", fmt.money_card(cfg))
    check_text("подключение опционов", fmt.pocket_card(False))
    check_text("приветствие", fmt.greeting("Иван", cfg, 1, 6, 66.7))
    check_text(
        "список инструментов",
        fmt.symbols_card([catalog.Choice("XAU/USDT:USDT", "Золото", "биржа", True)]),
    )
    check_text("образец сигнала", fmt.test_signal_card("XAU/USDT:USDT", 4300.0, False, cfg))

    check("цена крупная", fmt.money(4300.12345) == "4 300.12", fmt.money(4300.12345))
    check("цена мелкая", fmt.money(0.00012345) == "0.000123", fmt.money(0.00012345))
    check("цены нет", fmt.money(None) == "—")
    check("процент со знаком", fmt.pct(1.5) == "+1.50%")
    check("шкала заполняется", fmt.bar(50) == "█████░░░░░", fmt.bar(50))
    check("шкала на 100", fmt.bar(100) == "█" * 10)


def test_names() -> None:
    print("Имена инструментов")
    cases = {
        "XAU/USDT:USDT": "Золото",
        "BTC/USDT:USDT": "Биткоин",
        "po:EURUSD_otc": "EUR/USD",
        "po:GBPJPY": "GBP/JPY",
    }
    for symbol, expected in cases.items():
        check(f"{symbol} -> {expected}", catalog.pretty(symbol) == expected,
              catalog.pretty(symbol))
    check(
        "неизвестный инструмент не ломает вывод",
        catalog.pretty("ABC/USDT:USDT") == "ABC/USDT",
        catalog.pretty("ABC/USDT:USDT"),
    )

    # Один и тот же актив в разных парах не должен давать две
    # неотличимые строки в списке выбора
    shown = [catalog.Choice("BTC/USDT:USDT", "Биткоин", "биржа", False)]
    check(
        "совпавшее имя дополняется тикером",
        catalog.unique_title("BTC/EUR", shown) == "Биткоин · BTC/EUR",
        catalog.unique_title("BTC/EUR", shown),
    )
    check(
        "уникальное имя остаётся коротким",
        catalog.unique_title("XAU/USDT:USDT", shown) == "Золото",
        catalog.unique_title("XAU/USDT:USDT", shown),
    )


def test_help() -> None:
    print("Справка")
    topics = help_content.topic_list()
    check("темы есть", len(topics) >= 4, f"={len(topics)}")
    check("тем немного", len(topics) <= 8, f"={len(topics)}")
    for topic in topics:
        text = help_content.render_topic_text(topic["id"])
        check(f"тема «{topic['title']}» рендерится", bool(text and len(text) > 200))
        if text:
            check(
                f"тема «{topic['title']}»: теги сбалансированы",
                text.count("<b>") == text.count("</b>"),
            )
    check("несуществующая тема -> None", help_content.render_topic_text("нет") is None)
    check_text("меню справки", help_content.menu_text())


def test_buttons() -> None:
    """Каждая кнопка должна вести в существующий обработчик.

    Самая обидная поломка в кнопочном боте — нажатие, на которое ничего
    не происходит: пользователь видит вечные часики и решает, что бот
    сломался. Ловим это здесь, а не в переписке с заказчиком.
    """
    print("Кнопки и обработчики")
    import pathlib

    source = pathlib.Path("app/bot/handlers.py").read_text(encoding="utf-8")
    exact = set(re.findall(r'F\.data == "([^"]+)"', source))
    prefixes = set(re.findall(r'F\.data\.startswith\("([^"]+)"\)', source))
    texts = set(re.findall(r"F\.text == kb\.(\w+)", source))

    markups = [
        kb.signals_screen(),
        kb.settings_screen(True),
        kb.symbols_screen(
            [catalog.Choice("XAU/USDT:USDT", "Золото", "биржа", True),
             catalog.Choice("po:EURUSD_otc", "EUR/USD", "опционы", False)],
            True,
        ),
        kb.presets_screen("balanced"),
        kb.money_screen(1000.0, 1.0),
        kb.pocket_screen(True),
        kb.pocket_screen(False),
        kb.help_menu(),
        kb.help_topic(),
        kb.cancel(),
    ]

    seen = 0
    for markup in markups:
        for row in markup.inline_keyboard:
            for button in row:
                data = button.callback_data
                seen += 1
                handled = data in exact or any(data.startswith(p) for p in prefixes)
                check(f"кнопка «{button.text}» -> {data}", handled)
                check(
                    f"кнопка «{button.text}»: данные влезают в лимит",
                    len(data.encode()) <= 64,
                    f"{len(data.encode())} байт",
                )
    check("кнопки вообще нашлись", seen > 15, f"={seen}")

    labels = {"BTN_SIGNALS", "BTN_STATS", "BTN_ALERTS", "BTN_SETTINGS", "BTN_HELP"}
    check("все постоянные кнопки обработаны", labels <= texts, str(labels - texts))

    # Кнопка приложения обработчика не имеет — её нажатие Telegram
    # перехватывает сам и открывает окно, сообщения боту не приходит
    menu_buttons = {
        b.text for row in kb.main_menu().keyboard for b in row if not b.web_app
    }
    check(
        "подписи постоянных кнопок совпадают",
        menu_buttons == {getattr(kb, name) for name in labels},
        str(menu_buttons),
    )


async def test_alerts() -> None:
    """Будильник по цене: разбор фразы, хранение, срабатывание."""
    print("Уведомления по цене")

    # --- что человек написал ---
    cases = {
        "биткоин 95000": ("BTC", 95000.0, None, None),
        "золото -1.5%": ("XAU", None, 1.5, "down"),
        "биткоин +2%": ("BTC", None, 2.0, "up"),
        "биткоин 2%": ("BTC", None, 2.0, None),
        "уведоми когда биткоин будет 95000": ("BTC", 95000.0, None, None),
        "BTC 95 000 продать половину": ("BTC", 95000.0, None, None),
    }
    for text, (ticker, price, percent, direction) in cases.items():
        parsed = alerts_input.parse_request(text)
        ok = (
            parsed is not None
            and parsed.price == price
            and parsed.percent == percent
            and parsed.direction == direction
            and alerts_input.base_of(
                alerts_input.resolve_any(parsed.names, []) or ""
            ) == ticker
        )
        check(f"понял «{text}»", ok, str(parsed))

    check("в «просто текст» числа нет", alerts_input.parse_request("просто текст") is None)
    check(
        "заметка сохраняется",
        (alerts_input.parse_request("BTC 95000 продать половину") or
         alerts_input.Request([])).note == "продать половину",
    )
    check(
        "слова-паразиты не считаются инструментом",
        alerts_input.resolve_any(["когда", "будет", "биткоин"], []) == "BTC/USDT:USDT",
        str(alerts_input.resolve_any(["когда", "будет", "биткоин"], [])),
    )
    check(
        "свой инструмент важнее популярного",
        alerts_input.resolve_any(["биткоин"], ["BTC/EUR"]) == "BTC/EUR",
    )

    # --- если инструмент уже выбран кнопкой ---
    value = alerts_input.parse_value("-1.5% на всякий случай")
    check(
        "значение без названия разбирается",
        value is not None and value.percent == 1.5 and value.direction == "down",
        str(value),
    )

    # --- хранение ---
    uid = 555001
    by_price = await repo.create_alert(
        owner_id=uid, symbol="BTC/USDT:USDT", price=95000.0, start_price=90000.0,
        note="продать половину",
    )
    check("уровень по цене создан", by_price.id > 0)
    check("сторону определили сами", by_price.direction == repo.UP, by_price.direction)
    check("заметка на месте", by_price.note == "продать половину")

    by_percent = await repo.create_alert(
        owner_id=uid, symbol="BTC/USDT:USDT", percent=1.0,
        direction=repo.DOWN, start_price=90000.0,
    )
    check(
        "падение на 1% превратилось в цену",
        abs(by_percent.price - 89100.0) < 1e-6,
        str(by_percent.price),
    )

    up, down = await repo.create_alert_pair(
        owner_id=uid, symbol="XAU/USDT:USDT", percent=2.0, start_price=4000.0,
    )
    check("движение в любую сторону — это два уровня",
          abs(up.price - 4080.0) < 1e-6 and abs(down.price - 3920.0) < 1e-6,
          f"{up.price} / {down.price}")

    check("рост срабатывает сверху", by_price.reached(95001.0))
    check("рост не срабатывает снизу", not by_price.reached(94999.0))
    check("падение срабатывает снизу", by_percent.reached(89000.0))
    check("падение не срабатывает сверху", not by_percent.reached(89200.0))

    mine = await repo.list_alerts(owner_id=uid)
    check("свои уровни читаются", len(mine) == 4, f"={len(mine)}")
    check(
        "чужие уровни не видны",
        not await repo.list_alerts(owner_id=999999),
    )

    fired = await repo.trigger_alert(by_price.id, 95100.0)
    check("сработавший гаснет", fired is not None and fired.status == repo.ALERT_DONE)
    check(
        "сработавший уходит из списка",
        len(await repo.list_alerts(owner_id=uid)) == 3,
    )
    check("повторно не срабатывает", await repo.trigger_alert(by_price.id, 95100.0) is None)

    check("снятие работает", await repo.cancel_alert(by_percent.id, owner_id=uid))
    check(
        "чужое не снять",
        not await repo.cancel_alert(up.id, owner_id=999999),
    )
    left = await repo.cancel_all_alerts(uid)
    check("снять все разом", left == 2, f"={left}")
    check("после уборки пусто", not await repo.list_alerts(owner_id=uid))


    # --- сторож: от заказанного уровня до сообщения человеку ---
    from app.engine.alerts import AlertWatcher
    from app.market.feed import feed as market

    watcher = AlertWatcher()
    delivered: list = []

    async def on_hit(alert, price):
        delivered.append((alert.id, price))

    watcher.on_hit = on_hit

    waiting = await repo.create_alert(
        owner_id=uid, symbol="BTC/USDT:USDT", price=95000.0, start_price=90000.0,
    )
    far = await repo.create_alert(
        owner_id=uid, symbol="BTC/USDT:USDT", price=200000.0, start_price=90000.0,
    )

    async def cheap(symbols):
        return {s: 94000.0 for s in symbols}

    async def expensive(symbols):
        return {s: 95500.0 for s in symbols}

    real = market.fetch_prices
    try:
        market.fetch_prices = cheap
        fired = await watcher.check_once()
        check("до уровня не дошло — молчим", not fired and not delivered)
        check("но сторож считает, за чем следит", watcher.watching == 2, str(watcher.watching))

        market.fetch_prices = expensive
        fired = await watcher.check_once()
        check("уровень сработал", len(fired) == 1 and fired[0].id == waiting.id, str(fired))
        check("человеку отправлено", delivered == [(waiting.id, 95500.0)], str(delivered))

        fired = await watcher.check_once()
        check("второй раз то же самое не шлём", not fired, str(fired))
        check("далёкий уровень остался ждать", far.id in {
            a.id for a in await repo.list_alerts(owner_id=uid)
        })
    finally:
        market.fetch_prices = real
    await repo.cancel_all_alerts(uid)

    # --- тексты ---
    sample = await repo.create_alert(
        owner_id=42, symbol="BTC/USDT:USDT", price=95000.0, start_price=90000.0,
        note="продать половину",
    )
    check_text("список уведомлений", fmt.alerts_card([sample], {"BTC/USDT:USDT": 91000.0}))
    check_text("пустой список уведомлений", fmt.alerts_card([]))
    check_text("подтверждение уровня", fmt.alert_created(sample, 90000.0))
    check_text("сработавшее уведомление", fmt.alert_fired(sample, 95100.0))
    check_text("вопрос о цене", fmt.alert_ask_value("BTC/USDT:USDT", 90000.0))
    pair_up, pair_down = await repo.create_alert_pair(
        owner_id=42, symbol="BTC/USDT:USDT", percent=2.0, start_price=90000.0,
    )
    check_text("оба уровня разом", fmt.alert_pair_created(pair_up, pair_down, 90000.0, 2.0))
    check(
        "в уведомлении сказано, что это не сигнал",
        "не сигнал" in fmt.alert_fired(sample, 95100.0),
    )
    await repo.cancel_all_alerts(42)


async def test_webapp() -> None:
    """Мини-приложение: подпись Telegram и все его запросы."""
    print("Мини-приложение")
    import hashlib
    import hmac
    from urllib.parse import urlencode

    from fastapi.testclient import TestClient

    from app.api import server as api_server
    from app.api.auth import verify_init_data
    from app.market.feed import feed

    # --- подпись ---
    token = "123456:TEST"
    payload = {"auth_date": str(int(time.time())), "query_id": "x", "user": '{"id":42}'}
    check_string = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    good = urlencode({**payload, "hash": signature})

    check("правильная подпись принимается", verify_init_data(good, token) is not None)
    check(
        "подделанная подпись отвергается",
        verify_init_data(urlencode({**payload, "hash": "0" * 64}), token) is None,
    )
    check("пустая строка отвергается", verify_init_data("", token) is None)
    check(
        "чужой токен не подходит",
        verify_init_data(good, "999:OTHER") is None,
    )
    old = {**payload, "auth_date": str(int(time.time()) - 200000)}
    old_string = "\n".join(f"{k}={old[k]}" for k in sorted(old))
    old_hash = hmac.new(secret, old_string.encode(), hashlib.sha256).hexdigest()
    check(
        "просроченная подпись отвергается",
        verify_init_data(urlencode({**old, "hash": old_hash}), token) is None,
    )

    # --- сами запросы; биржу подменяем, тест не должен зависеть от сети ---
    async def fake_price(symbol: str) -> float:
        return 90000.0

    async def fake_prices(symbols: list[str]) -> dict:
        return {s: 90000.0 for s in symbols}

    real_price, real_prices = feed.fetch_price, feed.fetch_prices
    feed.fetch_price, feed.fetch_prices = fake_price, fake_prices
    try:
        with TestClient(api_server.create_app()) as client:
            r = client.get("/api/overview")
            check("экран приложения отдаётся", r.status_code == 200, f"={r.status_code}")
            body = r.json()
            for key in ("pulse", "active", "recent", "stats", "alerts", "settings"):
                check(f"в ответе есть «{key}»", key in body)
            check(
                "настройки приложения совпадают с ботом",
                body["settings"]["deposit"] == config.view(42).get("deposit"),
                str(body["settings"]["deposit"]),
            )

            r = client.post(
                "/api/alerts",
                json={"symbol": "BTC/USDT:USDT", "value": "-1%"},
            )
            check("уведомление ставится из приложения", r.status_code == 200, r.text[:80])
            created = r.json()["created"][0]
            check(
                "цена уровня посчитана от текущей",
                abs(created["price"] - 89100.0) < 1e-6,
                str(created["price"]),
            )

            r = client.post("/api/alerts", json={"symbol": "BTC/USDT:USDT", "value": "ерунда"})
            check("бессмыслицу приложение не принимает", r.status_code == 400, f"={r.status_code}")

            r = client.delete(f"/api/alerts/{created['id']}")
            check("уведомление снимается", r.status_code == 200 and r.json()["ok"])

            r = client.post("/api/settings", json={"preset": "careful"})
            check("режим меняется из приложения", r.status_code == 200, r.text[:80])
            check(
                "и правда применился",
                config.view(42).current_preset() == "careful",
                str(config.view(42).current_preset()),
            )
            r = client.post("/api/settings", json={"preset": "выдуманный"})
            check("неизвестный режим отклонён", r.status_code == 400, f"={r.status_code}")
            r = client.post("/api/settings", json={"symbols": []})
            check("пустой список инструментов отклонён", r.status_code == 400)
            client.post("/api/settings", json={"preset": "balanced"})

            r = client.get("/api/health")
            check("проверка здоровья отвечает", r.status_code in (200, 503))

            r = client.get("/")
            check("страница приложения отдаётся", r.status_code == 200)
            page = r.text
            check("в странице есть вкладки", 'data-tab="signals"' in page)
            for asset in ("/static/app.js", "/static/style.css"):
                check(f"страница ссылается на {asset}", asset in page)
                check(f"{asset} отдаётся", client.get(asset).status_code == 200)
    finally:
        feed.fetch_price, feed.fetch_prices = real_price, real_prices


def test_webapp_files() -> None:
    """Приложение не должно тянуть ничего лишнего и ломать вёрстку."""
    print("Файлы приложения")
    import pathlib
    import re as _re

    webapp = pathlib.Path("app/api/webapp")
    page = (webapp / "index.html").read_text(encoding="utf-8")
    script = (webapp / "app.js").read_text(encoding="utf-8")
    styles = (webapp / "style.css").read_text(encoding="utf-8")

    check("страница лёгкая", len(page) < 8000, f"{len(page)} байт")
    check("скрипт обозримый", len(script) < 40000, f"{len(script)} байт")
    check("стили обозримые", len(styles) < 30000, f"{len(styles)} байт")

    sources = _re.findall(r'src="(https?://[^"]+)"', page)
    check(
        "внешний скрипт только от Telegram",
        all(s.startswith("https://telegram.org/") for s in sources),
        str(sources),
    )
    check("подпись уходит на сервер", "X-Telegram-Init-Data" in script)
    check("текст от сервера экранируется", "function escape(" in script)

    # Вкладки в вёрстке и в коде должны совпадать: иначе нажатие открывает пустоту
    tabs = set(_re.findall(r'data-tab="(\w+)"', page))
    painted = set(_re.findall(r"^\s{4}(\w+): paint", script, _re.M))
    check("у каждой вкладки есть отрисовка", tabs == painted, f"{tabs} / {painted}")

    # Каждый onclick в шаблонах должен вести в существующую функцию
    handlers = set(_re.findall(r'onclick="(\w+)\(', script))
    defined = set(_re.findall(r"function (\w+)\(", script))
    check("все кнопки приложения ведут в код", handlers <= defined, str(handlers - defined))

    check("цвета берутся у Telegram", "--tg-theme-bg-color" in styles)
    check("вёрстка учитывает вырез экрана", "safe-area-inset-bottom" in styles)


async def test_heartbeat() -> None:
    """Отметка «я жив» — то, по чему docker судит о здоровье контейнера.

    Если путь в Dockerfile и путь в коде разойдутся, контейнер навсегда
    станет «нездоровым», а автодеплой будет откатывать каждое обновление
    и никто не поймёт почему.
    """
    print("Отметка о жизни")
    import pathlib

    from app.config import Settings, settings
    from app.runner import Application

    app = Application()
    task = asyncio.create_task(app._run_heartbeat())
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    beat = pathlib.Path(settings.heartbeat_path)
    check("отметка появилась", beat.exists(), str(beat))
    if beat.exists():
        check("в отметке время", beat.read_text(encoding="utf-8").isdigit())
        beat.unlink()

    dockerfile = pathlib.Path("Dockerfile")
    if dockerfile.exists():
        default_path = Settings.model_fields["heartbeat_path"].default
        check(
            "docker проверяет тот же файл",
            default_path in dockerfile.read_text(encoding="utf-8"),
            default_path,
        )


def test_bot_wiring() -> None:
    """Бот и диспетчер должны собираться без Telegram."""
    print("Сборка бота")
    from app.bot.instance import COMMANDS, create_dispatcher

    dp = create_dispatcher()
    check("диспетчер собран", dp is not None)
    check("команды описаны", len(COMMANDS) >= 4)
    commands = {c.command for c in COMMANDS}
    check("есть /start", "start" in commands)
    check(
        "у каждой команды есть описание",
        all(c.description for c in COMMANDS),
    )


# --------------------------------------------------------------------------


async def main() -> int:
    print()
    print("=" * 60)
    print("  ПРОВЕРКА СБОРКИ")
    print("=" * 60)
    print()

    try:
        await test_database()
        await test_outcomes_and_stats()
        await test_tracker_logic()
        await test_strategy()
        await test_settings()
        await test_messages()
        await test_alerts()
        await test_webapp()
        test_webapp_files()
        test_names()
        test_help()
        test_buttons()
        await test_heartbeat()
        test_bot_wiring()
    finally:
        await db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(_TMP_DB + suffix)
            except OSError:
                pass

    print()
    print("=" * 60)
    if failures:
        print(f"  ПРОВАЛЕНО: {len(failures)}")
        for name in failures:
            print(f"    • {name}")
        print("=" * 60)
        return 1
    print("  ВСЁ В ПОРЯДКЕ")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
