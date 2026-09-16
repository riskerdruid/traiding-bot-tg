"""Список инструментов, из которых человек выбирает в боте.

Раньше инструмент вписывался строкой вида XAU/USDT:USDT. Для новичка это
шифр: он не знает ни правил записи, ни того, что из этого вообще торгуется
на подключённой бирже. Поэтому выбор сведён к списку с понятными именами,
а доступность проверяется у площадки — в списке не бывает того, чего нет.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.market.feed import feed, split_symbol

log = logging.getLogger("market.catalog")

# Биржевые инструменты, которые вообще имеет смысл предлагать.
# Чего нет на подключённой бирже — в список не попадёт.
EXCHANGE_CHOICES: tuple[tuple[str, str], ...] = (
    ("XAU/USDT:USDT", "Золото"),
    ("XAG/USDT:USDT", "Серебро"),
    ("BTC/USDT:USDT", "Биткоин"),
    ("ETH/USDT:USDT", "Эфир"),
    ("SOL/USDT:USDT", "Солана"),
    ("XRP/USDT:USDT", "Рипл"),
)

# Сколько пар брокера показывать: список у него огромный, а выбирать
# из шестидесяти строк новичок не станет.
PO_LIMIT = 8

_NAMES = {
    "XAU": "Золото",
    "XAG": "Серебро",
    "BTC": "Биткоин",
    "ETH": "Эфир",
    "SOL": "Солана",
    "XRP": "Рипл",
    "DOGE": "Догикоин",
    "TON": "Тонкоин",
}


@dataclass(slots=True)
class Choice:
    """Одна строка в списке выбора."""

    symbol: str  # полный адрес: XAU/USDT:USDT или po:EURUSD_otc
    title: str  # «Золото»
    note: str  # «биржа» или «опционы, выплата 92%»
    chosen: bool = False


def pretty(symbol: str) -> str:
    """Человеческое имя инструмента: XAU/USDT:USDT -> Золото."""
    prefix, name = split_symbol(symbol)
    if prefix == "po":
        body = name.replace("_otc", "")
        if "/" not in body and len(body) == 6:
            body = f"{body[:3]}/{body[3:]}"
        return body
    base = name.split("/")[0]
    return _NAMES.get(base, name.split(":")[0])


def base_ticker(symbol: str) -> str:
    """Чем именно торгуем: XAU/USDT:USDT -> XAU.

    Нужно для строки «взять примерно 0.98 XAU»: у брокера в окне объёма
    написан именно тикер, а не «Золото».
    """
    _prefix, name = split_symbol(symbol)
    return name.split("/")[0].split(":")[0].replace("_otc", "")


async def available(chosen: list[str]) -> list[Choice]:
    """Что можно выбрать прямо сейчас, с отметкой уже выбранного.

    Уже выбранные инструменты показываются всегда, даже если площадка
    сейчас недоступна: иначе человек откроет список, не увидит своего
    золота и решит, что настройки слетели.
    """
    picked = list(chosen or [])
    out: list[Choice] = []
    seen: set[str] = set()

    for choice in await _exchange_choices(picked):
        out.append(choice)
        seen.add(choice.symbol)
    for choice in await _pocket_choices(picked):
        if choice.symbol not in seen:
            out.append(choice)
            seen.add(choice.symbol)

    # Всё, что человек выбрал раньше, но чего нет в предложенных списках
    for symbol in picked:
        if symbol not in seen:
            out.append(
                Choice(symbol, unique_title(symbol, out), "выбрано ранее", True)
            )
            seen.add(symbol)

    return out


def unique_title(symbol: str, existing: list[Choice]) -> str:
    """Имя, которое не спутать с уже показанным.

    Красивое имя одно на весь актив: и BTC/USDT, и BTC/EUR — «Биткоин».
    Две одинаковые строки в списке выбора — это гарантированное нажатие
    не туда, поэтому к совпавшему имени дописываем сам тикер.
    """
    title = pretty(symbol)
    if title not in {c.title for c in existing}:
        return title
    _prefix, name = split_symbol(symbol)
    return f"{title} · {name.split(':')[0]}"


def quick_list(chosen: list[str]) -> list[Choice]:
    """Список инструментов без похода в сеть — для выбора в уведомлениях.

    Здесь важнее мгновенный ответ, чем точность: человек ставит будильник
    по цене, и если инструмента вдруг не окажется, он узнает об этом сразу
    из ответа бота, а не из пустого экрана.
    """
    out: list[Choice] = []
    for symbol in chosen or []:
        out.append(Choice(symbol, unique_title(symbol, out), "вы за ним следите", True))
    for symbol, title in EXCHANGE_CHOICES:
        if any(c.symbol == symbol for c in out):
            continue
        out.append(Choice(symbol, unique_title(symbol, out), "биржа", False))
    return out


async def _exchange_choices(picked: list[str]) -> list[Choice]:
    wanted = [s for s, _ in EXCHANGE_CHOICES]
    titles = dict(EXCHANGE_CHOICES)
    try:
        ok, _missing = await feed.exchange_broker.validate_symbols(wanted)
    except Exception as exc:
        # Биржа недоступна — лучше показать список как есть, чем пустой
        # экран: выбор сохранится и заработает, когда связь вернётся.
        log.warning("Биржа не ответила, показываю полный список: %s", exc)
        ok = wanted
    return [
        Choice(symbol, titles[symbol], "биржа", symbol in picked)
        for symbol in wanted
        if symbol in ok
    ]


async def _pocket_choices(picked: list[str]) -> list[Choice]:
    broker = feed.pocket_broker
    if not broker.configured:
        return []
    try:
        assets = await broker.list_symbols()
    except Exception as exc:
        log.warning("Pocket Option не ответил: %s", exc)
        return []

    # Валютные пары с самой высокой выплатой: чем она выше, тем меньше
    # угаданных сделок нужно, чтобы выйти в плюс.
    pairs = [
        a
        for a in assets
        if a.active and (a.asset_type or "").lower() in ("currency", "currencies", "")
    ]
    pairs.sort(key=lambda a: (-(a.payout or 0), a.symbol))

    out: list[Choice] = []
    for asset in pairs[:PO_LIMIT]:
        full = f"po:{asset.symbol}"
        note = "опционы"
        if asset.payout:
            note += f", выплата {asset.payout:.0f}%"
        # У брокера одна и та же пара бывает обычной и круглосуточной.
        # Без пометки в списке оказались бы две одинаковые строки.
        title = pretty(full) + (" 24/7" if asset.is_otc else "")
        if asset.is_otc:
            note += ", торгуется и в выходные"
        out.append(Choice(full, title, note, full in picked))

    # Выбранные пары брокера, не попавшие в топ, тоже показываем
    for symbol in picked:
        if split_symbol(symbol)[0] != "po":
            continue
        if any(c.symbol == symbol for c in out):
            continue
        title = pretty(symbol) + (" 24/7" if symbol.endswith("_otc") else "")
        out.append(Choice(symbol, title, "опционы", True))
    return out
