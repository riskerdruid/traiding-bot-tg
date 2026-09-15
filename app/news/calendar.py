"""Экономический календарь и глушение сигналов вокруг новостей.

Золото — инструмент, который резко ходит на макростатистике США: NFP, CPI,
решения ФРС. Технический сигнал, выданный за минуту до такой публикации,
почти бесполезен: цену двинет новость, а не индикатор.

Поэтому перед выдачей сигнала сканер спрашивает у этого модуля, не находимся
ли мы в «окне тишины» вокруг важного события.

Источник — недельный JSON-фид Forex Factory. Он публичный и не требует ключа.
Данные кешируются в базе, так что сеть опрашивается раз в час, а при недоступности
источника используется последний успешный снимок.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.storage.db import db
from app.storage.settings_store import config

log = logging.getLogger("news.calendar")

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
REFRESH_INTERVAL = 3600  # раз в час
USER_AGENT = "Mozilla/5.0 (compatible; TradingSignalsBot/1.0)"


@dataclass(slots=True)
class NewsEvent:
    title: str
    currency: str
    impact: str
    event_at: int  # unix seconds
    forecast: str = ""
    previous: str = ""

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.event_at, tz=timezone.utc)

    def minutes_from(self, now: int | None = None) -> float:
        """Сколько минут до события. Отрицательное — событие уже прошло."""
        return (self.event_at - (now or int(time.time()))) / 60

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "currency": self.currency,
            "impact": self.impact,
            "event_at": self.event_at,
            "forecast": self.forecast,
            "previous": self.previous,
            "minutes_until": round(self.minutes_from(), 1),
        }


class EconomicCalendar:
    def __init__(self) -> None:
        self._last_refresh: float = 0.0
        self._events: list[NewsEvent] = []
        self.last_error: str | None = None

    # ----------------------------------------------------------------
    # Загрузка
    # ----------------------------------------------------------------

    async def refresh(self, force: bool = False) -> int:
        """Тянет календарь. Возвращает количество загруженных событий."""
        if not force and time.time() - self._last_refresh < REFRESH_INTERVAL:
            return len(self._events)

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    FEED_URL, headers={"User-Agent": USER_AGENT}
                )
                response.raise_for_status()
                raw = response.json()
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("Календарь недоступен (%s), работаю по кешу", exc)
            if not self._events:
                self._events = await self._load_cached()
            return len(self._events)

        events: list[NewsEvent] = []
        for item in raw:
            parsed = self._parse(item)
            if parsed is not None:
                events.append(parsed)

        self._events = sorted(events, key=lambda e: e.event_at)
        self._last_refresh = time.time()
        self.last_error = None
        await self._save_cached(self._events)
        log.info("Календарь обновлён: %d событий на неделю", len(self._events))
        return len(self._events)

    def _parse(self, item: dict) -> NewsEvent | None:
        try:
            raw_date = item.get("date")
            if not raw_date:
                return None
            dt = datetime.fromisoformat(raw_date)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return NewsEvent(
                title=str(item.get("title", "")).strip(),
                currency=str(item.get("country", "")).strip().upper(),
                impact=str(item.get("impact", "")).strip().capitalize(),
                event_at=int(dt.timestamp()),
                forecast=str(item.get("forecast", "") or ""),
                previous=str(item.get("previous", "") or ""),
            )
        except Exception:
            return None

    # ----------------------------------------------------------------
    # Кеш в базе
    # ----------------------------------------------------------------

    async def _save_cached(self, events: list[NewsEvent]) -> None:
        conn = await db.connect()
        # Чистим прошлое, чтобы таблица не росла
        await conn.execute(
            "DELETE FROM news_cache WHERE event_at < ?", (int(time.time()) - 86400 * 7,)
        )
        for e in events:
            await conn.execute(
                """
                INSERT INTO news_cache (title, currency, impact, event_at, forecast, previous)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(title, event_at) DO UPDATE SET
                    impact = excluded.impact,
                    forecast = excluded.forecast,
                    previous = excluded.previous
                """,
                (e.title, e.currency, e.impact, e.event_at, e.forecast, e.previous),
            )
        await conn.commit()

    async def _load_cached(self) -> list[NewsEvent]:
        conn = await db.connect()
        async with conn.execute(
            "SELECT * FROM news_cache WHERE event_at > ? ORDER BY event_at",
            (int(time.time()) - 86400,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            NewsEvent(
                title=r["title"],
                currency=r["currency"],
                impact=r["impact"],
                event_at=r["event_at"],
                forecast=r["forecast"] or "",
                previous=r["previous"] or "",
            )
            for r in rows
        ]

    # ----------------------------------------------------------------
    # Запросы
    # ----------------------------------------------------------------

    def _relevant(self, event: NewsEvent) -> bool:
        """Событие влияет на наши инструменты?"""
        if event.impact not in [i.strip().capitalize() for i in str(config.get("news_impact") or "High").split(",") if i.strip()]:
            return False
        currencies = [c.strip().upper() for c in str(config.get("news_currencies") or "USD").split(",") if c.strip()]
        if not currencies:
            return True
        return event.currency in currencies or event.currency == "ALL"

    def mute_reason(self, now: int | None = None) -> NewsEvent | None:
        """Возвращает событие, из-за которого сейчас нельзя выдавать сигналы."""
        if not config.get("news_filter_enabled"):
            return None

        now = now or int(time.time())
        before = config.get("news_mute_before_min")
        after = config.get("news_mute_after_min")

        for event in self._events:
            if not self._relevant(event):
                continue
            minutes = event.minutes_from(now)
            if -after <= minutes <= before:
                return event
        return None

    def upcoming(self, limit: int = 5, only_relevant: bool = True) -> list[NewsEvent]:
        """Ближайшие события — для /news и Mini App."""
        now = int(time.time())
        out = [
            e
            for e in self._events
            if e.event_at > now and (not only_relevant or self._relevant(e))
        ]
        return out[:limit]

    def today(self, only_relevant: bool = True) -> list[NewsEvent]:
        """События сегодняшнего дня в часовом поясе отчётов."""
        now_local = datetime.now(settings.tz)
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start.timestamp() + 86400
        return [
            e
            for e in self._events
            if start.timestamp() <= e.event_at < end
            and (not only_relevant or self._relevant(e))
        ]

    @property
    def loaded(self) -> int:
        return len(self._events)


calendar = EconomicCalendar()
