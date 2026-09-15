"""Проверка индикаторов на эталонных данных.

RSI сверяется с классическим примером Уайлдера (он же используется в
документации StockCharts) — это защищает от тихих ошибок в сглаживании,
которые иначе всплыли бы только в виде плохих сигналов.

Запуск:  python -m tests.test_indicators
"""

from __future__ import annotations

import sys

from app.market import indicators as ind

# Эталонный ряд Уайлдера
WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
    46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41,
    46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03,
    44.18, 44.22, 44.57, 43.42, 42.66, 43.13,
]

# Ожидаемые RSI(14) начиная с индекса 14
WILDER_RSI_EXPECTED = [
    70.46, 66.25, 66.48, 69.35, 66.29, 57.92, 62.88, 63.21, 56.01,
    62.34, 54.68, 50.39, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
]

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def approx(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def test_rsi_reference() -> None:
    print("RSI против эталона Уайлдера")
    got = ind.rsi(WILDER_CLOSES, 14)
    check("прогрев заполнен None", all(v is None for v in got[:14]))
    max_diff = 0.0
    for offset, expected in enumerate(WILDER_RSI_EXPECTED):
        actual = got[14 + offset]
        assert actual is not None
        max_diff = max(max_diff, abs(actual - expected))
    check(
        f"совпадение с эталоном (макс. отклонение {max_diff:.3f})",
        max_diff < 0.05,
        f"отклонение {max_diff:.4f} превышает 0.05",
    )


def test_ema_sma() -> None:
    print("EMA / SMA")
    values = [float(i) for i in range(1, 21)]
    s = ind.sma(values, 5)
    check("SMA(5) последней точки = 18", approx(s[-1], 18.0, 1e-9), f"={s[-1]}")
    check("SMA длина совпадает", len(s) == len(values))

    e = ind.ema(values, 5)
    check("EMA первая точка = SMA(5) = 3", approx(e[4], 3.0, 1e-9), f"={e[4]}")
    check("EMA растёт на растущем ряде", e[-1] > e[-2])

    # На линейном ряде EMA и SMA совпадают — обе отстают одинаково.
    # Разница видна на скачке: EMA должна подойти к новой цене ближе.
    step = [10.0] * 20 + [20.0] * 3
    e_step = ind.ema(step, 5)
    s_step = ind.sma(step, 5)
    check(
        "EMA реагирует на скачок быстрее SMA",
        e_step[-1] > s_step[-1],
        f"ema={e_step[-1]:.3f} sma={s_step[-1]:.3f}",
    )


def test_atr() -> None:
    print("ATR")
    highs = [10, 12, 11, 13, 15, 14, 16, 18, 17, 19, 20, 22, 21, 23, 25]
    lows = [8, 9, 9, 10, 12, 11, 13, 15, 14, 16, 17, 19, 18, 20, 22]
    closes = [9, 11, 10, 12, 14, 13, 15, 17, 16, 18, 19, 21, 20, 22, 24]
    a = ind.atr(
        [float(x) for x in highs], [float(x) for x in lows], [float(x) for x in closes], 14
    )
    value = ind.last_valid(a)
    check("ATR определён", value is not None)
    check("ATR положителен", value > 0, f"={value}")
    check("ATR в разумных пределах", 1.0 < value < 6.0, f"={value:.3f}")


def test_adx() -> None:
    print("ADX")
    # Чистый восходящий тренд: ADX должен быть высоким, +DI больше -DI
    n = 60
    highs = [100.0 + i * 2 for i in range(n)]
    lows = [98.0 + i * 2 for i in range(n)]
    closes = [99.5 + i * 2 for i in range(n)]
    adx_line, plus_di, minus_di = ind.adx(highs, lows, closes, 14)
    a = ind.last_valid(adx_line)
    p = ind.last_valid(plus_di)
    m = ind.last_valid(minus_di)
    check("ADX определён", a is not None)
    check("на сильном тренде ADX > 40", a > 40, f"={a:.2f}")
    check("+DI доминирует в аптренде", p > m, f"+DI={p:.2f} -DI={m:.2f}")
    check("ADX в диапазоне 0..100", 0 <= a <= 100, f"={a:.2f}")

    # Боковик: ADX должен быть низким
    flat_h = [100.0 + (1 if i % 2 else 0) for i in range(n)]
    flat_l = [99.0 - (1 if i % 2 else 0) for i in range(n)]
    flat_c = [99.5 for _ in range(n)]
    flat_adx, _, _ = ind.adx(flat_h, flat_l, flat_c, 14)
    fa = ind.last_valid(flat_adx)
    check("на боковике ADX < 25", fa is not None and fa < 25, f"={fa}")


def test_macd_bollinger_stoch() -> None:
    print("MACD / Bollinger / Stochastic")
    closes = [100.0 + (i % 7) - 3 + i * 0.5 for i in range(80)]
    line, sig, hist = ind.macd(closes)
    check("MACD определён", ind.last_valid(line) is not None)
    check("сигнальная определена", ind.last_valid(sig) is not None)
    check(
        "гистограмма = линия - сигнальная",
        approx(hist[-1], line[-1] - sig[-1], 1e-9),
    )

    up, mid, low = ind.bollinger(closes, 20, 2.0)
    check("верхняя полоса выше средней", up[-1] > mid[-1])
    check("нижняя полоса ниже средней", low[-1] < mid[-1])
    check("полосы симметричны", approx(up[-1] - mid[-1], mid[-1] - low[-1], 1e-9))

    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    k, d = ind.stochastic(highs, lows, closes, 14, 3)
    kv = ind.last_valid(k)
    check("%K в диапазоне 0..100", 0 <= kv <= 100, f"={kv:.2f}")
    check("%D определён", ind.last_valid(d) is not None)


def test_crosses() -> None:
    print("Пересечения")
    fast = [1.0, 2.0, 3.0, 4.0, 5.0]
    slow = [3.0, 3.0, 3.0, 3.0, 3.0]
    check("crossed_up срабатывает", ind.crossed_up(fast, slow, 3))
    check("crossed_up не ложный", not ind.crossed_up(fast, slow, 1))
    check("crossed_up не срабатывает вниз", not ind.crossed_up([5.0, 1.0], [3.0, 3.0], 1))

    down_fast = [5.0, 4.0, 3.0, 2.0, 1.0]
    check("crossed_down на развороте", ind.crossed_down(down_fast, slow, 3))
    check("None не ломает проверку", not ind.crossed_up([None, None], [None, None]))


def test_edge_cases() -> None:
    print("Граничные случаи")
    check("пустой вход не падает", ind.rsi([], 14) == [])
    check("короткий ряд возвращает None", all(v is None for v in ind.ema([1.0, 2.0], 14)))
    check("true_range на пустом", ind.true_range([], [], []) == [])
    check("last_valid на пустом", ind.last_valid([None, None]) is None)
    check("ADX на коротком ряде", all(v is None for v in ind.adx([1.0], [1.0], [1.0], 14)[0]))
    flat = ind.rsi([50.0] * 30, 14)
    check("RSI на плоском ряде = 100 (нет убытков)", ind.last_valid(flat) == 100.0)


def main() -> int:
    for test in (
        test_rsi_reference,
        test_ema_sma,
        test_atr,
        test_adx,
        test_macd_bollinger_stoch,
        test_crosses,
        test_edge_cases,
    ):
        test()
        print()

    if failures:
        print(f"ПРОВАЛЕНО проверок: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
