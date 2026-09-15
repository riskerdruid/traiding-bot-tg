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

# Настройки хранятся отдельно для каждого получателя: ключ выглядит
# как `u123456.cfg.deposit`. Общий префикс остался для переноса старых
# значений, записанных до разделения.


def _key(name: str, user_id: int | None) -> str:
    """Имя настройки в базе для конкретного пользователя."""
    if user_id is None:
        return PREFIX + name
    return f"u{user_id}.{PREFIX}{name}"


def _user_from_key(key: str) -> int | None:
    """Обратная операция: из `u123.cfg.deposit` достаём 123."""
    if not key.startswith("u"):
        return None
    head, _, rest = key.partition(".")
    if not rest.startswith(PREFIX):
        return None
    try:
        return int(head[1:])
    except ValueError:
        return None


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
    tip: str = ""  # конкретный совет: что поставить и что из этого выйдет

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


@dataclass(frozen=True, slots=True)
class Preset:
    """Готовый набор настроек под одну понятную цель.

    Человеку без опыта невозможно осмысленно выставить два десятка
    параметров: он не знает, что от чего зависит. Поэтому главный способ
    настройки — выбрать режим одним нажатием, а отдельные значения
    поправить потом, если захочется.
    """

    key: str
    emoji: str
    name: str
    summary: str          # что получится — одной строкой
    detail: str           # кому подходит и почему
    expect: str           # сколько примерно сигналов ждать
    values: dict[str, Any]


PRESETS: list[Preset] = [
    Preset(
        key="careful",
        emoji="🛡",
        name="Осторожный",
        summary="Редкие сигналы, зато самые надёжные",
        detail=(
            "Бот ждёт, пока совпадёт всё сразу: и сильное движение, "
            "и согласие старшего графика, и всплеск торгов. Большинство "
            "моментов он пропустит — и это намеренно."
        ),
        expect="примерно 1–3 сигнала в неделю",
        values={
            "min_confidence": 75,
            "adx_min": 25,
            "require_htf_agree": True,
            "require_volume": True,
            "cooldown_min": 120,
            "max_signals_per_day": 3,
            "timeframe": "15m",
            "htf_timeframe": "4h",
            "risk_per_trade": 1.0,
            "risk_reward": 2.0,
            "atr_sl_mult": 1.8,
            "news_filter_enabled": True,
        },
    ),
    Preset(
        key="balanced",
        emoji="⚖️",
        name="Обычный",
        summary="Разумная середина — с него стоит начинать",
        detail=(
            "Настройки по умолчанию: движение должно быть заметным, "
            "старший график — не против. Столько сигналов, чтобы было "
            "что разбирать, но не столько, чтобы утонуть."
        ),
        expect="примерно 1–3 сигнала в день",
        values={
            "min_confidence": 60,
            "adx_min": 20,
            "require_htf_agree": True,
            "require_volume": False,
            "cooldown_min": 45,
            "max_signals_per_day": 10,
            "timeframe": "15m",
            "htf_timeframe": "1h",
            "risk_per_trade": 1.0,
            "risk_reward": 1.8,
            "atr_sl_mult": 1.5,
            "news_filter_enabled": True,
        },
    ),
    Preset(
        key="active",
        emoji="⚡",
        name="Частые сигналы",
        summary="Много сообщений — чтобы посмотреть, как всё работает",
        detail=(
            "Планка опущена почти до пола: бот пишет по любому намёку "
            "на движение. Хорош, чтобы за вечер увидеть бота в деле, "
            "но торговать по нему настоящими деньгами не стоит — "
            "среди частых сигналов много случайных."
        ),
        expect="примерно 10–20 сигналов в день",
        values={
            "min_confidence": 40,
            "adx_min": 10,
            "require_htf_agree": False,
            "require_volume": False,
            "cooldown_min": 10,
            "max_signals_per_day": 0,
            "timeframe": "5m",
            "htf_timeframe": "1h",
            "risk_per_trade": 0.5,
            "risk_reward": 1.5,
            "atr_sl_mult": 1.2,
            "news_filter_enabled": False,
        },
    ),
]

PRESETS_BY_KEY = {p.key: p for p in PRESETS}
DEFAULT_PRESET = "balanced"



