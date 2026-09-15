"""Технические индикаторы на чистом Python.

Сознательно без numpy/pandas/TA-Lib: на нескольких сотнях свечей разницы в
скорости нет, а установка проекта становится тривиальной на любой машине —
не нужен ни компилятор C, ни системная библиотека ta-lib.

Все функции возвращают список той же длины, что и вход. Позиции, для которых
индикатор ещё не определён (период прогрева), заполнены None.
"""

from __future__ import annotations

Series = list[float]
OptSeries = list[float | None]


# --------------------------------------------------------------------------
# Скользящие средние
# --------------------------------------------------------------------------


def sma(values: Series, period: int) -> OptSeries:
    """Простая скользящая средняя."""
    out: OptSeries = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: Series, period: int) -> OptSeries:
    """Экспоненциальная скользящая средняя.

    Стартовое значение — SMA за первый период, дальше рекуррентно.
    """
    out: OptSeries = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def wilder_smooth(values: Series, period: int) -> OptSeries:
    """Сглаживание Уайлдера (RMA) — используется в RSI, ATR и ADX."""
    out: OptSeries = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


# --------------------------------------------------------------------------
# Осцилляторы
# --------------------------------------------------------------------------


def rsi(closes: Series, period: int = 14) -> OptSeries:
    """Relative Strength Index по классическому методу Уайлдера."""
    out: OptSeries = [None] * len(closes)
    if len(closes) <= period:
        return out

    gains: Series = [0.0]
    losses: Series = [0.0]
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def stochastic(
    highs: Series, lows: Series, closes: Series, k_period: int = 14, d_period: int = 3
) -> tuple[OptSeries, OptSeries]:
    """Стохастик: возвращает (%K, %D)."""
    n = len(closes)
    k_line: OptSeries = [None] * n
    for i in range(k_period - 1, n):
        window_high = max(highs[i - k_period + 1 : i + 1])
        window_low = min(lows[i - k_period + 1 : i + 1])
        spread = window_high - window_low
        k_line[i] = 50.0 if spread == 0 else (closes[i] - window_low) / spread * 100.0

    valid = [v for v in k_line if v is not None]
    d_raw = sma(valid, d_period)
    d_line: OptSeries = [None] * n
    offset = n - len(valid)
    for idx, value in enumerate(d_raw):
        d_line[offset + idx] = value
    return k_line, d_line


# --------------------------------------------------------------------------
# Волатильность
# --------------------------------------------------------------------------


def true_range(highs: Series, lows: Series, closes: Series) -> Series:
    """Истинный диапазон свечи."""
    if not highs:
        return []
    tr: Series = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        prev_close = closes[i - 1]
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
        )
    return tr


def atr(highs: Series, lows: Series, closes: Series, period: int = 14) -> OptSeries:
    """Average True Range — база для расчёта стоп-лосса."""
    return wilder_smooth(true_range(highs, lows, closes), period)


def bollinger(
    closes: Series, period: int = 20, mult: float = 2.0
) -> tuple[OptSeries, OptSeries, OptSeries]:
    """Полосы Боллинджера: (верхняя, средняя, нижняя)."""
    mid = sma(closes, period)
    upper: OptSeries = [None] * len(closes)
    lower: OptSeries = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        mean = mid[i]
        if mean is None:
            continue
        window = closes[i - period + 1 : i + 1]
        variance = sum((x - mean) ** 2 for x in window) / period
        sd = variance**0.5
        upper[i] = mean + mult * sd
        lower[i] = mean - mult * sd
    return upper, mid, lower


# --------------------------------------------------------------------------
# Трендовые
# --------------------------------------------------------------------------


def macd(
    closes: Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[OptSeries, OptSeries, OptSeries]:
    """MACD: (линия, сигнальная, гистограмма)."""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    n = len(closes)

    macd_line: OptSeries = [None] * n
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]

    valid = [v for v in macd_line if v is not None]
    signal_raw = ema(valid, signal)
    signal_line: OptSeries = [None] * n
    offset = n - len(valid)
    for idx, value in enumerate(signal_raw):
        signal_line[offset + idx] = value

    hist: OptSeries = [None] * n
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            hist[i] = macd_line[i] - signal_line[i]
    return macd_line, signal_line, hist


def adx(
    highs: Series, lows: Series, closes: Series, period: int = 14
) -> tuple[OptSeries, OptSeries, OptSeries]:
    """ADX и направленные индикаторы: (adx, +DI, -DI).

    ADX измеряет силу тренда независимо от его направления. Значение ниже 20
    обычно означает боковик, где трендовые входы работают плохо.
    """
    n = len(closes)
    if n < period * 2:
        return [None] * n, [None] * n, [None] * n

    plus_dm: Series = [0.0]
    minus_dm: Series = [0.0]
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    tr = true_range(highs, lows, closes)
    tr_s = wilder_smooth(tr, period)
    plus_s = wilder_smooth(plus_dm, period)
    minus_s = wilder_smooth(minus_dm, period)

    plus_di: OptSeries = [None] * n
    minus_di: OptSeries = [None] * n
    dx: OptSeries = [None] * n
    for i in range(n):
        if tr_s[i] is None or not tr_s[i] or plus_s[i] is None or minus_s[i] is None:
            continue
        pdi = 100.0 * plus_s[i] / tr_s[i]
        mdi = 100.0 * minus_s[i] / tr_s[i]
        plus_di[i] = pdi
        minus_di[i] = mdi
        total = pdi + mdi
        dx[i] = 0.0 if total == 0 else 100.0 * abs(pdi - mdi) / total

    dx_valid = [v for v in dx if v is not None]
    adx_raw = wilder_smooth(dx_valid, period)
    adx_line: OptSeries = [None] * n
    offset = n - len(dx_valid)
    for idx, value in enumerate(adx_raw):
        adx_line[offset + idx] = value
    return adx_line, plus_di, minus_di


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------


def crossed_up(fast: OptSeries, slow: OptSeries, at: int = -1) -> bool:
    """Быстрая линия пересекла медленную снизу вверх на свече `at`."""
    return _cross(fast, slow, at, up=True)


def crossed_down(fast: OptSeries, slow: OptSeries, at: int = -1) -> bool:
    """Быстрая линия пересекла медленную сверху вниз на свече `at`."""
    return _cross(fast, slow, at, up=False)


def _cross(fast: OptSeries, slow: OptSeries, at: int, up: bool) -> bool:
    if len(fast) < 2 or len(slow) < 2:
        return False
    i = at if at >= 0 else len(fast) + at
    if i < 1 or i >= len(fast) or i >= len(slow):
        return False
    a_prev, a_now = fast[i - 1], fast[i]
    b_prev, b_now = slow[i - 1], slow[i]
    if a_prev is None or a_now is None or b_prev is None or b_now is None:
        return False
    if up:
        return a_prev <= b_prev and a_now > b_now
    return a_prev >= b_prev and a_now < b_now


def slope(series: OptSeries, lookback: int = 5) -> float | None:
    """Наклон линии за последние `lookback` точек, в процентах."""
    valid = [v for v in series if v is not None]
    if len(valid) < lookback + 1 or valid[-lookback - 1] == 0:
        return None
    start, end = valid[-lookback - 1], valid[-1]
    return (end - start) / abs(start) * 100.0


def last_valid(series: OptSeries) -> float | None:
    """Последнее определённое значение индикатора."""
    for value in reversed(series):
        if value is not None:
            return value
    return None
