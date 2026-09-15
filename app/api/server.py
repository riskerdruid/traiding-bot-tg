"""HTTP-API и раздача Mini App.

Всё, что видит Mini App, приходит отсюда. Каждый эндпоинт закрыт проверкой
подписи Telegram — см. app/api/auth.py.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import help as help_content
from app.api.auth import current_user_id, require_owner
from app.bot import alerts_input
from app.bot.formatters import position_sizing
from app.config import settings
from app.market.feed import BrokerError, feed
from app.market.pocketoption import library_available as po_library_available
from app.news.calendar import calendar
from app.storage import repo
from app.storage.settings_store import (
    GROUPS,
    PRESETS,
    ValidationError,
    config,
)

log = logging.getLogger("api.server")

WEBAPP_DIR = Path(__file__).parent / "webapp"

# Ходовые активы показываются первыми, пока поиск пуст: биржа отдаёт
# тысячи пар, и без этого список начинается со случайных тикеров.
MAJORS = [
    "XAU", "XAG", "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE",
    "ADA", "TON", "AVAX", "LINK", "TRX", "DOT", "MATIC", "LTC",
]

PERIOD_WINDOWS = {
    "day": 86400,
    "week": 86400 * 7,
    "month": 86400 * 30,
    "all": None,
}

# Ссылки на живые компоненты, проставляются в runner.py
runtime: dict = {"scanner": None, "tracker": None, "started_at": time.time()}


def _since(period: str) -> int | None:
    window = PERIOD_WINDOWS.get(period, None)
    return int(time.time() - window) if window else None


def create_app() -> FastAPI:
    app = FastAPI(
        title="Trading Signals API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # ------------------------------------------------------------------
    # Данные
    # ------------------------------------------------------------------

    @app.get("/api/overview")
    async def overview(auth: dict = Depends(require_owner)) -> dict:
        """Всё для главного экрана одним запросом — меньше round-trip'ов."""
        uid = current_user_id(auth)
        cfg = config.view(uid) if uid else config
        active = await repo.get_active_signals(owner_id=uid)
        prices = await feed.fetch_prices(cfg.get("symbols") or [])
        day_stats = await repo.stats(since=_since("day"), owner_id=uid)
        all_stats = await repo.stats(owner_id=uid)

        scanner = runtime.get("scanner")
        tracker = runtime.get("tracker")
        scanner_state = (
            scanner.state(uid) if scanner else {"running": False, "scans_done": 0}
        )
        tracker_state = tracker.state() if tracker else {}

        enriched = []
        for s in active:
            item = s.to_dict()
            price = prices.get(s.symbol)
            item["current_price"] = price
            item["unrealized_pct"] = (
                round(s.unrealized_pct(price), 2) if price else None
            )
            item["sizing"] = position_sizing(s)
            if price is not None:
                # Прогресс от -100 (цена у стопа) до +100 (цена у цели).
                # В плюс меряем долей пути до цели, в минус — долей пути до
                # стопа: расстояния разные, и делить оба на одну базу нельзя,
                # иначе полоса врёт.
                moved = (price - s.entry) * (1 if s.is_long else -1)
                span = (
                    abs(s.take_profit - s.entry)
                    if moved >= 0
                    else abs(s.entry - s.stop_loss)
                )
                if span:
                    ratio = min(100.0, abs(moved) / span * 100)
                    item["progress"] = round(ratio if moved >= 0 else -ratio, 1)
                else:
                    item["progress"] = 0
            enriched.append(item)

        return {
            "prices": prices,
            "active": enriched,
            "stats_today": day_stats,
            "stats_all": all_stats,
            "scanner": scanner_state,
            "tracker": tracker_state,
            "uptime_sec": int(time.time() - runtime.get("started_at", time.time())),
            "config": {
                "symbols": cfg.get("symbols") or [],
                "timeframe": cfg.get("timeframe"),
                "htf_timeframe": cfg.get("htf_timeframe"),
                "min_confidence": cfg.get("min_confidence"),
                "risk_reward": cfg.get("risk_reward"),
                "news_filter": cfg.get("news_filter_enabled"),
                "deposit": cfg.get("deposit"),
                "risk_per_trade": cfg.get("risk_per_trade"),
            },
            "server_time": int(time.time()),
        }

    @app.get("/api/signals")
    async def signals(
        status: str | None = None,
        symbol: str | None = None,
        period: str = "all",
        limit: int = Query(default=50, le=200),
        offset: int = 0,
        auth: dict = Depends(require_owner),
    ) -> dict:
        uid = current_user_id(auth)
        items = await repo.list_signals(
            limit=limit,
            offset=offset,
            symbol=symbol,
            status=status,
            since=_since(period),
            owner_id=uid,
        )
        total = await repo.count_signals(
            symbol=symbol, status=status, owner_id=uid
        )

        def enrich(signal: repo.Signal) -> dict:
            data = signal.to_dict()
            data["sizing"] = position_sizing(signal)
            return data

        return {
            "items": [enrich(s) for s in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/signals/{signal_id}")
    async def signal_detail(
        signal_id: int, _: dict = Depends(require_owner)
    ) -> dict:
        signal = await repo.get_signal(signal_id)
        if signal is None:
            raise HTTPException(status_code=404, detail="Сигнал не найден")
        data = signal.to_dict()
        data["sizing"] = position_sizing(signal)
        if signal.status == repo.ACTIVE:
            price = await feed.fetch_price(signal.symbol)
            data["current_price"] = price
            data["unrealized_pct"] = (
                round(signal.unrealized_pct(price), 2) if price else None
            )
        return data

    @app.get("/api/stats")
    async def stats(
        period: str = "all",
        symbol: str | None = None,
        auth: dict = Depends(require_owner),
    ) -> dict:
        uid = current_user_id(auth)
        since = _since(period)
        return {
            "period": period,
            "summary": await repo.stats(
                since=since, symbol=symbol, owner_id=uid
            ),
            "by_symbol": await repo.stats_by_symbol(since=since, owner_id=uid),
            "by_hour": await repo.stats_by_hour(since=since, owner_id=uid),
            "equity": await repo.equity_curve(since=since, owner_id=uid),
        }

    @app.get("/api/candles")
    async def candles(
        symbol: str | None = None,
        timeframe: str | None = None,
        limit: int = Query(default=120, le=300),
        auth: dict = Depends(require_owner),
    ) -> dict:
        """Свечи для графика в приложении."""
        uid = current_user_id(auth)
        cfg = config.view(uid) if uid else config
        symbols_now = cfg.get("symbols") or []
        symbol = symbol or (symbols_now[0] if symbols_now else "")
        if not symbol:
            raise HTTPException(status_code=400, detail="Инструмент не задан")
        timeframe = timeframe or cfg.get("timeframe")
        try:
            data = await feed.fetch_candles(symbol, timeframe, limit=limit)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Биржа недоступна: {exc}")
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "candles": [
                {
                    "ts": c.ts,
                    "o": c.open,
                    "h": c.high,
                    "l": c.low,
                    "c": c.close,
                    "v": c.volume,
                }
                for c in data.items
            ],
        }

    @app.get("/api/news")
    async def news(auth: dict = Depends(require_owner)) -> dict:
        uid = current_user_id(auth)
        cfg = config.view(uid) if uid else config
        await calendar.refresh()
        muted = calendar.mute_reason(cfg)
        return {
            "upcoming": [
                e.to_dict() for e in calendar.upcoming(limit=12, cfg=cfg)
            ],
            "today": [e.to_dict() for e in calendar.today(cfg=cfg)],
            "muted_by": muted.to_dict() if muted else None,
            "enabled": cfg.get("news_filter_enabled"),
            "window": {
                "before_min": cfg.get("news_mute_before_min"),
                "after_min": cfg.get("news_mute_after_min"),
            },
        }

    # ------------------------------------------------------------------
    # Настройки — всё, что заказчик меняет сам, без .env и сервера
    # ------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config(auth: dict = Depends(require_owner)) -> dict:
        """Описание всех параметров + текущие значения.

        Интерфейс строится из этого ответа, поэтому новый параметр
        появляется в приложении сам, без правки вёрстки.
        """
        uid = current_user_id(auth)
        await config.load()
        return {
            "user_id": uid,
            "groups": [
                {"key": key, "label": label} for key, label in GROUPS.items()
            ],
            "fields": config.schema(user_id=uid),
            # Готовые режимы — главный способ настройки для новичка:
            # он выбирает цель, а не два десятка чисел
            "presets": [
                {
                    "key": p.key,
                    "emoji": p.emoji,
                    "name": p.name,
                    "summary": p.summary,
                    "detail": p.detail,
                    "expect": p.expect,
                }
                for p in PRESETS
            ],
            "preset": config.current_preset(user_id=uid),
            "configured": bool(uid and config.is_configured(uid)),
            "summary": config.explain(user_id=uid),
            "readonly": {
                "exchange": settings.exchange,
                "timezone": settings.timezone,
                "webapp_url": settings.webapp_url,
            },
        }

    @app.post("/api/config")
    async def update_config(
        payload: dict, auth: dict = Depends(require_owner)
    ) -> dict:
        """Меняет параметры ТОГО, кто прислал запрос."""
        uid = current_user_id(auth)
        if not isinstance(payload, dict) or not payload:
            raise HTTPException(status_code=400, detail="Нечего сохранять")
        try:
            applied = await config.set_many(payload, user_id=uid)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await repo.log_event(
            "info",
            "Настройки изменены: " + ", ".join(applied),
            {"applied": {k: str(v) for k, v in applied.items()}},
        )
        return {
            "applied": applied,
            "fields": config.schema(user_id=uid),
            "preset": config.current_preset(user_id=uid),
            "summary": config.explain(user_id=uid),
        }

    @app.post("/api/config/preset/{key}")
    async def apply_preset(key: str, auth: dict = Depends(require_owner)) -> dict:
        """Применяет готовый режим целиком — одним нажатием."""
        uid = current_user_id(auth)
        try:
            preset = await config.apply_preset(key, user_id=uid)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await repo.log_event("info", f"Выбран режим «{preset.name}»", {"preset": key})
        return {
            "preset": preset.key,
            "name": preset.name,
            "expect": preset.expect,
            "fields": config.schema(user_id=uid),
            "summary": config.explain(user_id=uid),
        }

    @app.post("/api/config/{key}/reset")
    async def reset_config(key: str, auth: dict = Depends(require_owner)) -> dict:
        uid = current_user_id(auth)
        try:
            value = await config.reset(key, user_id=uid)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"key": key, "value": value}

    # ------------------------------------------------------------------
    # Уведомления по цене
    # ------------------------------------------------------------------

    @app.get("/api/alerts")
    async def alerts_list(
        status: str = "ACTIVE", auth: dict = Depends(require_owner)
    ) -> dict:
        """Заказанные уровни вместе с текущими ценами."""
        uid = current_user_id(auth)
        items = await repo.list_alerts(owner_id=uid, status=status or None)
        prices: dict[str, float] = {}
        if items:
            try:
                prices = await feed.fetch_prices(
                    sorted({a.symbol for a in items})
                )
            except Exception:
                prices = {}

        out = []
        for alert in items:
            data = alert.to_dict()
            now = prices.get(alert.symbol)
            data["current_price"] = now
            # Сколько осталось идти — главное, что хочется видеть в списке
            data["distance_pct"] = (
                abs(now - alert.price) / now * 100 if now else None
            )
            out.append(data)
        return {"items": out, "prices": prices}

    @app.post("/api/alerts")
    async def alerts_create(
        payload: dict, auth: dict = Depends(require_owner)
    ) -> dict:
        """Заводит уведомление.

        Принимает либо готовые symbol и price, либо строку text —
        ту самую фразу «биткоин 95000», что человек пишет боту.
        """
        uid = current_user_id(auth)
        if uid is None:
            raise HTTPException(status_code=401, detail="Не удалось определить вас")

        symbol = str(payload.get("symbol") or "").strip()
        note = str(payload.get("note") or "")[:120]
        price = payload.get("price")

        text = str(payload.get("text") or "").strip()
        if text and (not symbol or price is None):
            parsed = alerts_input.parse_request(text)
            if parsed is None:
                raise HTTPException(
                    status_code=400,
                    detail="Не нашёл цену. Напишите, например: биткоин 95000",
                )
            names, price, parsed_note = parsed
            note = note or parsed_note
            known = list(config.get("symbols", user_id=uid) or [])
            symbol = symbol or (alerts_input.resolve_any(names, known) or "")
            if not symbol:
                raise HTTPException(
                    status_code=400,
                    detail="Не понял, о каком инструменте речь. "
                           "Напишите его название перед ценой.",
                )

        if not symbol:
            raise HTTPException(status_code=400, detail="Не указан инструмент")
        try:
            price = float(str(price).replace(",", "."))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Цена должна быть числом")
        if price <= 0:
            raise HTTPException(status_code=400, detail="Цена должна быть больше нуля")

        current = await feed.fetch_price(symbol)
        if current is None:
            raise HTTPException(
                status_code=400,
                detail="Не удалось узнать текущую цену этого инструмента",
            )

        alert = await repo.create_alert(
            owner_id=uid, symbol=symbol, price=price,
            start_price=current, note=note,
            repeat=bool(payload.get("repeat")),
        )
        data = alert.to_dict()
        data["current_price"] = current
        data["distance_pct"] = abs(current - price) / current * 100
        return data

    @app.delete("/api/alerts/{alert_id}")
    async def alerts_delete(
        alert_id: int, auth: dict = Depends(require_owner)
    ) -> dict:
        uid = current_user_id(auth)
        ok = await repo.cancel_alert(alert_id, owner_id=uid)
        if not ok:
            raise HTTPException(status_code=404, detail="Уведомление не найдено")
        return {"ok": True, "id": alert_id}

    @app.get("/api/brokers")
    async def brokers(_: dict = Depends(require_owner)) -> dict:
        """Площадки и их состояние — биржа и брокер опционов."""
        return {
            "items": await feed.health_all(),
            "pocket_configured": feed.pocket_broker.configured,
            "pocket_library": po_library_available(),
        }

    @app.get("/api/symbols")
    async def search_symbols(
        q: str = Query(default="", max_length=40),
        broker: str = Query(default="ex"),
        limit: int = Query(default=30, le=100),
        auth: dict = Depends(require_owner),
    ) -> dict:
        """Поиск инструментов — чтобы добавлять пары прямо из приложения."""
        uid = current_user_id(auth)
        cfg = config.view(uid) if uid else config
        broker = broker if broker in ("ex", "po") else "ex"
        try:
            available = await feed.list_symbols(broker)
        except BrokerError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Площадка недоступна: {exc}")

        query = q.strip().upper()
        selected = list(cfg.get("symbols") or [])
        otc_only = bool(cfg.get("po_otc_only")) and broker == "po"

        matched = [
            a for a in available
            if (not query or query in a.symbol.upper() or query in (a.name or "").upper())
            and (not otc_only or a.is_otc)
        ]

        def rank(a) -> tuple:
            if broker == "po":
                # Сначала OTC (работают в выходные) и с высокой выплатой
                return (0 if a.is_otc else 1, -(a.payout or 0), a.symbol)

            # На бирже вперёд ходовые активы, затем бессрочные фьючерсы.
            # Сортировать по длине имени нельзя: наверх всплывают
            # односимвольные тикеры вроде A/USDT, которые никому не нужны.
            base = a.symbol.split("/")[0].upper()
            popularity = MAJORS.index(base) if base in MAJORS else len(MAJORS)
            return (
                popularity,
                0 if a.symbol.endswith(":USDT") else 1,
                a.symbol,
            )

        matched = sorted(matched, key=rank)[:limit]
        return {
            "broker": broker,
            "items": [
                {**a.to_dict(), "selected": a.symbol in selected} for a in matched
            ],
            "total_available": len(available),
            "selected": selected,
        }

    @app.get("/api/help")
    async def help_topics(_: dict = Depends(require_owner)) -> dict:
        """Справка. Тексты общие с ботом — см. app/help.py."""
        return {"topics": help_content.TOPICS}

    @app.get("/api/events")
    async def events(
        limit: int = Query(default=30, le=100), _: dict = Depends(require_owner)
    ) -> dict:
        return {"items": await repo.recent_events(limit)}

    @app.get("/api/health")
    async def health() -> JSONResponse:
        """Открытый эндпоинт для мониторинга — без приватных данных."""
        scanner = runtime.get("scanner")
        ok = bool(scanner and scanner.running)
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "status": "ok" if ok else "starting",
                "uptime_sec": int(time.time() - runtime.get("started_at", time.time())),
            },
        )

    # ------------------------------------------------------------------
    # Mini App
    # ------------------------------------------------------------------

    if WEBAPP_DIR.exists():
        app.mount(
            "/static", StaticFiles(directory=str(WEBAPP_DIR)), name="static"
        )

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(
                WEBAPP_DIR / "index.html",
                headers={"Cache-Control": "no-store"},
            )

    return app
