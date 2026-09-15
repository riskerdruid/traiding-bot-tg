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

from app.api.auth import require_owner
from app.bot.formatters import position_sizing
from app.config import settings
from app.market.feed import BrokerError, feed
from app.market.pocketoption import library_available as po_library_available
from app.news.calendar import calendar
from app.storage import repo
from app.storage.settings_store import GROUPS, ValidationError, config

log = logging.getLogger("api.server")

WEBAPP_DIR = Path(__file__).parent / "webapp"

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
    async def overview(_: dict = Depends(require_owner)) -> dict:
        """Всё для главного экрана одним запросом — меньше round-trip'ов."""
        active = await repo.get_active_signals()
        prices = await feed.fetch_prices(config.get("symbols") or [])
        day_stats = await repo.stats(since=_since("day"))
        all_stats = await repo.stats()

        scanner = runtime.get("scanner")
        tracker = runtime.get("tracker")
        scanner_state = scanner.state() if scanner else {"running": False, "scans_done": 0}
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
                "symbols": config.get("symbols") or [],
                "timeframe": config.get("timeframe"),
                "htf_timeframe": config.get("htf_timeframe"),
                "min_confidence": config.get("min_confidence"),
                "risk_reward": config.get("risk_reward"),
                "news_filter": config.get("news_filter_enabled"),
                "deposit": config.get("deposit"),
                "risk_per_trade": config.get("risk_per_trade"),
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
        _: dict = Depends(require_owner),
    ) -> dict:
        items = await repo.list_signals(
            limit=limit,
            offset=offset,
            symbol=symbol,
            status=status,
            since=_since(period),
        )
        total = await repo.count_signals(symbol=symbol, status=status)

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
        period: str = "all", symbol: str | None = None, _: dict = Depends(require_owner)
    ) -> dict:
        since = _since(period)
        return {
            "period": period,
            "summary": await repo.stats(since=since, symbol=symbol),
            "by_symbol": await repo.stats_by_symbol(since=since),
            "by_hour": await repo.stats_by_hour(since=since),
            "equity": await repo.equity_curve(since=since),
        }

    @app.get("/api/candles")
    async def candles(
        symbol: str | None = None,
        timeframe: str | None = None,
        limit: int = Query(default=120, le=300),
        _: dict = Depends(require_owner),
    ) -> dict:
        """Свечи для графика в приложении."""
        symbols_now = config.get("symbols") or []
        symbol = symbol or (symbols_now[0] if symbols_now else "")
        if not symbol:
            raise HTTPException(status_code=400, detail="Инструмент не задан")
        timeframe = timeframe or config.get("timeframe")
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
    async def news(_: dict = Depends(require_owner)) -> dict:
        await calendar.refresh()
        muted = calendar.mute_reason()
        return {
            "upcoming": [e.to_dict() for e in calendar.upcoming(limit=12)],
            "today": [e.to_dict() for e in calendar.today()],
            "muted_by": muted.to_dict() if muted else None,
            "enabled": config.get("news_filter_enabled"),
            "window": {
                "before_min": config.get("news_mute_before_min"),
                "after_min": config.get("news_mute_after_min"),
            },
        }

    # ------------------------------------------------------------------
    # Настройки — всё, что заказчик меняет сам, без .env и сервера
    # ------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config(_: dict = Depends(require_owner)) -> dict:
        """Описание всех параметров + текущие значения.

        Интерфейс строится из этого ответа, поэтому новый параметр
        появляется в приложении сам, без правки вёрстки.
        """
        await config.load()
        return {
            "groups": [
                {"key": key, "label": label} for key, label in GROUPS.items()
            ],
            "fields": config.schema(),
            "readonly": {
                "exchange": settings.exchange,
                "timezone": settings.timezone,
                "webapp_url": settings.webapp_url,
            },
        }

    @app.post("/api/config")
    async def update_config(
        payload: dict, _: dict = Depends(require_owner)
    ) -> dict:
        """Меняет один или несколько параметров. Применяется немедленно."""
        if not isinstance(payload, dict) or not payload:
            raise HTTPException(status_code=400, detail="Нечего сохранять")
        try:
            applied = await config.set_many(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await repo.log_event(
            "info",
            "Настройки изменены: " + ", ".join(applied),
            {"applied": {k: str(v) for k, v in applied.items()}},
        )
        return {"applied": applied, "fields": config.schema()}

    @app.post("/api/config/{key}/reset")
    async def reset_config(key: str, _: dict = Depends(require_owner)) -> dict:
        try:
            value = await config.reset(key)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"key": key, "value": value}

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
        _: dict = Depends(require_owner),
    ) -> dict:
        """Поиск инструментов — чтобы добавлять пары прямо из приложения."""
        broker = broker if broker in ("ex", "po") else "ex"
        try:
            available = await feed.list_symbols(broker)
        except BrokerError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Площадка недоступна: {exc}")

        query = q.strip().upper()
        selected = list(config.get("symbols") or [])
        otc_only = bool(config.get("po_otc_only")) and broker == "po"

        matched = [
            a for a in available
            if (not query or query in a.symbol.upper() or query in (a.name or "").upper())
            and (not otc_only or a.is_otc)
        ]

        def rank(a) -> tuple:
            if broker == "po":
                # Сначала OTC (работают в выходные) и с высокой выплатой
                return (0 if a.is_otc else 1, -(a.payout or 0), a.symbol)
            # На бирже вперёд бессрочные фьючерсы к USDT
            return (0 if a.symbol.endswith(":USDT") else 1, len(a.symbol), a.symbol)

        matched = sorted(matched, key=rank)[:limit]
        return {
            "broker": broker,
            "items": [
                {**a.to_dict(), "selected": a.symbol in selected} for a in matched
            ],
            "total_available": len(available),
            "selected": selected,
        }

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