# Короткий совет к настройке: что поставить и что из этого выйдет.
# Держим отдельно от FIELDS, чтобы описание параметра не разрасталось,
# а совет можно было переписать, не трогая тип и границы. Показывается
# во всплывающей подсказке рядом с названием.
TIPS: dict[str, str] = {
    "symbols": "Начните с одного-двух. Золото (XAU) двигается заметно "
               "и понятно; валютные пары с пометкой OTC работают даже "
               "в выходные.",
    "timeframe": "15 минут — хорошее начало: сигналов достаточно, а шума "
                 "уже немного. 5 минут дадут сигналов втрое больше, но "
                 "случайных среди них тоже втрое больше.",
    "htf_timeframe": "Берите примерно в 4 раза длиннее рабочего: к 15 минутам "
                     "подходит час, к 5 минутам — полчаса.",
    "scan_interval": "60 секунд подходит всем. Чаще не нужно: решение "
                     "принимается по закрытой свече, а она не меняется.",
    "min_confidence": "Это главный переключатель «много / мало». 40 — сигналы "
                      "польются потоком, 60 — несколько в день, 80 — пара "
                      "в неделю. Начните с 60.",
    "adx_min": "20 — стандарт. Поставьте 10, если сигналов совсем мало, "
               "и 25, если бот ловит движения, которые тут же затухают.",
    "cooldown_min": "45 минут хватает, чтобы не получить пять сообщений "
                    "об одном и том же движении. Поставьте 10, если хотите "
                    "видеть каждую попытку.",
    "max_signals_per_day": "10 — разумный потолок. 0 снимает ограничение "
                           "совсем: в дёрганый день может прийти и полсотни.",
    "trade_hours": "Оставьте пустым, если торгуете когда придётся. "
                   "09:00-18:00 — если садитесь за график только днём.",
    "require_htf_agree": "Держите включённым. Выключите, только если сигналов "
                         "почти нет: их станет заметно больше, но каждый "
                         "будет слабее.",
    "require_volume": "Выключено по умолчанию. Включайте, если ложных "
                      "сигналов много: бот станет ждать подтверждения "
                      "деньгами и писать реже.",
    "signal_ttl_min": "Обычно хватает 6–8 длительностей рабочей свечи: "
                      "для 15-минутных свечей это около 120 минут.",
    "deposit": "Впишите настоящую сумму — от неё считается размер сделки "
               "в карточке. Цифра хранится только у вас и никуда не уходит.",
    "risk_per_trade": "1% — то, с чего начинают все. При 1000 USDT это "
                      "10 USDT на сделку: десять неудач подряд заберут "
                      "около 10% счёта. При 10% те же десять заберут почти всё.",
    "atr_sl_mult": "1.5 — золотая середина. Меньше — страховка близко "
                   "и её чаще задевает случайное колебание; больше — "
                   "реже выбивает, но убыток крупнее.",
    "risk_reward": "1.8 значит: рискуете 10 USDT, целитесь в 18. Ниже 1.2 "
                   "ставить не стоит — тогда даже частые удачи не окупают "
                   "редкие потери.",
    "po_ssid": "Берётся из браузера за две минуты — в «Помощи» есть "
               "пошаговая инструкция с картинками. Пароль от счёта "
               "вводить нигде не нужно.",
    "po_expiry_min": "Поставьте ровно столько, сколько выставляете у брокера. "
                     "Обычно 5 минут. Если цифры не совпадут, статистика "
                     "будет считать не то.",
    "po_min_payout": "80% — граница выгодности. При выплате 92% достаточно "
                     "угадывать 52% сделок, при 70% — уже 59%, а это "
                     "заметно труднее.",
    "po_otc_only": "Включите, если торгуете по выходным: обычные валютные "
                   "пары в субботу и воскресенье стоят на месте.",
    "news_filter_enabled": "Держите включённым. На выходе важной статистики "
                           "цена прыгает на новости, а не по графику — "
                           "любые индикаторы там бесполезны.",
    "news_mute_before_min": "15 минут достаточно: рынок начинает нервничать "
                            "примерно за четверть часа.",
    "news_mute_after_min": "30 минут — цена обычно успокаивается за полчаса.",
    "news_impact": "High хватает большинству. Добавляйте Medium, только если "
                   "видите, что бот всё равно попадает в новостные скачки.",
    "news_currencies": "USD влияет и на золото, и на большинство пар. "
                       "Добавляйте EUR, если торгуете европейскими парами.",
    "notify_signals": "Выключать имеет смысл, только если хотите смотреть "
                      "сигналы в приложении, а не получать сообщения.",
    "notify_outcomes": "Держите включённым: без результатов не видно, "
                       "работает стратегия или нет.",
    "daily_report": "Одно сообщение вечером вместо перелистывания журнала.",
    "daily_report_at": "Ставьте время, когда рынок для вас закрылся — "
                       "например 21:00.",
    "ema_fast": "9 — обычное значение. Уменьшите до 5, если хотите ловить "
                "развороты раньше, ценой ложных срабатываний.",
    "ema_slow": "21 — обычное значение. Разница с быстрой должна быть "
                "хотя бы вдвое, иначе линии будут пересекаться постоянно.",
    "ema_trend": "50 — обычное значение. Это просто фон: помогает не "
                 "покупать против затяжного падения.",
    "ema_trend_slow": "200 — не меняйте без причины. На эту линию смотрит "
                      "большинство участников рынка, потому она и работает.",
    "adx_period": "14 — классика. Менять стоит только при осознанном "
                  "эксперименте.",
    "rsi_period": "14 — классика. Менять стоит только при осознанном "
                  "эксперименте.",
    "rsi_long_min": "40 — разумный низ. Ниже него рост выглядит уже не "
                    "ростом, а отскоком в падении.",
    "rsi_long_max": "70 — разумный верх. Выше входить поздно: движение "
                    "перегрето.",
    "rsi_short_min": "30 — ниже падение уже перегрето, разворот ближе, "
                     "чем продолжение.",
    "rsi_short_max": "60 — выше продавать рано: движение ещё смотрит вверх.",
    "atr_period": "14 — классика. От этого числа зависит, насколько далеко "
                  "встаёт страховка.",
    "quiet_hours": "23:00-07:00 — чтобы телефон не будил ночью. Сигналы "
                   "за это время не пропадут: они останутся в журнале.",
}

