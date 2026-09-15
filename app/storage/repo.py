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
    owner_id: int | None = None

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
        owner_id=_row_get(row, "owner_id", None),
    )


def _row_get(row: Any, key: str, default: Any) -> Any:
    """Безопасное чтение колонки: база могла быть создана до миграции."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _owner_clause(owner_id: int | None, prefix: str = " AND") -> tuple[str, list]:
    """Условие «этот сигнал мой».

    Сигналы, заведённые до разделения по пользователям, имеют owner_id
    NULL — показываем их всем, иначе прежняя история исчезла бы.
    """
    if owner_id is None:
        return "", []
    return f"{prefix} (owner_id = ? OR owner_id IS NULL)", [owner_id]


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
    owner_id: int | None = None,
) -> Signal:
    conn = await db.connect()
    now = int(time.time())
    cursor = await conn.execute(
        """
        INSERT INTO signals
            (symbol, side, timeframe, entry, stop_loss, take_profit,
             confidence, reasons, indicators, status, created_at,
             broker, kind, expiry_at, payout, owner_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            owner_id,
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


async def get_active_signals(
    symbol: str | None = None, owner_id: int | None = None
) -> list[Signal]:
    conn = await db.connect()
    sql = "SELECT * FROM signals WHERE status = ?"
    params: list[Any] = [ACTIVE]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    clause, extra = _owner_clause(owner_id)
    sql += clause
    params += extra
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
    owner_id: int | None = None,
) -> list[Signal]:
    conn = await db.connect()
    sql = "SELECT * FROM signals WHERE 1=1"
    params: list[Any] = []
    clause, extra = _owner_clause(owner_id)
    sql += clause
    params += extra
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


async def count_signals(
    symbol: str | None = None,
    status: str | None = None,
    owner_id: int | None = None,
) -> int:
    conn = await db.connect()
    sql = "SELECT COUNT(*) AS n FROM signals WHERE 1=1"
    params: list[Any] = []
    clause, extra = _owner_clause(owner_id)
    sql += clause
    params += extra
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


async def last_signal_at(symbol: str, owner_id: int | None = None) -> int | None:
    """Время последнего сигнала по инструменту — для паузы.

    Пауза личная: сигнал одного получателя не должен затыкать другого.
    """
    conn = await db.connect()
    sql = "SELECT created_at FROM signals WHERE symbol = ?"
    params: list[Any] = [symbol]
    if owner_id is not None:
        sql += " AND owner_id = ?"
        params.append(owner_id)
    sql += " ORDER BY created_at DESC LIMIT 1"
    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    return row["created_at"] if row else None


# --------------------------------------------------------------------------
# Статистика
# --------------------------------------------------------------------------


async def stats(
    since: int | None = None,
    symbol: str | None = None,
    owner_id: int | None = None,
) -> dict:
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
    clause, extra = _owner_clause(owner_id)
    sql += clause
    params += extra
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
    active = await count_signals(symbol=symbol, status=ACTIVE, owner_id=owner_id)

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


async def stats_by_symbol(
    since: int | None = None, owner_id: int | None = None
) -> list[dict]:
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
    clause, extra = _owner_clause(owner_id)
    sql += clause
    params += extra
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


async def stats_by_hour(
    since: int | None = None, owner_id: int | None = None
) -> list[dict]:
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
    clause, extra = _owner_clause(owner_id)
    sql += clause
    params += extra
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


async def equity_curve(
    since: int | None = None, limit: int = 200,
    owner_id: int | None = None,
) -> list[dict]:
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
    clause, extra = _owner_clause(owner_id)
    sql += clause
    params += extra
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


# --------------------------------------------------------------------------
# Уведомления по цене
#
# Человек называет уровень — бот сообщает, когда рынок до него дошёл.
# Это не сигнал и не сделка: ни входа, ни стопа здесь нет, только
# «сообщи, когда биткоин будет стоить 95 000».
# --------------------------------------------------------------------------

ALERT_ACTIVE = "ACTIVE"
ALERT_DONE = "DONE"
ALERT_CANCELLED = "CANCELLED"

UP = "up"
DOWN = "down"


@dataclass(slots=True)
class Alert:
    id: int
    owner_id: int
    symbol: str
    price: float
    direction: str
    start_price: float | None
    note: str
    repeat: bool
    status: str
    created_at: int
    triggered_at: int | None = None
    hit_price: float | None = None
    # Уровень мог быть задан не ценой, а движением в процентах от текущей.
    # Само движение храним, чтобы сказать человеку «упал на 1.5%», а не
    # только назвать цену, которую он не вводил.
    percent: float | None = None

    @property
    def is_active(self) -> bool:
        return self.status == ALERT_ACTIVE

    def reached(self, price: float) -> bool:
        """Дошла ли цена до заказанного уровня."""
        if self.direction == UP:
            return price >= self.price
        return price <= self.price

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "symbol": self.symbol,
            "price": self.price,
            "direction": self.direction,
            "start_price": self.start_price,
            "note": self.note,
            "repeat": self.repeat,
            "percent": self.percent,
            "status": self.status,
            "created_at": self.created_at,
            "triggered_at": self.triggered_at,
            "hit_price": self.hit_price,
        }


def _row_to_alert(row: Any) -> Alert:
    return Alert(
        id=row["id"],
        owner_id=row["owner_id"],
        symbol=row["symbol"],
        price=row["price"],
        direction=row["direction"],
        start_price=row["start_price"],
        note=row["note"] or "",
        repeat=bool(row["repeat"]),
        percent=row["percent"],
        status=row["status"],
        created_at=row["created_at"],
        triggered_at=row["triggered_at"],
        hit_price=row["hit_price"],
    )


