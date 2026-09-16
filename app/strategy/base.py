"""Базовые типы для стратегий."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.market.feed import Candles

LONG = "LONG"
SHORT = "SHORT"


@dataclass(slots=True)
class StrategyResult:
    """Готовый сигнал со всем обоснованием.

    reasons — причины простыми словами: они уходят прямо в сообщение.
    Без них сигнал выглядит как чёрный ящик, которому не доверяют.
    """

    side: str
    entry: float
    stop_loss: float
    take_profit: float
    confidence: int
    reasons: list[str] = field(default_factory=list)
    indicators: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_abs(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def risk_pct(self) -> float:
        return self.risk_abs / self.entry * 100 if self.entry else 0.0

    @property
    def reward_pct(self) -> float:
        return abs(self.take_profit - self.entry) / self.entry * 100 if self.entry else 0.0

    @property
    def rr(self) -> float:
        return abs(self.take_profit - self.entry) / self.risk_abs if self.risk_abs else 0.0


class Strategy(ABC):
    """Интерфейс стратегии.

    Чтобы подключить свою логику, достаточно унаследоваться и реализовать
    analyze(). Сканер, журнал и уведомления останутся прежними.
    """

    name: str = "base"
    description: str = ""

    @abstractmethod
    def analyze(
        self, candles: Candles, htf_candles: Candles | None = None
    ) -> StrategyResult | None:
        """Возвращает сигнал или None, если условий для входа нет."""

    def min_candles(self) -> int:
        """Сколько свечей нужно для расчёта."""
        return 210
