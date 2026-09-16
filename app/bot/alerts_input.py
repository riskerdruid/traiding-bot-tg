"""Разбор сообщений вида «биткоин 95000».

Самый простой способ заказать уведомление — написать его словами, как
написали бы человеку. Никаких команд, форм и кнопок: назвал инструмент,
назвал цену.

Поэтому здесь два умения: вытащить из фразы цену и понять, о каком
инструменте речь. Название человек пишет как привык — «биткоин», «BTC»,
«золото», «евродоллар», — и всё это должно приводить к одному и тому же
инструменту у брокера.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Стороны движения — те же, что в хранилище
UP = "up"
DOWN = "down"

# Как люди называют инструменты. Ключ — то, что может написать человек,
# значение — базовый тикер инструмента.
ALIASES: dict[str, str] = {
    # Металлы
    "золото": "XAU", "золота": "XAU", "голд": "XAU", "gold": "XAU", "xau": "XAU",
    "серебро": "XAG", "серебра": "XAG", "silver": "XAG", "xag": "XAG",
    # Криптовалюта
    "биткоин": "BTC", "биткойн": "BTC", "битки": "BTC", "битка": "BTC",
    "биток": "BTC", "бтц": "BTC", "btc": "BTC", "bitcoin": "BTC",
    "эфир": "ETH", "эфириум": "ETH", "эфира": "ETH", "eth": "ETH",
    "ethereum": "ETH",
    "солана": "SOL", "сол": "SOL", "sol": "SOL",
    "рипл": "XRP", "xrp": "XRP",
    "донат": "TON", "тон": "TON", "ton": "TON",
    "додж": "DOGE", "дож": "DOGE", "doge": "DOGE",
    # Валютные пары
    "евродоллар": "EURUSD", "евро": "EURUSD", "eurusd": "EURUSD",
    "фунт": "GBPUSD", "гбп": "GBPUSD", "gbpusd": "GBPUSD",
    "иена": "USDJPY", "йена": "USDJPY", "usdjpy": "USDJPY",
    "франк": "USDCHF", "usdchf": "USDCHF",
    "канадец": "USDCAD", "usdcad": "USDCAD",
    "аусси": "AUDUSD", "audusd": "AUDUSD",
}

# Цена: число, возможно с пробелами внутри («95 000») и дробной частью
_PRICE = re.compile(r"(\d[\d\s ]*(?:[.,]\d+)?)")

# Процент: «-1.5%», «+2 %», «1,5%». Знак, если он есть, сразу задаёт сторону.
_PERCENT = re.compile(r"([+-]?)\s*(\d+(?:[.,]\d+)?)\s*%")

# Слова, по которым понятно направление, когда знака нет
_DOWN_WORDS = ("упад", "падени", "вниз", "ниже", "снизит", "просед", "минус",
               "дешев", "подешев", "сдует", "сольёт", "сольет")
_UP_WORDS = ("вырас", "рост", "вверх", "выше", "подним", "подорож", "плюс",
             "взлет", "взлёт", "прибав")


def direction_from_words(text: str) -> str | None:
    """Куда человек ждёт движения, если знак не поставлен."""
    low = (text or "").lower()
    for word in _DOWN_WORDS:
        if word in low:
            return DOWN
    for word in _UP_WORDS:
        if word in low:
            return UP
    return None


def parse_percent(text: str) -> tuple[float, str | None, int, int] | None:
    """Находит процент движения.

    Возвращает (процент, направление или None, начало, конец). Направление
    None означает «в любую сторону»: человек написал просто «2%», и логично
    предупредить его и о росте, и о падении.
    """
    match = _PERCENT.search(text or "")
    if match is None:
        return None
    try:
        value = float(match.group(2).replace(",", "."))
    except ValueError:
        return None
    if value <= 0:
        return None

    sign = match.group(1)
    if sign == "-":
        direction = DOWN
    elif sign == "+":
        direction = UP
    else:
        direction = direction_from_words(text)
    return value, direction, match.start(), match.end()


# Разделители, которые человек может поставить между названием и ценой
_JUNK = re.compile(r"^[\s,:;=\-–—>@]+|[\s,:;=\-–—<]+$")


def parse_price(text: str) -> tuple[float, int, int] | None:
    """Находит в тексте цену. Возвращает (цена, начало, конец) или None."""
    for match in _PRICE.finditer(text):
        raw = match.group(1)
        cleaned = raw.replace(" ", "").replace(" ", "").replace(",", ".")
        # «95 000» — это одно число, а «5 минут» — нет: одиночная цифра
        # с пробелом почти всегда часть фразы, а не цена
        try:
            value = float(cleaned)
        except ValueError:
            continue
        if value <= 0:
            continue
        return value, match.start(1), match.end(1)
    return None


@dataclass(slots=True)
class Request:
    """Что человек попросил.

    Либо конкретная цена, либо движение в процентах от текущей —
    ровно одно из двух. Направление заполнено только для процентов
    со знаком или со словом: без них уведомить нужно в обе стороны.
    """

    names: list[str]
    price: float | None = None
    percent: float | None = None
    direction: str | None = None
    note: str = ""

    @property
    def by_percent(self) -> bool:
        return self.percent is not None


def parse_request(text: str) -> Request | None:
    """Разбирает фразу на кандидатов в инструменты, цену и заметку.

    «биткоин 95000 продать половину»
        -> (["биткоин"], 95000.0, "продать половину")
    «уведоми когда биткоин будет 95000»
        -> (["будет", "биткоин", "когда", "уведоми"], 95000.0, "")

    Слова идут справа налево: первое, которое удастся опознать как
    инструмент, и будет ответом.

    Возвращает None, если цены в сообщении нет или перед ней не осталось
    ни одного слова: по одному числу непонятно, о чём человек просит.
    """
    text = (text or "").strip()
    if not text:
        return None

    # Процент проверяем первым: в «-1.5%» тоже есть число, и разбор цены
    # схватил бы его раньше
    percent = parse_percent(text)
    if percent is not None:
        value, direction, start, end = percent
        price = None
    else:
        found = parse_price(text)
        if found is None:
            return None
        price, start, end = found
        value, direction = None, None

    name = _JUNK.sub("", text[:start])
    note = _JUNK.sub("", text[end:])
    if not name:
        return None

    # «уведоми когда биткоин будет 95000»: инструмент здесь не последнее
    # слово перед ценой, а последнее ПОДХОДЯЩЕЕ. Поэтому возвращаем все
    # слова справа налево — пусть решает тот, кто знает список
    # инструментов человека.
    words = [w for w in re.split(r"[\s,]+", name) if w]
    if not words:
        return None

    # Название может стоять и после цены: «сообщи на 4400 по золоту».
    # Слова из хвоста идут последними — они менее вероятные кандидаты,
    # но лучше опознать инструмент, чем отказать человеку.
    tail = [w for w in re.split(r"[\s,]+", note) if w]
    return Request(
        names=list(reversed(words)) + tail,
        price=price,
        percent=value,
        direction=direction,
        note=note[:120],
    )


def parse_value(text: str) -> Request | None:
    """Разбирает «95000» или «-1.5%», когда инструмент уже выбран.

    Отличие от parse_request одно: здесь перед числом не обязано стоять
    название — человек уже нажал кнопку с инструментом, и требовать его
    заново было бы издевательством.
    """
    text = (text or "").strip()
    if not text:
        return None

    percent = parse_percent(text)
    if percent is not None:
        value, direction, _start, end = percent
        note = _JUNK.sub("", text[end:])
        return Request(names=[], percent=value, direction=direction, note=note[:120])

    found = parse_price(text)
    if found is None:
        return None
    price, _start, end = found
    note = _JUNK.sub("", text[end:])
    return Request(names=[], price=price, note=note[:120])


# Окончания в русском меняются («золоту», «биткоина»), а перечислять все
# формы в словаре бессмысленно. Поэтому рядом со словарём держим начала
# слов: по ним ищем, если точного совпадения не нашлось.
_STEMS: dict[str, str] = {}
for _word, _ticker in ALIASES.items():
    if len(_word) >= 5:
        _STEMS.setdefault(_word[:5], _ticker)


def normalize(name: str) -> str:
    """Приводит написанное человеком к базовому тикеру."""
    key = name.strip().lower().replace("/", "").replace("-", "")
    if key in ALIASES:
        return ALIASES[key]
    if len(key) >= 5 and key[:5] in _STEMS:
        return _STEMS[key[:5]]
    # Не нашли по словарю — считаем, что человек написал тикер как есть
    return name.strip().upper().replace("/", "").replace("-", "")


def base_of(symbol: str) -> str:
    """Базовый тикер инструмента: 'po:EURUSD_otc' -> 'EURUSD'."""
    body = symbol
    if ":" in body and body.split(":", 1)[0].isalpha() and len(body.split(":", 1)[0]) <= 3:
        # Префикс площадки: 'po:EURUSD_otc' -> 'EURUSD_otc'
        head, rest = body.split(":", 1)
        if head.lower() == "po":
            body = rest
    body = body.split(":")[0].replace("_otc", "")
    if "/" in body:
        left, right = body.split("/", 1)
        # Для валютной пары важны обе половины: EUR/USD -> EURUSD
        if len(left) == 3 and len(right) == 3:
            return (left + right).upper()
        return left.upper()
    return body.upper()


def resolve(name: str, known: list[str]) -> str | None:
    """Ищет инструмент среди тех, что человеку уже доступны.

    Сначала смотрим в его собственном списке: если он следит за золотом,
    «золото» должно означать именно его инструмент, а не первый попавшийся
    с биржи.
    """
    ticker = normalize(name)
    if not ticker:
        return None

    for symbol in known:
        if base_of(symbol) == ticker:
            return symbol

    # Частичное совпадение: человек написал «BTC», а в списке «BTC/USDT»
    for symbol in known:
        if base_of(symbol).startswith(ticker) and len(ticker) >= 3:
            return symbol
    return None


# Куда приводить популярные инструменты, если человек о них спросил,
# но в его списке их нет
FALLBACKS: dict[str, str] = {
    "XAU": "XAU/USDT:USDT",
    "XAG": "XAG/USDT:USDT",
    "BTC": "BTC/USDT:USDT",
    "ETH": "ETH/USDT:USDT",
    "SOL": "SOL/USDT:USDT",
    "XRP": "XRP/USDT:USDT",
    "TON": "TON/USDT:USDT",
    "DOGE": "DOGE/USDT:USDT",
}


def fallback_symbol(name: str) -> str | None:
    """Готовое имя инструмента для популярных запросов вне списка."""
    return FALLBACKS.get(normalize(name))


# Слова, которые встречаются рядом с ценой и точно не являются
# названием инструмента
_SKIP_WORDS = {
    "на", "до", "по", "в", "если", "когда", "будет", "станет", "упадёт",
    "упадет", "вырастет", "поднимется", "опустится", "сообщи", "уведоми",
    "напиши", "скажи", "процент", "процента", "процентов", "вверх", "вниз",
    "выше", "ниже", "рост", "падение",
}


def resolve_any(names: list[str], known: list[str]) -> str | None:
    """Первый из кандидатов, который удалось опознать.

    Сначала ищем среди инструментов человека, затем среди популярных.
    Слово, не похожее ни на то, ни на другое, просто пропускаем —
    так во фразе выживает только настоящее название.
    """
    for name in names:
        if name.lower().strip(".,!?") in _SKIP_WORDS:
            continue
        symbol = resolve(name, known)
        if symbol:
            return symbol
    for name in names:
        symbol = fallback_symbol(name)
        if symbol:
            return symbol
    return None
