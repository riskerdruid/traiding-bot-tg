"""Бэктест стратегии на исторических данных биржи.

Прогоняет стратегию по закрытым свечам, симулирует исходы по тем же правилам,
что и живой трекер, и печатает отчёт: winrate, профит-фактор, результат в R.

Это тот самый документ, который показывают заказчику до первого живого сигнала.

Запуск:
    python -m tools.backtest
    python -m tools.backtest --symbol "XAU/USDT:USDT" --timeframe 15m --limit 1000
    python -m tools.backtest --min-confidence 70
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from app.config import settings
from app.market.feed import Candles, feed
from app.strategy.base import LONG
from app.strategy.trend_momentum import strategy


@dataclass
class Trade:
    idx: int
    ts: int
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    confidence: int
    status: str = "OPEN"
    exit_price: float = 0.0
    pnl_pct: float = 0.0
    r_multiple: float = 0.0
    bars_held: int = 0


def simulate(candles: Candles, htf: Candles, min_confidence: int, ttl_bars: int) -> list[Trade]:
    """Проходит по истории свеча за свечой, как это делал бы живой бот."""
    trades: list[Trade] = []
    open_trade: Trade | None = None
    warmup = strategy.min_candles()

    htf_by_ts = htf.items
    cooldown_until = -1

    for i in range(warmup, len(candles)):
        window = Candles(candles.symbol, candles.timeframe, candles.items[: i + 1])
        current = candles.items[i]

        # Сопоставляем старший таймфрейм по времени, чтобы не заглядывать в будущее
        htf_slice = [c for c in htf_by_ts if c.ts <= current.ts]
        htf_window = Candles(htf.symbol, htf.timeframe, htf_slice)

        # --- сопровождение открытой позиции ---
        if open_trade is not None:
            open_trade.bars_held += 1
            is_long = open_trade.side == LONG
            if is_long:
                hit_sl = current.low <= open_trade.stop_loss
                hit_tp = current.high >= open_trade.take_profit
            else:
                hit_sl = current.high >= open_trade.stop_loss
                hit_tp = current.low <= open_trade.take_profit

            closed_now = False
            # Тот же пессимистичный порядок, что и в живом трекере
            if hit_sl:
                open_trade.status = "SL_HIT"
                open_trade.exit_price = open_trade.stop_loss
                closed_now = True
            elif hit_tp:
                open_trade.status = "TP_HIT"
                open_trade.exit_price = open_trade.take_profit
                closed_now = True
            elif open_trade.bars_held >= ttl_bars:
                open_trade.status = "EXPIRED"
                open_trade.exit_price = current.close
                closed_now = True

            if closed_now:
                direction = 1 if is_long else -1
                risk = abs(open_trade.entry - open_trade.stop_loss)
                open_trade.pnl_pct = (
                    (open_trade.exit_price - open_trade.entry) / open_trade.entry * 100 * direction
                )
                open_trade.r_multiple = (
                    (open_trade.exit_price - open_trade.entry) * direction / risk if risk else 0
                )
                trades.append(open_trade)
                cooldown_until = i + max(1, settings.cooldown_min // _tf_minutes(candles.timeframe))
                open_trade = None
            continue

        if i < cooldown_until:
            continue

        # --- поиск входа ---
        result = strategy.analyze(window, htf_window)
        if result is None or result.confidence < min_confidence:
            continue

        open_trade = Trade(
            idx=i,
            ts=current.ts,
            side=result.side,
            entry=result.entry,
            stop_loss=result.stop_loss,
            take_profit=result.take_profit,
            confidence=result.confidence,
        )

    return trades


def _tf_minutes(timeframe: str) -> int:
    units = {"m": 1, "h": 60, "d": 1440}
    try:
        return int(timeframe[:-1]) * units[timeframe[-1]]
    except Exception:
        return 15


def report(trades: list[Trade], symbol: str, timeframe: str, bars: int) -> None:
    print()
    print("=" * 64)
    print(f"  БЭКТЕСТ  {symbol}  {timeframe}  ({bars} свечей)")
    print("=" * 64)

    if not trades:
        print("\n  Сигналов не найдено.")
        print("  Попробуйте снизить MIN_CONFIDENCE или ADX_MIN в .env,")
        print("  либо взять больше истории через --limit.\n")
        return

    decided = [t for t in trades if t.status in ("TP_HIT", "SL_HIT")]
    wins = [t for t in decided if t.status == "TP_HIT"]
    losses = [t for t in decided if t.status == "SL_HIT"]
    expired = [t for t in trades if t.status == "EXPIRED"]

    gross_profit = sum(t.pnl_pct for t in wins)
    gross_loss = abs(sum(t.pnl_pct for t in losses))
    total_r = sum(t.r_multiple for t in trades)
    winrate = len(wins) / len(decided) * 100 if decided else 0

    # Профит-фактор — в единицах риска: риск на сигнал одинаков,
    # а ATR-стоп в процентах цены каждый раз разный.
    r_profit = sum(t.r_multiple for t in trades if t.r_multiple > 0)
    r_loss = abs(sum(t.r_multiple for t in trades if t.r_multiple < 0))

    longs = [t for t in trades if t.side == LONG]
    shorts = [t for t in trades if t.side != LONG]

    print(f"\n  Всего сигналов      {len(trades)}")
    print(f"  Из них LONG/SHORT   {len(longs)} / {len(shorts)}")
    print(f"  Дошли до цели       {len(wins)}")
    print(f"  Дошли до стопа      {len(losses)}")
    print(f"  Истекли по времени  {len(expired)}")
    print()
    print(f"  WINRATE             {winrate:.1f}%   (по {len(decided)} решённым)")
    pf = f"{r_profit / r_loss:.2f}" if r_loss else "—"
    print(f"  Профит-фактор       {pf}   (в единицах риска)")
    print(f"  Суммарно в R        {total_r:+.2f}R")
    print(f"  Средний результат   {total_r / len(trades):+.2f}R на сигнал")
    print(f"  Сумма движения      {sum(t.pnl_pct for t in trades):+.2f}%")

    if wins:
        print(f"  Средняя прибыль     {gross_profit / len(wins):+.2f}%")
    if losses:
        print(f"  Средний убыток      {-gross_loss / len(losses):+.2f}%")

    avg_conf = sum(t.confidence for t in trades) / len(trades)
    avg_bars = sum(t.bars_held for t in trades) / len(trades)
    print(f"  Средняя уверенность {avg_conf:.0f}%")
    print(f"  Среднее время в позиции {avg_bars:.0f} свечей")

    # Просадка по кривой R
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.r_multiple
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    print(f"  Макс. просадка      {max_dd:.2f}R")

    print("\n  Последние сигналы:")
    print("  " + "-" * 60)
    print(f"  {'дата':<17}{'стор.':<7}{'вход':>10}{'исход':>10}{'R':>8}{'увер.':>7}")
    print("  " + "-" * 60)
    from datetime import datetime, timezone

    for t in trades[-12:]:
        when = datetime.fromtimestamp(t.ts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        mark = {"TP_HIT": "цель", "SL_HIT": "стоп", "EXPIRED": "истёк"}.get(t.status, t.status)
        print(
            f"  {when:<17}{t.side:<7}{t.entry:>10.2f}{mark:>10}{t.r_multiple:>+8.2f}{t.confidence:>6}%"
        )
    print()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Бэктест стратегии")
    parser.add_argument("--symbol", default=None, help="инструмент, напр. XAU/USDT:USDT")
    parser.add_argument("--timeframe", default=None, help="таймфрейм, напр. 15m")
    parser.add_argument("--limit", type=int, default=1000, help="сколько свечей взять")
    parser.add_argument(
        "--min-confidence", type=int, default=None, help="порог уверенности"
    )
    args = parser.parse_args()

    symbol = args.symbol or (settings.symbol_list[0] if settings.symbol_list else None)
    if not symbol:
        print("Не задан инструмент: заполните SYMBOLS в .env или передайте --symbol")
        return 1

    timeframe = args.timeframe or settings.timeframe
    min_conf = (
        args.min_confidence if args.min_confidence is not None else settings.min_confidence
    )

    print(f"Загружаю историю {symbol} {timeframe} ...")
    try:
        candles = await feed.fetch_history(symbol, timeframe, bars=args.limit)
        # Старшему таймфрейму нужно покрыть тот же период времени
        htf_bars = max(
            300, int(args.limit * _tf_minutes(timeframe) / _tf_minutes(settings.htf_timeframe)) + 250
        )
        htf = await feed.fetch_history(symbol, settings.htf_timeframe, bars=htf_bars)
    except Exception as exc:
        print(f"Не удалось загрузить данные: {exc}")
        await feed.close()
        return 1

    print(f"Получено {len(candles)} свечей {timeframe} и {len(htf)} свечей {settings.htf_timeframe}")

    if len(candles) < strategy.min_candles() + 50:
        print(
            f"Мало данных: {len(candles)}, стратегии нужно минимум "
            f"{strategy.min_candles()} только на прогрев."
        )
        await feed.close()
        return 1

    ttl_bars = max(1, settings.signal_ttl_min // _tf_minutes(timeframe))
    trades = simulate(candles.closed_only(), htf.closed_only(), min_conf, ttl_bars)
    report(trades, symbol, timeframe, len(candles))

    await feed.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
