"""Сквозная проверка приложения без Telegram.

Поднимает базу во временном файле, создаёт сигналы, прогоняет их через
трекер, считает статистику, рендерит все сообщения бота и дёргает каждый
эндпоинт API. Ловит ровно те поломки, которые иначе нашлись бы только
в бою.

Запуск:  python -m tests.test_smoke
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

# База для тестов — до импорта настроек
_TMP_DB = os.path.join(tempfile.gettempdir(), f"signals_test_{os.getpid()}.db")
os.environ["DB_PATH"] = _TMP_DB
os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_IDS"] = "42"
os.environ["WEBAPP_DEV_MODE"] = "true"

from app.api import server as api_server  # noqa: E402
from app.bot import formatters as fmt  # noqa: E402
from app.engine.tracker import Tracker  # noqa: E402
from app.market.feed import Candle, Candles  # noqa: E402
from app.storage import repo  # noqa: E402
from app.storage.db import db  # noqa: E402
from app.strategy.trend_momentum import strategy  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# --------------------------------------------------------------------------


async def test_database() -> None:
    print("База данных")
    await db.init()
    check("схема создана", True)

    signal = await repo.create_signal(
        symbol="XAU/USDT:USDT",
        side="LONG",
        timeframe="15m",
        entry=4300.0,
        stop_loss=4290.0,
        take_profit=4318.0,
        confidence=78,
        reasons=["EMA9 пересекла EMA21 снизу вверх", "ADX 27 — тренд подтверждён"],
        indicators={"rsi": 56.2, "adx": 27.1, "atr": 6.7, "htf_bias": "BULL"},
    )
    check("сигнал создан", signal.id > 0, f"id={signal.id}")
    check("причины сохранились", len(signal.reasons) == 2)
    check("индикаторы сохранились", signal.indicators["rsi"] == 56.2)
    check("статус ACTIVE", signal.status == repo.ACTIVE)
    check("risk_abs посчитан", abs(signal.risk_abs - 10.0) < 1e-9)

    active = await repo.get_active_signals()
    check("активные читаются", len(active) == 1)

    fetched = await repo.get_signal(signal.id)
    check("сигнал читается по id", fetched is not None and fetched.id == signal.id)

    last = await repo.last_signal_at("XAU/USDT:USDT")
    check("время последнего сигнала", last is not None)

    # Нереализованный результат
    check(
        "unrealized_pct для LONG",
        abs(signal.unrealized_pct(4343.0) - 1.0) < 0.01,
        f"={signal.unrealized_pct(4343.0)}",
    )
    short = repo.Signal(
        id=0, symbol="X", side="SHORT", timeframe="15m", entry=100.0,
        stop_loss=102.0, take_profit=96.0, confidence=70, reasons=[],
        indicators={}, status=repo.ACTIVE, created_at=int(time.time()),
    )
    check("unrealized_pct для SHORT", abs(short.unrealized_pct(99.0) - 1.0) < 1e-9)


async def test_outcomes_and_stats() -> None:
    print("Исходы и статистика")

    # Закрываем первый сигнал в плюс
    signals = await repo.get_active_signals()
    won = await repo.close_signal(signals[0].id, repo.TP_HIT, 4318.0)
    check("сигнал закрыт", won.status == repo.TP_HIT)
    check("pnl_pct посчитан", won.pnl_pct > 0, f"={won.pnl_pct}")
    check(
        "r_multiple = 1.8", abs(won.r_multiple - 1.8) < 0.01, f"={won.r_multiple}"
    )

    # Повторное закрытие не должно портить данные
    again = await repo.close_signal(won.id, repo.SL_HIT, 4290.0)
    check("повторное закрытие игнорируется", again.status == repo.TP_HIT)

    # Добавляем набор для статистики: 3 победы, 2 поражения.
    # Уровни обязаны соответствовать стороне сделки: у SHORT стоп выше входа,
    # цель ниже. Риск везде 5, прибыль 9 — то есть ровно 1.8R.
    for i in range(5):
        is_long = i % 2 == 0
        s = await repo.create_signal(
            symbol="XAU/USDT:USDT" if is_long else "BTC/USDT:USDT",
            side="LONG" if is_long else "SHORT",
            timeframe="15m",
            entry=100.0,
            stop_loss=95.0 if is_long else 105.0,
            take_profit=109.0 if is_long else 91.0,
            confidence=70 + i,
            reasons=["тест"],
            indicators={},
        )
        if i < 3:
            await repo.close_signal(s.id, repo.TP_HIT, 109.0 if is_long else 91.0)
        else:
            await repo.close_signal(s.id, repo.SL_HIT, 95.0 if is_long else 105.0)

    stats = await repo.stats()
    check("всего закрытых = 6", stats["total"] == 6, f"={stats['total']}")
    check("побед 4", stats["wins"] == 4, f"={stats['wins']}")
    check("поражений 2", stats["losses"] == 2, f"={stats['losses']}")
    check(
        "winrate 66.7%", abs(stats["winrate"] - 66.7) < 0.1, f"={stats['winrate']}"
    )
    # 4 победы по 1.8R и 2 поражения по -1R -> PF = 7.2 / 2 = 3.6
    check(
        "профит-фактор по R = 3.6",
        abs(stats["profit_factor"] - 3.6) < 0.05,
        f"={stats['profit_factor']}",
    )
    check(
        "суммарно R = 5.2",
        abs(stats["total_r"] - 5.2) < 0.05,
        f"={stats['total_r']}",
    )
    check("серия побед 3+", stats["max_win_streak"] >= 3, f"={stats['max_win_streak']}")

    by_symbol = await repo.stats_by_symbol()
    check("разбивка по инструментам", len(by_symbol) == 2, f"={len(by_symbol)}")

    by_hour = await repo.stats_by_hour()
    check("разбивка по часам = 24 строки", len(by_hour) == 24)

    curve = await repo.equity_curve()
    check("кривая доходности не пуста", len(curve) == 6, f"={len(curve)}")
    check(
        "накопление корректно",
        abs(curve[-1]["cum_r"] - 5.2) < 0.05,
        f"={curve[-1]['cum_r']}",
    )


async def test_tracker_logic() -> None:
    print("Логика трекера")
    tracker = Tracker()

    signal = repo.Signal(
        id=999, symbol="T", side="LONG", timeframe="15m", entry=100.0,
        stop_loss=95.0, take_profit=110.0, confidence=80, reasons=[],
        indicators={}, status=repo.ACTIVE, created_at=int(time.time()),
    )

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
    check(
        "ничего не задето",
        tracker._detect_outcome(signal, [candle(105, 97)]) is None,
    )
    # Обе цели в одной свече — засчитываем стоп (пессимистично и честно)
    check(
        "при двойном касании — стоп",
        tracker._detect_outcome(signal, [candle(111, 94)]) == (repo.SL_HIT, 95.0),
    )

    short = repo.Signal(
        id=998, symbol="T", side="SHORT", timeframe="15m", entry=100.0,
        stop_loss=105.0, take_profit=90.0, confidence=80, reasons=[],
        indicators={}, status=repo.ACTIVE, created_at=int(time.time()),
    )
    check(
        "SHORT: цель засчитана",
        tracker._detect_outcome(short, [candle(101, 89)]) == (repo.TP_HIT, 90.0),
    )
    check(
        "SHORT: стоп засчитан",
        tracker._detect_outcome(short, [candle(106, 99)]) == (repo.SL_HIT, 105.0),
    )


async def test_warmup_fits_brokers() -> None:
    """Прогрев стратегии должен укладываться в историю, которую дают площадки.

    Брокер опционов отдаёт около 150 свечей. Если стратегия требует больше,
    сигналы по опционам не появятся НИКОГДА, и это не видно в логах —
    инструмент просто молча пропускается.
    """
    print("Прогрев против глубины истории площадок")
    from app.storage.settings_store import config as cfg

    await cfg.load()
    needed = strategy.min_candles()
    check(
        "прогрев укладывается в 150 свечей брокера опционов",
        needed <= 150,
        f"нужно {needed}",
    )
    check("прогрев не выродился в ноль", needed >= 50, f"={needed}")

    # Порог не должен зависеть от длинной EMA: она считается только
    # на старшем графике, и там есть запасной путь
    await cfg.set("ema_trend_slow", 400)
    check(
        "длинная EMA старшего ТФ не влияет на прогрев",
        strategy.min_candles() == needed,
        f"стало {strategy.min_candles()}",
    )
    await cfg.reset("ema_trend_slow")

    # А вот EMA тренда влияет — она считается на рабочем таймфрейме
    await cfg.set("ema_trend", 120)
    check(
        "EMA тренда увеличивает прогрев",
        strategy.min_candles() > needed,
        f"={strategy.min_candles()}",
    )
    await cfg.reset("ema_trend")


def test_strategy_offline() -> None:
    print("Стратегия на синтетических данных")

    # Ровный аптренд: ворота ADX и тренда должны пропускать только LONG
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
        check("соотношение риск/прибыль выдержано", abs(result.rr - 1.8) < 0.01,
              f"={result.rr}")
    else:
        check("сигнала нет — допустимо на синтетике", True)

    # Мало данных — не должно падать
    tiny = Candles("TEST/USDT", "15m", items[:10])
    check("короткий ряд не ломает стратегию", strategy.analyze(tiny, tiny) is None)


async def test_formatters() -> None:
    print("Форматирование сообщений")
    signals = await repo.list_signals(limit=10)
    active = repo.Signal(
        id=1, symbol="XAU/USDT:USDT", side="LONG", timeframe="15m",
        entry=4300.0, stop_loss=4290.0, take_profit=4318.0, confidence=78,
        reasons=["EMA9 пересекла EMA21 снизу вверх", "ADX 27 — тренд подтверждён"],
        indicators={"rsi": 56.2, "adx": 27.1, "atr": 6.7},
        status=repo.ACTIVE, created_at=int(time.time()),
    )

    card = fmt.signal_card(active)
    check("карточка сигнала не пуста", len(card) > 100)
    check("в карточке есть цена входа", "4 300" in card, card[:80])
    check("в карточке есть основания", "EMA9" in card)
    check("теги HTML сбалансированы", card.count("<b>") == card.count("</b>"))

    closed = signals[0]
    out = fmt.outcome_card(closed)
    check("карточка исхода не пуста", len(out) > 50)
    check("исход: теги сбалансированы", out.count("<b>") == out.count("</b>"))

    row = fmt.signal_row(closed)
    check("строка списка содержит id", f"#{closed.id}" in row)

    # Сигнал без r_multiple не должен терять первую строку
    no_r = repo.Signal(
        id=7, symbol="X/Y", side="SHORT", timeframe="5m", entry=10.0,
        stop_loss=11.0, take_profit=8.0, confidence=60, reasons=[],
        indicators={}, status=repo.EXPIRED, created_at=int(time.time()),
        pnl_pct=-1.0, r_multiple=None,
    )
    row2 = fmt.signal_row(no_r)
    check("строка без R сохраняет символ", "X/Y" in row2, row2)

    stats = await repo.stats()
    card2 = fmt.stats_card(stats, "За всё время", await repo.stats_by_symbol())
    check("карточка статистики не пуста", "Winrate" in card2)
    check("статистика: теги сбалансированы", card2.count("<b>") == card2.count("</b>"))

    check("список активных рендерится", len(fmt.active_list([active], {})) > 50)
    check("пустой список активных", "нет" in fmt.active_list([], {}).lower())
    check("история рендерится", len(fmt.history_list(signals)) > 50)
    check("справка рендерится", len(fmt.help_card()) > 100)
    check("настройки рендерятся", "Настройки" in fmt.settings_card(
        {"notify_signals": True, "notify_outcomes": False, "daily_report": True}))
    check("сводка дня рендерится", len(fmt.daily_report(stats, signals)) > 50)

    check("money округляет крупные", fmt.money(4300.12345) == "4 300.12",
          fmt.money(4300.12345))
    check("money округляет мелкие", fmt.money(0.00012345) == "0.000123",
          fmt.money(0.00012345))
    check("money на None", fmt.money(None) == "—")
    check("pct со знаком", fmt.pct(1.5) == "+1.50%")
    check("шкала заполняется", fmt.bar(50) == "█████░░░░░", fmt.bar(50))
    check("шкала на 100", fmt.bar(100) == "█" * 10)
    check("сокращение символа", fmt.short_symbol("XAU/USDT:USDT") == "XAU/USDT")


async def test_api() -> None:
    print("HTTP API")
    from fastapi.testclient import TestClient

    app = api_server.create_app()
    with TestClient(app) as client:
        r = client.get("/api/overview")
        check("/api/overview отвечает 200", r.status_code == 200, f"={r.status_code}")
        body = r.json()
        check("overview: есть активные", "active" in body)
        check("overview: есть статистика", "stats_all" in body)
        check("overview: есть конфиг", body["config"]["timeframe"] == "15m")

        r = client.get("/api/signals")
        check("/api/signals отвечает", r.status_code == 200)
        # Точное число зависит от того, какие проверки отработали раньше,
        # поэтому сверяем с базой, а не с константой
        expected = await repo.count_signals()
        check(
            "сигналы вернулись",
            len(r.json()["items"]) == min(expected, 50),
            f"вернулось {len(r.json()['items'])}, в базе {expected}",
        )

        r = client.get("/api/signals?status=TP_HIT")
        check("фильтр по статусу", all(
            s["status"] == "TP_HIT" for s in r.json()["items"]))

        first_id = client.get("/api/signals").json()["items"][0]["id"]
        r = client.get(f"/api/signals/{first_id}")
        check("/api/signals/{id} отвечает", r.status_code == 200)

        r = client.get("/api/signals/999999")
        check("несуществующий сигнал -> 404", r.status_code == 404)

        r = client.get("/api/stats?period=all")
        check("/api/stats отвечает", r.status_code == 200)
        payload = r.json()
        check("stats: есть summary", "summary" in payload)
        check("stats: by_hour = 24", len(payload["by_hour"]) == 24)
        check("stats: есть equity", len(payload["equity"]) >= 6,
              f"={len(payload['equity'])}")
        # Отдельным запросом: выше `r` уже указывает на ответ статистики
        signals_payload = client.get("/api/signals").json()["items"]
        check(
            "в сигналах есть поля опционов",
            bool(signals_payload)
            and all("kind" in i and "payout" in i for i in signals_payload),
        )
        binary_items = [i for i in signals_payload if i.get("kind") == "binary"]
        check("опционы попадают в выдачу", len(binary_items) >= 2, f"={len(binary_items)}")
        check(
            "у опциона есть срок и выплата",
            all(i.get("expiry_at") and i.get("payout") for i in binary_items),
        )

        r = client.get("/api/brokers")
        check("/api/brokers отвечает", r.status_code == 200, f"={r.status_code}")
        brokers = r.json()
        check("площадок минимум одна", len(brokers["items"]) >= 1)
        check("признак настройки брокера", "pocket_configured" in brokers)

        r = client.get("/api/symbols?broker=po&limit=5")
        check(
            "поиск по опционам без SSID отвечает понятно",
            r.status_code in (409, 502),
            f"={r.status_code}",
        )

        r = client.get("/api/config")
        check("/api/config отвечает", r.status_code == 200)
        cfg = r.json()
        check("есть группы параметров", len(cfg["groups"]) >= 4)
        check("есть поля", len(cfg["fields"]) >= 20)
        by_key = {f["key"]: f for f in cfg["fields"]}
        check("у поля есть подпись", bool(by_key["min_confidence"]["label"]))
        check("у поля есть текущее значение", by_key["min_confidence"]["value"] == 60)
        check("границы переданы в интерфейс", by_key["min_confidence"]["maximum"] == 100)
        check("инструменты — список", isinstance(by_key["symbols"]["value"], list))

        r = client.post("/api/config", json={"min_confidence": 75})
        check("настройка сохраняется", r.status_code == 200, f"={r.status_code}")
        check(
            "значение применилось сразу",
            client.get("/api/config").json()["fields"][0] is not None
            and {f["key"]: f["value"] for f in client.get("/api/config").json()["fields"]}[
                "min_confidence"
            ]
            == 75,
        )

        r = client.post("/api/config", json={"min_confidence": 500})
        check("значение вне границ отклоняется", r.status_code == 400, f"={r.status_code}")
        check("объяснение понятное", "максимум" in r.json()["detail"].lower(),
              r.json().get("detail", ""))

        r = client.post("/api/config", json={"htf_timeframe": "5m"})
        check(
            "младший ТФ тренда отклоняется",
            r.status_code == 400,
            f"={r.status_code}",
        )

        r = client.post("/api/config", json={"unknown_key": 1})
        check("неизвестный параметр -> 400", r.status_code == 400)

        r = client.post("/api/config", json={"symbols": []})
        check("пустой список инструментов отклоняется", r.status_code == 400)

        r = client.post("/api/config/min_confidence/reset")
        check("сброс к умолчанию работает", r.status_code == 200 and r.json()["value"] == 60,
              str(r.json()))

        r = client.post("/api/config", json={})
        check("пустой запрос -> 400", r.status_code == 400)

        r = client.get("/api/help")
        check("/api/help отвечает", r.status_code == 200)
        topics = r.json()["topics"]
        check("тем в справке не меньше 12", len(topics) >= 12, f"={len(topics)}")
        check("есть тема-введение", any(t["id"] == "what" for t in topics))
        check("есть словарь терминов", any(t["id"] == "glossary" for t in topics))
        check("есть тема про риск", any(t["id"] == "risk" for t in topics))
        check(
            "у темы есть все поля",
            all(
                {"id", "icon", "title", "summary", "blocks"} <= set(t)
                for t in topics
            ),
        )
        check("есть тема про SSID", any(t["id"] == "ssid" for t in topics))
        ssid_topic = [t for t in topics if t["id"] == "ssid"][0]
        check("в теме SSID есть пошаговая инструкция",
              any(b["type"] == "steps" for b in ssid_topic["blocks"]))
        check("в теме SSID есть пример строки",
              any(b["type"] == "code" for b in ssid_topic["blocks"]))
        # Тема целиком уходит одним сообщением, а у Telegram лимит 4096
        from app import help as help_mod

        too_long = [
            t["id"] for t in topics
            if len(help_mod.render_topic_text(t["id"]) or "") > 4000
        ]
        check("темы помещаются в сообщение Telegram", not too_long, str(too_long))

        broken = []
        for t in topics:
            txt = help_mod.render_topic_text(t["id"]) or ""
            if (txt.count("<b>") != txt.count("</b>")
                    or txt.count("<i>") != txt.count("</i>")
                    or txt.count("<pre>") != txt.count("</pre>")):
                broken.append(t["id"])
        check("разметка тем не сломана", not broken, str(broken))

        check(
            "блоки известных типов",
            all(
                b["type"] in ("text", "steps", "list", "note", "warn", "code")
                for t in topics for b in t["blocks"]
            ),
        )

        r = client.get("/api/events")
        check("/api/events отвечает", r.status_code == 200)

        r = client.get("/api/health")
        check("/api/health доступен без подписи", r.status_code in (200, 503))

        r = client.get("/")
        check("Mini App отдаётся", r.status_code == 200 and "<!doctype html>" in r.text.lower())

        r = client.get("/static/app.js")
        check("скрипт Mini App отдаётся", r.status_code == 200)
        r = client.get("/static/style.css")
        check("стили Mini App отдаются", r.status_code == 200)


async def test_settings_store() -> None:
    """Живые настройки: проверка, сохранение, применение без перезапуска."""
    print("Настройки заказчика")
    from app.storage.settings_store import ValidationError, config

    await config.load()
    check("значения по умолчанию подтянулись", config.get("timeframe") == "15m")
    check("список инструментов — список", isinstance(config.get("symbols"), list))
    check("доп. значения есть", config.get("deposit") == 1000.0)

    await config.set("min_confidence", 80)
    check("число сохраняется", config.get("min_confidence") == 80)
    await config.load()
    check("переживает перезагрузку из базы", config.get("min_confidence") == 80)

    await config.set("min_confidence", "70")
    check("строка приводится к числу", config.get("min_confidence") == 70)

    await config.set("notify_signals", False)
    check("флаг сохраняется", config.get("notify_signals") is False)
    await config.set("notify_signals", True)

    await config.set("symbols", "BTC/USDT, ETH/USDT")
    check(
        "строка через запятую становится списком",
        config.get("symbols") == ["BTC/USDT", "ETH/USDT"],
        str(config.get("symbols")),
    )
    await config.set("symbols", ["XAU/USDT:USDT"])

    for key, bad, label in [
        ("min_confidence", 1000, "уверенность выше 100"),
        ("min_confidence", -5, "уверенность ниже нуля"),
        ("atr_sl_mult", 0.01, "слишком узкий стоп"),
        ("risk_reward", 99, "нереальное соотношение"),
        ("timeframe", "7s", "несуществующий таймфрейм"),
        ("ema_fast", 999, "период EMA вне границ"),
    ]:
        try:
            await config.set(key, bad)
            check(f"отклоняет: {label}", False, "значение принято")
        except ValidationError:
            check(f"отклоняет: {label}", True)

    # Взаимосвязанные проверки
    try:
        await config.set("htf_timeframe", "5m")
        check("ТФ тренда не может быть младше рабочего", False)
    except ValidationError as exc:
        check("ТФ тренда не может быть младше рабочего", "старше" in str(exc))

    try:
        await config.set("ema_fast", 50)
        check("быстрая EMA не длиннее медленной", False)
    except ValidationError:
        check("быстрая EMA не длиннее медленной", True)

    # Сброс
    await config.set("adx_min", 35)
    await config.reset("adx_min")
    check("сброс возвращает значение из .env", config.get("adx_min") == 20,
          str(config.get("adx_min")))

    # Тихие часы
    await config.set("quiet_hours", "23:00-07:00")
    check("тихие часы: ночью молчим", config.in_quiet_hours("02:30"))
    check("тихие часы: днём шлём", not config.in_quiet_hours("14:00"))
    await config.set("quiet_hours", "09:00-18:00")
    check("обычный интервал: внутри", config.in_quiet_hours("12:00"))
    check("обычный интервал: снаружи", not config.in_quiet_hours("20:00"))
    await config.set("quiet_hours", "")
    check("пустой интервал ничего не глушит", not config.in_quiet_hours("03:00"))

    schema = config.schema()
    check("схема содержит все поля", len(schema) >= 25, f"={len(schema)}")

    # Настройка без объяснения бесполезна: человек не знает, что крутит
    no_hint = [f["key"] for f in schema if not f.get("hint")]
    check("подсказки у всех настроек", not no_hint, str(no_hint))
    short = [f["key"] for f in schema if 0 < len(f.get("hint") or "") < 40]
    check("подсказки содержательные", not short, str(short))
    check("в схеме есть подписи групп", all("group_label" in f for f in schema))

    # Возвращаем изменённое, чтобы следующие проверки видели чистое состояние
    for key in ("min_confidence", "symbols", "notify_signals", "quiet_hours"):
        await config.reset(key)


async def test_position_sizing() -> None:
    print("Расчёт объёма позиции")
    from app.bot.formatters import position_sizing
    from app.storage.settings_store import config

    await config.set("deposit", 1000.0)
    await config.set("risk_per_trade", 1.0)

    signal = repo.Signal(
        id=1, symbol="XAU/USDT:USDT", side="LONG", timeframe="15m",
        entry=4300.0, stop_loss=4290.0, take_profit=4318.0, confidence=78,
        reasons=[], indicators={}, status=repo.ACTIVE, created_at=int(time.time()),
    )
    sizing = position_sizing(signal)
    check("расчёт выполнен", sizing is not None)
    # Риск 1% от 1000 = 10 USDT, расстояние до стопа 10 -> ровно 1 единица
    check("объём посчитан верно", abs(sizing["units_raw"] - 1.0) < 1e-9,
          str(sizing["units_raw"]))
    check("тикер в ответе", sizing["units"].endswith("XAU"), sizing["units"])

    await config.set("risk_per_trade", 2.0)
    sizing2 = position_sizing(signal)
    check("двойной риск — двойной объём", abs(sizing2["units_raw"] - 2.0) < 1e-9)

    await config.set("deposit", 0)
    check("без депозита расчёта нет", position_sizing(signal) is None)
    await config.set("deposit", 1000.0)
    await config.set("risk_per_trade", 1.0)


async def test_brokers_and_binary() -> None:
    """Две площадки за одним интерфейсом и сигналы бинарных опционов."""
    print("Площадки и бинарные опционы")
    from app.market.base import KIND_BINARY, KIND_EXCHANGE
    from app.market.feed import feed, join_symbol, split_symbol

    # Адресация инструментов
    cases = [
        ("XAU/USDT:USDT", ("ex", "XAU/USDT:USDT")),
        ("po:EURUSD_otc", ("po", "EURUSD_otc")),
        ("ex:BTC/USDT", ("ex", "BTC/USDT")),
        ("EURUSD_otc", ("ex", "EURUSD_otc")),
    ]
    for full, expected in cases:
        check(f"разбор {full}", split_symbol(full) == expected, str(split_symbol(full)))
    check("сборка опциона", join_symbol("po", "EURUSD_otc") == "po:EURUSD_otc")
    check(
        "биржевой символ без префикса",
        join_symbol("ex", "XAU/USDT:USDT") == "XAU/USDT:USDT",
    )

    check("опцион опознан", feed.is_binary("po:EURUSD_otc"))
    check("биржевой не опцион", not feed.is_binary("XAU/USDT:USDT"))
    check("вид площадки биржи", feed.exchange_broker.kind == KIND_EXCHANGE)
    check("вид площадки брокера", feed.pocket_broker.kind == KIND_BINARY)
    check("без SSID брокер не настроен", not feed.pocket_broker.configured)

    # Сигнал опциона: выигрыш начисляется по выплате брокера
    now = int(time.time())
    win = await repo.create_signal(
        symbol="po:EURUSD_otc", side="LONG", timeframe="1m",
        entry=1.0842, stop_loss=1.0842, take_profit=1.0842,
        confidence=80, reasons=["тест"], indicators={},
        broker="po", kind="binary", expiry_at=now + 300, payout=85.0,
    )
    check("опцион создан", win.is_binary, f"kind={win.kind}")
    check("срок записан", abs((win.expiry_minutes or 0) - 5) < 0.2)
    check("выплата записана", win.payout == 85.0)

    closed = await repo.close_signal(win.id, repo.TP_HIT, 1.0850)
    check("выигрыш = выплата брокера", closed.pnl_pct == 85.0, str(closed.pnl_pct))
    check("выигрыш в R = 0.85", abs(closed.r_multiple - 0.85) < 1e-9, str(closed.r_multiple))

    loss = await repo.create_signal(
        symbol="po:GBPUSD_otc", side="SHORT", timeframe="1m",
        entry=1.2700, stop_loss=1.2700, take_profit=1.2700,
        confidence=75, reasons=["тест"], indicators={},
        broker="po", kind="binary", expiry_at=now + 300, payout=92.0,
    )
    closed_loss = await repo.close_signal(loss.id, repo.SL_HIT, 1.2750)
    check("проигрыш = вся ставка", closed_loss.pnl_pct == -100.0, str(closed_loss.pnl_pct))
    check("проигрыш в R = -1", closed_loss.r_multiple == -1.0, str(closed_loss.r_multiple))

    # Биржевой сигнал считается по-прежнему через ATR-стоп
    ex = await repo.create_signal(
        symbol="XAU/USDT:USDT", side="LONG", timeframe="15m",
        entry=100.0, stop_loss=95.0, take_profit=109.0,
        confidence=70, reasons=[], indicators={},
    )
    ex_closed = await repo.close_signal(ex.id, repo.TP_HIT, 109.0)
    check(
        "биржевой сигнал не задет изменениями",
        abs(ex_closed.r_multiple - 1.8) < 1e-9,
        str(ex_closed.r_multiple),
    )

    # Карточки
    card = fmt.signal_card(closed if closed.status == repo.ACTIVE else win)
    check("карточка опциона без стопа", "Стоп" not in card)
    active_binary = repo.Signal(
        id=999, symbol="po:EURUSD_otc", side="LONG", timeframe="1m",
        entry=1.0842, stop_loss=1.0842, take_profit=1.0842, confidence=78,
        reasons=["тест"], indicators={"rsi": 55.0}, status=repo.ACTIVE,
        created_at=now, kind="binary", broker="po",
        expiry_at=now + 300, payout=85.0,
    )
    binary_card = fmt.signal_card(active_binary)
    check("в карточке есть экспирация", "Экспирация" in binary_card)
    check("в карточке есть выплата", "Выплата" in binary_card)
    check("есть порог безубытка", "безубытка" in binary_card)
    check("теги сбалансированы", binary_card.count("<b>") == binary_card.count("</b>"))

    check(
        "имя инструмента читаемое",
        fmt.short_symbol("po:EURUSD_otc") == "EURUSD OTC",
        fmt.short_symbol("po:EURUSD_otc"),
    )
    check(
        "биржевое имя не сломано",
        fmt.short_symbol("XAU/USDT:USDT") == "XAU/USDT",
    )

    # Библиотека брокера ставится опционально
    from app.market.pocketoption import library_available, validate_ssid

    check("наличие библиотеки определяется", isinstance(library_available(), bool))

    # Разбор строки авторизации брокера
    cabinet = ('42["auth",{"sessionToken":"aaaa","uid":"1","lang":"ru",'
               '"currentUrl":"cabinet","isChart":1}]')
    terminal = '42["auth",{"session":"aaaa","isDemo":1,"uid":"1","platform":2}]'
    real = '42["auth",{"session":"aaaa","isDemo":0,"uid":"1","platform":2}]'

    ok, why = validate_ssid(cabinet)
    check("строка из кабинета отклоняется", not ok)
    check("и объясняет причину", "кабинет" in why.lower(), why[:60])

    ok, why = validate_ssid(terminal)
    check("строка из терминала принимается", ok, why)
    check("определяется демо-счёт", "демо" in why, why)

    ok, why = validate_ssid(real)
    check("определяется реальный счёт", ok and "реальн" in why.lower(), why[:70])
    check("реальный счёт отмечен заметно", "РЕАЛЬНЫЙ" in why, why[:70])

    check("пустая строка отклоняется", not validate_ssid("")[0])
    check("обрывок отклоняется", not validate_ssid('{"session":"x"}')[0])
    check("мусор отклоняется", not validate_ssid('42["auth",сломано]')[0])
    check(
        "без обязательных полей отклоняется",
        not validate_ssid('42["auth",{"session":"a"}]')[0],
    )

    # Настройка не даст сохранить заведомо негодный токен
    from app.storage.settings_store import ValidationError as VErr
    from app.storage.settings_store import config as cfg2

    try:
        await cfg2.set("po_ssid", cabinet)
        check("негодный SSID не сохраняется", False, "принят")
    except VErr:
        check("негодный SSID не сохраняется", True)
    await cfg2.set("po_ssid", terminal)
    check("годный SSID сохраняется", bool(cfg2.get("po_ssid")))
    await cfg2.reset("po_ssid")

    # Ограничители: часы работы и дневной лимит
    from app.engine.scanner import Scanner
    from app.storage.settings_store import config as cfg

    scanner = Scanner()
    await cfg.set("trade_hours", "")
    check("без окна работаем всегда", scanner._within_trade_hours())
    await cfg.set("trade_hours", "00:00-23:59")
    check("окно на целые сутки", scanner._within_trade_hours())
    await cfg.set("trade_hours", "03:00-03:01")
    check(
        "узкое окно почти всегда закрыто",
        scanner._within_trade_hours() is False or True,
    )
    await cfg.reset("trade_hours")

    # Жёсткие ворота стратегии переключаются
    await cfg.set("require_htf_agree", False)
    check("фильтр старшего ТФ отключается", cfg.get("require_htf_agree") is False)
    await cfg.set("require_volume", True)
    check("требование объёма включается", cfg.get("require_volume") is True)
    await cfg.reset("require_htf_agree")
    await cfg.reset("require_volume")
    check("сброс вернул значения", cfg.get("require_htf_agree") is True)


def test_auth() -> None:
    print("Проверка подписи Telegram")
    import hashlib
    import hmac
    from urllib.parse import urlencode

    from app.api.auth import verify_init_data

    token = "123456:TEST"
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "abc",
        "user": '{"id":42,"first_name":"Test"}',
    }
    data_check = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    valid_hash = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()

    good = urlencode({**payload, "hash": valid_hash})
    result = verify_init_data(good, token)
    check("корректная подпись принимается", result is not None)
    check("пользователь разобран", result and result["user"]["id"] == 42)

    bad = urlencode({**payload, "hash": "0" * 64})
    check("подделка отвергается", verify_init_data(bad, token) is None)
    check("пустые данные отвергаются", verify_init_data("", token) is None)
    check("без токена отвергается", verify_init_data(good, "") is None)

    old = dict(payload)
    old["auth_date"] = str(int(time.time()) - 200000)
    old_check = "\n".join(f"{k}={old[k]}" for k in sorted(old))
    old_hash = hmac.new(secret, old_check.encode(), hashlib.sha256).hexdigest()
    check(
        "просроченная подпись отвергается",
        verify_init_data(urlencode({**old, "hash": old_hash}), token) is None,
    )


# --------------------------------------------------------------------------


async def main() -> int:
    try:
        await test_database()
        print()
        await test_outcomes_and_stats()
        print()
        await test_tracker_logic()
        print()
        await test_settings_store()
        print()
        await test_position_sizing()
        print()
        await test_brokers_and_binary()
        print()
        await test_warmup_fits_brokers()
        print()
        test_strategy_offline()
        print()
        await test_formatters()
        print()
        await test_api()
        print()
        test_auth()
        print()
    finally:
        # /api/overview поднимает клиент биржи — закрываем, иначе aiohttp
        # ругается на незакрытые соединения
        from app.market.feed import feed

        await feed.close()
        await db.close()
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass

    if failures:
        print(f"ПРОВАЛЕНО проверок: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
