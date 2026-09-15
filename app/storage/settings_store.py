"""Живые настройки, которыми управляет заказчик из Telegram.

Зачем это нужно. Файл .env — для инженера при развёртывании: токен бота,
домен, кому слать. Всё остальное заказчик должен менять сам, не открывая
сервер. Поэтому рабочие параметры лежат в базе и перекрывают значения
из .env, а .env остаётся набором значений по умолчанию на первый запуск.

Изменения применяются сразу: сканер перечитывает снимок настроек в начале
каждого цикла, перезапуск не нужен.

Описание каждого параметра (тип, границы, подпись, подсказка) хранится
здесь же, в FIELDS. Интерфейс в Mini App строится из этого описания
автоматически — добавить новый параметр можно одной строкой, не трогая
ни бота, ни вёрстку.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from app.config import settings
from app.storage import repo

log = logging.getLogger("storage.settings")

PREFIX = "cfg."


@dataclass(slots=True)
class Field:
    key: str
    kind: str  # int | float | bool | str | choice | list
    label: str
    group: str
    hint: str = ""
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float = 1
    choices: list[str] = field(default_factory=list)
    advanced: bool = False

    def default(self) -> Any:
        return getattr(settings, self.key, None)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["default"] = self.default()
        return d


TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"]

GROUPS = {
    "market": "Рынок",
    "strategy": "Стратегия",
    "risk": "Риск и уровни",
    "binary": "Бинарные опционы",
    "news": "Фильтр новостей",
    "notify": "Уведомления",
}

FIELDS: list[Field] = [
    # --- Рынок ---
    Field("symbols", "list", "Инструменты", "market",
          hint="Что анализировать. Добавляется из списка биржи."),
    Field("timeframe", "choice", "Рабочий таймфрейм", "market",
          hint="На нём ищутся точки входа", choices=TIMEFRAMES),
    Field("htf_timeframe", "choice", "Таймфрейм тренда", "market",
          hint="На нём определяется направление. Должен быть старше рабочего",
          choices=TIMEFRAMES),
    Field("scan_interval", "int", "Как часто проверять рынок", "market",
          unit="сек", minimum=15, maximum=600, step=15, advanced=True),

    # --- Стратегия ---
    Field("min_confidence", "int", "Порог уверенности", "strategy",
          hint="Сигналы слабее не отправляются. Выше — реже, но качественнее",
          unit="%", minimum=0, maximum=100, step=5),
    Field("adx_min", "float", "Минимальная сила тренда (ADX)", "strategy",
          hint="Ниже этого значения рынок считается боковиком, входов нет",
          minimum=0, maximum=60, step=1),
    Field("cooldown_min", "int", "Пауза после сигнала", "strategy",
          hint="Защита от серии входов на одном движении",
          unit="мин", minimum=0, maximum=1440, step=5),
    Field("max_signals_per_day", "int", "Лимит сигналов в сутки", "strategy",
          hint="0 — без ограничения. Защищает от серии входов в дёрганый день",
          unit="шт", minimum=0, maximum=200, step=1),
    Field("trade_hours", "str", "Часы работы", "strategy",
          hint="Диапазон ЧЧ:ММ-ЧЧ:ММ, когда бот ищет входы. "
               "Пусто — круглосуточно. Полезно, если рынок вам понятен "
               "только в определённые сессии"),
    Field("require_htf_agree", "bool", "Только по тренду старшего ТФ", "strategy",
          hint="Выключение резко увеличит число сигналов и долю ложных"),
    Field("require_volume", "bool", "Требовать всплеск объёма", "strategy",
          hint="Вход только если объём выше среднего. Сигналов станет меньше"),
    Field("signal_ttl_min", "int", "Срок жизни сигнала", "strategy",
          hint="Если за это время не сработал ни стоп, ни цель — сигнал истекает",
          unit="мин", minimum=15, maximum=10080, step=15),
    Field("ema_fast", "int", "Быстрая EMA", "strategy",
          minimum=2, maximum=100, advanced=True),
    Field("ema_slow", "int", "Медленная EMA", "strategy",
          minimum=3, maximum=200, advanced=True),
    Field("ema_trend", "int", "EMA тренда", "strategy",
          minimum=10, maximum=400, advanced=True),
    Field("ema_trend_slow", "int", "Длинная EMA старшего ТФ", "strategy",
          hint="По ней определяется направление рынка на старшем таймфрейме",
          minimum=20, maximum=500, advanced=True),
    Field("adx_period", "int", "Период ADX", "strategy",
          minimum=2, maximum=100, advanced=True),
    Field("rsi_period", "int", "Период RSI", "strategy",
          minimum=2, maximum=50, advanced=True),
    Field("rsi_long_min", "float", "RSI: нижняя граница покупки", "strategy",
          minimum=0, maximum=100, advanced=True),
    Field("rsi_long_max", "float", "RSI: верхняя граница покупки", "strategy",
          minimum=0, maximum=100, advanced=True),
    Field("rsi_short_min", "float", "RSI: нижняя граница продажи", "strategy",
          minimum=0, maximum=100, advanced=True),
    Field("rsi_short_max", "float", "RSI: верхняя граница продажи", "strategy",
          minimum=0, maximum=100, advanced=True),

    # --- Риск ---
    Field("atr_sl_mult", "float", "Ширина стопа", "risk",
          hint="В множителях ATR. Больше — стоп дальше, выбивает реже",
          unit="× ATR", minimum=0.3, maximum=6, step=0.1),
    Field("risk_reward", "float", "Соотношение риск/прибыль", "risk",
          hint="Цель во столько раз дальше стопа. 1.8 значит 1:1.8",
          minimum=0.5, maximum=10, step=0.1),
    Field("atr_period", "int", "Период ATR", "risk",
          minimum=2, maximum=100, advanced=True),
    Field("deposit", "float", "Размер депозита", "risk",
          hint="Для расчёта объёма позиции в карточке сигнала",
          unit="USDT", minimum=0, maximum=100_000_000, step=100),
    Field("risk_per_trade", "float", "Риск на сделку", "risk",
          hint="Сколько процентов депозита допустимо потерять на одном сигнале",
          unit="%", minimum=0.1, maximum=100, step=0.1),

    # --- Бинарные опционы (Pocket Option) ---
    Field("po_ssid", "secret", "SSID сессии Pocket Option", "binary",
          hint="Токен входа из браузера. Без него опционы недоступны. "
               "Живёт ограниченное время — при отказе получите новый"),
    Field("po_expiry_min", "int", "Срок опциона", "binary",
          hint="Через сколько минут после входа истекает сделка",
          unit="мин", minimum=1, maximum=240, step=1),
    Field("po_min_payout", "float", "Минимальная выплата", "binary",
          hint="Сигналы по активам с выплатой ниже не отправляются: "
               "при низком проценте даже хорошая точность уходит в минус",
          unit="%", minimum=0, maximum=100, step=1),
    Field("po_otc_only", "bool", "Только OTC-активы", "binary",
          hint="OTC торгуются и в выходные, обычные пары — нет"),

    # --- Новости ---
    Field("news_filter_enabled", "bool", "Глушить сигналы на новостях", "news",
          hint="Во время выхода важной статистики ценой двигает новость, "
               "а не график"),
    Field("news_mute_before_min", "int", "Молчать до новости", "news",
          unit="мин", minimum=0, maximum=240, step=5),
    Field("news_mute_after_min", "int", "Молчать после новости", "news",
          unit="мин", minimum=0, maximum=240, step=5),
    Field("news_impact", "choice", "Какие новости учитывать", "news",
          choices=["High", "High,Medium", "High,Medium,Low"]),
    Field("news_currencies", "str", "Валюты новостей", "news",
          hint="Через запятую. Для золота это в первую очередь USD",
          advanced=True),

    # --- Уведомления ---
    Field("notify_signals", "bool", "Новые сигналы", "notify",
          hint="Присылать карточку при каждом входе"),
    Field("notify_outcomes", "bool", "Исходы сигналов", "notify",
          hint="Сообщать, когда сработал стоп или достигнута цель"),
    Field("daily_report", "bool", "Сводка за день", "notify",
          hint="Итоги вечером одним сообщением"),
    Field("daily_report_at", "str", "Время сводки", "notify",
          hint="В формате ЧЧ:ММ по вашему часовому поясу"),
    Field("quiet_hours", "str", "Не беспокоить", "notify",
          hint="Диапазон ЧЧ:ММ-ЧЧ:ММ, когда уведомления не приходят. "
               "Пусто — присылать всегда"),
]

FIELDS_BY_KEY = {f.key: f for f in FIELDS}

# Значения по умолчанию для параметров, которых нет в .env
EXTRA_DEFAULTS = {
    "notify_signals": True,
    "notify_outcomes": True,
    "daily_report": True,
    "deposit": 1000.0,
    "risk_per_trade": 1.0,
    "quiet_hours": "",
}


class ValidationError(ValueError):
    """Значение не прошло проверку — показывается пользователю как есть."""


class RuntimeConfig:
    """Снимок настроек в памяти + запись в базу."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._loaded = False

    # ----------------------------------------------------------------

    def _fallback(self, key: str) -> Any:
        if key in EXTRA_DEFAULTS:
            return EXTRA_DEFAULTS[key]
        value = getattr(settings, key, None)
        if key == "symbols":
            return settings.symbol_list
        return value

    async def load(self) -> dict[str, Any]:
        """Перечитывает настройки из базы. Дёшево — один SELECT."""
        stored = await repo.all_settings()
        values: dict[str, Any] = {}
        for f in FIELDS:
            raw = stored.get(PREFIX + f.key)
            if raw is None:
                values[f.key] = self._fallback(f.key)
            else:
                try:
                    values[f.key] = self._decode(f, raw)
                except Exception:
                    log.warning("Значение %s повреждено, беру по умолчанию", f.key)
                    values[f.key] = self._fallback(f.key)
        self._values = values
        self._loaded = True
        return values

    def get(self, key: str, default: Any = None) -> Any:
        """Синхронное чтение — стратегия работает без await.

        Ключ, не описанный в FIELDS, тоже читается: берём значение из .env.
        Так редко меняемые параметры не обязаны попадать в интерфейс,
        но и не превращаются в None при обращении.
        """
        if key in self._values:
            return self._values[key]
        fallback = self._fallback(key)
        return default if fallback is None and default is not None else fallback

    def all(self) -> dict[str, Any]:
        return dict(self._values)

    # ----------------------------------------------------------------

    def _decode(self, f: Field, raw: str) -> Any:
        if f.kind == "int":
            return int(float(raw))
        if f.kind == "float":
            return float(raw)
        if f.kind == "bool":
            return raw in ("1", "true", "True")
        if f.kind == "list":
            return json.loads(raw)
        return raw

    def _encode(self, f: Field, value: Any) -> str:
        if f.kind == "bool":
            return "1" if value else "0"
        if f.kind == "list":
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def validate(self, f: Field, value: Any) -> Any:
        """Приводит значение к нужному типу и проверяет границы."""
        if f.kind in ("int", "float"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValidationError(f"«{f.label}»: нужно число")
            if f.minimum is not None and number < f.minimum:
                raise ValidationError(
                    f"«{f.label}»: минимум {f.minimum:g}{' ' + f.unit if f.unit else ''}"
                )
            if f.maximum is not None and number > f.maximum:
                raise ValidationError(
                    f"«{f.label}»: максимум {f.maximum:g}{' ' + f.unit if f.unit else ''}"
                )
            return int(number) if f.kind == "int" else number

        if f.kind == "bool":
            return bool(value) if isinstance(value, bool) else str(value) in ("1", "true", "True")

        if f.kind == "choice":
            text = str(value)
            if f.choices and text not in f.choices:
                raise ValidationError(
                    f"«{f.label}»: допустимо только {', '.join(f.choices)}"
                )
            return text

        if f.kind == "secret":
            return str(value or "").strip()

        if f.kind == "list":
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
            if not isinstance(value, list):
                raise ValidationError(f"«{f.label}»: ожидается список")
            return value

        return str(value)

    async def set(self, key: str, value: Any) -> Any:
        """Проверяет, сохраняет и сразу применяет одно значение."""
        f = FIELDS_BY_KEY.get(key)
        if f is None:
            raise ValidationError(f"Неизвестный параметр: {key}")
        clean = self.validate(f, value)
        self._cross_check(key, clean)
        await repo.set_setting(PREFIX + key, self._encode(f, clean))
        self._values[key] = clean
        log.info("Настройка изменена: %s = %s", key, clean)
        return clean

    async def set_many(self, updates: dict[str, Any]) -> dict[str, Any]:
        applied = {}
        for key, value in updates.items():
            applied[key] = await self.set(key, value)
        return applied

    async def reset(self, key: str) -> Any:
        """Возвращает параметр к значению по умолчанию."""
        if key not in FIELDS_BY_KEY:
            raise ValidationError(f"Неизвестный параметр: {key}")
        await repo.set_setting(PREFIX + key, "")
        conn_value = self._fallback(key)
        self._values[key] = conn_value
        # Пустая строка читается как «нет значения» — убираем запись совсем
        await repo.delete_setting(PREFIX + key)
        return conn_value

    def _cross_check(self, key: str, value: Any) -> None:
        """Проверки, затрагивающие несколько параметров сразу."""
        if key in ("timeframe", "htf_timeframe"):
            tf = value if key == "timeframe" else self.get("timeframe")
            htf = value if key == "htf_timeframe" else self.get("htf_timeframe")
            if _tf_minutes(htf) < _tf_minutes(tf):
                raise ValidationError(
                    "Таймфрейм тренда должен быть старше рабочего: "
                    f"{htf} мельче, чем {tf}"
                )
        if key == "rsi_long_min" and value >= self.get("rsi_long_max", 100):
            raise ValidationError("Нижняя граница RSI должна быть меньше верхней")
        if key == "rsi_long_max" and value <= self.get("rsi_long_min", 0):
            raise ValidationError("Верхняя граница RSI должна быть больше нижней")
        if key == "ema_fast" and value >= self.get("ema_slow", 999):
            raise ValidationError("Быстрая EMA должна быть короче медленной")
        if key == "ema_slow" and value <= self.get("ema_fast", 0):
            raise ValidationError("Медленная EMA должна быть длиннее быстрой")
        if key == "symbols" and not value:
            raise ValidationError("Нужен хотя бы один инструмент")

    # ----------------------------------------------------------------

    def schema(self) -> list[dict]:
        """Описание всех параметров для построения интерфейса.

        Секреты наружу не отдаются: вместо значения уходит только признак
        «задан / не задан» и короткий хвост для опознания. Иначе токен
        сессии брокера утёк бы в любой ответ API.
        """
        out = []
        for f in FIELDS:
            item = {**f.to_dict(), "group_label": GROUPS[f.group]}
            value = self._values.get(f.key)
            if f.kind == "secret":
                text = str(value or "")
                item["value"] = ""
                item["default"] = ""
                item["is_set"] = bool(text)
                item["preview"] = (
                    f"…{text[-6:]} ({len(text)} символов)" if text else "не задан"
                )
            else:
                item["value"] = value
            out.append(item)
        return out

    def masked(self, key: str) -> str:
        """Безопасное представление секрета для логов и сообщений."""
        text = str(self.get(key) or "")
        return f"…{text[-6:]}" if text else "не задан"

    def in_quiet_hours(self, now_hm: str) -> bool:
        """Попадает ли текущее время в интервал «не беспокоить»."""
        window = str(self.get("quiet_hours") or "").strip()
        if not window or "-" not in window:
            return False
        try:
            start, end = (part.strip() for part in window.split("-", 1))
            if start == end:
                return False
            if start < end:
                return start <= now_hm < end
            # Интервал через полночь, например 23:00-07:00
            return now_hm >= start or now_hm < end
        except Exception:
            return False


def _tf_minutes(timeframe: str) -> int:
    units = {"m": 1, "h": 60, "d": 1440}
    try:
        return int(timeframe[:-1]) * units[timeframe[-1]]
    except Exception:
        return 0


config = RuntimeConfig()
