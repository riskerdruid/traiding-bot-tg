"""Настройки, которыми пользователь управляет прямо из Telegram.

Их намеренно мало. Всё, что можно решить за человека, решено в коде или
спрятано в «режим работы»: новичок не может осмысленно выставить два
десятка порогов, а ошибётся — решит, что виноват бот.

Из бота меняется ровно четыре вещи:

    symbols         за чем следить
    режим           как часто присылать сигналы (PRESETS)
    deposit + risk  сколько денег на счёте и сколько рискуем на сделке
    quiet_night     не будить ночью

Всё остальное — производные значения режима. Они тоже хранятся здесь,
потому что сканер читает их на каждом цикле, и меняются они без
перезапуска.

Значения лежат в базе отдельно для каждого получателя: ключ выглядит
как u123456.cfg.deposit. Значения из .env — общий запасной слой.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.storage import repo

log = logging.getLogger("storage.settings")

PREFIX = "cfg."

# Ночная тишина одна для всех: выбор времени — ещё один вопрос,
# на который новичку нечего ответить.
QUIET_FROM = "23:00"
QUIET_TO = "07:00"


def _key(name: str, user_id: int | None) -> str:
    """Имя настройки в базе для конкретного пользователя."""
    if user_id is None:
        return PREFIX + name
    return f"u{user_id}.{PREFIX}{name}"


def _user_from_key(key: str) -> int | None:
    """Обратная операция: из u123.cfg.deposit достаём 123."""
    if not key.startswith("u"):
        return None
    head, _, rest = key.partition(".")
    if not rest.startswith(PREFIX):
        return None
    try:
        return int(head[1:])
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Spec:
    """Тип и границы одного параметра — этого хватает для проверки."""

    kind: str  # int | float | bool | str | choice | list | secret
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    label: str = ""  # человеческое имя, нужно только для текста ошибки


TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")

FIELDS: dict[str, Spec] = {
    # --- то, что человек меняет сам ---
    "symbols": Spec("list", label="Список инструментов"),
    "deposit": Spec("float", 0, 100_000_000, label="Сумма на счёте"),
    "risk_per_trade": Spec("float", 0.1, 100, label="Риск на сделку"),
    "quiet_night": Spec("bool", label="Ночной режим"),
    "po_ssid": Spec("secret", label="Ключ Pocket Option"),
    # --- то, что выставляет выбранный режим ---
    "timeframe": Spec("choice", choices=TIMEFRAMES, label="Длина свечи"),
    "htf_timeframe": Spec("choice", choices=TIMEFRAMES, label="Свеча общей картины"),
    "min_confidence": Spec("int", 0, 100, label="Порог уверенности"),
    "adx_min": Spec("float", 0, 60, label="Сила движения"),
    "cooldown_min": Spec("int", 0, 1440, label="Пауза после сигнала"),
    "max_signals_per_day": Spec("int", 0, 200, label="Сигналов в сутки"),
    "require_htf_agree": Spec("bool", label="Идти за общим движением"),
    "require_volume": Spec("bool", label="Ждать всплеска торгов"),
    "risk_reward": Spec("float", 0.5, 10, label="Цель относительно стопа"),
    "atr_sl_mult": Spec("float", 0.3, 6, label="Дальность стопа"),
    # --- служебное, из .env; в боте не показывается ---
    "scan_interval": Spec("int", 15, 600, label="Период опроса"),
    "signal_ttl_min": Spec("int", 15, 10080, label="Срок жизни сигнала"),
    "ema_fast": Spec("int", 2, 100, label="Быстрая средняя"),
    "ema_slow": Spec("int", 3, 200, label="Медленная средняя"),
    "ema_trend": Spec("int", 10, 400, label="Длинная средняя"),
    "ema_trend_slow": Spec("int", 20, 500, label="Средняя общей картины"),
    "rsi_period": Spec("int", 2, 50, label="Период RSI"),
    "rsi_long_min": Spec("float", 0, 100, label="Покупка: нижняя граница"),
    "rsi_long_max": Spec("float", 0, 100, label="Покупка: верхняя граница"),
    "rsi_short_min": Spec("float", 0, 100, label="Продажа: нижняя граница"),
    "rsi_short_max": Spec("float", 0, 100, label="Продажа: верхняя граница"),
    "adx_period": Spec("int", 2, 100, label="Период ADX"),
    "atr_period": Spec("int", 2, 100, label="Период ATR"),
    "po_expiry_min": Spec("int", 1, 240, label="Время опциона"),
    "po_min_payout": Spec("float", 0, 100, label="Минимальная выплата"),
}

# Значения по умолчанию для параметров, которых нет в .env
EXTRA_DEFAULTS: dict[str, Any] = {
    "deposit": 1000.0,
    "risk_per_trade": 1.0,
    "quiet_night": True,
}


@dataclass(frozen=True, slots=True)
class Preset:
    """Готовый набор настроек под одну понятную цель.

    Это единственный способ настройки стратегии в боте. Человеку без
    опыта невозможно осмысленно выставить два десятка параметров: он
    не знает, что от чего зависит. Выбрать цель — может.
    """

    key: str
    emoji: str
    name: str
    summary: str  # что получится — одной строкой
    expect: str  # сколько примерно сигналов ждать
    values: dict[str, Any] = field(default_factory=dict)


PRESETS: tuple[Preset, ...] = (
    Preset(
        key="careful",
        emoji="🛡",
        name="Осторожный",
        summary="Реже, но надёжнее: бот ждёт, пока совпадёт всё сразу",
        expect="1–3 сигнала в неделю",
        values={
            "timeframe": "15m",
            "htf_timeframe": "4h",
            "min_confidence": 75,
            "adx_min": 25,
            "require_htf_agree": True,
            "require_volume": True,
            "cooldown_min": 120,
            "max_signals_per_day": 3,
            "risk_reward": 2.0,
            "atr_sl_mult": 1.8,
        },
    ),
    Preset(
        key="balanced",
        emoji="⚖️",
        name="Обычный",
        summary="Разумная середина. С него и стоит начинать",
        expect="1–3 сигнала в день",
        values={
            "timeframe": "15m",
            "htf_timeframe": "1h",
            "min_confidence": 60,
            "adx_min": 20,
            "require_htf_agree": True,
            "require_volume": False,
            "cooldown_min": 45,
            "max_signals_per_day": 10,
            "risk_reward": 1.8,
            "atr_sl_mult": 1.5,
        },
    ),
    Preset(
        key="active",
        emoji="⚡",
        name="Частые сигналы",
        summary="Много сообщений — чтобы посмотреть бота в деле",
        expect="10–20 сигналов в день",
        values={
            "timeframe": "5m",
            "htf_timeframe": "1h",
            "min_confidence": 40,
            "adx_min": 10,
            "require_htf_agree": False,
            "require_volume": False,
            "cooldown_min": 10,
            "max_signals_per_day": 0,
            "risk_reward": 1.5,
            "atr_sl_mult": 1.2,
        },
    ),
)

PRESETS_BY_KEY = {p.key: p for p in PRESETS}
DEFAULT_PRESET = "balanced"


class ValidationError(ValueError):
    """Значение не прошло проверку — показывается пользователю как есть."""


class RuntimeConfig:
    """Снимок настроек в памяти + запись в базу."""

    def __init__(self) -> None:
        # {user_id: {параметр: значение}}. Ключ None — общий слой из .env.
        self._by_user: dict[int | None, dict[str, Any]] = {}
        self._loaded = False

    # ----------------------------------------------------------------
    # Чтение
    # ----------------------------------------------------------------

    def _fallback(self, key: str) -> Any:
        if key in EXTRA_DEFAULTS:
            return EXTRA_DEFAULTS[key]
        if key == "symbols":
            return settings.symbol_list
        return getattr(settings, key, None)

    async def load(self) -> dict[str, Any]:
        """Перечитывает настройки всех пользователей. Дёшево — один SELECT."""
        stored = await repo.all_settings()

        raw_by_user: dict[int | None, dict[str, str]] = {}
        for key, value in stored.items():
            uid = _user_from_key(key)
            if uid is not None:
                name = key.split(".", 1)[1][len(PREFIX):]
                raw_by_user.setdefault(uid, {})[name] = value
            elif key.startswith(PREFIX):
                raw_by_user.setdefault(None, {})[key[len(PREFIX):]] = value

        by_user: dict[int | None, dict[str, Any]] = {}
        for uid, raw in raw_by_user.items():
            # В личном слое держим ТОЛЬКО то, что человек менял сам.
            # Если дополнить его значениями по умолчанию, общий слой
            # перестанет действовать.
            by_user[uid] = self._decode_all(raw, fill_defaults=uid is None)

        by_user.setdefault(None, self._decode_all({}))
        self._by_user = by_user
        self._loaded = True
        return by_user[None]

    def _decode_all(
        self, raw: dict[str, str], fill_defaults: bool = True
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key, spec in FIELDS.items():
            text = raw.get(key)
            if text is None:
                if fill_defaults:
                    values[key] = self._fallback(key)
                continue
            try:
                values[key] = self._decode(spec, text)
            except Exception:
                log.warning("Значение %s повреждено, беру по умолчанию", key)
                values[key] = self._fallback(key)
        return values

    def get(self, key: str, default: Any = None, user_id: int | None = None) -> Any:
        """Синхронное чтение — стратегия работает без await.

        Порядок поиска: личное значение, общее, значение из .env.
        """
        if user_id is not None:
            personal = self._by_user.get(user_id)
            if personal is not None and key in personal:
                return personal[key]

        shared = self._by_user.get(None)
        if shared is not None and key in shared:
            return shared[key]

        fallback = self._fallback(key)
        return default if fallback is None and default is not None else fallback

    def all(self, user_id: int | None = None) -> dict[str, Any]:
        merged = dict(self._by_user.get(None, {}))
        if user_id is not None:
            merged.update(self._by_user.get(user_id, {}))
        return merged

    def view(self, user_id: int) -> "UserConfig":
        """Настройки конкретного получателя."""
        return UserConfig(self, user_id)

    def known_users(self) -> list[int]:
        return sorted(u for u in self._by_user if u is not None)

    # ----------------------------------------------------------------
    # Запись
    # ----------------------------------------------------------------

    def _decode(self, spec: Spec, raw: str) -> Any:
        if spec.kind == "int":
            return int(float(raw))
        if spec.kind == "float":
            return float(raw)
        if spec.kind == "bool":
            return raw in ("1", "true", "True")
        if spec.kind == "list":
            return json.loads(raw)
        return raw

    def _encode(self, spec: Spec, value: Any) -> str:
        if spec.kind == "bool":
            return "1" if value else "0"
        if spec.kind == "list":
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def validate(self, key: str, spec: Spec, value: Any) -> Any:
        """Приводит значение к нужному типу и проверяет границы."""
        name = spec.label or key

        if spec.kind in ("int", "float"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValidationError(f"«{name}»: нужно число") from None
            if spec.minimum is not None and number < spec.minimum:
                raise ValidationError(f"«{name}»: минимум {spec.minimum:g}")
            if spec.maximum is not None and number > spec.maximum:
                raise ValidationError(f"«{name}»: максимум {spec.maximum:g}")
            return int(number) if spec.kind == "int" else number

        if spec.kind == "bool":
            if isinstance(value, bool):
                return value
            return str(value) in ("1", "true", "True")

        if spec.kind == "choice":
            text = str(value)
            if spec.choices and text not in spec.choices:
                raise ValidationError(
                    f"«{name}»: допустимо только {', '.join(spec.choices)}"
                )
            return text

        if spec.kind == "list":
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
            if not isinstance(value, list):
                raise ValidationError(f"«{name}»: ожидается список")
            return value

        return str(value or "").strip()

    async def set(self, key: str, value: Any, user_id: int | None = None) -> Any:
        """Проверяет, сохраняет и сразу применяет одно значение."""
        spec = FIELDS.get(key)
        if spec is None:
            raise ValidationError(f"Неизвестный параметр: {key}")
        clean = self.validate(key, spec, value)
        self._cross_check(key, clean, user_id=user_id)
        await repo.set_setting(_key(key, user_id), self._encode(spec, clean))
        self._by_user.setdefault(user_id, {})[key] = clean
        log.info(
            "Настройка изменена: %s = %s%s",
            key,
            "скрыто" if spec.kind == "secret" else clean,
            f" (получатель {user_id})" if user_id else "",
        )
        return clean

    async def set_many(
        self, updates: dict[str, Any], user_id: int | None = None
    ) -> dict[str, Any]:
        return {
            key: await self.set(key, value, user_id=user_id)
            for key, value in updates.items()
        }

    async def reset(self, key: str, user_id: int | None = None) -> Any:
        """Возвращает параметр к значению по умолчанию."""
        if key not in FIELDS:
            raise ValidationError(f"Неизвестный параметр: {key}")
        await repo.delete_setting(_key(key, user_id))
        personal = self._by_user.get(user_id)
        if personal is not None:
            personal.pop(key, None)
        if user_id is None:
            self._by_user.setdefault(None, {})[key] = self._fallback(key)
        return self.get(key, user_id=user_id)

    def _cross_check(self, key: str, value: Any, user_id: int | None = None) -> None:
        """Проверки, затрагивающие несколько параметров сразу."""

        def cur(name: str) -> Any:
            return self.get(name, user_id=user_id)

        if key in ("timeframe", "htf_timeframe"):
            tf = value if key == "timeframe" else cur("timeframe")
            htf = value if key == "htf_timeframe" else cur("htf_timeframe")
            if _tf_minutes(htf) < _tf_minutes(tf):
                raise ValidationError(
                    "Общая картина должна быть крупнее рабочей свечи: "
                    f"{htf} мельче, чем {tf}"
                )
        if key == "symbols" and not value:
            raise ValidationError(
                "Нужен хотя бы один инструмент — иначе следить не за чем"
            )
        if key == "po_ssid" and value:
            # Непустой ключ проверяем сразу: иначе ошибка всплывёт только
            # при первом обращении к брокеру, и связать её с настройкой
            # будет трудно.
            from app.market.pocketoption import validate_ssid

            ok, reason = validate_ssid(str(value))
            if not ok:
                raise ValidationError(reason)

    # ----------------------------------------------------------------
    # Режимы работы
    # ----------------------------------------------------------------

    async def apply_preset(self, key: str, user_id: int | None = None) -> Preset:
        """Применяет готовый режим целиком.

        Порядок важен: рабочий таймфрейм выставляется раньше старшего,
        иначе взаимная проверка отклонит промежуточное состояние, когда
        старший график на миг оказывается мельче рабочего.
        """
        preset = PRESETS_BY_KEY.get(key)
        if preset is None:
            raise ValidationError(f"Неизвестный режим: {key}")

        order = {"timeframe": 0, "htf_timeframe": 1}
        items = sorted(preset.values.items(), key=lambda kv: order.get(kv[0], 2))
        for name, value in items:
            await self.set(name, value, user_id=user_id)

        log.info("Выбран режим «%s» (получатель %s)", preset.name, user_id)
        return preset

    def current_preset(self, user_id: int | None = None) -> str | None:
        """Какой режим сейчас стоит — или None, если значения не совпали."""
        for preset in PRESETS:
            if all(
                self.get(name, user_id=user_id) == value
                for name, value in preset.values.items()
            ):
                return preset.key
        return None

    def preset(self, user_id: int | None = None) -> Preset:
        """Текущий режим.

        Несовпадение возможно только у баз, оставшихся от версии с ручной
        правкой параметров. Показываем обычный, чтобы экран настроек
        никогда не пустовал.
        """
        return PRESETS_BY_KEY.get(
            self.current_preset(user_id) or DEFAULT_PRESET, PRESETS[1]
        )

    # ----------------------------------------------------------------
    # Прочее
    # ----------------------------------------------------------------

    def risk_money(self, user_id: int | None = None) -> float:
        """Сколько денег рискуем на одной сделке."""
        deposit = float(self.get("deposit", user_id=user_id) or 0)
        risk = float(self.get("risk_per_trade", user_id=user_id) or 0)
        return deposit * risk / 100

    def in_quiet_hours(self, now_hm: str, user_id: int | None = None) -> bool:
        """Попадает ли текущее время в ночную тишину."""
        if not self.get("quiet_night", user_id=user_id):
            return False
        # Интервал через полночь: 23:00-07:00
        return now_hm >= QUIET_FROM or now_hm < QUIET_TO


def _tf_minutes(timeframe: str) -> int:
    units = {"m": 1, "h": 60, "d": 1440}
    try:
        return int(timeframe[:-1]) * units[timeframe[-1]]
    except Exception:
        return 0


class UserConfig:
    """Настройки одного пользователя.

    Лёгкая обёртка: держит ссылку на хранилище и свой идентификатор.
    Передаётся в стратегию и форматирование вместо глобального состояния —
    так два получателя не мешают друг другу.
    """

    __slots__ = ("_store", "user_id")

    def __init__(self, store: RuntimeConfig, user_id: int) -> None:
        self._store = store
        self.user_id = user_id

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default, user_id=self.user_id)

    def all(self) -> dict[str, Any]:
        return self._store.all(user_id=self.user_id)

    async def set(self, key: str, value: Any) -> Any:
        return await self._store.set(key, value, user_id=self.user_id)

    async def set_many(self, updates: dict[str, Any]) -> dict[str, Any]:
        return await self._store.set_many(updates, user_id=self.user_id)

    async def reset(self, key: str) -> Any:
        return await self._store.reset(key, user_id=self.user_id)

    async def apply_preset(self, key: str) -> Preset:
        return await self._store.apply_preset(key, user_id=self.user_id)

    def current_preset(self) -> str | None:
        return self._store.current_preset(user_id=self.user_id)

    def preset(self) -> Preset:
        return self._store.preset(user_id=self.user_id)

    def risk_money(self) -> float:
        return self._store.risk_money(user_id=self.user_id)

    def in_quiet_hours(self, now_hm: str) -> bool:
        return self._store.in_quiet_hours(now_hm, user_id=self.user_id)

    def __repr__(self) -> str:
        return f"<UserConfig {self.user_id}>"


config = RuntimeConfig()
