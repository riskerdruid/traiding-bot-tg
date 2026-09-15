"""Стратегия «тренд + импульс».

Идея простая и проверяемая: торгуем только по направлению старшего
таймфрейма, входим на пересечении быстрых скользящих, но лишь когда рынок
действительно трендовый (ADX), и отсекаем входы в перекупленность.

Логика разделена на два слоя:

1. ВОРОТА (gates) — жёсткие условия. Не выполнено хотя бы одно — сигнала нет.
   Это защита от входов в боковике, где трендовая логика systematically теряет.

2. ПОДТВЕРЖДЕНИЯ (confirmations) — каждое добавляет уверенности.
   Сумма даёт confidence 0..100. Порог задаётся заказчиком в Mini App.

Стоп ставится по ATR — то есть по реальной волатильности инструмента,
а не фиксированным числом пунктов. Тейк рассчитывается от стопа через
заданное соотношение риск/прибыль.

Все числовые параметры читаются из живых настроек (app/storage/settings_store),
поэтому заказчик меняет их прямо в приложении, без перезапуска.
"""

from __future__ import annotations

from app.market import indicators as ind
from app.market.feed import Candles
from app.storage.settings_store import config
from app.strategy.base import LONG, SHORT, Strategy, StrategyResult

# Базовая уверенность при срабатывании триггера, до подтверждений
BASE_CONFIDENCE = 35