for _f in FIELDS:
    _f.tip = TIPS.get(_f.key, "")


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
        # Значения по пользователям: {user_id: {параметр: значение}}.
        # Ключ None — значения по умолчанию из .env, общие для всех.
        self._by_user: dict[int | None, dict[str, Any]] = {}
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
        """Перечитывает настройки всех пользователей. Дёшево — один SELECT."""
        stored = await repo.all_settings()

        # Разбираем плоский список ключей на пользователей
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
            # перестанет действовать: человек поправил одну настройку —
            # и молча потерял все остальные, выставленные для всех.
            by_user[uid] = self._decode_all(raw, fill_defaults=uid is None)

        # Значения по умолчанию всегда есть, даже если в базе пусто
        by_user.setdefault(None, self._decode_all({}))

        self._by_user = by_user
        self._loaded = True
        return by_user[None]

    def _decode_all(
        self, raw: dict[str, str], fill_defaults: bool = True
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for f in FIELDS:
            text = raw.get(f.key)
            if text is None:
                if fill_defaults:
                    values[f.key] = self._fallback(f.key)
                continue
            try:
                values[f.key] = self._decode(f, text)
            except Exception:
                log.warning("Значение %s повреждено, беру по умолчанию", f.key)
                values[f.key] = self._fallback(f.key)
        return values

    def view(self, user_id: int) -> "UserConfig":
        """Настройки конкретного получателя."""
        return UserConfig(self, user_id)

    def known_users(self) -> list[int]:
        """Кто уже что-то настраивал."""
        return sorted(u for u in self._by_user if u is not None)

    def get(self, key: str, default: Any = None, user_id: int | None = None) -> Any:
        """Синхронное чтение — стратегия работает без await.

        Порядок поиска: значение пользователя, затем общее по умолчанию,
        затем значение из .env. Ключ, не описанный в FIELDS, тоже читается —
        так редкие параметры не обязаны попадать в интерфейс.
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

    async def set(self, key: str, value: Any, user_id: int | None = None) -> Any:
        """Проверяет, сохраняет и сразу применяет одно значение."""
        f = FIELDS_BY_KEY.get(key)
        if f is None:
            raise ValidationError(f"Неизвестный параметр: {key}")
        clean = self.validate(f, value)
        self._cross_check(key, clean, user_id=user_id)
        await repo.set_setting(_key(key, user_id), self._encode(f, clean))
        self._by_user.setdefault(user_id, {})[key] = clean
        log.info(
            "Настройка изменена: %s = %s%s",
            key, clean, f" (получатель {user_id})" if user_id else "",
        )
        return clean

    async def set_many(
        self, updates: dict[str, Any], user_id: int | None = None
    ) -> dict[str, Any]:
        applied = {}
        for key, value in updates.items():
            applied[key] = await self.set(key, value, user_id=user_id)
        return applied

    async def reset(self, key: str, user_id: int | None = None) -> Any:
        """Возвращает параметр к значению по умолчанию."""
        if key not in FIELDS_BY_KEY:
            raise ValidationError(f"Неизвестный параметр: {key}")
        await repo.delete_setting(_key(key, user_id))
        value = self._fallback(key)
        personal = self._by_user.get(user_id)
        if personal is not None:
            personal.pop(key, None)
        if user_id is None:
            self._by_user.setdefault(None, {})[key] = value
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
                    "Таймфрейм тренда должен быть старше рабочего: "
                    f"{htf} мельче, чем {tf}"
                )
        if key == "rsi_long_min" and value >= cur("rsi_long_max"):
            raise ValidationError("Нижняя граница RSI должна быть меньше верхней")
        if key == "rsi_long_max" and value <= cur("rsi_long_min"):
            raise ValidationError("Верхняя граница RSI должна быть больше нижней")
        if key == "ema_fast" and value >= cur("ema_slow"):
            raise ValidationError("Быстрая EMA должна быть короче медленной")
        if key == "ema_slow" and value <= cur("ema_fast"):
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

    def schema(self, user_id: int | None = None) -> list[dict]:
        """Описание всех параметров для построения интерфейса.

        Секреты наружу не отдаются: вместо значения уходит только признак
        «задан / не задан» и короткий хвост для опознания. Иначе токен
        сессии брокера утёк бы в любой ответ API.
        """
        out = []
        for f in FIELDS:
            item = {**f.to_dict(), "group_label": GROUPS[f.group]}
            value = self.get(f.key, user_id=user_id)
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

    # ----------------------------------------------------------------
    # Готовые режимы и человеческое описание
    # ----------------------------------------------------------------

    async def apply_preset(self, key: str, user_id: int | None = None) -> "Preset":
        """Применяет готовый режим целиком.

        Порядок важен: рабочий таймфрейм выставляется раньше старшего,
        иначе взаимная проверка отклонит промежуточное состояние, когда
        старший график на минуту оказывается мельче рабочего.
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
        """Какой режим сейчас стоит — или None, если настройки правили вручную.

        Сверяем не запомненное имя, а сами значения: человек мог выбрать
        режим, а потом что-то подкрутить. Тогда честнее показать «своя
        настройка», чем врать, что режим по-прежнему тот.
        """
        for preset in PRESETS:
            if all(
                self.get(name, user_id=user_id) == value
                for name, value in preset.values.items()
            ):
                return preset.key
        return None

    def is_configured(self, user_id: int) -> bool:
        """Заходил ли человек в настройки хоть раз.

        По этому признаку приложение решает, показывать ли новичку
        пошаговый вопросник вместо простыни параметров.
        """
        personal = self._by_user.get(user_id)
        return bool(personal)

    def explain(self, user_id: int | None = None) -> list[str]:
        """Что бот делает прямо сейчас — обычными словами.

        Настройки, разложенные по группам, не отвечают на главный вопрос
        новичка: «а что в итоге происходит?». Поэтому собираем из значений
        несколько фраз, которые читаются как описание поведения.
        """
        def g(name: str, default: Any = None) -> Any:
            return self.get(name, default, user_id=user_id)

        lines: list[str] = []

        symbols = list(g("symbols") or [])
        if symbols:
            names = ", ".join(_symbol_words(s) for s in symbols[:4])
            more = f" и ещё {len(symbols) - 4}" if len(symbols) > 4 else ""
            lines.append(f"Инструменты, за которыми слежу: {names}{more}.")
        else:
            lines.append("Пока не выбрано ни одного инструмента — следить не за чем.")

        lines.append(
            f"Решение принимаю раз в {_tf_words(str(g('timeframe')))}, "
            "а общее направление рынка смотрю по "
            f"{_tf_words(str(g('htf_timeframe')), dative=True)}."
        )

        conf = int(g("min_confidence") or 0)
        if conf >= 75:
            grade = "очень придирчиво — большинство моментов пропущу"
        elif conf >= 55:
            grade = "разумно придирчиво"
        else:
            grade = "почти без отбора — сообщений будет много"
        lines.append(f"Пишу, когда совпало не меньше {conf}% признаков: {grade}.")

        limit = int(g("max_signals_per_day") or 0)
        pause = int(g("cooldown_min") or 0)
        parts = []
        if limit:
            parts.append(f"не больше {limit} сигналов в сутки")
        if pause:
            parts.append(f"по одному активу — не чаще раза в {pause} мин")
        if parts:
            lines.append("Ограничения: " + ", ".join(parts) + ".")

        hours = str(g("trade_hours") or "").strip()
        lines.append(
            f"Ищу входы с {hours.replace('-', ' до ')}."
            if hours else "Ищу входы круглосуточно."
        )

        quiet = str(g("quiet_hours") or "").strip()
        if quiet:
            lines.append(
                f"С {quiet.replace('-', ' до ')} не беспокою — "
                "пропущенное найдёте утром в журнале."
            )

        if g("news_filter_enabled"):
            lines.append("Во время важных новостей молчу: там цена летит не по графику.")

        deposit = float(g("deposit") or 0)
        risk = float(g("risk_per_trade") or 0)
        if deposit > 0 and risk > 0:
            money = deposit * risk / 100
            lines.append(
                f"Объём считаю от {deposit:,.0f} USDT".replace(",", " ")
                + f" с риском {risk:g}% — это примерно "
                + f"{money:,.2f} USDT на сделку.".replace(",", " ")
            )
        else:
            lines.append(
                "Объём сделки не считаю: не заданы сумма счёта или допустимый риск."
            )

        return lines

    def masked(self, key: str, user_id: int | None = None) -> str:
        """Безопасное представление секрета для логов и сообщений."""
        text = str(self.get(key, user_id=user_id) or "")
        return f"…{text[-6:]}" if text else "не задан"

    def in_quiet_hours(self, now_hm: str, user_id: int | None = None) -> bool:
        """Попадает ли текущее время в интервал «не беспокоить»."""
        window = str(self.get("quiet_hours", user_id=user_id) or "").strip()
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



# «15m» -> «15 минут»: в описании поведения жаргон неуместен. Форм две,
# потому что фразы разные: «раз в 15 минут», но «по 15 минутам».
_TF_ACC = {
    "1m": "минуту", "3m": "3 минуты", "5m": "5 минут", "15m": "15 минут",
    "30m": "полчаса", "1h": "час", "2h": "2 часа", "4h": "4 часа",
    "6h": "6 часов", "12h": "12 часов", "1d": "сутки",
}
_TF_DAT = {
    "1m": "минуте", "3m": "3 минутам", "5m": "5 минутам", "15m": "15 минутам",
    "30m": "получасу", "1h": "часу", "2h": "2 часам", "4h": "4 часам",
    "6h": "6 часам", "12h": "12 часам", "1d": "суткам",
}


def _tf_words(timeframe: str, dative: bool = False) -> str:
    """Длительность свечи словами."""
    source = _TF_DAT if dative else _TF_ACC
    return source.get(timeframe, timeframe)


def _symbol_words(symbol: str) -> str:
    """Название инструмента без биржевой записи."""
    body = symbol.split(":")[0]
    if body.startswith("po:"):
        body = body[3:]
    otc = body.endswith("_otc")
    if otc:
        body = body[:-4]
    if "/" not in body and len(body) == 6:
        body = f"{body[:3]}/{body[3:]}"
    pretty = {"XAU": "золото", "XAG": "серебро"}.get(body.split("/")[0])
    if pretty:
        body = pretty
    return body + (" (круглосуточная)" if otc else "")


class UserConfig:
    """Настройки одного пользователя.

    Лёгкий объект-обёртка: держит ссылку на хранилище и свой идентификатор.
    Передаётся в стратегию и форматирование вместо глобального состояния —
    так два пользователя не мешают друг другу.
    """

    __slots__ = ("_store", "user_id")

    def __init__(self, store: "RuntimeConfig", user_id: int) -> None:
        self._store = store
        self.user_id = user_id

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default, user_id=self.user_id)

    def all(self) -> dict[str, Any]:
        return self._store.all(user_id=self.user_id)

    def schema(self) -> list[dict]:
        return self._store.schema(user_id=self.user_id)

    async def set(self, key: str, value: Any) -> Any:
        return await self._store.set(key, value, user_id=self.user_id)

    async def set_many(self, updates: dict[str, Any]) -> dict[str, Any]:
        return await self._store.set_many(updates, user_id=self.user_id)

    async def reset(self, key: str) -> Any:
        return await self._store.reset(key, user_id=self.user_id)

    def masked(self, key: str) -> str:
        return self._store.masked(key, user_id=self.user_id)

    def in_quiet_hours(self, now_hm: str) -> bool:
        return self._store.in_quiet_hours(now_hm, user_id=self.user_id)

    async def apply_preset(self, key: str) -> "Preset":
        return await self._store.apply_preset(key, user_id=self.user_id)

    def current_preset(self) -> str | None:
        return self._store.current_preset(user_id=self.user_id)

    def explain(self) -> list[str]:
        return self._store.explain(user_id=self.user_id)

    def __repr__(self) -> str:
        return f"<UserConfig {self.user_id}>"


config = RuntimeConfig()