async def create_alert(
    *,
    owner_id: int,
    symbol: str,
    price: float | None = None,
    start_price: float | None = None,
    direction: str | None = None,
    note: str = "",
    repeat: bool = False,
    percent: float | None = None,
) -> Alert:
    """Заводит уведомление.

    Уровень задаётся либо ценой, либо движением в процентах от текущей —
    во втором случае цену считаем здесь и дальше живём с обычным уровнем.
    Так вся проверка остаётся одной строчкой сравнения.

    Сторону определяем сами по текущей цене: человек называет число,
    а не направление. Если цена уже выше заказанной, ждать её сверху
    бессмысленно — значит, ждём снижения.
    """
    if percent is not None:
        if start_price is None:
            raise ValueError("Для движения в процентах нужна текущая цена")
        if direction is None:
            direction = UP
        shift = start_price * float(percent) / 100
        price = start_price + shift if direction == UP else start_price - shift

    if price is None:
        raise ValueError("Не задан уровень уведомления")

    if direction is None:
        direction = UP if (start_price is None or price >= start_price) else DOWN

    conn = await db.connect()
    now = int(time.time())
    cursor = await conn.execute(
        """
        INSERT INTO alerts
            (owner_id, symbol, price, direction, start_price, note,
             repeat, status, created_at, percent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (owner_id, symbol, float(price), direction, start_price, note,
         int(bool(repeat)), ALERT_ACTIVE, now, percent),
    )
    await conn.commit()
    return Alert(
        id=cursor.lastrowid,
        owner_id=owner_id,
        symbol=symbol,
        price=float(price),
        direction=direction,
        start_price=start_price,
        note=note,
        repeat=bool(repeat),
        status=ALERT_ACTIVE,
        created_at=now,
        percent=percent,
    )


async def create_alert_pair(
    *,
    owner_id: int,
    symbol: str,
    percent: float,
    start_price: float,
    note: str = "",
) -> list[Alert]:
    """Движение на N процентов в любую сторону — это два уведомления.

    Человек, написавший просто «биткоин 2%», хочет знать о заметном
    движении, а не о росте именно вверх. Двумя записями это выражается
    честнее, чем одна запись с признаком: сработает та, до которой
    дошла цена, вторую можно снять.
    """
    return [
        await create_alert(
            owner_id=owner_id, symbol=symbol, percent=percent,
            start_price=start_price, direction=side, note=note,
        )
        for side in (UP, DOWN)
    ]


async def get_alert(alert_id: int) -> Alert | None:
    conn = await db.connect()
    async with conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_alert(row) if row else None


async def list_alerts(
    owner_id: int | None = None,
    status: str | None = ALERT_ACTIVE,
    limit: int = 100,
) -> list[Alert]:
    conn = await db.connect()
    sql = "SELECT * FROM alerts WHERE 1 = 1"
    params: list[Any] = []
    if owner_id is not None:
        sql += " AND owner_id = ?"
        params.append(owner_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_alert(r) for r in rows]


async def active_alert_symbols() -> list[str]:
    """За какими инструментами вообще нужно следить ради уведомлений."""
    conn = await db.connect()
    async with conn.execute(
        "SELECT DISTINCT symbol FROM alerts WHERE status = ?", (ALERT_ACTIVE,)
    ) as cur:
        rows = await cur.fetchall()
    return [r["symbol"] for r in rows]


async def trigger_alert(alert_id: int, price: float) -> Alert | None:
    """Отмечает срабатывание.

    Повторяющееся уведомление остаётся активным, но его уровень
    переворачивается: иначе на границе оно зазвонит на каждой проверке.
    """
    alert = await get_alert(alert_id)
    if alert is None or not alert.is_active:
        return alert

    conn = await db.connect()
    now = int(time.time())
    if alert.repeat:
        flipped = DOWN if alert.direction == UP else UP
        await conn.execute(
            "UPDATE alerts SET direction = ?, triggered_at = ?, hit_price = ? "
            "WHERE id = ?",
            (flipped, now, price, alert_id),
        )
        alert.direction = flipped
    else:
        await conn.execute(
            "UPDATE alerts SET status = ?, triggered_at = ?, hit_price = ? "
            "WHERE id = ?",
            (ALERT_DONE, now, price, alert_id),
        )
        alert.status = ALERT_DONE
    await conn.commit()
    alert.triggered_at = now
    alert.hit_price = price
    return alert


async def cancel_alert(alert_id: int, owner_id: int | None = None) -> bool:
    """Снимает уведомление. Чужое снять нельзя."""
    conn = await db.connect()
    sql = "UPDATE alerts SET status = ? WHERE id = ? AND status = ?"
    params: list[Any] = [ALERT_CANCELLED, alert_id, ALERT_ACTIVE]
    if owner_id is not None:
        sql += " AND owner_id = ?"
        params.append(owner_id)
    cursor = await conn.execute(sql, params)
    await conn.commit()
    return cursor.rowcount > 0


async def cancel_all_alerts(owner_id: int) -> int:
    conn = await db.connect()
    cursor = await conn.execute(
        "UPDATE alerts SET status = ? WHERE owner_id = ? AND status = ?",
        (ALERT_CANCELLED, owner_id, ALERT_ACTIVE),
    )
    await conn.commit()
    return cursor.rowcount


async def count_alerts(owner_id: int, status: str = ALERT_ACTIVE) -> int:
    conn = await db.connect()
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE owner_id = ? AND status = ?",
        (owner_id, status),
    ) as cur:
        row = await cur.fetchone()
    return row["n"] if row else 0
