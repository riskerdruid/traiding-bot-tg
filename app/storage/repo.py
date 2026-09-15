"""Запросы к базе: сигналы, статистика, настройки, события.

Здесь же считается вся статистика, которую видит заказчик — winrate,
профит-фактор, разбивка по инструментам и часам. Это главный отчётный
артефакт продукта, поэтому расчёты держим в одном месте.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from app.storage.db import db

ACTIVE = "ACTIVE"
TP_HIT = "TP_HIT"
SL_HIT = "SL_HIT"
EXPIRED = "EXPIRED"
CANCELLED = "CANCELLED"

CLOSED_STATUSES = (TP_HIT, SL_HIT, EXPIRED)


@dataclass(slots=True)
class Signal:
    id: int
    symbol: str
    side: str
    timeframe: str
    entry: float
    stop_loss: float
    take_profit: float
    confidence: int
    reasons: list[str]
    indicators: dict[str, Any]
    status: str
    created_at: int
    closed_at: int | None = None
    exit_price: float | None = None
    pnl_pct: float | None = None
    r_multiple: float | None = None
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    notified: bool = False

    # Бинарные опционы
    broker: str = "ex"
    kind: str = "exchange"
    expiry_at: int | None = None
    payout: float | None = None

    @property
    def is_long(self) -> bool:
        return self.side == "LONG"

    @property
    def is_binary(self) -> bool:
        """Опцион с фиксированным сроком, а не позиция со стопом."""
        return self.kind == "binary"

    @property
    def expiry_minutes(self) -> float | None:
        if not self.expiry_at:
            return None
        return (self.expiry_at - self.created_at) / 60

    def seconds_left(self, now: int | None = None) -> int | None:
        """Сколько секунд до экспирации. Отрицательное — уже истёк."""
        if not self.expiry_at:
            return None
        return self.expiry_at - (now or int(time.time()))

    @property
    def risk_abs(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def reward_abs(self) -> float:
        return abs(self.take_profit - self.entry)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_long"] = self.is_long
        return d

    def unrealized_pct(self, price: float) -> float:
        """Текущий результат в процентах, если сигнал ещё активен."""
        direction = 1 if self.is_long else -1
        return (price - self.entry) / self.entry * 100 * direction


def _row_to_signal(row: Any) -> Signal:
    return Signal(
        id=row["id"],
        symbol=row["symbol"],
        side=row["side"],
        timeframe=row["timeframe"],
        entry=row["entry"],
        stop_loss=row["stop_loss"],
        take_profit=row["take_profit"],
        confidence=row["confidence"],
        reasons=json.loads(row["reasons"] or "[]"),
        indicators=json.loads(row["indicators"] or "{}"),
        status=row["status"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
        exit_price=row["exit_price"],
        pnl_pct=row["pnl_pct"],
        r_multiple=row["r_multiple"],
        mfe_pct=row["mfe_pct"] or 0.0,
        mae_pct=row["mae_pct"] or 0.0,
        notified=bool(row["notified"]),
        broker=_row_get(row, "broker", "ex"),
        kind=_row_get(row, "kind", "exchange"),
        expiry_at=_row_get(row, "expiry_at", None),
        payout=_row_get(row, "payout", None),
    )


def _row_get(row: Any, key: str, default: Any) -> Any:
    """Безопасное чтение колонки: база могла быть создана до миграции."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


# --------------------------------------------------------------------------
# Сигналы
# --------------------------------------------------------------------------


