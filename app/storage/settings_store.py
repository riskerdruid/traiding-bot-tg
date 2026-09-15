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
    "market": "За чем следить",
    "strategy": "Когда давать сигнал",
    "risk": "Деньги и риск",
    "binary": "Опционы Pocket Option",
    "news": "Новости",
    "notify": "Что присылать",
}

FIELDS: list[Field] = [
    # --- Рынок ---
    Field("symbols", "list", "За чем следить", "market",
          hint="За чем следить: золото, валютные пары, криптовалюта. "
               "Начните с одного-двух — так проще понять, работает ли "
               "стратегия на вашем рынке."),
    Field("timeframe", "choice", "Длина одной свечи", "market",
          hint="Длительность одной свечи на графике, по которому ищутся "
               "входы. 15m значит, что решение принимается раз в 15 минут. "
               "Чем короче, тем больше сигналов и больше случайного шума.",
          choices=TIMEFRAMES),
    Field("htf_timeframe", "choice", "Свеча для общей картины", "market",
          hint="Более длинный график для общей картины. Если на нём всё "
               "падает, бот не станет покупать. Должен быть заметно длиннее "
               "рабочего: 15m и 1h — хорошая пара.",
          choices=TIMEFRAMES),
    Field("scan_interval", "int", "Как часто заглядывать на график", "market",
          hint="Раз в сколько секунд бот опрашивает биржу. Чаще 60 секунд "
               "смысла нет: решение всё равно принимается по закрытой свече.",
          unit="сек", minimum=15, maximum=600, step=5, advanced=True),

    # --- Стратегия ---
    Field("min_confidence", "int", "Насколько бот должен быть уверен", "strategy",
          hint="Главный регулятор количества сигналов. Бот считает, сколько "
               "признаков совпало, и отправляет только те, что набрали больше "
               "этого числа. Выше — реже, но отобраннее.",
          unit="%", minimum=0, maximum=100, step=1),
    Field("adx_min", "float", "Насколько сильным должно быть движение", "strategy",
          hint="Это индикатор ADX. ADX измеряет, насколько уверенно цена идёт в одну сторону. "
               "Ниже 20 рынок обычно топчется на месте, и входить некуда — "
               "бот молчит.",
          minimum=0, maximum=60, step=1),
    Field("cooldown_min", "int", "Пауза после сигнала", "strategy",
          hint="Сколько молчать по тому же активу после сигнала. Без паузы "
               "на одном движении может прилететь пять сообщений подряд.",
          unit="мин", minimum=0, maximum=1440, step=1),
    Field("max_signals_per_day", "int", "Не больше сигналов за сутки", "strategy",
          hint="Защита от дёрганого дня, когда рынок мечется и правила "
               "срабатывают слишком часто. 0 — без ограничения.",
          unit="шт", minimum=0, maximum=200, step=1),
    Field("trade_hours", "str", "Когда присылать сигналы", "strategy",
          hint="Промежуток, когда бот ищет входы, в виде 09:00-18:00. "
               "Пусто — круглосуточно. Полезно, если торгуете только "
               "в определённую сессию или не хотите сигналов ночью."),
    Field("require_htf_agree", "bool", "Идти только за общим движением", "strategy",
          hint="Не покупать, когда общая картина смотрит вниз, и наоборот. "
               "Если выключить, сигналов станет заметно больше, но и ложных "
               "среди них тоже."),
    Field("require_volume", "bool", "Ждать всплеска торгов", "strategy",
          hint="Входить, только когда в момент сигнала торговали активнее "
               "обычного. Это признак, что за движением стоят реальные "
               "деньги. Сигналов станет меньше."),
    Field("signal_ttl_min", "int", "Сколько ждать результата", "strategy",
          hint="Если за это время цена не дошла ни до стопа, ни до цели, "
               "сигнал помечается истёкшим. Он не считается ни выигрышем, "
               "ни проигрышем.",
          unit="мин", minimum=15, maximum=10080, step=1),

    Field("ema_fast", "int", "Быстрая средняя линия", "strategy",
          hint="Индикатор EMA. Период короткой скользящей средней. Её пересечение "
               "с медленной служит поводом для входа. Меньше — чувствительнее "
               "и больше ложных срабатываний.",
          minimum=2, maximum=100, advanced=True),
    Field("ema_slow", "int", "Медленная средняя линия", "strategy",
          hint="Период длинной скользящей средней. Должна быть заметно "
               "длиннее быстрой, иначе они будут пересекаться постоянно.",
          minimum=3, maximum=200, advanced=True),
    Field("ema_trend", "int", "Длинная средняя линия", "strategy",
          hint="Ещё более длинная средняя. Положение цены относительно неё "
               "добавляет уверенности сигналу.",
          minimum=10, maximum=400, advanced=True),
    Field("ema_trend_slow", "int", "Средняя линия общей картины", "strategy",
          hint="По ней определяется направление рынка на старшем графике. "
               "Стандарт — 200: именно на неё смотрит большинство "
               "участников рынка.",
          minimum=20, maximum=500, advanced=True),
    Field("adx_period", "int", "За сколько свечей мерить силу движения", "strategy",
          hint="Индикатор ADX. За сколько свечей считать силу тренда. 14 — классическое "
               "значение, менять обычно не нужно.",
          minimum=2, maximum=100, advanced=True),
    Field("rsi_period", "int", "За сколько свечей мерить перегрев", "strategy",
          hint="Индикатор RSI. За сколько свечей считать перегретость движения. "
               "14 — классическое значение.",
          minimum=2, maximum=50, advanced=True),
    Field("rsi_long_min", "float", "Покупка: нижняя граница перегрева", "strategy",
          hint="Ниже этого значения покупки не рассматриваются: движение "
               "вниз слишком сильное.",
          minimum=0, maximum=100, advanced=True),
    Field("rsi_long_max", "float", "Покупка: верхняя граница перегрева", "strategy",
          hint="Выше этого значения покупки не рассматриваются: рост уже "
               "перегрет, входить поздно.",
          minimum=0, maximum=100, advanced=True),
    Field("rsi_short_min", "float", "Продажа: нижняя граница перегрева", "strategy",
          hint="Ниже этого значения продажи не рассматриваются: падение "
               "уже перегрето.",
          minimum=0, maximum=100, advanced=True),
    Field("rsi_short_max", "float", "Продажа: верхняя граница перегрева", "strategy",
          hint="Выше этого значения продажи не рассматриваются: движение "
               "вверх слишком сильное.",
          minimum=0, maximum=100, advanced=True),

    # --- Риск ---
    Field("deposit", "float", "Сколько у вас денег на счёте", "risk",
          hint="Сумма, которой вы торгуете. Нужна только для расчёта объёма "
               "в карточке сигнала — никуда не передаётся и никому, кроме "
               "вас, не видна.",
          unit="USDT", minimum=0, maximum=100_000_000, step=100),
    Field("risk_per_trade", "float", "Сколько готовы потерять на одной сделке", "risk",
          hint="Сколько процентов депозита допустимо потерять на одном "
               "сигнале. 1% — разумное начало: даже десять неудач подряд "
               "заберут около 10% счёта. При 10% те же десять заберут почти "
               "всё.",
          unit="%", minimum=0.1, maximum=100, step=0.1),
    Field("atr_sl_mult", "float", "Как далеко ставить страховку", "risk",
          hint="Считается по индикатору ATR. Насколько далеко ставить страховочный уровень. Считается от "
               "текущей дёрганости рынка: 1.5 — это полтора обычных хода "
               "свечи. Больше — реже выбивает случайным колебанием, но "
               "и убыток крупнее.",
          unit="× ATR", minimum=0.3, maximum=6, step=0.1),
    Field("risk_reward", "float", "Во сколько раз цель дальше страховки", "risk",
          hint="Во сколько раз цель дальше стопа. 1.8 значит: рискуете одним, "
               "целитесь в 1.8. Чем больше, тем реже цель достигается, но тем "
               "прибыльнее удачные сделки.",
          minimum=0.5, maximum=10, step=0.1),
    Field("atr_period", "int", "За сколько свечей мерить размах цены", "risk",
          hint="Индикатор ATR. За сколько свечей измерять дёрганость рынка для расчёта "
               "стопа. 14 — классическое значение.",
          minimum=2, maximum=100, advanced=True),

    # --- Бинарные опционы (Pocket Option) ---
    Field("po_ssid", "secret", "Ключ доступа к Pocket Option", "binary",
          hint="Ключ доступа к котировкам брокера. Как его получить — "
               "пошагово в разделе «Помощь». Пароль от аккаунта не нужен."),
    Field("po_expiry_min", "int", "Через сколько подводить итог", "binary",
          hint="Через сколько минут после входа подводится итог: угадали "
               "направление или нет. Должен совпадать с тем, что вы "
               "выставляете у брокера.",
          unit="мин", minimum=1, maximum=240, step=1),
    Field("po_min_payout", "float", "Не брать активы с выплатой ниже", "binary",
          hint="Активы с меньшей выплатой пропускаются. Причина в "
               "арифметике: при выплате 92% достаточно угадывать 52% сделок, "
               "а при 70% — уже 59%. Ниже 80% брать невыгодно.",
          unit="%", minimum=0, maximum=100, step=1),
    Field("po_otc_only", "bool", "Только круглосуточные пары", "binary",
          hint="OTC — пары, котировки которых рисует сам брокер. Они "
               "торгуются круглосуточно, включая выходные, когда обычный "
               "рынок закрыт."),

    # --- Новости ---
    Field("news_filter_enabled", "bool", "Молчать во время новостей", "news",
          hint="В момент выхода важной статистики цена летит на самой "
               "новости, а не по графику. Индикаторы там бесполезны, поэтому "
               "бот замолкает."),
    Field("news_mute_before_min", "int", "За сколько замолкать до новости", "news",
          hint="За сколько минут до публикации прекращать выдачу сигналов. "
               "Рынок начинает нервничать заранее.",
          unit="мин", minimum=0, maximum=240, step=1),
    Field("news_mute_after_min", "int", "Сколько молчать после новости", "news",
          hint="Сколько минут после публикации переждать, пока цена "
               "успокоится и вернётся к нормальному поведению.",
          unit="мин", minimum=0, maximum=240, step=1),
    Field("news_impact", "choice", "Насколько важные новости учитывать", "news",
          hint="High — только самые сильные события: ставки центробанков, "
               "инфляция, занятость. Добавлять Medium стоит, только если "
               "ложных сигналов на новостях всё равно много.",
          choices=["High", "High,Medium", "High,Medium,Low"]),
    Field("news_currencies", "str", "Новости по каким странам важны", "news",
          hint="Новости по каким валютам учитывать, через запятую. Золото "
               "и большинство пар сильнее всего реагируют на USD.",
          advanced=True),

    # --- Уведомления ---
    Field("notify_signals", "bool", "Присылать новые сигналы", "notify",
          hint="Присылать карточку, когда бот находит точку входа. "
               "Основное, ради чего всё работает."),
    Field("notify_outcomes", "bool", "Сообщать, чем всё закончилось", "notify",
          hint="Сообщать, чем закончился каждый сигнал: дошёл до цели, "
               "до стопа или истёк. Полезно, чтобы видеть картину целиком, "
               "а не только входы."),
    Field("daily_report", "bool", "Итоги дня одним сообщением", "notify",
          hint="Итоги вечером одним сообщением: сколько сигналов было "
               "и с каким результатом."),
    Field("daily_report_at", "str", "Во сколько присылать итоги", "notify",
          hint="Когда присылать итоги дня, в виде 21:00. "
               "По вашему часовому поясу."),
    Field("quiet_hours", "str", "Не будить ночью", "notify",
          hint="Промежуток, когда уведомления не приходят, в виде "
               "23:00-07:00. Сигналы за это время всё равно попадут "
               "в журнал — увидите их утром. Пусто — присылать всегда."),
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

        if key == "po_ssid" and value:
            # Пустое значение означает «оставить как было», а вот непустое
            # проверяем сразу: иначе ошибка всплывёт только при первом
            # обращении к брокеру, и связать её с настройкой будет трудно.
            from app.market.pocketoption import validate_ssid

            ok, reason = validate_ssid(str(value))
            if not ok:
                raise ValidationError(reason)

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
