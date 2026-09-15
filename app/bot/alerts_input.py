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


def parse_request(text: str) -> tuple[list[str], float, str] | None:
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

    found = parse_price(text)
    if found is None:
        return None
    price, start, end = found

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
    return list(reversed(words)) + tail, price, note[:120]


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


def resolve_any(names: list[str], known: list[str]) -> str | None:
    """Первый из кандидатов, который удалось опознать.

    Сначала ищем среди инструментов человека, затем среди популярных.
    Слово, не похожее ни на то, ни на другое, просто пропускаем —
    так во фразе выживает только настоящее название.
    """
    for name in names:
        symbol = resolve(name, known)
        if symbol:
            return symbol
    for name in names:
        symbol = fallback_symbol(name)
        if symbol:
            return symbol
    return None