async def create_signal(
    *,
    symbol: str,
    side: str,
    timeframe: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    confidence: int,
    reasons: list[str],
    indicators: dict,
    broker: str = "ex",
    kind: str = "exchange",
    expiry_at: int | None = None,
    payout: float | None = None,
) -> Signal:
    conn = await db.connect()
    now = int(time.time())
    cursor = await conn.execute(
        """
        INSERT INTO signals
            (symbol, side, timeframe, entry, stop_loss, take_profit,
             confidence, reasons, indicators, status, created_at,
             broker, kind, expiry_at, payout)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            side,
            timeframe,
            entry,
            stop_loss,
            take_profit,
            confidence,
            json.dumps(reasons, ensure_ascii=False),
            json.dumps(indicators, ensure_ascii=False),
            ACTIVE,
            now,
            broker,
            kind,
            expiry_at,
            payout,
        ),
    )
    await conn.commit()
    signal = await get_signal(cursor.lastrowid)
    assert signal is not None
    return signal


async def get_signal(signal_id: int) -> Signal | None:
    conn = await db.connect()
    async with conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_signal(row) if row else None


async def get_active_signals(symbol: str | None = None) -> list[Signal]:
    conn = await db.connect()
    sql = "SELECT * FROM signals WHERE status = ?"
    params: list[Any] = [ACTIVE]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    sql += " ORDER BY created_at DESC"
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_signal(r) for r in rows]


async def list_signals(
    limit: int = 50,
    offset: int = 0,
    symbol: str | None = None,
    status: str | None = None,
    since: int | None = None,
) -> list[Signal]:
    conn = await db.connect()
    sql = "SELECT * FROM signals WHERE 1=1"
    params: list[Any] = []
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_signal(r) for r in rows]


async def count_signals(symbol: str | None = None, status: str | None = None) -> int:
    conn = await db.connect()
    sql = "SELECT COUNT(*) AS n FROM signals WHERE 1=1"
    params: list[Any] = []
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    if status:
        sql += " AND status = ?"
        params.append(status)
    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    return row["n"] if row else 0


async def close_signal(
    signal_id: int, status: str, exit_price: float
) -> Signal | None:
    """Закрывает сигнал и считает итоговые pnl_pct и R."""
    signal = await get_signal(signal_id)
    if signal is None or signal.status != ACTIVE:
        return signal

    direction = 1 if signal.is_long else -1

    if signal.is_binary:
        # У опциона исход двоичный: угадал направление — получаешь выплату
        # брокера, не угадал — теряешь всю ставку. Поэтому и результат
        # считается не движением цены, а выплатой.
        payout = signal.payout if signal.payout is not None else 80.0
        if status == TP_HIT:
            pnl_pct = payout
            r_multiple = payout / 100.0
        elif status == SL_HIT:
            pnl_pct = -100.0
            r_multiple = -1.0
        else:
            # Истёк, не дойдя до экспирации (бот остановили) — считаем в ноль
            pnl_pct = 0.0
            r_multiple = 0.0
    else:
        pnl_pct = (exit_price - signal.entry) / signal.entry * 100 * direction
        risk = signal.risk_abs
        r_multiple = ((exit_price - signal.entry) * direction / risk) if risk else 0.0

    conn = await db.connect()
    await conn.execute(
        """
        UPDATE signals
           SET status = ?, closed_at = ?, exit_price = ?, pnl_pct = ?, r_multiple = ?
         WHERE id = ?
        """,
        (status, int(time.time()), exit_price, pnl_pct, r_multiple, signal_id),
    )
    await conn.commit()
    return await get_signal(signal_id)


async def update_excursion(signal_id: int, mfe_pct: float, mae_pct: float) -> None:
    """Обновляет максимальный ход в плюс и в минус за жизнь сигнала."""
    conn = await db.connect()
    await conn.execute(
        """
        UPDATE signals
           SET mfe_pct = MAX(COALESCE(mfe_pct, 0), ?),
               mae_pct = MIN(COALESCE(mae_pct, 0), ?)
         WHERE id = ?
        """,
        (mfe_pct, mae_pct, signal_id),
    )
    await conn.commit()


async def mark_notified(signal_id: int) -> None:
    conn = await db.connect()
    await conn.execute("UPDATE signals SET notified = 1 WHERE id = ?", (signal_id,))
    await conn.commit()


async def last_signal_at(symbol: str) -> int | None:
    """Время последнего сигнала по инструменту — для кулдауна."""
    conn = await db.connect()
    async with conn.execute(
        "SELECT created_at FROM signals WHERE symbol = ? ORDER BY created_at DESC LIMIT 1",
        (symbol,),
    ) as cur:
        row = await cur.fetchone()
    return row["created_at"] if row else None


# --------------------------------------------------------------------------
# Статистика
# --------------------------------------------------------------------------


async def stats(since: int | None = None, symbol: str | None = None) -> dict:
    """Сводная статистика по закрытым сигналам.

    winrate считается только по сигналам, дошедшим до тейка или стопа.
    Истёкшие показываем отдельно, чтобы не размывать картину.
    """
    conn = await db.connect()
    sql = "SELECT * FROM signals WHERE status IN (?, ?, ?)"
    params: list[Any] = [TP_HIT, SL_HIT, EXPIRED]
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()

    closed = [_row_to_signal(r) for r in rows]
    decided = [s for s in closed if s.status in (TP_HIT, SL_HIT)]
    wins = [s for s in decided if s.status == TP_HIT]
    losses = [s for s in decided if s.status == SL_HIT]
    expired = [s for s in closed if s.status == EXPIRED]

    gross_profit = sum(s.pnl_pct for s in wins if s.pnl_pct)
    gross_loss = abs(sum(s.pnl_pct for s in losses if s.pnl_pct))
    r_values = [s.r_multiple for s in decided if s.r_multiple is not None]
    active = await count_signals(symbol=symbol, status=ACTIVE)

    # Профит-фактор считаем в единицах риска, а не в процентах цены.
    # Трейдер рискует одинаковой суммой на каждом сигнале, а вот ATR-стоп
    # в процентах от цены каждый раз разный — если делить проценты,
    # получится искажённая цифра, противоречащая среднему R.
    r_profit = sum(r for r in r_values if r > 0)
    r_loss = abs(sum(r for r in r_values if r < 0))

    return {
        "total": len(closed),
        "decided": len(decided),
        "wins": len(wins),
        "losses": len(losses),
        "expired": len(expired),
        "active": active,
        "winrate": round(len(wins) / len(decided) * 100, 1) if decided else 0.0,
        "pnl_pct": round(sum(s.pnl_pct for s in decided if s.pnl_pct), 2),
        "avg_win_pct": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": (
            round(r_profit / r_loss, 2)
            if r_loss > 0
            else (round(r_profit, 2) if r_profit else 0.0)
        ),
        "total_r": round(sum(r_values), 2) if r_values else 0.0,
        "avg_r": round(sum(r_values) / len(r_values), 2) if r_values else 0.0,
        "best_pct": round(max((s.pnl_pct or 0) for s in decided), 2) if decided else 0.0,
        "worst_pct": round(min((s.pnl_pct or 0) for s in decided), 2) if decided else 0.0,
        "max_win_streak": _max_streak(closed, TP_HIT),
        "max_loss_streak": _max_streak(closed, SL_HIT),
    }


def _max_streak(signals: list[Signal], status: str) -> int:
    """Самая длинная серия подряд идущих исходов заданного типа."""
    ordered = sorted(signals, key=lambda s: s.created_at)
    best = current = 0
    for s in ordered:
        if s.status == status:
            current += 1
            best = max(best, current)
        elif s.status in (TP_HIT, SL_HIT):
            current = 0
    return best


async def stats_by_symbol(since: int | None = None) -> list[dict]:
    conn = await db.connect()
    sql = """
        SELECT symbol,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'TP_HIT' THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN status = 'SL_HIT' THEN 1 ELSE 0 END) AS losses,
               COALESCE(SUM(pnl_pct), 0) AS pnl
          FROM signals
         WHERE status IN ('TP_HIT', 'SL_HIT')
    """
    params: list[Any] = []
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " GROUP BY symbol ORDER BY total DESC"
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()

    out = []
    for r in rows:
        decided = r["wins"] + r["losses"]
        out.append(
            {
                "symbol": r["symbol"],
                "total": r["total"],
                "wins": r["wins"],
                "losses": r["losses"],
                "winrate": round(r["wins"] / decided * 100, 1) if decided else 0.0,
                "pnl_pct": round(r["pnl"], 2),
            }
        )
    return out


async def stats_by_hour(since: int | None = None) -> list[dict]:
    """Разбивка по часам суток (UTC) — показывает, когда стратегия работает."""
    conn = await db.connect()
    sql = """
        SELECT CAST(strftime('%H', created_at, 'unixepoch') AS INTEGER) AS hour,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'TP_HIT' THEN 1 ELSE 0 END) AS wins
          FROM signals
         WHERE status IN ('TP_HIT', 'SL_HIT')
    """
    params: list[Any] = []
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " GROUP BY hour ORDER BY hour"
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()

    by_hour = {
        r["hour"]: {
            "hour": r["hour"],
            "total": r["total"],
            "wins": r["wins"],
            "winrate": round(r["wins"] / r["total"] * 100, 1) if r["total"] else 0.0,
        }
        for r in rows
    }
    return [
        by_hour.get(h, {"hour": h, "total": 0, "wins": 0, "winrate": 0.0})
        for h in range(24)
    ]


async def equity_curve(since: int | None = None, limit: int = 200) -> list[dict]:
    """Накопленный результат в R по закрытым сигналам — для графика."""
    conn = await db.connect()
    sql = """
        SELECT id, symbol, closed_at, r_multiple, pnl_pct, status
          FROM signals
         WHERE status IN ('TP_HIT', 'SL_HIT') AND r_multiple IS NOT NULL
    """
    params: list[Any] = []
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " ORDER BY closed_at ASC LIMIT ?"
    params.append(limit)
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()

    curve = []
    cumulative_r = 0.0
    cumulative_pct = 0.0
    for idx, r in enumerate(rows, start=1):
        cumulative_r += r["r_multiple"] or 0
        cumulative_pct += r["pnl_pct"] or 0
        curve.append(
            {
                "n": idx,
                "id": r["id"],
                "symbol": r["symbol"],
                "closed_at": r["closed_at"],
                "status": r["status"],
                "r": round(r["r_multiple"] or 0, 2),
                "cum_r": round(cumulative_r, 2),
                "cum_pct": round(cumulative_pct, 2),
            }
        )
    return curve


# --------------------------------------------------------------------------
# Настройки и события
# --------------------------------------------------------------------------


async def get_setting(key: str, default: str | None = None) -> str | None:
    conn = await db.connect()
    async with conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ) as cur:
        row = await cur.fetchone()
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    conn = await db.connect()
    await conn.execute(
        """
        INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at
        """,
        (key, value, int(time.time())),
    )
    await conn.commit()


async def get_bool_setting(key: str, default: bool) -> bool:
    raw = await get_setting(key)
    if raw is None:
        return default
    return raw == "1"


async def set_bool_setting(key: str, value: bool) -> None:
    await set_setting(key, "1" if value else "0")


async def delete_setting(key: str) -> None:
    conn = await db.connect()
    await conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    await conn.commit()


async def all_settings() -> dict[str, str]:
    conn = await db.connect()
    async with conn.execute("SELECT key, value FROM app_settings") as cur:
        rows = await cur.fetchall()
    return {r["key"]: r["value"] for r in rows}


async def log_event(kind: str, message: str, payload: dict | None = None) -> None:
    conn = await db.connect()
    await conn.execute(
        "INSERT INTO events (kind, message, payload, created_at) VALUES (?, ?, ?, ?)",
        (kind, message, json.dumps(payload or {}, ensure_ascii=False), int(time.time())),
    )
    await conn.commit()


async def recent_events(limit: int = 30) -> list[dict]:
    conn = await db.connect()
    async with conn.execute(
        "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)
    ) as cur:
        rows = await cur.fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "message": r["message"],
            "payload": json.loads(r["payload"] or "{}"),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def prune_events(keep: int = 2000) -> None:
    """Чтобы журнал событий не рос бесконечно на долгоживущем сервере."""
    conn = await db.connect()
    await conn.execute(
        """
        DELETE FROM events
         WHERE id NOT IN (SELECT id FROM events ORDER BY created_at DESC LIMIT ?)
        """,
        (keep,),
    )
    await conn.commit()
