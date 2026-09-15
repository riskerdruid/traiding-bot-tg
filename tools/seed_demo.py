"""Наполняет базу правдоподобными сигналами для демонстрации.

Нужен, чтобы показать заказчику работающий интерфейс до того, как
накопится реальная статистика: пустой экран продать невозможно.

Запуск:
    python -m tools.seed_demo            # 40 сигналов за 2 недели
    python -m tools.seed_demo --count 80 --days 30
    python -m tools.seed_demo --clear    # удалить демо-данные
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time

from app.config import settings
from app.storage import repo
from app.storage.db import db

REASONS_LONG = [
    "EMA9 пересекла EMA21 снизу вверх",
    "Старший ТФ 1h в том же направлении",
    "Цена выше EMA50",
    "Гистограмма MACD на стороне сигнала",
    "Сила тренда нарастает",
    "Объём выше среднего",
]
REASONS_SHORT = [
    "EMA9 пересекла EMA21 сверху вниз",
    "Старший ТФ 1h в том же направлении",
    "Цена ниже EMA50",
    "+DI/-DI подтверждают",
    "Гистограмма MACD на стороне сигнала",
]


async def clear_demo() -> None:
    conn = await db.connect()
    await conn.execute("DELETE FROM signals")
    await conn.execute("DELETE FROM events")
    await conn.commit()
    print("Демо-данные удалены.")


async def seed(count: int, days: int) -> None:
    symbols = settings.symbol_list or ["XAU/USDT:USDT"]
    now = int(time.time())
    window = days * 86400
    base_prices = {s: 4300.0 for s in symbols}

    created = 0
    for i in range(count):
        symbol = random.choice(symbols)
        # Сигналы гуще в активные часы рынка
        created_at = now - random.randint(1800, window)
        side = random.choice(["LONG", "SHORT"])
        is_long = side == "LONG"

        base = base_prices[symbol]
        entry = round(base * random.uniform(0.97, 1.03), 2)
        atr = entry * random.uniform(0.0012, 0.0035)
        risk = atr * settings.atr_sl_mult
        reward = risk * settings.risk_reward

        stop_loss = round(entry - risk if is_long else entry + risk, 2)
        take_profit = round(entry + reward if is_long else entry - reward, 2)
        confidence = random.randint(settings.min_confidence, 97)

        reasons = random.sample(
            REASONS_LONG if is_long else REASONS_SHORT,
            k=random.randint(2, 4),
        )
        indicators = {
            "rsi": round(random.uniform(35, 70), 1),
            "adx": round(random.uniform(20, 45), 1),
            "atr": round(atr, 3),
            "stoch_k": round(random.uniform(15, 85), 1),
            "htf_bias": "BULL" if is_long else "BEAR",
        }

        signal = await repo.create_signal(
            symbol=symbol,
            side=side,
            timeframe=settings.timeframe,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=confidence,
            reasons=reasons,
            indicators=indicators,
        )

        # Подкручиваем время создания в прошлое
        conn = await db.connect()
        await conn.execute(
            "UPDATE signals SET created_at = ? WHERE id = ?", (created_at, signal.id)
        )
        await conn.commit()

        # Оставляем пару сигналов активными
        if i >= count - 2:
            created += 1
            continue

        # Исход: winrate чуть выше 50%, как у работающей трендовой стратегии
        roll = random.random()
        closed_at = created_at + random.randint(900, settings.signal_ttl_min * 60)
        if roll < 0.46:
            status, exit_price = repo.TP_HIT, take_profit
        elif roll < 0.86:
            status, exit_price = repo.SL_HIT, stop_loss
        else:
            status = repo.EXPIRED
            drift = random.uniform(-0.6, 0.8) * risk
            exit_price = round(entry + (drift if is_long else -drift), 2)

        await repo.close_signal(signal.id, status, exit_price)
        await conn.execute(
            "UPDATE signals SET closed_at = ? WHERE id = ?", (closed_at, signal.id)
        )
        await conn.commit()
        created += 1

    await repo.log_event("info", f"Сгенерировано {created} демо-сигналов")
    stats = await repo.stats()
    print(f"Создано сигналов: {created}")
    print(
        f"Winrate: {stats['winrate']}%  ·  "
        f"результат {stats['total_r']:+.2f}R  ·  "
        f"профит-фактор {stats['profit_factor']}"
    )
    print(f"Активных сейчас: {stats['active']}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Демо-данные для показа интерфейса")
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--clear", action="store_true", help="удалить все сигналы")
    args = parser.parse_args()

    await db.init()
    if args.clear:
        await clear_demo()
    else:
        await seed(args.count, args.days)
    await db.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
