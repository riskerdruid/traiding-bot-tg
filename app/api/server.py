"""HTTP-сервер мини-приложения.

Приложение показывает то же самое, что и бот, но глазами: карточки
сигналов, результаты и уведомления по цене. Поэтому и API здесь узкий —
один запрос отдаёт всё, что нужно нарисовать, а менять можно ровно то,
что человек меняет в боте.

Отдельной «панели администратора» тут нет намеренно: десяток вкладок
с полусотней параметров — это то, из-за чего в прошлой версии
приложением никто не пользовался.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth import current_user_id, require_owner
from app.bot import alerts_input
from app.bot import formatters as fmt
from app.config import settings
from app.market import catalog
from app.market.feed import feed
from app.storage import repo
from app.storage.settings_store import PRESETS, ValidationError, config

log = logging.getLogger("api")

WEBAPP_DIR = Path(__file__).parent / "webapp"

# Живые компоненты — заполняется при старте из runner.py
runtime: dict = {"scanner": None, "tracker": None, "alerts": None}


def create_app() -> FastAPI:
    app = FastAPI(title="Бот торговых сигналов", docs_url=None, redoc_url=None)

    if (WEBAPP_DIR / "app.js").exists():
        app.mount(
            "/static", StaticFiles(directory=str(WEBAPP_DIR)), name="static"
        )

    # ------------------------------------------------------------------
    # Данные
    # ------------------------------------------------------------------

    @app.get("/api/overview")
    async def overview(auth: dict = Depends(require_owner)) -> dict:
        """Всё для первого экрана одним запросом.

        Дробить это на пять вызовов смысла нет: приложение всё равно
        рисует их вместе, а на мобильной сети каждый лишний запрос —
        заметная задержка.
        """
        uid = current_user_id(auth)
        cfg = config.view(uid) if uid else config

        active = await repo.get_active_signals(owner_id=uid)
        closed = [
            s
            for s in await repo.list_signals(limit=20, owner_id=uid)
            if s.status != repo.ACTIVE
        ][:10]
        alerts = await repo.list_alerts(owner_id=uid)

        prices = await _prices(
            {s.symbol for s in active} | {a.symbol for a in alerts}
        )

        data = await repo.stats(owner_id=uid)
        by_symbol = await repo.stats_by_symbol(owner_id=uid)
        risk_money = cfg.risk_money()

        return {
            "user": {"name": (auth.get("user") or {}).get("first_name") or ""},
            "pulse": _pulse(uid),
            "active": [_signal(s, prices.get(s.symbol)) for s in active],
            "recent": [_closed(s, risk_money) for s in closed],
            "stats": {
                **{
                    key: data[key]
                    for key in (
                        "wins", "losses", "expired", "active", "decided", "winrate"
                    )
                },
                "money": round(data["total_r"] * risk_money, 2) if risk_money else None,
                "by_symbol": [
                    {
                        "title": fmt.short_symbol(row["symbol"]),
                        "winrate": row["winrate"],
                        "wins": row["wins"],
                        "total": row["wins"] + row["losses"],
                    }
                    for row in by_symbol
                    if row["wins"] + row["losses"] > 0
                ][:6],
            },
            "alerts": [_alert(a, prices.get(a.symbol)) for a in alerts],
            "settings": _settings(cfg),
            "version": settings.app_version,
        }

    @app.get("/api/instruments")
    async def instruments(auth: dict = Depends(require_owner)) -> dict:
        """Что можно выбрать: и для слежения, и для уведомлений."""
        uid = current_user_id(auth)
        cfg = config.view(uid) if uid else config
        chosen = list(cfg.get("symbols") or [])
        choices = await catalog.available(chosen)
        return {
            "items": [
                {
                    "symbol": c.symbol,
                    "title": c.title,
                    "note": c.note,
                    "chosen": c.chosen,
                }
                for c in choices
            ],
            "quick": [
                {"symbol": c.symbol, "title": c.title, "note": c.note}
                for c in catalog.quick_list(chosen)
            ],
        }

    # ------------------------------------------------------------------
    # Уведомления по цене
    # ------------------------------------------------------------------

    @app.post("/api/alerts")
    async def create_alert(body: dict, auth: dict = Depends(require_owner)) -> dict:
        """Ставит будильник по цене.

        Текст разбирается тем же парсером, что и сообщение в чате:
        «95000», «-1.5%», «+2% продать половину» — что угодно из этого.
        """
        uid = current_user_id(auth)
        if uid is None:
            raise HTTPException(status_code=403, detail="Неизвестный пользователь")

        symbol = str(body.get("symbol") or "").strip()
        text = str(body.get("value") or "").strip()
        if not symbol or not text:
            raise HTTPException(status_code=400, detail="Нужны инструмент и число")

        parsed = alerts_input.parse_value(text)
        if parsed is None:
            raise HTTPException(
                status_code=400,
                detail="Не понял число. Например: 95000 или -1.5%",
            )

        current = await feed.fetch_price(symbol)
        if current is None:
            raise HTTPException(
                status_code=503,
                detail=f"Не удалось узнать цену {fmt.short_symbol(symbol)}",
            )

        if parsed.by_percent and parsed.direction is None:
            up, down = await repo.create_alert_pair(
                owner_id=uid, symbol=symbol, percent=parsed.percent,
                start_price=current, note=parsed.note,
            )
            return {"created": [_alert(up, current), _alert(down, current)]}

        alert = await repo.create_alert(
            owner_id=uid,
            symbol=symbol,
            price=None if parsed.by_percent else parsed.price,
            percent=parsed.percent if parsed.by_percent else None,
            direction=parsed.direction if parsed.by_percent else None,
            start_price=current,
            note=parsed.note,
        )
        return {"created": [_alert(alert, current)]}

    @app.delete("/api/alerts/{alert_id}")
    async def drop_alert(alert_id: int, auth: dict = Depends(require_owner)) -> dict:
        uid = current_user_id(auth)
        ok = await repo.cancel_alert(alert_id, owner_id=uid)
        return {"ok": ok}

    # ------------------------------------------------------------------
    # Настройки — те же четыре, что и в боте
    # ------------------------------------------------------------------

    @app.post("/api/settings")
    async def update_settings(body: dict, auth: dict = Depends(require_owner)) -> dict:
        uid = current_user_id(auth)
        cfg = config.view(uid) if uid else config

        try:
            if "preset" in body:
                await cfg.apply_preset(str(body["preset"]))
            if "symbols" in body:
                await cfg.set("symbols", list(body["symbols"]))
            for key in ("deposit", "risk_per_trade", "quiet_night"):
                if key in body:
                    await cfg.set(key, body[key])
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        scanner = runtime.get("scanner")
        if scanner is not None and "symbols" in body:
            scanner.invalidate()
        return {"settings": _settings(cfg)}

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------

    @app.get("/api/health")
    async def health() -> JSONResponse:
        """Без подписи: сюда стучится только мониторинг."""
        scanner = runtime.get("scanner")
        alive = bool(scanner and scanner.running)
        return JSONResponse(
            {"ok": alive, "version": settings.app_version},
            status_code=200 if alive else 503,
        )

    @app.get("/")
    async def index() -> FileResponse:
        page = WEBAPP_DIR / "index.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="Приложение не собрано")
        return FileResponse(page)

    return app


# ----------------------------------------------------------------------
# Сборка ответов
# ----------------------------------------------------------------------


async def _prices(symbols: set[str]) -> dict[str, float]:
    if not symbols:
        return {}
    try:
        return await feed.fetch_prices(sorted(symbols))
    except Exception as exc:
        log.warning("Не удалось получить цены: %s", exc)
        return {}


def _pulse(uid: int | None) -> dict:
    """Строка «бот жив» — та же, что внизу экрана сигналов в боте."""
    scanner = runtime.get("scanner")
    state = dict(scanner.state(uid)) if scanner else {"running": False}
    if not state.get("symbols"):
        state["symbols"] = list(config.get("symbols", user_id=uid) or [])
    return {
        "running": bool(state.get("running")),
        "last_scan_at": state.get("last_scan_at"),
        "muted_by_news": state.get("muted_by_news"),
        "watching": [fmt.short_symbol(s) for s in state.get("symbols") or []],
    }


def _signal(signal: repo.Signal, price: float | None) -> dict:
    """Активный сигнал вместе с тем, где сейчас цена между стопом и целью."""
    progress = None
    if price is not None and signal.take_profit != signal.stop_loss:
        span = abs(signal.take_profit - signal.stop_loss)
        done = (
            price - signal.stop_loss
            if signal.is_long
            else signal.stop_loss - price
        )
        progress = max(0.0, min(100.0, done / span * 100)) if span else None

    return {
        "id": signal.id,
        "title": fmt.short_symbol(signal.symbol),
        "side": "LONG" if signal.is_long else "SHORT",
        "side_text": _side_text(signal),
        "entry": signal.entry,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "confidence": signal.confidence,
        "reasons": signal.reasons[:6],
        "price": price,
        "pnl_pct": round(signal.unrealized_pct(price), 2) if price else None,
        "progress": round(progress, 1) if progress is not None else None,
        "is_binary": signal.is_binary,
        "expiry_at": signal.expiry_at,
        "payout": signal.payout,
        "size": fmt.position_size(signal) if not signal.is_binary else fmt.binary_stake(signal),
        "created_at": signal.created_at,
    }


def _side_text(signal: repo.Signal) -> str:
    if signal.is_binary:
        return "ВВЕРХ" if signal.is_long else "ВНИЗ"
    return "ПОКУПКА" if signal.is_long else "ПРОДАЖА"


def _closed(signal: repo.Signal, risk_money: float) -> dict:
    money = (
        round(signal.r_multiple * risk_money, 2)
        if signal.r_multiple is not None and risk_money
        else None
    )
    icon, title = (
        fmt.BINARY_STATUS_VIEW if signal.is_binary else fmt.STATUS_VIEW
    ).get(signal.status, ("•", signal.status))
    return {
        "id": signal.id,
        "title": fmt.short_symbol(signal.symbol),
        "side_text": _side_text(signal),
        "icon": icon,
        "status_text": title,
        "won": signal.status == repo.TP_HIT,
        "lost": signal.status == repo.SL_HIT,
        "pnl_pct": signal.pnl_pct,
        "money": money,
        "closed_at": signal.closed_at,
    }


def _alert(alert: repo.Alert, price: float | None) -> dict:
    distance = None
    if price:
        distance = round(abs(price - alert.price) / price * 100, 2)
    return {
        "id": alert.id,
        "symbol": alert.symbol,
        "title": fmt.short_symbol(alert.symbol),
        "price": alert.price,
        "percent": alert.percent,
        "up": alert.direction == repo.UP,
        "note": alert.note,
        "current": price,
        "distance": distance,
        "created_at": alert.created_at,
    }


def _settings(cfg) -> dict:
    symbols = list(cfg.get("symbols") or [])
    preset = cfg.preset()
    return {
        "symbols": [
            {"symbol": s, "title": fmt.short_symbol(s)} for s in symbols
        ],
        "preset": preset.key if cfg.current_preset() else None,
        "preset_name": f"{preset.emoji} {preset.name}",
        "presets": [
            {
                "key": p.key,
                "emoji": p.emoji,
                "name": p.name,
                "summary": p.summary,
                "expect": p.expect,
            }
            for p in PRESETS
        ],
        "deposit": float(cfg.get("deposit") or 0),
        "risk_per_trade": float(cfg.get("risk_per_trade") or 0),
        "risk_money": round(cfg.risk_money(), 2),
        "quiet_night": bool(cfg.get("quiet_night")),
        "now": int(time.time()),
    }