class TrendMomentumStrategy(Strategy):
    name = "trend_momentum"
    description = "Пересечение EMA по тренду старшего ТФ с фильтром ADX и RSI"

    def min_candles(self) -> int:
        return max(config.get("ema_trend_slow") + 10, 60)

    # ----------------------------------------------------------------
    # Основной метод
    # ----------------------------------------------------------------

    def analyze(
        self, candles: Candles, htf_candles: Candles | None = None
    ) -> StrategyResult | None:
        if len(candles) < self.min_candles():
            return None

        closes = candles.closes
        highs = candles.highs
        lows = candles.lows
        price = closes[-1]

        ema_fast = ind.ema(closes, config.get("ema_fast"))
        ema_slow = ind.ema(closes, config.get("ema_slow"))
        ema_trend = ind.ema(closes, config.get("ema_trend"))
        rsi_line = ind.rsi(closes, config.get("rsi_period"))
        adx_line, plus_di, minus_di = ind.adx(highs, lows, closes, config.get("adx_period"))
        atr_line = ind.atr(highs, lows, closes, config.get("atr_period"))
        _, _, macd_hist = ind.macd(closes)
        stoch_k, _ = ind.stochastic(highs, lows, closes)

        rsi_now = ind.last_valid(rsi_line)
        adx_now = ind.last_valid(adx_line)
        atr_now = ind.last_valid(atr_line)
        trend_now = ind.last_valid(ema_trend)
        pdi_now = ind.last_valid(plus_di)
        mdi_now = ind.last_valid(minus_di)
        hist_now = ind.last_valid(macd_hist)
        stoch_now = ind.last_valid(stoch_k)

        if None in (rsi_now, adx_now, atr_now, trend_now) or not atr_now:
            return None

        htf_bias = self._htf_bias(htf_candles)

        # --- ВОРОТА 1: рынок должен быть трендовым ---
        if adx_now < config.get("adx_min"):
            return None

        # --- ВОРОТА 2: должен сработать триггер входа ---
        cross_up = ind.crossed_up(ema_fast, ema_slow)
        cross_down = ind.crossed_down(ema_fast, ema_slow)
        if not cross_up and not cross_down:
            return None

        side = LONG if cross_up else SHORT

        # --- ВОРОТА 3: направление не должно спорить со старшим ТФ ---
        # Заказчик может отключить это условие, но тогда сигналов станет
        # заметно больше, а доля ложных вырастет — предупреждение в подсказке
        if config.get("require_htf_agree"):
            if htf_bias == "BEAR" and side == LONG:
                return None
            if htf_bias == "BULL" and side == SHORT:
                return None

        # --- ВОРОТА 4: RSI в рабочей зоне (не входим в перегрев) ---
        if side == LONG and not (config.get("rsi_long_min") <= rsi_now <= config.get("rsi_long_max")):
            return None
        if side == SHORT and not (
            config.get("rsi_short_min") <= rsi_now <= config.get("rsi_short_max")
        ):
            return None

        # --- ПОДТВЕРЖДЕНИЯ ---
        confidence = BASE_CONFIDENCE
        reasons: list[str] = []

        arrow = "снизу вверх" if side == LONG else "сверху вниз"
        reasons.append(
            f"EMA{config.get('ema_fast')} пересекла EMA{config.get('ema_slow')} {arrow}"
        )
        reasons.append(f"ADX {adx_now:.0f} — тренд подтверждён")

        if htf_bias == ("BULL" if side == LONG else "BEAR"):
            confidence += 15
            reasons.append(f"Старший ТФ {config.get('htf_timeframe')} в том же направлении")

        if (side == LONG and price > trend_now) or (side == SHORT and price < trend_now):
            confidence += 10
            above = "выше" if side == LONG else "ниже"
            reasons.append(f"Цена {above} EMA{config.get('ema_trend')}")

        if pdi_now is not None and mdi_now is not None:
            if (side == LONG and pdi_now > mdi_now) or (
                side == SHORT and mdi_now > pdi_now
            ):
                confidence += 10
                reasons.append(f"+DI/-DI подтверждают ({pdi_now:.0f}/{mdi_now:.0f})")

        if hist_now is not None:
            if (side == LONG and hist_now > 0) or (side == SHORT and hist_now < 0):
                confidence += 12
                reasons.append("Гистограмма MACD на стороне сигнала")

        adx_slope = ind.slope(adx_line, 3)
        if adx_slope is not None and adx_slope > 0:
            confidence += 8
            reasons.append("Сила тренда нарастает")

        if stoch_now is not None:
            overheated = (side == LONG and stoch_now > 85) or (
                side == SHORT and stoch_now < 15
            )
            if not overheated:
                confidence += 8
            else:
                confidence -= 10
                reasons.append(f"Осторожно: стохастик в зоне перегрева ({stoch_now:.0f})")

        volume_spike = self._volume_spike(candles)
        if config.get("require_volume") and not volume_spike:
            # Жёсткое требование: без всплеска объёма вход не рассматриваем
            return None
        if volume_spike:
            confidence += 5
            reasons.append("Объём выше среднего")

        rsi_mid = abs(rsi_now - 50)
        if rsi_mid < 20:
            confidence += 5
            reasons.append(f"RSI {rsi_now:.0f} — запас хода есть")

        confidence = max(0, min(100, confidence))

        # --- Уровни ---
        risk = atr_now * config.get("atr_sl_mult")
        if side == LONG:
            stop_loss = price - risk
            take_profit = price + risk * config.get("risk_reward")
        else:
            stop_loss = price + risk
            take_profit = price - risk * config.get("risk_reward")

        indicators_snapshot = {
            "rsi": round(rsi_now, 1),
            "adx": round(adx_now, 1),
            "atr": round(atr_now, 4),
            "plus_di": round(pdi_now, 1) if pdi_now is not None else None,
            "minus_di": round(mdi_now, 1) if mdi_now is not None else None,
            "macd_hist": round(hist_now, 4) if hist_now is not None else None,
            "stoch_k": round(stoch_now, 1) if stoch_now is not None else None,
            "ema_fast": round(ind.last_valid(ema_fast) or 0, 4),
            "ema_slow": round(ind.last_valid(ema_slow) or 0, 4),
            "ema_trend": round(trend_now, 4),
            "htf_bias": htf_bias,
            "candle_ts": candles.last.ts if candles.last else None,
        }

        return StrategyResult(
            side=side,
            entry=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=confidence,
            reasons=reasons,
            indicators=indicators_snapshot,
        )

    # ----------------------------------------------------------------
    # Вспомогательное
    # ----------------------------------------------------------------

    def _htf_bias(self, htf: Candles | None) -> str:
        """Направление старшего таймфрейма: BULL, BEAR или NEUTRAL."""
        if htf is None or len(htf) < config.get("ema_trend") + 5:
            return "NEUTRAL"

        closes = htf.closes
        fast = ind.last_valid(ind.ema(closes, config.get("ema_trend")))
        if fast is None:
            return "NEUTRAL"

        # Если истории хватает — сверяемся с медленной EMA, иначе с ценой
        if len(htf) >= config.get("ema_trend_slow") + 5:
            slow = ind.last_valid(ind.ema(closes, config.get("ema_trend_slow")))
            if slow is not None:
                return "BULL" if fast > slow else "BEAR"

        price = closes[-1]
        drift = (price - fast) / fast * 100 if fast else 0
        if drift > 0.15:
            return "BULL"
        if drift < -0.15:
            return "BEAR"
        return "NEUTRAL"

    def _volume_spike(self, candles: Candles, lookback: int = 20) -> bool:
        """Объём последней свечи заметно выше среднего."""
        volumes = candles.volumes
        if len(volumes) < lookback + 1:
            return False
        window = volumes[-lookback - 1 : -1]
        avg = sum(window) / len(window) if window else 0
        return bool(avg) and volumes[-1] > avg * 1.3


strategy = TrendMomentumStrategy()
